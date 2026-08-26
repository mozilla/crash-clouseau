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
    big_data = {}
    small_data = {}
    selection = []

    for sgn, numbers in data.items():
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
