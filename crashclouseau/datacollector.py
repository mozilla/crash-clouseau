# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from collections import defaultdict
import copy
from datetime import datetime
from dateutil.relativedelta import relativedelta
import functools
from libmozdata import socorro, utils as lmdutils
from libmozdata.connection import Connection, Query
import pytz
import re
from . import config, models, sigtrend, utils
from .logger import logger


def get_builds(product, channel, date):
    """Get the buildids for a product/channel prior to date"""
    if channel == "nightly":
        # for nightly, the strategy is pretty simple:
        #  - just get builds few day before (and update the old one too)
        # The window is much wider than the `ndays` baseline on purpose. A build-day is
        # only spike-TESTABLE while at least `ndays` earlier build-days sit ahead of it
        # in the window (utils.evaluate_days), so the old `ndays + 5` gave each build
        # about six run-days of cover -- and crashes concentrating on it after that were
        # structurally invisible. Kill switch: config `nightly_window_ndays` back to 8.
        few_days_ago = date - relativedelta(days=config.get_nightly_window_ndays())
        # Localized, not naive: utils.get_buildid() calls astimezone(), which reads a
        # naive datetime as LOCAL time -- correct on the UTC dynos, two hours off when
        # this is replayed anywhere else, which silently moves the window's lower edge.
        few_days_ago = pytz.utc.localize(
            datetime(few_days_ago.year, few_days_ago.month, few_days_ago.day)
        )
        search_buildid = [
            ">=" + utils.get_buildid(few_days_ago),
            "<=" + utils.get_buildid(date),
        ]
        search_date = ">=" + lmdutils.get_date_str(few_days_ago)
        bids = get_buildids_from_socorro(search_buildid, search_date, product)
    else:
        bids = []
        search_date = ""
        min_date = None
        data = models.Build.get_last_versions(date, channel, product, n=3)
        if data:
            # data are ordered by buildid (desc)
            bids = [x["buildid"] for x in data]
            first_date = utils.get_build_date(bids[-1])
            if min_date is None or min_date > first_date:
                min_date = first_date
            if min_date:
                search_date = ">=" + lmdutils.get_date_str(min_date)

    return bids, search_date


def get_buildids_from_socorro(search_buildid, search_date, product):
    """Get the builds from socorro for nightly channel.
    For other channels we use the database (fed with buildhub data)"""

    def handler(json, data):
        if json["errors"] or not json["facets"]["build_id"]:
            return
        for facets in json["facets"]["build_id"]:
            bid = facets["term"]
            data.append(bid)

    params = {
        "product": product,
        "release_channel": "nightly",
        "date": search_date,
        "build_id": search_buildid,
        "_facets": "build_id",
        "_results_number": 0,
        "_facets_size": config.get_build_facets_limit(),
    }

    data = []
    socorro.SuperSearch(params=params, handler=handler, handlerdata=data).wait()

    data = sorted(data)

    return data


def get_maturity_bar(product, channel):
    """``(mature_after_days, mature_installs)`` for ``utils.evaluate_days`` — **nightly
    only**, which is why this is a function and not two config reads.

    The bar exists to price the wider nightly build window, and only nightly's window
    widened (beta/release take their builds from ``Build.get_last_versions(n=3)``).
    Applying it everywhere silently over-gated beta, whose spike floor (10) sits ABOVE its
    install threshold (6), so a mature build-day with 6-9 crashes went from selected to
    ``immature``. Only the INSTALL half of the bar is inert off nightly (via
    ``max(threshold, mature_installs)``); the floor half is not, so the gate has to be
    here rather than in the config values."""
    if channel != "nightly":
        return None, 1
    return (
        config.get_spike("mature_after_days", product, channel),
        config.get_spike("mature_installs", product, channel),
    )


