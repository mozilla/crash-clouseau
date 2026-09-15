# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Fenix builds from the TaskCluster index -- ``buildhub``'s interface for a product Buildhub
does not carry (plan 16 §2, §13 D5/D6).

THE SET AND THE REVISION COME FROM DIFFERENT PLACES, and that split is the whole design.

* The build SET is the ``mobile.fenix-nightly`` leaves under ``gecko.v2.mozilla-central.pushdate
  .<Y>.<M>.<D>.<buildid>``. Only the index knows which pushes shipped an APK: Fenix does not
  build on every push desktop does (46 fenix-nightly builds against 47 firefox-nightly buildids
  over 23 days), and borrowing Firefox's ordering for "the previous build" collapsed one
  changeset window from 173 changesets to 2 (plan 16 §2.5). Measured 2026-09-10: 9 children,
  2 with the leaf. A 404 on the leaf is a real answer -- that push built no shipped APK (or the
  buildid is a local/emulator build: 1 of 972 Socorro buildids, 20260202034059, is not a child
  of its day at all) -- so it yields NO row and the crash abstains at ``Build.get_id``.
* The REVISION is the leaf's task ``metadata.source`` (``/file/<40 hex>``), cross-checked
  against its ``tc-treeherder.v2.<repo>.<rev>`` route. Buildhub queried AS firefox at the
  Fenix buildid agrees where it has the build (14 of 14 on 2026-09-15) but it is not total:
  20260910002721 is a Fenix-only push with no firefox document. It still supplies
  ``target.version`` in the same POST, so it is the version source; ``mobile/android/
  version.txt`` at the revision is the fallback (``158.0a1`` at 8590488daa5e).
* The buildid IS the push timestamp (46 of 46), so hg ``json-pushes`` over the two-second
  window around it is the total-function fallback for the revision -- ISO strings only, both
  bounds EXCLUSIVE, bare string read as UTC (numeric seconds return ``pushes: {}``).

The index has no range query and its responses are ``no-store``, so the ``builds`` table IS the
cache: ``update.update_builds`` starts a day before the newest stored build (a leaf attaches
~18 minutes after its push, so the children newer than that build are re-probed each tick
until they resolve), and a cold table pays the 30-day lookback once (~30 POST + ~150 GET).

