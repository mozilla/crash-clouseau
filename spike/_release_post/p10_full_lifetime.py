"""The TRUE per-pair lifetime union: 72 ticks x EVERY run-day the pair stays selected.
20 capped pairs (stratified by report volume, drawn from the 60 already tick-sampled).
Note `since` MOVES as builds enter Build.get_last_versions(n=3), so the date range's LOWER
bound advances and a proto's count can DROP -- another source of top-N churn.
"""
import functools, json, os, datetime as dt
from libmozdata import socorro
from libmozdata.connection import Query
from crashclouseau import config, utils

CAP = config.get_threshold("protos", "Firefox", "release")
PLAN = {p[0]: (p[2], p[3]) for p in json.load(open("spike/_release_recon/plan_long.json"))}
PICK = json.load(open("spike/_release_post/pickdays.json"))
SUB = json.load(open("spike/_release_post/sub20.json"))
OUT = "spike/_release_post/full_life.json"
res = json.load(open(OUT)) if os.path.exists(OUT) else {}
# reuse the day-1 ticks already fetched
day1 = json.load(open("spike/_release_post/ticks.json"))
for k, v in day1.items():
    res.setdefault(k, v)

def handler(k, j, d):
    d[k] = {"error": j["errors"]} if j["errors"] else \
        [x["term"] for x in j["facets"].get("proto_signature", [])]

jobs = []
for p in SUB:
    for day in PICK[p["sig"] + "\t" + p["bid"]]:
        since, upto = PLAN[day]
        d0 = dt.date.fromisoformat(day)
        his = ["%sT%02d:%02d:00" % (d0.isoformat(), m//60, m % 60) for m in range(0, 24*60, 20)]
        for hi in his + [upto]:
            k = "%s\t%s\t%s\t%s" % (p["sig"], p["bid"], day, hi)
            if k not in res:
                jobs.append((p, since, hi, k))
print("queries todo", len(jobs), flush=True)
B = 24
for i in range(0, len(jobs), B):
    queries = []
    for p, since, hi, k in jobs[i:i + B]:
        queries.append(Query(socorro.SuperSearch.URL, params={
            "product": "Firefox",
            "release_channel": utils.get_search_channel("release"),
            "date": [">=" + since, "<" + hi],
            "build_id": p["bid"],
            "signature": "=" + p["sig"],
            "_aggs.proto_signature": "uuid",
            "_results_number": 0,
            "_facets": "_cardinality.proto_signature",
            "_facets_size": CAP,
        }, handler=functools.partial(handler, k), handlerdata=res))
    socorro.SuperSearch(queries=queries).wait()
    if (i // B) % 20 == 0 or i + B >= len(jobs):
        json.dump(res, open(OUT, "w"))
        print("  %d/%d" % (min(i+B, len(jobs)), len(jobs)), flush=True)
json.dump(res, open(OUT, "w"))
print("errors", sum(1 for v in res.values() if isinstance(v, dict)))