def get_no_user_build_floor(product, channel):
    """Minimum distinct INSTALLATIONS a build-day needs before it may act as a BASELINE —
    **not nightly**, which is why this is a function and not a config read (same shape as
    ``get_maturity_bar``).

    THE PROBLEM IS A BUILD NOBODY RAN. Every Firefox cycle ships two builds tagged ``N.0b1``:
    the merge-day build (whose revision is "Update configs after merge day operations") and,
    days later, the one that actually reaches users. Lifetime figures for the merge-day builds
    of v151-v155, all channels, no date bound: **8/4, 13/6, 7/7, 17/4 and 1/1 reports over
    installations**, against **435-9,124 reports and 268-5,084 installations for all 54 other
    builds since 2026-04-01** (median 2,850/1,974).

    INSTALLATIONS, NOT REPORTS, and that correction is the whole of this docstring's history.
    The report gap looks bigger (25x, no overlap, "any floor in [20, 400]") but it is a gap
    between LIFETIME totals, while the code can only see what has arrived BY NOW — and that is
    steeply age-dependent, which is why the newest day is exempt at all (0.2-2.7% on its own
    ship day). The exemption is one build-day wide and the curve is still steep after it: the
    one measured early point is 154.0b10 at **11.0% of its eventual crashes at 1.25 days**, and
    window index 1 is exactly one cadence gap old (min 1.26 d, p25 2.00 d over 58 gaps). So the
    quietest REAL build (435 lifetime reports) shows about 48 reports while it sits at index 1 —
    under a floor of 100. And index 1 is the ONLY index that ever selects anything: 135 of 135
    replayed selections landed there, index 2 can never clear ``3 x max(before)`` and index 0 is
    untestable. Dropping it costs the entire run-day, silently, which is the same switch-off
    ``Build.get_last_versions`` was just fixed for, reached from the other direction.

    Installations do not have that problem, because a build nobody runs never acquires any: the
    merge-day builds sit at **4-7 installations FOREVER** while a real build passes 268. The
    statistic is the MAXIMUM over signatures, never the sum — per-signature install
    cardinalities do not add (a machine crashing on five signatures would be counted five
    times), whereas the max is a true lower bound on the build's distinct installations.

    **15 is defensible over [8, 24] and no wider**, and that is a smaller margin than the report
    gap, so it is stated rather than glossed: the merge-day maximum is 7 (v153, 7 reports from 7
    installations) and the quietest real build shows ~29 at index 1 (268 lifetime installs at
    ~11% arrival). If a future cycle ships a merge-day build to more than ~24 installations, this
    number needs re-measuring, not nudging.

    THE HARM IS THE BASELINE, NOT THE SELECTION. Sitting between two real builds in a 3-build
    window, that build-day is a ZERO for every signature — and ``utils.is_spike``'s from-zero
    branch is gated by neither ``floor`` nor ``ratio``, so every signature clearing the
    6-install threshold on the NEXT build spikes. Replayed over 30 beta run-days: **4 run-days
    carried 108 of 179 selections (60%) and 104 of 160 from-zero fires (65%)**, and the top of
    the burst is boilerplate no analysis can act on (``OOM | small`` 236, ``OOM | unknown |
    js::AutoEnterOOMUnsafeRegion::crash_impl`` 125, ``shutdownhang | RtlWaitOnAddress``,
    ``AsyncShutdownTimeout | profile-before-change``). Removing it takes distinct selected
    pairs from 105 to 40 per 30 days and the worst run from 38 to 8.

    NEVER APPLIED TO THE NEWEST BUILD-DAY IN THE WINDOW (the caller enforces it): a build holds
    only **0.2-2.7% of its eventual crashes on its own ship day** (154.0b10 1.1%, 155.0b1 0.2%)
    and 77-96% by day 4, so a fresh build is quiet for a reason that has nothing to do with
    users. The merge-day build only ever hurts once it is no longer the newest.

    AND NEVER ON NIGHTLY. Nightly's builds come from Socorro, not from the ``builds`` table;
    there is no merge-day build; and a quiet nightly build-day is ordinary (median 315 lifetime
    reports per build against beta's 2,674). Dropping one there would REMOVE a real baseline
    and make the from-zero branch fire more, which is the opposite of the fix. ``0`` disables.

    THIS IS NOT THE SAME FIX AS ``Build.get_last_versions``' major-version break, and neither
    subsumes the other: with the no-user build removed the merge blackout SHIFTS to the shipped
    b1 and shortens to 2 days each (10 of 127 run-days, replayed). Both are needed."""
    if channel == "nightly":
        return 0
    return config.get_spike("min_build_installs", product, channel)


def find_no_user_days(data, floor):
    """The build-days no installation ever ran, excluding the newest day in the window.
    ``floor <= 0`` disables and returns an empty set.

    ``data`` is ``{signature: {day: {"count": n, "bids": {...}, "installs": {...}}}}`` as
    ``get_new_signatures`` assembles it, so this needs no extra request.

    THE MAXIMUM PER-SIGNATURE INSTALL CARDINALITY, and both halves of that matter. Not reports,
    because the report count is age-dependent and the floor would fire on a real build seen early
    (see ``get_no_user_build_floor``). Not the SUM of the cardinalities either: they do not add,
    since a machine crashing on five signatures is counted five times — the max is the only one
    of the three that is a true lower bound on the build's distinct installations.

    The newest day is exempt unconditionally. It is the day we are here to select, and it is
    quiet for a reason that is about the clock rather than about users (0.2-2.7% of a build's
    crashes have arrived on its own ship day)."""
    if not floor or floor <= 0 or not data:
        return set()
    installs = defaultdict(int)
    for numbers in data.values():
        for day, info in numbers.items():
            seen = max((info["installs"] or {}).values(), default=0)
            installs[day] = max(installs[day], seen)
    if not installs:
        return set()
    newest = max(installs)
    return {day for day, n in installs.items() if day != newest and n < floor}


def dropped_day_records(numbers, dead_days):
    """``Selection``-shaped records for the dropped build-days this signature crashed on.

    Same key set as ``utils.evaluate_days`` emits, because ``models.Selection._row`` reads
    them positionally by name. ``index``/``baseline``/``evaluable`` describe a day that was
    never placed in a series at all, so they say so (-1 / [] / False) rather than inventing a
    position the day never had."""
    records = []
    for day in sorted(dead_days):
        info = numbers.get(day)
        if not info or not info["count"]:
            continue
        records.append(
            {
                "day": day,
                "count": info["count"],
                "index": -1,
                "baseline": [],
                "evaluable": False,
                "spiked": False,
                "bids": dict(info["bids"]),
                "installs": dict(info["installs"]),
                "picked": None,
                "outcome": utils.DROPPED_NO_USERS,
            }
        )
    return records


