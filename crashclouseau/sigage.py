# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""How OLD was a crash signature when the build we are triaging was produced?

The triage pipeline's premise is "the regressor is somewhere in this build's pushlog window".
That premise only holds if the signature is actually NEW as of that build. When the signature
has existed for months, nothing in the window introduced it, and naming a window changeset
produces a confident-looking lead that cannot be right.

This is not hypothetical. Over the canary's first three prod days, 10 of 10 HIGH-confidence
blind-second-opinion refutations rested on exactly this argument, and all 10 verified
deterministically with a median gap of 178 days between the signature first appearing and the
named candidate landing (`spike/verify_so_timing_claims.py`). 24 of 32 reported leads came from
signatures already more than a week old.

One Socorro lookup answers it. Four gotchas, all learned the hard way and all encoded below:

  * The `build_id` FACET is ordered by COUNT, so truncating it silently drops the oldest build —
    the one value we need. Sort ASCENDING by `build_id` and take one row instead.
  * `_facets_size=10000` is rejected outright (HTTP 400).
  * A date range of exactly 365 days is rejected too ("Date range is bigger than 365 days"),
    because the implicit upper bound is *now*. Clamp to 364.
  * `date` filters the crash REPORT date, not the build date, so even a short window surfaces
    very old buildids — which is exactly what makes this cheap.

