"""(c) The per-tick UNION. Production's clock runs `update_all()` every 20 MINUTES
(bin/schedule.py `@scheduled(minutes=20)`) = 72 ticks/day, and `models.UUID.add` dedups on
(signatureid, protohash, buildid) -- verified crashclouseau/models.py:1496-1523 -- so a proto
that enters the COUNT-ORDERED top-`thresholds.protos` on a later tick is a NEW ROW.

For each sampled pair, re-issue the EXACT production proto query 72 times, once per 20-minute
tick of its first-pick run-day, changing only the implicit upper bound (`< tick`), and take the
union of the kept term sets.  Read-only.
"""
import functools, json, os, sys, datetime as dt
from libmozdata import socorro
from libmozdata.connection import Query
from crashclouseau import config, utils

CAP = config.get_threshold("protos", "Firefox", "release")
OUT = os.getenv("OUT", "spike/_release_post/ticks.json")
res = json.load(open(OUT)) if os.path.exists(OUT) else {}
SAMPLE = json.load(open(os.getenv("SAMPLE", "spike/_release_post/sample.json")))

def handler(k, j, d):
    if j["errors"]:
        d[k] = {"error": j["errors"]}
        return
    d[k] = [x["term"] for x in j["facets"].get("proto_signature", [])]

def qkey(p, hi):
    return "%s\t%s\t%s\t%s" % (p["sig"], p["bid"], p["day"], hi)

jobs = []
for p in SAMPLE:
    day = dt.date.fromisoformat(p["day"])
    for m in range(0, 24 * 60, 20):          # 72 ticks
        hi = "%sT%02d:%02d:00" % (day.isoformat(), m // 60, m % 60)
        jobs.append((p, hi))
    jobs.append((p, p["upto"]))              # end-of-day = the (b) anchor
jobs = [(p, hi) for p, hi in jobs if qkey(p, hi) not in res]
print("sample %d pairs, %d queries todo" % (len(SAMPLE), len(jobs)), flush=True)
B = 24
for i in range(0, len(jobs), B):
    queries = []
    for p, hi in jobs[i:i + B]:
        queries.append(Query(socorro.SuperSearch.URL, params={
            "product": "Firefox",
            "release_channel": utils.get_search_channel("release"),
            "date": [">=" + p["since"], "<" + hi],
            "build_id": p["bid"],
            "signature": "=" + p["sig"],
            "_aggs.proto_signature": "uuid",
            "_results_number": 0,
            "_facets": "_cardinality.proto_signature",
            "_facets_size": CAP,
        }, handler=functools.partial(handler, qkey(p, hi)), handlerdata=res))
    socorro.SuperSearch(queries=queries).wait()
    if (i // B) % 10 == 0 or i + B >= len(jobs):
        json.dump(res, open(OUT, "w"))
        print("  %d/%d" % (min(i + B, len(jobs)), len(jobs)), flush=True)
json.dump(res, open(OUT, "w"))
print("errors:", sum(1 for v in res.values() if isinstance(v, dict) and "error" in v))