def _day_records(numbers, outcome):
    """``Selection``-shaped records carrying *outcome* for every build-day a signature removed
    BEFORE the spike test was reported on. Same key set as ``dropped_day_records``, and the same
    "never placed in a series" markers."""
    return [
        {
            "day": day,
            "count": info["count"],
            "index": -1,
            "baseline": [],
            "evaluable": False,
            "spiked": False,
            "bids": dict(info["bids"]),
            "installs": dict(info["installs"]),
            "picked": None,
            "outcome": outcome,
        }
        for day, info in sorted(numbers.items())
        if info["count"]
    ]


def ignored_day_records(numbers):
    """Outcome ``ignored``: a signature ``config.ignored_signatures`` names."""
    return _day_records(numbers, utils.IGNORED)


def no_stack_day_records(numbers):
    """Outcome ``no_stack``: an ``EMPTY: ...`` signature (``utils.signature_class``)."""
    return _day_records(numbers, utils.NO_STACK)


def get_new_signatures(product, channel, date):
    """Collect the crash signatures worth triaging for a product/channel. A signature is
    kept when its per-day crash count SPIKES -- it clears an absolute floor and jumps well
    above the loudest of the preceding ``ndays`` days (see ``utils.is_spike``). This
    catches both a signature appearing from ~zero and a sudden worsening of an existing
    one, without firing on low-volume churn.

    Note the axis: the series is counts per BUILD-day, not per crash-day, and a build's
    count keeps growing as its reports arrive. So this answers "is this build crashier
    than the ones before it", which is the regression question -- not "did this signature
    spike today", which is what a human means by the word.

    Returns ``(data, selection)``: the signatures to analyse, and one record per near-miss
    build-day for ``models.Selection``, so a declined signature leaves a trace. ``data`` holds
    only pairs with at least one report to ingest: an ``EMPTY: ...`` signature is declined
    ``no_stack`` before the test, a kept pair Socorro then holds no proto cluster or Java report
    for is dropped and its record rewritten ``no_protos`` (``declare_no_protos``)."""

    limit = config.get_limit_facets()
    bids, search_date = get_builds(product, channel, date)
    if not bids:
        logger.warning("No buildids for {}-{}.".format(product, channel))
        # Two values, like the tail of this function: put_crashes unpacks the pair, and a
        # bare {} here raised "not enough values to unpack". Reachable persistently on
        # beta/release, whose branch of get_builds reads the `builds` table -- empty after
        # a DB wipe or a buildhub gap, not just on a Socorro blip.
        return {}, []

    base = {}
    for bid in bids:
        bid = utils.get_build_date(bid)
        day = datetime(bid.year, bid.month, bid.day)
        if day not in base:
            base[day] = {"installs": {}, "bids": {}, "count": 0}
        base[day]["bids"][bid] = 0

    logger.info("Get crash numbers for {}-{}: started.".format(product, channel))

    def handler(base, json, data):
        if json["errors"]:
            raise Exception(
                "Error in json data from SuperSearch: {}".format(json["errors"])
            )
        if not json["facets"]["signature"]:
            return
        for facets in json["facets"]["signature"]:
            installs = facets["facets"]["cardinality_install_time"]["value"]
            sgn = facets["term"]
            bid_info = facets["facets"]["build_id"][0]
            count = bid_info["count"]
            bid = bid_info["term"]
            bid = utils.get_build_date(bid)
            day = datetime(bid.year, bid.month, bid.day)
            if sgn in data:
                numbers = data[sgn]
            else:
                data[sgn] = numbers = copy.deepcopy(base)
            numbers[day]["count"] += count
            numbers[day]["bids"][bid] = count
            numbers[day]["installs"][bid] = 1 if installs == 0 else installs
        del json

    params = {
        "product": product,
        "release_channel": utils.get_search_channel(channel),
        "date": search_date,
        "build_id": "",
        "_aggs.signature": ["build_id", "_cardinality.install_time"],
        "_results_number": 0,
        "_facets": "release_channel",
        "_facets_size": limit,
    }

    data = {}
    hdler = functools.partial(handler, base)
    for bid in bids:
        params["build_id"] = bid
        socorro.SuperSearch(params=params, handler=hdler, handlerdata=data).wait()

    # Deliberate test crashes (`config.ignored_signatures`) leave the series HERE, before any
    # test can pick them -- whatever their numbers -- and leave one `ignored` row per build-day
    # they were reported on, so the selection log still answers "why not". `ignored` is also
    # handed to the rate path below, which reads its own rollup rather than this series.
    selection = []
    ignored = {sgn for sgn in data if config.is_ignored_signature(sgn)}
    for sgn in sorted(ignored):
        selection.extend(dict(rec, signature=sgn) for rec in ignored_day_records(data.pop(sgn)))
        logger.info("Ignoring {} on {}-{}: a signature config.ignored_signatures names".format(
            sgn, product, channel))
    # `EMPTY: no frame data available` leaves the series here too, ON EVERY PRODUCT: no native
    # stack and no Java stack means nothing could ever be scored, so the spike test could only
    # hand it to a report fetch that comes back empty. Before this it was logged `selected`
    # (ever_selected, offered to the spike sweep) and `put_crashes` wrote a Stats row with
    # installs=0 and no uuid -- a Fenix matter at scale (20-40% of its nightly reports, five
    # signatures, plans/16 §5) and desktop's rare EmptyMinidump spikes did exactly the same.
    empty = {sgn for sgn in data if utils.signature_class(sgn) == "empty"}
    for sgn in sorted(empty):
        selection.extend(dict(rec, signature=sgn) for rec in no_stack_day_records(data.pop(sgn)))
        logger.info("Declining {} on {}-{}: no stack to score ({})".format(
            sgn, product, channel, utils.NO_STACK))

    shift = config.get_ndays() if channel == "nightly" else 1
    threshold = config.get_threshold("installs", product, channel)
    floor = config.get_spike("floor", product, channel)
    ratio = config.get_spike("ratio", product, channel)
    mature_after, mature_installs = get_maturity_bar(product, channel)
    # Build-days carried by a build nobody ran: removed from the series BEFORE it is
    # evaluated, so they cannot be the zero baseline that makes the next build's every
    # signature spike. Free — the per-signature/per-build facet this function already
    # fetched is all the arithmetic needs. See `get_no_user_build_floor`.
    dead_days = find_no_user_days(data, get_no_user_build_floor(product, channel))
    if dead_days:
        logger.info(
            "Dropping {} build-day(s) with no users for {}-{}: {}".format(
                len(dead_days), product, channel,
                ", ".join(sorted(d.strftime("%Y-%m-%d") for d in dead_days)),
            )
        )
    big_data = {}
    small_data = {}
    # What the spike test declined, series and all, for the rate path below.
    declined = {}
    # ONE LAMBDA, TWO SIGNATURES (`utils.lambda_family`): a family is decided ONCE, on the
    # summed series, and each member then carries its own share of the picked builds. Split,
    # the 2026-08-14 nightly build-day of `QuotaManager::Shutdown::<T>::operator()` read 19 vs
    # a bar of 21 on one half and 8 vs 9 on the other; merged, 27 vs 27 fires.
    families = utils.lambda_families(data)
    decided = set()

    def decide(numbers):
        """The spike test over one series minus the no-user build-days; ``None`` when nothing
        is left to test."""
        if dead_days:
            numbers = {d: v for d, v in numbers.items() if d not in dead_days}
            if not numbers:
                return None
        return utils.evaluate_days(
            numbers,
            shift,
            threshold,
            floor,
            ratio,
            today=date,
            mature_after=mature_after,
            mature_installs=mature_installs,
        )

    def keep(sgn, bids, big):
        d = {
            "bids": bids,
            "protos": {b: [] for b in bids},
            "installs": {b: 0 for b in bids},
        }
        if big:
            big_data[sgn] = d
        else:
            small_data[sgn] = d

    def worth_logging(records):
        # Keep every decision that is not a plain "nothing happened", plus the loud days
        # that did not spike -- "we had N crashes and you did nothing" is a question the
        # pipeline could not answer before. Everything quieter is dropped: it is the
        # overwhelming majority and it carries no signal.
        return [
            rec for rec in records
            if rec["outcome"] != utils.NOT_SPIKING or rec["count"] >= floor
        ]

    for sgn, numbers in data.items():
        # The drop leaves a trace, at this table's own grain: one row per signature that
        # actually crashed on the dropped day. A no-user build carries 1-17 reports, so this
        # is a handful of rows, and "why was signature X not selected on the 13th" now has an
        # answer inside the system instead of needing Socorro rebuilt by hand.
        selection.extend(
            dict(rec, signature=sgn)
            for rec in dropped_day_records(numbers, dead_days)
        )
        members = families.get(sgn)
        if members:
            if sgn in decided:
                continue
            decided.update(members)
            verdict = decide(utils.merge_day_series([data[m] for m in members]))
            if verdict is None:
                continue
            bids, big, records = verdict
            for m in members:
                others = [o for o in members if o != m]
                selection.extend(
                    dict(rec, signature=m, merged_with=others)
                    for rec in worth_logging(records)
                )
                # This member's own crashes on the builds the FAMILY picked; a half with none
                # there has no proto-signatures to fetch.
                own = {}
                for b in bids:
                    n = sum(e["bids"].get(b, 0) for e in data[m].values())
                    if n:
                        own[b] = n
                if own:
                    keep(m, own, big)
                elif not bids:
                    declined[m] = data[m]
            continue
        verdict = decide(numbers)
        if verdict is None:
            continue
        bids, big, records = verdict
        selection.extend(dict(rec, signature=sgn) for rec in worth_logging(records))
        if bids:
            keep(sgn, bids, big)
        else:
            declined[sgn] = numbers

    rising_data = _rising_picks(
        product, channel, date, declined,
        set(big_data) | set(small_data) | ignored | empty, threshold, selection,
    )
    del data
    del declined

    logger.info("Get crash numbers for {}-{}: finished.".format(product, channel))
    # What the spike test and the rate path KEPT, before Socorro is asked for reports. A pair
    # that comes back without one cluster is removed by its fetcher (`drop_unresolved`) and its
    # record is rewritten below.
    kept = set(big_data) | set(small_data) | set(rising_data)
    # A Java signature has no proto_signature facet -- the two columns are mutually exclusive
    # (Fenix nightly, 14 d: 17,143 reports with java_stack_trace, 7,764 with proto_signature,
    # 0 with both; plans/16 §5) -- so it is read from the java_stack_trace column instead,
    # under the cap its pick kind gives a native signature.
    java_data = _split_java(big_data)
    java_data.update(_split_java(small_data))
    java_rising = _split_java(rising_data)
    if big_data:
        get_proto_big(product, big_data, search_date, channel)

    if small_data:
        get_proto_small(product, small_data, search_date, channel)

    if rising_data:
        get_proto_small(
            product, rising_data, search_date, channel,
            proto_cap=config.get_spike("rising_protos", product, channel),
        )

    if java_data:
        get_uuids_java(
            product, java_data, search_date, channel,
            config.get_threshold("protos", product, channel),
        )

    if java_rising:
        get_uuids_java(
            product, java_rising, search_date, channel,
            config.get_spike("rising_protos", product, channel),
        )

    data = {}
    for part in (small_data, big_data, rising_data, java_data, java_rising):
        data.update(part)
    declare_no_protos(kept - set(data), selection, product, channel)

    return data, selection