Window truncation can only make first-seen look NEWER than it really is, so an age computed here
is a LOWER bound and the resulting downweight is conservative.
"""
from datetime import datetime, timedelta, timezone

from libmozdata import socorro

from crashclouseau.logger import logger

# Socorro hard-rejects more than 365 days; the implicit "to now" upper bound pushes an exact
# 365 over the line. Public because the second-opinion agent's crash-stats tool states the
# window in its agent-facing text, and the figure must not drift between the two files.
MAX_WINDOW_DAYS = 364


def _buildid_to_dt(buildid):
    """``YYYYMMDDHHMMSS`` (UTC) -> datetime, or None when it is not a buildid."""
    try:
        return datetime.strptime(str(buildid)[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def first_seen_buildid(signature, product="Firefox", channel="nightly",
                       days=MAX_WINDOW_DAYS):
    """The OLDEST buildid this signature appears in, within ``days``. ``None`` when the lookup
    finds nothing or fails. Raises nothing."""
    if not signature:
        return None
    days = max(1, min(int(days or MAX_WINDOW_DAYS), MAX_WINDOW_DAYS))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    params = {
        "signature": "=" + signature,
        "product": product or "Firefox",
        "date": ">=" + since,
        # Sort ascending and take ONE row: the build_id facet is count-ordered, so paging it
        # can drop the oldest build, and an untruncated facet size is a 400.
        "_columns": ["build_id"],
        "_sort": "build_id",
        "_results_number": 1,
    }
    if channel:
        params["release_channel"] = channel
    got = {}

    def handler(json_, data):
        data["result"] = json_

    try:
        socorro.SuperSearch(params=params, handler=handler, handlerdata=got).wait()
    except Exception as exc:  # pragma: no cover - network; never break a seed
        logger.warning("sigage: first-seen lookup failed for %r: %s", signature, exc)
        return None
    for hit in (got.get("result") or {}).get("hits") or []:
        if hit.get("build_id"):
            return str(hit["build_id"])
    return None


_JSON_REV_CACHE: dict = {}


def json_rev(node, channel="nightly"):
    """hg's ``json-rev`` for a changeset: ``{node, pushdate, git_commit, ...}``.

    ONE request serves both things we want about a changeset — when it landed
    (``pushdate_for_node``) and its git counterpart (``git_commit_for_node``) — so they share
    this cache rather than each paying for the same lookup. The endpoint is SLOW (measured
    8-13s on mozilla-central, whichever of hg.mozilla.org / hg-edge you ask), which is why it
    belongs in the worker and never on a page render.

    Takes a SHORT rev, unlike lando's ``hg2git`` which needs the full 40 chars. ``{}`` when
    unresolvable; raises nothing. Cached per ``(node, channel)`` — both fields are immutable."""
    if not node:
        return {}
    key = (node, channel or "")
    if key in _JSON_REV_CACHE:
        return _JSON_REV_CACHE[key]
    from libmozdata.hgmozilla import Mercurial

    from crashclouseau import net

    out = {}
    repo_url = Mercurial.get_repo_url(channel) if channel else ""
    if repo_url:
        try:
            r = net.get("{}/json-rev/{}".format(repo_url, node), allow_redirects=True)
            r.raise_for_status()
            out = r.json() or {}
        except Exception as exc:  # pragma: no cover - network; never break a gate
            logger.warning("sigage: json-rev lookup failed for %s: %s", node, exc)
    _JSON_REV_CACHE[key] = out
    return out


def pushdate_for_node(node, channel="nightly"):
    """When a changeset landed (``[epoch, tzoffset]``), or ``None``.

    This is the FALLBACK for the stale-signature gate. The seed pre-computes a pushdate for
    every candidate in the build's pushlog window, but the agent can choose a candidate that
    was never in that window (it found it via blame), and such a candidate has no pre-computed
    landing date -- so the gate used to silently no-op on precisely the crashes it exists to
    catch. Seen in prod on ``0cf2a052-2eae-4228-824f-6284d0260728``: the candidate landed 126
    days after the signature first appeared, the gate skipped, and only the (paid) blind second
    opinion noticed."""
    return json_rev(node, channel).get("pushdate") or None


def git_commit_for_node(node, channel="nightly"):
    """The git sha for an hg changeset, or ``""``. Firefox lives in both forges since the
    hg->git migration, so the filed bug links the changeset on each; resolved HERE, in the
    worker, and persisted on the candidate so no page render ever pays for it."""
    return json_rev(node, channel).get("git_commit") or ""


def backedout_by_for_node(node, channel="nightly"):
    """The sha that BACKED OUT this changeset: ``""`` when hg says it was not backed out, and
    ``None`` when we could not find out.

    TRI-STATE on purpose. A backed-out candidate is SUPPRESSED outright, not downweighted, so
    "we don't know" must never collapse into "it's clean" — a failed lookup has to leave the
    verdict alone. ``json_rev`` returns (and caches) ``{}`` for every no-answer case: an empty
    channel makes no request at all, and a 404/timeout is swallowed. Hence the ``node`` sentinel
    rather than testing ``backedoutby`` directly, which is simply ABSENT on a clean changeset
    and would be indistinguishable from a failure.

    Free in practice: this is the same cached ``json-rev`` request ``pushdate_for_node`` and
    ``git_commit_for_node`` already make for this same node on every online run.

    Note it says nothing about WHEN the backout landed, and it stays set forever — a change
    that was backed out and later RE-LANDED (as a new node) still reports the old backout."""
    rev = json_rev(node, channel)
    if not rev.get("node"):
        return None
    return rev.get("backedoutby") or ""


def to_datetime(value):
    """Best-effort UTC datetime from any of the pushdate shapes the candidate builders produce:
    a tz-aware ``datetime`` (on-stack, straight from the DB column), hg's ``[epoch, tzoffset]``
    pair or a bare epoch number (off-stack, from ``json-pushes``), an ISO string, or a
    ``YYYYMMDDHHMMSS`` buildid. ``None`` when it is none of those."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (list, tuple)):
        return to_datetime(value[0]) if value else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.isdigit() and len(text) in (8, 14):
        return _buildid_to_dt(text)
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def days_landed_after_first_seen(first_seen, pushdate):
    """Days the candidate landed AFTER the signature was first seen. Positive means the crash
    already existed before the candidate landed — so that candidate cannot be its ORIGIN.
    ``None`` when either side is unknown/unparseable.

    This is the comparison that actually discriminates. Comparing first-seen against the crash's
    BUILD date instead does NOT: two thirds of all triaged signatures are old, so a build-based
    rule fires on ~83% of independently-CONFIRMED leads as well as on the wrong ones. Measured
    on 23 real prod leads with the blind second opinion as the yardstick, this comparison at a
    7-day threshold fires on 10/10 high-confidence refutations while sparing 5 of 6
    corroborated leads."""
    seen_dt = _buildid_to_dt(first_seen)
    push_dt = to_datetime(pushdate)
    if seen_dt is None or push_dt is None:
        return None
    return round((push_dt - seen_dt).total_seconds() / 86400.0, 1)
