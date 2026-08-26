# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""How many machines is this signature actually coming from?

A crash REPORT count is not a volume metric, and the gap is not small. Measured on the 59
loudest Firefox nightly signatures of 2026-08-05..12:

* **7 of 59 came from ONE installation.** The worst was ``GMPChild::RecvPreloadLibs`` -- 1066
  crash reports, one install_time. ``abort | libgallium-24.2.8.so`` was 267 from one. Read as
  volume, those are the two biggest problems on the channel; read as machines they are two
  broken computers.
* 13 of 59 had a single installation supplying half or more of the reports.
* The median signature is the opposite: 0.18 top share, installations hours apart.

So the page shows installations next to reports, and flags the two shapes that mean the report
count is lying. Nothing here gates or scores anything -- ``_apply_bad_machine_gate`` is the gate,
with its own (stricter, CPU-checked) predicate. This is the same question asked for the reader's
benefit, one query cheaper, with no power to suppress a verdict.

TWO THINGS ``install_time`` IS NOT, both of which shape what is computed here:

* **It is not a machine id.** It is a (machine, BUILD) id -- the updater resets it, so a machine
  that updates daily leaves a new one every day. Distinct-install counts therefore OVERCOUNT
  machines over a multi-day window. The count is still the best available floor on "how many
  distinct installations", and it is the same quantity ``stats.installs`` already stores, so the
  two lines on the page mean the same thing.
* **It collides.** One install second can be shared by many machines -- VM and distro images.
  That is what ``clustered`` is for: 25 installations 20 seconds apart are not 25 users.