def _split_java(signatures):
    """Pop the Java-class signatures (``utils.signature_class``) out of *signatures*, in place,
    into a dict of their own. The native fetchers would spend a query per chunk on them and
    return before their signature loop (``get_proto_small``'s handler exits on an empty
    proto_signature facet), leaving ``protos=[]`` and ``installs=0``."""
    return {
        sgn: signatures.pop(sgn)
        for sgn in list(signatures)
        if utils.signature_class(sgn) == "java"
    }


def drop_unresolved(signatures, product, channel, what):
    """Remove from *signatures* (in place) every signature the fetch just run yielded no
    cluster for on ANY of its builds, and return the set removed. Each fetcher calls this on
    the dict it handled, because the fetcher is the one that knows it asked.

    What is removed never reaches ``update.put_crashes``, which used to write a Stats row with
    installs=0 (``Stats.get_for`` reads 0 as a known count -- "0 installations" on the crash
    page) and no uuid for it. 37.8% of Fenix's non-Java nightly reports carry no proto_signature
    (the Android stackwalker failed on them, plans/16 §5), so on Fenix this is the common case,
    not the corner."""
    gone = {sgn for sgn, info in signatures.items() if not any(info["protos"].values())}
    for sgn in sorted(gone):
        del signatures[sgn]
        logger.info("No {} for {} on {}-{}: nothing to ingest".format(
            what, sgn, product, channel))
    return gone


