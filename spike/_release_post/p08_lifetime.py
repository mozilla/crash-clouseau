"""(c) part 2: the MULTI-DAY lifetime union. A pair stays SELECTED for a mean of 4.45
run-days (median 5, max 11; spike/_release_post/pickdays.json, from the production selector
replayed on the cached as-of facets). On each of those days production re-issues the proto
query on every one of its 72 ticks, with a `search_date` lower bound that can MOVE (a new
build enters the 3-build window, so `Build.get_last_versions(n=3)`'s oldest changes).

Here: the end-of-day kept set for every pick-day of every sampled pair. Union across days,
then combined with the intra-day 72-tick union from p05/p06.
"""
import functools, json, os, datetime as dt
from libmozdata import socorro
from libmozdata.connection import Query
from crashclouseau import config, utils

CAP = config.get_threshold("protos", "Firefox", "release")
PLAN = {p[0]: (p[2], p[3]) for p in json.load(open("spike/_release_recon/plan_long.json"))}
PICK = json.load(open("spike/_release_post/pickdays.json"))
SAMPLE = json.load(open("spike/_release_post/sample.json"))
OUT = "spike/_release_post/lifetime.json"
res = json.load(open(OUT)) if os.path.exists(OUT) else {}

def handler(k, j, d):
    d[k] = {"error": j["errors"]} if j["errors"] else \
        [x["term"] for x in j["facets"].get("proto_signature", [])]

jobs = []
for p in SAMPLE:
    for day in PICK[p["sig"] + "\t" + p["bid"]]:
        since, upto = PLAN[day]
        k = "%s\t%s\t%s" % (p["sig"], p["bid"], day)
        if k not in res:
            jobs.append((p, day, since, upto, k))
print("queries todo", len(jobs), flush=True)
B = 24
for i in range(0, len(jobs), B):
    queries = []
    for p, day, since, upto, k in jobs[i:i + B]:
        queries.append(Query(socorro.SuperSearch.URL, params={
            "product": "Firefox",
            "release_channel": utils.get_search_channel("release"),
            "date": [">=" + since, "<" + upto],
            "build_id": p["bid"],
            "signature": "=" + p["sig"],
            "_aggs.proto_signature": "uuid",
            "_results_number": 0,
            "_facets": "_cardinality.proto_signature",
            "_facets_size": CAP,
        }, handler=functools.partial(handler, k), handlerdata=res))
    socorro.SuperSearch(queries=queries).wait()
    json.dump(res, open(OUT, "w"))
print("done", len(res), "errors", sum(1 for v in res.values() if isinstance(v, dict)))