A future install_time is impossible and does occur (one value in the 2026-08-05..12 sample sits
in 2124), which is why ``span`` is not reported at all: one bogus value moves it by a century.
Every quantity here is a median or a count.
"""
from datetime import datetime, timedelta, timezone
import statistics

from libmozdata import socorro
from libmozdata.connection import Query

from . import config, utils
from .logger import logger

# install_time values outside this range are dropped as unreadable rather than trusted: below it
# predates Firefox itself, above it is in the future. Reported to the reader as `dropped` so a
# thinned population is never silently presented as the whole one.
_MIN_INSTALL_TIME = 1104537600  # 2005-01-01


def _plausible(ts, now):
    return _MIN_INSTALL_TIME <= ts <= int(now.timestamp()) + 86400


def _facet_installs(facets, now):
    """``([(install_time, crashes)], dropped)`` from a raw ``install_time`` facet list."""
    kept, dropped = [], 0
    for f in facets or []:
        term = str(f.get("term", ""))
        if not term.lstrip("-").isdigit():
            dropped += 1
            continue
        ts = int(term)
        if not _plausible(ts, now):
            dropped += 1
            continue
        kept.append((ts, int(f.get("count") or 0)))
    return sorted(kept), dropped


def summarize(facets, total=None, own_install_time=None, now=None, cfg=None):
    """Turn an ``install_time`` facet list into the numbers the page shows. PURE — every
    threshold decision lives here so it is testable without touching the network.

    ``total`` is SuperSearch's own hit count, kept alongside the facet sum because the facet list
    is capped: when the sum falls short of the total, the installation count is a floor and the
    page has to say so rather than present a truncated population as the whole one.

    Returns None when there is nothing to say (no readable installation at all)."""
    cfg = cfg or config.get_population()
    now = now or datetime.now(timezone.utc)
    installs, dropped = _facet_installs(facets, now)
    if not installs:
        return None

    counts = sorted((c for _, c in installs), reverse=True)
    faceted = sum(counts)
    stamps = [ts for ts, _ in installs]
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    median_gap = statistics.median(gaps) if gaps else None
    top_share = counts[0] / faceted if faceted else 0.0

    own = None
    if own_install_time is not None:
        try:
            own_ts = int(own_install_time)
        except (TypeError, ValueError):
            own_ts = None
        if own_ts is not None:
            for rank, (ts, c) in enumerate(
                sorted(installs, key=lambda p: -p[1]), start=1
            ):
                if ts == own_ts:
                    own = {
                        "crashes": c,
                        "share": c / faceted if faceted else 0.0,
                        "rank": rank,
                        "install_time": own_ts,
                    }
                    break

    # The two flags, at thresholds measured against the 59-signature sample (see the module
    # docstring). Each carries a minimum n, because the shape of 2 reports is not a shape:
    # `top_share` on 2 crashes is either 0.5 or 1.0 whatever the population is really like, and a
    # "median" of a single gap is that gap.
    enough_crashes = faceted >= cfg["min_crashes"]
    enough_installs = len(installs) >= cfg["min_installs_for_gap"]
    single = len(installs) == 1 and enough_crashes
    concentrated = enough_crashes and top_share >= cfg["concentrated_share"]
    clustered = enough_installs and median_gap is not None and median_gap <= cfg["clustered_gap_s"]
    return {
        "crashes": total if total is not None else faceted,
        "faceted_crashes": faceted,
        "installs": len(installs),
        "dropped": dropped,
        # Reports per installation — the ratio that makes "1066 reports" and "1 machine"
        # impossible to read as the same thing.
        "per_install": faceted / len(installs),
        "top_crashes": counts[0],
        "top_share": top_share,
        "median_gap_s": median_gap,
        "own": own,
        "single_install": single,
        "concentrated": concentrated,
        "clustered": clustered,
        # A capped facet list means `installs` is a FLOOR. Compared against the facet sum, not
        # the list length: Socorro caps the list, and the sum is what we actually summed.
        "truncated": bool(total and faceted < total),
    }


def _window_start(build_date, now, cfg):
    """Window lower bound, clamped between ``min_lookback_days`` and ``max_lookback_days``.

    The build's own date is the natural anchor -- a build cannot crash before it exists -- but on
    its own it makes the block useless in both directions. A build a few hours old would give a
    population of one report ("1 report from 1 install" says nothing about anything, and every
    flag's minimum n would correctly suppress it), while a build from last spring would ask
    Socorro for a quarter of history. So the window reaches back to the build date, but always at
    least ``min_lookback_days`` and never more than ``max_lookback_days``.

    Returns ``(start, anchored_at_build)`` -- the page states which, because "since 2026-08-11"
    means something different when it is this build's date than when it is simply a week ago."""
    oldest = now - timedelta(days=cfg["max_lookback_days"])
    newest = now - timedelta(days=cfg["min_lookback_days"])
    if build_date is None:
        return oldest, False
    bd = build_date if build_date.tzinfo else build_date.replace(tzinfo=timezone.utc)
    start = min(max(bd, oldest), newest)
    return start, start == bd


def for_crash(uuid_info):
    """The population block for one crash's page, or None.

    TWO concurrent SuperSearches (libmozdata runs a ``queries=`` list in parallel, as
    ``datacollector.get_proto_small`` does): the signature's ``install_time`` facets, and this
    one crash's own ``install_time`` so the reader can see whether THIS report is the one
    supplying the pile. Best-effort throughout — a stats block must never take the page down,
    and a partial answer (facets but no own-install) is still worth showing."""
    cfg = config.get_population()
    if not cfg["enabled"] or not uuid_info:
        return None
    signature = uuid_info.get("signature")
    if not signature:
        return None
    now = datetime.now(timezone.utc)
    start, at_build = _window_start(uuid_info.get("buildid"), now, cfg)
    channel = uuid_info.get("channel")
    product = uuid_info.get("product") or "Firefox"

    facet_params = {
        "product": product,
        "signature": "=" + signature,
        "date": ">=" + start.strftime("%Y-%m-%d"),
        "_facets": "install_time",
        "_facets_size": cfg["facets_size"],
        "_results_number": 0,
    }
    if channel:
        # `get_search_channel`, not the raw label: `aurora` (DevEdition) is 36-41% of beta,
        # and this panel's whole subject is how CONCENTRATED a signature's installations
        # are -- a denominator missing a third of the channel reads as concentration that
        # is not there.
        facet_params["release_channel"] = utils.get_search_channel(channel)
    own_params = {
        "uuid": uuid_info.get("uuid", ""),
        "_columns": ["install_time"],
        "_results_number": 1,
        "_facets": "product",
    }

    got = {}

    def handler(key, json_, data):
        data[key] = json_

    try:
        socorro.SuperSearch(
            queries=[
                Query(
                    socorro.SuperSearch.URL,
                    params=facet_params,
                    handler=lambda j, d: handler("facets", j, d),
                    handlerdata=got,
                ),
                Query(
                    socorro.SuperSearch.URL,
                    params=own_params,
                    handler=lambda j, d: handler("own", j, d),
                    handlerdata=got,
                ),
            ]
        ).wait()
    except Exception as exc:  # pragma: no cover - network
        logger.warning("population: lookup failed for %s: %s", signature, exc)
        return None

    facet_json = got.get("facets") or {}
    facets = (facet_json.get("facets") or {}).get("install_time")
    own_hits = ((got.get("own") or {}).get("hits")) or []
    own_install_time = own_hits[0].get("install_time") if own_hits else None

    res = summarize(
        facets,
        total=facet_json.get("total"),
        own_install_time=own_install_time,
        now=now,
        cfg=cfg,
    )
    if res is None:
        return None
    res["since"] = start.strftime("%Y-%m-%d")
    res["since_is_build"] = at_build
    res["channel"] = channel
    res["product"] = product
    # This build's own numbers come from the `stats` row ingestion already wrote — the same
    # (crashes, install cardinality) pair, for free, no third query.
    res["build"] = _build_stats(uuid_info)
    return res


def _build_stats(uuid_info):
    """``{crashes, installs}`` for this (signature, build) from the ``stats`` table, or None.
    Imported here rather than at module scope to keep ``population`` importable without the DB
    (the pure ``summarize`` is unit-tested on its own)."""
    try:
        from . import models

        return models.Stats.get_for(uuid_info["signature"], uuid_info["buildid"],
                                    uuid_info["channel"], uuid_info["product"])
    except Exception:  # pragma: no cover - defensive; never break the page
        logger.warning("population: build stats lookup failed", exc_info=True)
        return None