def declare_no_protos(signatures, selection, product, channel):
    """Rewrite the ``selected`` / ``rising_rate`` records of *signatures* -- pairs the selector
    kept and the report fetch then removed (``drop_unresolved``) -- to ``no_protos``, ``picked``
    cleared. Returns the number of records rewritten.

    ``selected`` sets ``ever_selected`` (``models.SELECTED_OUTCOMES``), which claimed an
    analysis that never happened and made ``Selection.escalation_candidates`` offer the pair to
    the spike sweep -- one ``build_history`` SuperSearch per pair per sweep before
    ``representative_uuid`` found no ingested report and parked it. ``no_protos`` is outside
    that set, so the sweep never sees the pair and the rate path does not count it as taken
    (``_rising_picks`` reads the ``no_protos`` rows instead, so it does not re-pick it every
    tick either)."""
    if not signatures:
        return 0
    rewritten = 0
    for rec in selection:
        if rec["signature"] in signatures and rec["outcome"] in models.SELECTED_OUTCOMES:
            rec["outcome"] = utils.NO_PROTOS
            rec["picked"] = None
            rewritten += 1
    logger.info("{} kept signature(s) on {}-{} yielded no report to ingest ({}): {}".format(
        len(signatures), product, channel, utils.NO_PROTOS, ", ".join(sorted(signatures))))
    return rewritten


def _rising_picks(product, channel, date, declined, already, threshold, selection):
    """The RATE path: among the signatures the spike test declined, the ones whose
    exposure-normalised daily rate is rising (``sigtrend.rising_candidates``), within today's
    budget, each on its freshest build with users. Returns the same ``{signature: {"bids",
    "protos", "installs"}}`` shape as a spike pick and appends one ``rising_rate`` record per
    picked signature to *selection*.

    Budgeted, not thresholded: ``spike.rising_per_day`` picks a day per channel, best statistic
    first, and a family already selected -- by the spike test within a week, or by this path --
    is skipped (``Selection.covered_recently``), so a rise that lasts its whole 7-day window is
    paid for once. A rising family with no crash on a current-window build that clears the
    install threshold is not picked: there is nothing current to analyse. Never raises -- the
    rollup is observability, and observability must not be able to stop the spike test."""
    budget = config.get_spike("rising_per_day", product, channel)
    if budget <= 0 or not declined:
        return {}
    room = budget - models.Selection.taken_today(product, channel, utils.RISING_RATE)
    if room <= 0:
        return {}
    asof = date.date() if hasattr(date, "date") else date
    try:
        candidates = sigtrend.rising_candidates(product, channel, asof=asof, exclude=already)
    except Exception:
        logger.error("Cannot scan %s-%s for rising signatures", product, channel, exc_info=True)
        return {}
    if not candidates:
        return {}
    covered = models.Selection.covered_recently(
        product, channel, [m for _, members, _ in candidates for m in members]
    )
    covered |= _no_protos_recently(product, channel)
    picks = {}
    for family, members, facts in candidates:
        if room <= 0:
            break
        if covered.intersection(members):
            continue
        chosen = {}
        for m in members:
            numbers = declined.get(m)
            latest = utils.pick_latest_build(numbers, threshold) if numbers else None
            if latest:
                chosen[m] = (numbers, latest)
        if not chosen:
            continue
        room -= 1
        for m, (numbers, (day, bid, n)) in chosen.items():
            picks[m] = {"bids": {bid: n}, "protos": {bid: []}, "installs": {bid: 0}}
            selection.append({
                "signature": m,
                "day": day,
                "count": numbers[day]["count"],
                "index": sorted(numbers).index(day),
                "baseline": [],
                "evaluable": True,
                "spiked": False,
                "bids": dict(numbers[day]["bids"]),
                "installs": dict(numbers[day]["installs"]),
                "picked": bid,
                "outcome": utils.RISING_RATE,
                "trend_ratio": facts.get("signature_trend_ratio"),
                "trend_installs": facts.get("signature_trend_installs"),
                "merged_with": [o for o in members if o != m],
            })
        logger.info(
            "Rising rate for {}-{}: selecting {} ({}x, {} installs in {} days) on {}".format(
                product, channel, family, facts.get("signature_trend_ratio"),
                facts.get("signature_trend_installs"), facts.get("signature_trend_window_days"),
                ", ".join(utils.get_buildid(latest[1]) for _, latest in chosen.values()),
            )
        )
    return picks


