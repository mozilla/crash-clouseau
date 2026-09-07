# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import asyncio
from copy import deepcopy
from functools import partial
import json
from . import config
from . import net
import six
import time
from . import utils
from .logger import logger


URL = "https://buildhub.moz.tools/api/search"
PRODS = {"Firefox": "firefox", "FennecAndroid": "fennec", "Thunderbird": "thunderbird"}
RPRODS = {v: k for k, v in PRODS.items()}

# regexp matching the correct version formats for elastic search query
VERSION_PATS = {
    "nightly": r'[0-9]+".0a1"',
    "beta": r'[0-9]+".0b"[0-9]+',
    "release": r"[0-9]+\.[0-9]+(\.[0-9]+)?",
}


def target_channel(channel):
    """Buildhub's ``target.channel`` for one of OUR labels. Every ESR line -- ``esr115``,
    ``esr140``, ``esr153`` -- is ``esr`` there (and in Socorro); the line is told apart by
    ``version_pat``."""
    return "esr" if config.channel_family(channel) == "esr" else channel


def version_pat(channel):
    """The ``target.version`` regexp that keeps a channel's Buildhub rows to ITS builds.

    Load-bearing off nightly: Buildhub files 26-30 RC/dot-release builds under
    ``target.channel=beta`` that only this pattern keeps out of beta's ``builds`` table (see
    ``models.Build.get_last_versions``), and it files every ESR line under one channel. For an
    ESR line the pattern is the line's own major -- ``140\\.[0-9]+(\\.[0-9]+)?esr`` matches
    140.15.0esr and nothing from esr115 or esr153, verified live 2026-09-07 -- so each line's
    ``builds`` rows are one lineage and ``get_two_last`` / ``get_pushdate_before`` never pair a
    153 build with a 140 one. Release's pattern matches NO esr build (the ``esr`` suffix), which
    is why the family needs its own. Unknown channel: everything, as before."""
    major = config.esr_major(channel)
    if major is not None:
        return r"{}\.[0-9]+(\.[0-9]+)?esr".format(major)
    if config.channel_family(channel) == "esr":
        return r"[0-9]+\.[0-9]+(\.[0-9]+)?esr"
    return VERSION_PATS.get(channel, "*")


def make_request(params, sleep, retry, callback):
    """Query Buildhub"""
    params = json.dumps(params)

    for _ in range(retry):
        r = net.post(URL, data=params)
        if "Backoff" in r.headers:
            time.sleep(sleep)
        else:
            try:
                return callback(r.json())
            except BaseException as e:
                logger.error(
                    "Buildhub query failed with parameters: {}.".format(params)
                )
                logger.error(e, exc_info=True)
                return None

    logger.error("Too many attempts in buildhub.make_request (retry={})".format(retry))

    return None


def get(
    min_buildid, channel, prods=["firefox", "fennec", "thunderbird"], max_buildid=None
):
    """Get all builds info for buildids >= min_build"""
    if isinstance(prods, six.string_types):
        prods = [prods]
    prods = [PRODS.get(x, x) for x in prods]
    r = {}
    if min_buildid:
        r["gte"] = utils.get_buildid(min_buildid)
    if max_buildid:
        r["lte"] = utils.get_buildid(max_buildid)

    data = {
        "aggs": {
            "products": {
                "terms": {"field": "source.product", "size": len(prods)},
                "aggs": {
                    "channels": {
                        "terms": {"field": "target.channel", "size": 1},
                        "aggs": {
                            "buildids": {
                                "terms": {"field": "build.id", "size": 1000},
                                "aggs": {
                                    "revisions": {
                                        "terms": {"field": "source.revision", "size": 1}
                                    },
                                    "versions": {
                                        "terms": {"field": "target.version", "size": 1}
                                    },
                                },
                            }
                        },
                    }
                },
            }
        },
        "query": {
            "bool": {
                "filter": [
                    {"term": {"target.channel": target_channel(channel)}},
                    {"terms": {"source.product": prods}},
                    {"range": {"build.id": r}},
                    {"regexp": {"target.version": version_pat(channel)}},
                ]
            }
        },
        "size": 0,
    }

    def get_info(data):
        res = {}
        aggs = data["aggregations"]
        for product in aggs["products"]["buckets"]:
            prod = product["key"]
            prod = RPRODS.get(prod, prod)
            if prod in res:
                res_p = res[prod]
            else:
                res[prod] = res_p = {}
            for bucket in product["channels"]["buckets"]:
                # Keyed by OUR label, not by Buildhub's `target.channel`: the query filtered on
                # one channel, so this bucket IS the channel asked for -- and for an ESR line
                # the two names differ (`esr140` here, `esr` there). `Build.put_data` writes
                # this key straight into `builds.channel`, which is the CHANNEL_TYPE enum.
                chan = channel
                if chan in res_p:
                    res_pc = res_p[chan]
                else:
                    res_p[chan] = res_pc = {}

                for buildid in bucket["buildids"]["buckets"]:
                    bid = utils.get_build_date(buildid["key"])
                    rev = buildid["revisions"]["buckets"][0]["key"]
                    version = buildid["versions"]["buckets"][0]["key"]
                    res_pc[bid] = {"revision": utils.short_rev(rev), "version": version}
        return res

    return make_request(data, 1, 100, get_info)


