"""(a)+(b): the PRODUCTION proto query shape, replayed over the FULL 329-pair set at the
DEPLOYED cadence (each pair as-of the END of the run-day it was first selected on).

Shape (verified against crashclouseau/datacollector.py get_proto_big/get_proto_small):
    product            = Firefox
    release_channel    = utils.get_search_channel("release")            -> "release"
    date               = [">=" + oldest build-DAY of the 3-build window,   <-- get_builds()
                          "<"  + run-day + 1]                              <-- "as of the run-day"
    build_id           = the selected buildid
    signature          = "=" + signature
    _aggs.proto_signature = "uuid"
    _facets            = _cardinality.proto_signature (+install_time)
    _facets_size       = config.get_threshold("protos","Firefox","release") = 20
Production keeps min(distinct protos, 20) either way: get_proto_big asks Socorro for 20
facets; get_proto_small asks for config.facets_limit=10000 and truncates in its handler at
`len(protos) < threshold`. Same kept count, same count-ordering.
"""
import json, os, sys, time
from libmozdata import socorro
from libmozdata.connection import Query
from crashclouseau import config, utils

CAP = config.get_threshold("protos", "Firefox", "release")
OUT = "spike/_release_post/prod329.json"
pairs = json.load(open("spike/_release_post/pairs329.json"))
res = json.load(open(OUT)) if os.path.exists(OUT) else {}

def key(p):
    return p["sig"] + "\t" + p["bid"] + "\t" + p["day"]

def handler(k, j, d):
    if j["errors"]:
        d[k] = {"error": j["errors"]}
        return
    f = j["facets"]
    d[k] = {
        "reports": j["total"],
        "card": f["cardinality_proto_signature"]["value"],
        "installs": f["cardinality_install_time"]["value"],
        "kept": len(f.get("proto_signature", [])),
        "protos": [[x["term"], x["count"]] for x in f.get("proto_signature", [])],
    }

todo = [p for p in pairs if key(p) not in res]
print("pairs %d, cached %d, todo %d" % (len(pairs), len(res), len(todo)), flush=True)
B = 12
for i in range(0, len(todo), B):
    batch = todo[i:i + B]
    queries = []
    for p in batch:
        import functools
        queries.append(Query(socorro.SuperSearch.URL, params={
            "product": "Firefox",
            "release_channel": utils.get_search_channel("release"),
            "date": [">=" + p["since"], "<" + p["upto"]],
            "build_id": p["bid"],
            "signature": "=" + p["sig"],
            "_aggs.proto_signature": "uuid",
            "_results_number": 0,
            "_facets": ["_cardinality.proto_signature", "_cardinality.install_time"],
            "_facets_size": CAP,
        }, handler=functools.partial(handler, key(p)), handlerdata=res))
    socorro.SuperSearch(queries=queries).wait()
    json.dump(res, open(OUT, "w"))
    print("  %d/%d done" % (min(i + B, len(todo)), len(todo)), flush=True)
print("TOTAL cached:", len(res))
print("errors:", sum(1 for v in res.values() if "error" in v))