def _no_protos_recently(product, channel, days=7):
    """The signatures whose pick came back with nothing to ingest (``no_protos``) on a build-day
    of the last ``days``. ``covered_recently`` reads ``ever_selected``, which ``no_protos`` does
    not set, and a rate pick that is rewritten does not count as ``taken_today`` either -- so
    without this a rising signature Socorro holds no report for would be picked again on every
    20-minute tick for the week its rise lasts, ahead of every other candidate. Fails toward
    "not covered", i.e. the pre-existing behaviour."""
    try:
        rows = models.Selection.recent(
            outcome=utils.NO_PROTOS, days=days, product=product, channel=channel)
    except Exception:
        logger.error("Cannot read the no_protos rows for %s-%s", product, channel, exc_info=True)
        # Like `covered_recently` / `taken_today`: on Postgres a failed statement leaves the
        # transaction aborted, and the next one on this session is the tick's selection-log
        # upsert, which would then fail whole (`InFailedSqlTransaction`).
        models.db.session.rollback()
        return set()
    return {row["signature"] for row in rows}


def get_proto_small(product, signatures, search_date, channel, proto_cap=None):
    """Get the proto-signatures for signature with a small number of crashes.
    Since we 'must' aggregate uuid on proto-signatures, to be faster we query
    several signatures: it's possible because we know that card(proto) <= card(crashes)
    for a given signature.

    ``proto_cap`` overrides ``thresholds.protos`` -- the rate path's picks are capped lower
    than nightly's spike picks (see ``config._SPIKE_DEFAULTS["rising_protos"]``)."""
    logger.info(
        "Get proto-signatures (small) for {}-{}: started.".format(product, channel)
    )

    def handler(bid, threshold, json, data):
        if not json["facets"]["proto_signature"]:
            return
        for facets in json["facets"]["proto_signature"]:
            _facets = facets["facets"]
            sgn = _facets["signature"][0]["term"]
            protos = data[sgn]["protos"][bid]
            if len(protos) < threshold:
                proto = facets["term"]
                count = facets["count"]
                uuid = _facets["uuid"][0]["term"]
                protos.append({"proto": proto, "count": count, "uuid": uuid})
        for facets in json["facets"]["signature"]:
            sgn = facets["term"]
            count = facets["facets"]["cardinality_install_time"]["value"]
            data[sgn]["installs"][bid] = 1 if count == 0 else count

    limit = config.get_limit_facets()
    threshold = config.get_threshold("protos", product, channel)
    if proto_cap is not None:
        threshold = proto_cap
    base_params = {
        "product": product,
        "release_channel": utils.get_search_channel(channel),
        "date": search_date,
        "build_id": "",
        "signature": "",
        "_aggs.proto_signature": ["uuid", "signature"],
        "_aggs.signature": "_cardinality.install_time",
        "_results_number": 0,
        "_facets": "release_channel",
        "_facets_size": limit,
    }

    sgns_by_bids = utils.get_sgns_by_bids(signatures)
    for bid, all_signatures in sgns_by_bids.items():
        params = copy.deepcopy(base_params)
        params["build_id"] = utils.get_buildid(bid)
        queries = []
        hdler = functools.partial(handler, bid, threshold)
        for sgns in Connection.chunks(all_signatures, 5):
            params = copy.deepcopy(params)
            params["signature"] = ["=" + s for s in sgns]
            queries.append(
                Query(
                    socorro.SuperSearch.URL,
                    params=params,
                    handler=hdler,
                    handlerdata=signatures,
                )
            )

        socorro.SuperSearch(queries=queries).wait()

    drop_unresolved(signatures, product, channel, "proto-signature")
    logger.info(
        "Get proto-signatures (small) for {}-{}: finished.".format(product, channel)
    )