Every entry point mirrors ``buildhub``'s signature and return shape so ``buildsource.
for_product`` can hand either module to the same caller. HTTP goes through ``net.get`` /
``net.post`` (allowlisted UA, bounded timeout) or ``hgedge._get`` for hg; this module never
imports ``pushlog`` (which imports the dispatcher).
"""

from datetime import datetime, timedelta
import re
import time

import pytz
import requests
import six

from . import buildhub, hgedge, models, net, utils
from .logger import logger

INDEX = "https://firefox-ci-tc.services.mozilla.com/api/index/v1"
QUEUE = "https://firefox-ci-tc.services.mozilla.com/api/queue/v1"

# Socorro product names this module serves (see `buildsource.TC_PRODUCTS`).
PRODUCTS = frozenset({"Fenix"})

# channel -> (TC namespace repo, mobile leaf). The repo is the namespace segment, NOT the hg
# `releases/mozilla-beta` path; the leaf is the signing-apk task, i.e. the SHIPPED APK
# (`fenix-nightly-simulation` is a rehearsal, `fenix-debug` is not what users run). Beta and
# release leaves verified live 2026-09-15 (gecko.v2.mozilla-beta.pushdate.2026.09.14.
# 20260914090352.mobile.fenix-beta, gecko.v2.mozilla-release.pushdate.2026.09.09.
# 20260909172920.mobile.fenix-release) but NOT served yet: Fenix beta/release are separate
# ship-it releases, so Buildhub-as-firefox is not expected to be total there and the
# json-pushes route on a `releases/` repo is unverified. The table stays so the day the
# channel is switched on is a one-line change here plus D1's `product_channels`.
LEAVES = {
    "nightly": ("mozilla-central", "fenix-nightly"),
    "beta": ("mozilla-beta", "fenix-beta"),
    "release": ("mozilla-release", "fenix-release"),
}
SERVED_CHANNELS = frozenset({"nightly"})

# Day children are the 14-digit pushdate buildids. `latest` is a REAL child (resolves to the
# newest leaf) and sorts after every digit string, so `!= "latest"` or `max()` over raw names
# is not a filter -- match the shape.
_BID = re.compile(r"^\d{14}$")
# `metadata.source` is `https://hg.mozilla.org/mozilla-central/file/<rev>/taskcluster/...` on
# central and `https://hg.mozilla.org/releases/mozilla-beta/file/<rev>//builds/worker/...` on
# beta/release (extra `releases/` segment AND a double slash after the rev, verified live on
# bLY59bhKSYunaNyQPVtLdg and Zji82EYFQJ20I4F3EwztPw). The scratch regex anchored on one host
# segment and a trailing slash broke on both, so match only what is stable.
_SOURCE_REV = re.compile(r"/file/([0-9a-f]{40})")
# Routes are NOT positional (the hg and git `revision.<hash>` routes come in insertion order,
# 23 of 46 lexically inverted); the treeherder route is the one route that is hg-only.
_TREEHERDER = re.compile(r"^tc-treeherder\.v2\.[^.]+\.([0-9a-f]{40})$")

# The walk-back bound for a predecessor when the table cannot answer. 14 is the prototype's
# bound (spike/_fenix_scratch/step2c_walkback.py): the longest gap seen in 30 days of builds
# was one empty day (2026-07-11, a Saturday with no push), and anything older than the 30-day
# `nodes` retention cannot be scored anyway.
_MAX_WALKBACK_DAYS = 14
_VERSION_FILE = "mobile/android/version.txt"
_HG_FMT = "%Y-%m-%d %H:%M:%S"

# One retry on a throttle or a 5xx from the index/queue. The index never rate-limited the
# spike's ~1,400 requests, so this is not about throughput: a cold sweep probes each day ONCE
# and the next tick starts a day before the newest stored build, so a transient failure on an
# older day would lose that day's builds for good. One second is enough for a blip and short
# enough not to matter inside a 20-minute tick.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_RETRY_SLEEP = 1.0


# ------------------------------------------------------------------ helpers


def _served(channel, product):
    """``(repo, leaf)`` when this module answers for *product* on *channel*, else None."""
    if product not in PRODUCTS or channel not in SERVED_CHANNELS:
        return None
    return LEAVES.get(channel)


def _as_buildid(value):
    """A 14-digit buildid string from a buildid string or a datetime. A NAIVE datetime is read
    as UTC (`utils.get_buildid` would read it as local time)."""
    if isinstance(value, datetime) and value.tzinfo is None:
        value = pytz.utc.localize(value)
    return utils.get_buildid(value)


class _Unreachable(object):
    """The response stand-in for a request that never got an HTTP status (connection refused,
    read timeout, chunked-encoding error): a status no server sends, so every caller's
    ``!= 200`` / ``!= 404`` branch treats it as a transient failure, and a ``json()`` that
    has nothing to give. This module's contract is that ``get`` never raises into the RQ
    job -- ``update.update_builds`` runs outside ``update()``'s try/except, so an exception
    here would end the Fenix tick before ``put_crashes``."""

    status_code = 599

    def __init__(self, exc):
        self.reason = repr(exc)

    def json(self):
        return None


def _request(method, url, **kwargs):
    """``method(url, **kwargs)`` with a single retry on ``_RETRY_STATUS`` or a transport
    failure; the last response (or an ``_Unreachable``) is returned whatever its status so the
    caller decides what a failure means."""
    r = None
    for attempt in (0, 1):
        try:
            r = method(url, **kwargs)
        except requests.RequestException as exc:
            logger.warning("tcindex: %s unreachable (%s)", url, exc)
            r = _Unreachable(exc)
        if r.status_code not in _RETRY_STATUS and r.status_code != _Unreachable.status_code:
            break
        if attempt == 0:
            time.sleep(_RETRY_SLEEP)
    return r


def _json(r):
    """The response body as a dict, or None: a 200 whose body is not JSON (a CDN / WAF
    interstitial) or not an object is a transient failure, not data."""
    try:
        data = r.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _day_namespace(repo, day):
    return "gecko.v2.{}.pushdate.{:04d}.{:02d}.{:02d}".format(
        repo, day.year, day.month, day.day
    )


def _day_children(repo, day):
    """The pushdate buildids indexed under *day* (sorted), following ``continuationToken``.
    ``[]`` on an empty day (a Saturday with no push is a 200 with no namespaces) and on a
    non-200, which is logged: a silent ``[]`` would read as "no builds that day".

    The POST carries ``json={}``: a bodyless POST is a 400 ``MalformedPayload`` ("Payload must
    be JSON with content-type: application/json"), verified live 2026-09-15."""
    url = "{}/namespaces/{}".format(INDEX, _day_namespace(repo, day))
    names = []
    payload = {}
    while True:
        r = _request(net.post, url, json=payload)
        data = _json(r) if r.status_code == 200 else None
        if data is None:
            logger.warning("tcindex: namespaces %s -> %d, day read as empty", url, r.status_code)
            return []
        names.extend(
            n["name"] for n in data.get("namespaces", ())
            if isinstance(n, dict) and _BID.match(n.get("name") or "")
        )
        token = data.get("continuationToken")
        if not token:
            break
        payload = {"continuationToken": token}
    return sorted(names)


def _leaf(repo, day, bid, leaf):
    """The taskId indexed at ``<day namespace>.<bid>.mobile.<leaf>``.

    200 -> taskId. 404 -> None: no shipped APK for that push (7 of 9 children on 2026-09-10;
    also a local/emulator buildid, which is not a child at all). Any OTHER non-200 -> None
    AND a warning: a transient 5xx must never be read as "no build", because "no build" is
    permanent for the crash (no ``builds`` row, abstain at ``Build.get_id``) while the tick
    that re-probes this child is only 20 minutes away."""
    url = "{}/task/{}.{}.mobile.{}".format(INDEX, _day_namespace(repo, day), bid, leaf)
    r = _request(net.get, url)
    if r.status_code == 200:
        data = _json(r)
        if data is None or not data.get("taskId"):
            logger.warning("tcindex: leaf probe %s -> unreadable body", url)
            return None
        return data["taskId"]
    if r.status_code != 404:
        logger.warning("tcindex: leaf probe %s -> %d (not read as 'no build')", url,
                       r.status_code)
    return None


def _task_rev(task_id):
    """The 40-hex hg revision the signing task was built from: ``metadata.source``'s
    ``/file/<rev>`` cross-checked against the single ``tc-treeherder.v2.<repo>.<rev>`` route.
    None when either is absent or they disagree (logged): the caller falls back to the push
    at the buildid second rather than trust half an answer."""
    url = "{}/task/{}".format(QUEUE, task_id)
    r = _request(net.get, url)
    task = _json(r) if r.status_code == 200 else None
    if task is None:
        logger.warning("tcindex: task %s -> %d", url, r.status_code)
        return None
    source = (task.get("metadata") or {}).get("source") or ""
    m = _SOURCE_REV.search(source)
    from_source = m.group(1) if m else None
    from_routes = [
        mm.group(1) for mm in (_TREEHERDER.match(rt) for rt in task.get("routes") or ()) if mm
    ]
    if from_source is None or len(from_routes) != 1 or from_routes[0] != from_source:
        logger.warning("tcindex: task %s revision unresolved (source %r, treeherder %r)",
                       task_id, from_source, from_routes)
        return None
    return from_source


def _buildhub_as_firefox(bid, channel):
    """``(revision40 | None, version | None)`` from Buildhub queried as ``source.product=
    firefox`` at the Fenix buildid -- one POST returning both (20260910214118 ->
    8590488daa5e... / 158.0a1). Empty buckets when Buildhub lacks the buildid (a Fenix-only
    push such as 20260910002721), handled here rather than through ``buildhub.get_rev_from``,
    whose callback indexes ``buckets[0]`` and turns that ordinary miss into two ERROR lines."""
    data = {
        "aggs": {
            "revisions": {"terms": {"field": "source.revision", "size": 1}},
            "versions": {"terms": {"field": "target.version", "size": 1}},
        },
        "query": {
            "bool": {
                "filter": [
                    {"term": {"target.channel": buildhub.target_channel(channel)}},
                    {"term": {"source.product": buildhub.PRODS["Firefox"]}},
                    {"term": {"build.id": bid}},
                ]
            }
        },
        "size": 0,
    }

    def cb(data):
        aggs = data["aggregations"]
        revs = aggs["revisions"]["buckets"]
        versions = aggs["versions"]["buckets"]
        return (revs[0]["key"] if revs else None, versions[0]["key"] if versions else None)

    return buildhub.make_request(data, 0.1, 100, cb) or (None, None)


def _hg_push_rev(bid, channel):
    """The tip of the push whose date IS the buildid, from hg-edge ``json-pushes`` over
    ``(bid - 1s, bid + 1s)`` -- both bounds exclusive, ISO strings (numeric seconds return
    ``pushes: {}``), read as UTC. None when no push sits at that second (a non-CI buildid) or
    when the window is ambiguous."""
    t = utils.get_build_date(bid)
    params = {
        "version": 2,
        "startdate": (t - timedelta(seconds=1)).strftime(_HG_FMT),
        "enddate": (t + timedelta(seconds=1)).strftime(_HG_FMT),
    }
    url = "{}/json-pushes".format(hgedge._edge_base(channel))
    data = hgedge._get(url, params=params, as_json=True)
    if not isinstance(data, dict):
        return None
    epoch = int(t.timestamp())
    hits = [p for p in (data.get("pushes") or {}).values()
            if p.get("date") == epoch and p.get("changesets")]
    if len(hits) != 1:
        logger.info("tcindex: json-pushes at %s: %d push(es) at that second", bid, len(hits))
        return None
    return hits[0]["changesets"][-1]


def _version(bid, channel, rev):
    """The build's ``target.version``: Buildhub-as-firefox first (same POST as its revision,
    which is compared against the task's -- a disagreement is logged, the task's wins), then
    ``mobile/android/version.txt`` at *rev*. None when both miss."""
    bh_rev, version = _buildhub_as_firefox(bid, channel)
    if bh_rev and rev and bh_rev != rev:
        logger.warning("tcindex: %s: Buildhub-as-firefox revision %s differs from the task's %s",
                       bid, utils.short_rev(bh_rev), utils.short_rev(rev))
    if version:
        return version
    text = hgedge.raw_file(_VERSION_FILE, rev, channel) if rev else None
    return text.strip() if text else None


def _resolve(repo, leaf, bid, channel, task_id=None):
    """``(revision40, version)`` for a leaf-bearing *bid*, or None when the revision cannot be
    established (task unreadable AND no push at that second) or the version cannot: a row
    with no version would be half a build, and the next tick re-probes for free."""
    day = utils.get_build_date(bid).date()
    if task_id is None:
        task_id = _leaf(repo, day, bid, leaf)
        if task_id is None:
            return None
    rev = _task_rev(task_id)
    if rev is None:
        rev = _hg_push_rev(bid, channel)
        if rev is None:
            logger.warning("tcindex: %s/%s build %s: revision unresolved, skipped", channel,
                           leaf, bid)
            return None
        logger.info("tcindex: %s revision %s from json-pushes", bid, utils.short_rev(rev))
    version = _version(bid, channel, rev)
    if version is None:
        logger.warning("tcindex: %s/%s build %s (%s): version unresolved, skipped", channel,
                       leaf, bid, utils.short_rev(rev))
        return None
    return rev, version


def _previous(repo, leaf, bid):
    """``(bid, taskId)`` of the newest leaf-bearing build older than *bid*: same-day earlier
    children first, then up to ``_MAX_WALKBACK_DAYS`` days back (an empty day is skipped, not
    a stop). None when nothing within the bound."""
    day = utils.get_build_date(bid).date()
    for k in range(0, _MAX_WALKBACK_DAYS + 1):
        d = day - timedelta(days=k)
        for child in reversed(_day_children(repo, d)):
            if k == 0 and child >= bid:
                continue
            task_id = _leaf(repo, d, child, leaf)
            if task_id:
                return child, task_id
    return None


def _row(bid, rev, version):
    return {"buildid": bid, "revision": utils.short_rev(rev), "version": version}


def _table_row(q):
    """``buildhub``'s build dict from a ``(buildid, version, node)`` row. A naive buildid (what
    sqlite hands back) is UTC."""
    bid = q.buildid if q.buildid.tzinfo else pytz.utc.localize(q.buildid)
    return _row(utils.get_buildid(bid), q.node, q.version)


# ------------------------------------------------------------------ buildhub's interface


def get(min_buildid, channel, prods="Fenix", max_buildid=None):
    """Every shipped build of *prods* on *channel* with ``min_buildid <= buildid <=
    max_buildid`` (default: now), in ``buildhub.get``'s shape:
    ``{"Fenix": {channel: {utils.get_build_date(bid): {"revision": 12-char, "version": str}}}}``,
    ``{}`` when nothing. One namespace POST per UTC day, one leaf GET per child in range, one
    task GET + one Buildhub POST per shipped build. A build whose revision or version cannot
    be resolved is skipped and logged, never raised on."""
    if isinstance(prods, six.string_types):
        prods = [prods]
    lo = _as_buildid(min_buildid)
    hi = _as_buildid(max_buildid) if max_buildid else _as_buildid(datetime.now(pytz.utc))
    res = {}
    for product in prods:
        served = _served(channel, product)
        if served is None:
            logger.info("tcindex: nothing served for %s/%s", channel, product)
            continue
        builds = _walk(served, lo, hi, channel)
        if builds:
            res[product] = {channel: builds}
    return res


def _walk(served, lo, hi, channel):
    """``{utils.get_build_date(bid): {"revision", "version"}}`` for the leaf-bearing children
    of every UTC day from *lo*'s to *hi*'s whose buildid is in ``[lo, hi]``."""
    repo, leaf = served
    builds = {}
    if lo > hi:
        return builds
    day = utils.get_build_date(lo).date()
    last_day = utils.get_build_date(hi).date()
    while day <= last_day:
        for bid in _day_children(repo, day):
            if bid < lo or bid > hi:
                continue
            task_id = _leaf(repo, day, bid, leaf)
            if task_id is None:
                logger.debug("tcindex: %s has no %s leaf", bid, leaf)
                continue
            resolved = _resolve(repo, leaf, bid, channel, task_id=task_id)
            if resolved is None:
                continue
            rev, version = resolved
            builds[utils.get_build_date(bid)] = {
                "revision": utils.short_rev(rev), "version": version,
            }
            logger.info("tcindex: %s/%s build %s -> %s (%s)", channel, leaf, bid,
                        utils.short_rev(rev), version)
        day += timedelta(days=1)
    return builds


def get_rev_from(buildid, channel, product):
    """The 12-char hg revision of one build, or None. Cheapest first: Buildhub-as-firefox (one
    POST, total except for a Fenix-only push), the leaf's task (two GETs), the push at the
    buildid second (one hg-edge GET). None also means "not a CI build": the caller abstains."""
    served = _served(channel, product)
    if served is None:
        return None
    repo, leaf = served
    bid = _as_buildid(buildid)
    rev, _ = _buildhub_as_firefox(bid, channel)
    if rev is None:
        task_id = _leaf(repo, utils.get_build_date(bid).date(), bid, leaf)
        rev = _task_rev(task_id) if task_id else None
    if rev is None:
        rev = _hg_push_rev(bid, channel)
    return utils.short_rev(rev) if rev else None


def get_two_last(buildid, channel, product):
    """``[previous, current]`` for the build at *buildid*, each ``{"buildid", "revision",
    "version"}``, or None. The ``builds`` table answers when it holds the asked build and a
    predecessor -- the set ingested from the index IS the Fenix set, so no Firefox-only buildid
    can sit between them (the 173 -> 2 truncation). Otherwise the index is walked back
    (``_previous``); None when the asked buildid has no leaf or no predecessor in bound."""
    served = _served(channel, product)
    if served is None:
        return None
    repo, leaf = served
    bid = _as_buildid(buildid)
    rows = models.Build.get_two_last(utils.get_build_date(bid), channel, product)
    if len(rows) == 2 and rows[1]["buildid"] == bid:
        return rows
    cur = _resolve(repo, leaf, bid, channel)
    if cur is None:
        return None
    prev = _previous(repo, leaf, bid)
    if prev is None:
        logger.info("tcindex: no %s build within %d days before %s", leaf, _MAX_WALKBACK_DAYS,
                    bid)
        return None
    prev_bid, prev_task = prev
    prev_res = _resolve(repo, leaf, prev_bid, channel, task_id=prev_task)
    if prev_res is None:
        return None
    return [_row(prev_bid, *prev_res), _row(bid, *cur)]


def get_enclosing_builds(pushdate, channel, product):
    """``[before | None, after | None]`` around *pushdate* from the ``builds`` table ONLY --
    the newest build strictly before it and the oldest at or after it, in ``buildhub``'s dict
    shape. No network: the sole consumer is a UI link (``pushlog.pushlog_for_pushdate_url``),
    and the table is the set once ingested."""
    if product not in PRODUCTS or channel not in LEAVES:
        return [None, None]
    when = utils.get_build_date(_as_buildid(pushdate))
    base = (
        models.db.session.query(models.Build.buildid, models.Build.version, models.Node.node)
        .select_from(models.Build)
        .join(models.Node)
        .filter(models.Build.product == product, models.Build.channel == channel)
    )
    before = base.filter(models.Build.buildid < when).order_by(
        models.Build.buildid.desc()).first()
    after = base.filter(models.Build.buildid >= when).order_by(
        models.Build.buildid.asc()).first()
    return [_table_row(before) if before else None, _table_row(after) if after else None]
