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
from . import config, models, utils
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
    build-day for ``models.Selection``, so a declined signature leaves a trace."""

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
    selection = []

    for sgn, numbers in data.items():
        # The drop leaves a trace, at this table's own grain: one row per signature that
        # actually crashed on the dropped day. A no-user build carries 1-17 reports, so this
        # is a handful of rows, and "why was signature X not selected on the 13th" now has an
        # answer inside the system instead of needing Socorro rebuilt by hand.
        selection.extend(
            dict(rec, signature=sgn)
            for rec in dropped_day_records(numbers, dead_days)
        )
        if dead_days:
            numbers = {d: v for d, v in numbers.items() if d not in dead_days}
            if not numbers:
                continue
        bids, big, records = utils.evaluate_days(
            numbers,
            shift,
            threshold,
            floor,
            ratio,
            today=date,
            mature_after=mature_after,
            mature_installs=mature_installs,
        )
        # Keep every decision that is not a plain "nothing happened", plus the loud days
        # that did not spike -- "we had N crashes and you did nothing" is a question the
        # pipeline could not answer before. Everything quieter is dropped: it is the
        # overwhelming majority and it carries no signal.
        selection.extend(
            dict(rec, signature=sgn)
            for rec in records
            if rec["outcome"] != utils.NOT_SPIKING or rec["count"] >= floor
        )
        if bids:
            d = {
                "bids": bids,
                "protos": {b: [] for b in bids},
                "installs": {b: 0 for b in bids},
            }
            if big:
                big_data[sgn] = d
            else:
                small_data[sgn] = d
        else:
            data[sgn] = None

    del data

    logger.info("Get crash numbers for {}-{}: finished.".format(product, channel))
    if big_data:
        get_proto_big(product, big_data, search_date, channel)

    if small_data:
        get_proto_small(product, small_data, search_date, channel)

    small_data.update(big_data)
    data = small_data

    if product == "Fennec":
        # Java crashes don't have any proto-signature...
        get_uuids_fennec(data, search_date, channel)

    return data, selection


def get_proto_small(product, signatures, search_date, channel):
    """Get the proto-signatures for signature with a small number of crashes.
    Since we 'must' aggregate uuid on proto-signatures, to be faster we query
    several signatures: it's possible because we know that card(proto) <= card(crashes)
    for a given signature."""
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

    logger.info(
        "Get proto-signatures (big) for {}-{}: finished.".format(product, channel)
    )


def get_uuids_fennec(signatures, search_date, channel):
    """Get the uuids for Fennec java crashes"""
    logger.info("Get uuids for Fennec-{}: started.".format(channel))

    def handler(json, data):
        if json["errors"] or not json["facets"]["signature"]:
            return
        bid = json["facets"]["build_id"][0]["term"]
        bid = utils.get_build_date(bid)
        for facets in json["facets"]["signature"]:
            sgn = facets["term"]
            count = facets["count"]
            facets = facets["facets"]
            uuid = facets["uuid"][0]["term"]
            protos = data[sgn]["protos"][bid]
            if not protos:
                protos.append({"proto": "", "count": count, "uuid": uuid})

    base_params = {
        "product": "Fennec",
        "release_channel": utils.get_search_channel(channel),
        "date": search_date,
        "build_id": "",
        "signature": "",
        "_aggs.signature": "uuid",
        "_results_number": 0,
        "_facets": "build_id",
        "_facets_size": 100,
    }

    queries = []
    sgns_by_bids = utils.get_sgns_by_bids(signatures)

    for bid, all_signatures in sgns_by_bids.items():
        params = copy.deepcopy(base_params)
        params["build_id"] = utils.get_buildid(bid)

        for sgns in Connection.chunks(all_signatures, 10):
            params = copy.deepcopy(params)
            params["signature"] = ["=" + s for s in sgns]
            queries.append(
                Query(
                    socorro.SuperSearch.URL,
                    params=params,
                    handler=handler,
                    handlerdata=signatures,
                )
            )
    socorro.SuperSearch(queries=queries).wait()

    logger.info("Get uuids for Fennec-{}: finished.".format(channel))


def get_changeset(buildid, channel, product):
    """Trick to get changeset for a particular buildid/channel/product"""
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
        "topmost_filenames": '@"hg:hg.mozilla.org/".*:[0-9a-f]+',
        "_aggs.build_id": "topmost_filenames",
        "_results_number": 0,
        "_facets": "product",
        "_facets_size": 100,
    }

    data = defaultdict(lambda: 0)
    socorro.SuperSearch(params=params, handler=handler, handlerdata=data).wait()
    chgset = None
    if data:
        chgset, _ = max(data.items(), key=lambda p: p[1])
        chgset = utils.short_rev(chgset)

    logger.info("Get changeset: finished.")

    return chgset