def get_proto_big(product, signatures, search_date, channel):
    """Get proto-signatures for signatures which have a high # of crashes (>=500)"""
    logger.info(
        "Get proto-signatures (big) for {}-{}: started.".format(product, channel)
    )

    def handler(bid, threshold, json, data):
        if not json["facets"]["proto_signature"]:
            return
        installs = json["facets"]["cardinality_install_time"]["value"]
        data["installs"][bid] = 1 if installs == 0 else installs
        for facets in json["facets"]["proto_signature"]:
            protos = data["protos"][bid]
            if len(protos) < threshold:
                proto = facets["term"]
                count = facets["count"]
                uuid = facets["facets"]["uuid"][0]["term"]
                protos.append({"proto": proto, "count": count, "uuid": uuid})

    threshold = config.get_threshold("protos", product, channel)
    base_params = {
        "product": product,
        "release_channel": utils.get_search_channel(channel),
        "date": search_date,
        "build_id": "",
        "signature": "",
        "_aggs.proto_signature": "uuid",
        "_results_number": 0,
        "_facets": "_cardinality.install_time",
        "_facets_size": threshold,
    }

    sgns_by_bids = utils.get_sgns_by_bids(signatures)
    for bid, all_signatures in sgns_by_bids.items():
        params = copy.deepcopy(base_params)
        params["build_id"] = utils.get_buildid(bid)
        queries = []
        hdler = functools.partial(handler, bid, threshold)
        for sgn in all_signatures:
            params = copy.deepcopy(params)
            params["signature"] = "=" + sgn
            queries.append(
                Query(
                    socorro.SuperSearch.URL,
                    params=params,
                    handler=hdler,
                    handlerdata=signatures[sgn],
                )
            )

        socorro.SuperSearch(queries=queries).wait()

    drop_unresolved(signatures, product, channel, "proto-signature")
    logger.info(
        "Get proto-signatures (big) for {}-{}: finished.".format(product, channel)
    )


# One frame of a Socorro ``java_stack_trace``, after ``strip()``:
# ``at pkg.Class.method(File.kt:123)``, ``at pkg.Class.method(Native Method)``,
# ``at pkg.Class$$ExternalSyntheticLambda4.invoke(R8$$SyntheticClass:5)``. Group 1 is the
# dotted ``pkg.Class.method``, group 2 the file; the line, when present, is matched and DROPPED.
_JAVA_FRAME = re.compile(r"^at ([^\(]+)\(([^:\)]+)(?::\d+)?\)$")
# How many of OUR frames make a shape, and how many of anyone's when none is ours.
JAVA_PROTO_FRAMES = 6
JAVA_PROTO_FALLBACK_FRAMES = 3


def java_proto(stack_trace):
    """A LINE-FREE pseudo proto-signature for a Java report: ``java | pkg.Class.method(File.kt)
    | ...`` over the first ``JAVA_PROTO_FRAMES`` frames in our packages
    (``config.is_java_package``), else the first ``JAVA_PROTO_FALLBACK_FRAMES`` frames of any
    package; ``""`` when no frame parses.

    Line numbers are left out because R8 remaps them (``java.trust_line_numbers``): two reports
    of one exception site differ only in their remapped lines, and a shape carrying them would
    split one cluster per line. The frame set is capped for the same reason the native proto
    is: deeper frames are the framework's, not the site's. Measured 2026-09-15 on Fenix nightly:
    20 hits of ``java.lang.OutOfMemoryError: at java.util.Arrays.copyOf`` = 1 shape; 20 hits of
    the ``IllegalBlockSizeException`` at ``AndroidKeyStoreCipherSpiBase.engineDoFinal`` = 2 raw
    traces (one had more coroutine frames below ours) = 1 shape.

    ``utils.hash(proto)`` is what ``UUID.add`` dedups on per build and what
    ``proto_already_analyzed`` closes per channel, so identical shapes on two builds share one
    cluster and two exception sites of one signature do not. The dead Fennec path used
    ``proto=""`` and hashed every Java report of a signature into ONE cluster forever."""
    ours, anyone = [], []
    for line in (stack_trace or "").splitlines():
        m = _JAVA_FRAME.match(line.strip())
        if not m:
            continue
        frame = "{}({})".format(m.group(1), m.group(2))
        if len(anyone) < JAVA_PROTO_FALLBACK_FRAMES:
            anyone.append(frame)
        if config.is_java_package(m.group(1)):
            ours.append(frame)
            if len(ours) == JAVA_PROTO_FRAMES:
                break
    frames = ours or anyone
    if not frames:
        return ""
    return "java | " + " | ".join(frames)