def get_rev_from(buildid, channel, product):
    """Get the revision for a given build"""
    buildid = utils.get_buildid(buildid)
    product = PRODS.get(product, product)
    data = {
        "aggs": {"revisions": {"terms": {"field": "source.revision", "size": 1}}},
        "query": {
            "bool": {
                "filter": [
                    {"term": {"target.channel": target_channel(channel)}},
                    {"term": {"source.product": product}},
                    {"term": {"build.id": buildid}},
                ]
            }
        },
        "size": 0,
    }

    def cb(data):
        return utils.short_rev(data["aggregations"]["revisions"]["buckets"][0]["key"])

    return make_request(data, 0.1, 100, cb)


def get_two_last(buildid, channel, product):
    """Get the two last build (including the one from buildid)"""
    buildid = utils.get_buildid(buildid)
    product = PRODS.get(product, product)
    data = {
        "aggs": {
            "buildids": {
                "terms": {"field": "build.id", "size": 2, "order": {"_term": "desc"}},
                "aggs": {
                    "revisions": {"terms": {"field": "source.revision", "size": 1}},
                    "versions": {"terms": {"field": "target.version", "size": 1}},
                },
            }
        },
        "query": {
            "bool": {
                "filter": [
                    {"term": {"target.channel": target_channel(channel)}},
                    {"term": {"source.product": product}},
                    {"range": {"build.id": {"lte": buildid}}},
                    {"regexp": {"target.version": version_pat(channel)}},
                ]
            }
        },
        "size": 0,
    }

    def get_info(data):
        bids = []
        for i in data["aggregations"]["buildids"]["buckets"]:
            bid = i["key"]
            revision = utils.short_rev(i["revisions"]["buckets"][0]["key"])
            version = i["versions"]["buckets"][0]["key"]
            bids.append({"buildid": bid, "revision": revision, "version": version})

        if bids[0]["buildid"] != buildid:
            return None

        x = bids[0]
        bids[0] = bids[1]
        bids[1] = x

        return bids

    return make_request(data, 0.1, 100, get_info)


async def get_enclosing_builds_helper(pushdate, channel, product):
    # TODO: we must handle the case where the timezone of buildid was not utc
    # check with jlorenzo when the changed has been made
    buildid = utils.get_buildid(pushdate)
    product = PRODS.get(product, product)
    lt_data = {
        "aggs": {
            "buildids": {
                "terms": {"field": "build.id", "size": 1, "order": {"_term": "desc"}},
                "aggs": {
                    "revisions": {"terms": {"field": "source.revision", "size": 1}},
                    "versions": {"terms": {"field": "target.version", "size": 1}},
                },
            }
        },
        "query": {
            "bool": {
                "filter": [
                    {"term": {"target.channel": target_channel(channel)}},
                    {"term": {"source.product": product}},
                    {"range": {"build.id": {"lt": buildid}}},
                    {"regexp": {"target.version": version_pat(channel)}},
                ]
            }
        },
        "size": 0,
    }

    gte_data = deepcopy(lt_data)
    gte_data["aggs"]["buildids"]["terms"]["order"]["_term"] = "asc"
    gte_data["query"]["bool"]["filter"][2]["range"]["build.id"] = {"gte": buildid}
    data = [lt_data, gte_data]

    def get_info(data):
        data = data["aggregations"]["buildids"]["buckets"]
        if len(data) == 0:
            return None
        data = data[0]
        bid = data["key"]
        revision = utils.short_rev(data["revisions"]["buckets"][0]["key"])
        version = data["versions"]["buckets"][0]["key"]
        return {"buildid": bid, "revision": revision, "version": version}

    loop = asyncio.get_running_loop()
    fs = []
    for d in data:
        fs.append(
            loop.run_in_executor(None, partial(make_request, d, 0.1, 100, get_info))
        )
    res = []
    for f in fs:
        res.append(await f)

    return res


def get_enclosing_builds(pushdate, channel, product):
    """Get the build before and the one after the given pushdate"""
    return asyncio.run(get_enclosing_builds_helper(pushdate, channel, product))