def get_uuids_java(product, signatures, search_date, channel, cap):
    """The uuids of the Java-class signatures in *signatures*: ONE SuperSearch per (signature,
    build) reading the ``uuid`` and ``java_stack_trace`` columns, clustered client-side by
    ``java_proto`` into at most *cap* shapes per pair (loudest first), each entered as
    ``{"proto", "count", "uuid"}`` exactly like a native proto-signature cluster. ``installs``
    comes from a ``_cardinality.install_time`` facet on the same query, 0 coerced to 1 the way
    the native fetchers do.

    A column read, not a facet, because Socorro has no ``java_stack_trace`` aggregation and a
    ``uuid`` facet gives one arbitrary report per signature. The sample is
    ``max(4 * cap, 20)`` hits (a 20-hit signature collapsed to 1 shape live), so ``count`` is a
    within-sample figure that nothing downstream reads -- ``UUID.add`` stores the uuid under
    ``hash(proto)``. *cap* is the caller's: ``thresholds.protos`` for a spike pick,
    ``spike.rising_protos`` for a rate pick, mirroring the native path. A report whose trace
    yields no frame is skipped; a pair left without a shape is removed (``drop_unresolved``)."""
    logger.info("Get uuids (java) for {}-{}: started.".format(product, channel))

    def handler(bid, sgn, cap, json, data):
        if json.get("errors"):
            logger.warning("SuperSearch errors on the java uuids of {} / {}: {}".format(
                sgn, utils.get_buildid(bid), json["errors"]))
            return
        shapes = {}
        for hit in json["hits"]:
            proto = java_proto(hit.get("java_stack_trace"))
            if not proto:
                continue
            if proto in shapes:
                shapes[proto][0] += 1
            else:
                shapes[proto] = [1, hit["uuid"]]
        protos = data[sgn]["protos"][bid]
        ranked = sorted(shapes.items(), key=lambda kv: -kv[1][0])
        for proto, (count, uuid) in ranked[:cap]:
            protos.append({"proto": proto, "count": count, "uuid": uuid})
        installs = json["facets"]["cardinality_install_time"]["value"]
        data[sgn]["installs"][bid] = 1 if installs == 0 else installs

    base_params = {
        "product": product,
        "release_channel": utils.get_search_channel(channel),
        "date": search_date,
        "build_id": "",
        "signature": "",
        "_columns": ["uuid", "java_stack_trace"],
        "_results_number": max(4 * cap, 20),
        "_facets": "_cardinality.install_time",
    }

    sgns_by_bids = utils.get_sgns_by_bids(signatures)
    for bid, all_signatures in sgns_by_bids.items():
        queries = []
        for sgn in all_signatures:
            params = copy.deepcopy(base_params)
            params["build_id"] = utils.get_buildid(bid)
            params["signature"] = "=" + sgn
            queries.append(
                Query(
                    socorro.SuperSearch.URL,
                    params=params,
                    handler=functools.partial(handler, bid, sgn, cap),
                    handlerdata=signatures,
                )
            )

        socorro.SuperSearch(queries=queries).wait()

    drop_unresolved(signatures, product, channel, "Java report")
    logger.info("Get uuids (java) for {}-{}: finished.".format(product, channel))


# The source URI Socorro's symbolication stamps on a frame since the hg->git move:
# ``git:github.com/mozilla-firefox/firefox:<path>:<40-hex git sha>`` (``inspector.GIT_PAT``;
# no slash after the repository). Live 2026-09-15: 240 reports voted on Firefox nightly
# 20260912093409, 33 on Fenix nightly 20260912211859, one sha each.
_GIT_TOPMOST_FILENAMES = '@"git:github.com/mozilla-firefox/firefox:".*:[0-9a-f]+'


def get_changeset(buildid, channel, product):
    """The hg revision of a build by VOTING on the ``topmost_filenames`` of its reports -- the
    last resort behind the ``builds`` table and the build source (``tools.get_changeset``).

    Frames carry the GIT sha of the source they were built from, so the vote is over git shas
    and the winner goes through ``inspector.git2hg`` (Lando) before ``utils.short_rev``; a sha
    Lando does not map (or a build with no such frame) answers ``None``. The hg-shaped filter
    this used to send matched nothing built after 2025-11-10, so this returned ``None`` for
    every build for ten months while its callers fell through it in silence."""
    search_date = ">=" + lmdutils.get_date_str(buildid)
    buildid = utils.get_buildid(buildid)
    logger.info("Get changeset for {}-{}-{}.".format(buildid, product, channel))

    def handler(json, data):
        pat = re.compile(r"^.*:([0-9a-f]+)$")
        if not json["facets"]["build_id"]:
            return
        for facets in json["facets"]["build_id"]:
            for tf in facets["facets"]["topmost_filenames"]:
                m = pat.match(tf["term"])
                if m:
                    chgset = m.group(1)
                    count = tf["count"]
                    data[chgset] += count

    params = {
        "product": product,
        # `get_search_channel`, like the four queries above it in this module: a beta build
        # and its DevEdition twin share a buildid (58 of 59 since 2026-04-01, identical
        # revisions), so the raw label throws away a third of the reports this vote counts.
        "release_channel": utils.get_search_channel(channel),
        "build_id": buildid,
        "date": search_date,
        "topmost_filenames": _GIT_TOPMOST_FILENAMES,
        "_aggs.build_id": "topmost_filenames",
        "_results_number": 0,
        "_facets": "product",
        "_facets_size": 100,
    }

    data = defaultdict(lambda: 0)
    socorro.SuperSearch(params=params, handler=handler, handlerdata=data).wait()
    chgset = None
    if data:
        sha, _ = max(data.items(), key=lambda p: p[1])
        # Imported here: `tools` imports this module and `inspector` imports `tools`.
        from . import inspector

        hg_rev = inspector.git2hg(sha)
        chgset = utils.short_rev(hg_rev) if hg_rev else None

    logger.info("Get changeset: finished ({}).".format(chgset))

    return chgset
