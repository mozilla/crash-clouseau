"""(a) diff: the two published shapes over the SAME 329 pairs.
  sel_18_protos.py  : date='>=2026-07-01'   (fixed 61-day floor, no as-of bound)
  v19_protos.py     : NO `date` key         (SuperSearch silently substitutes ~7 days,
                                             relative to NOW -- the shape has no as-of anchor)
"""
import functools, json, os
from libmozdata import socorro
from libmozdata.connection import Query
from crashclouseau import config, utils

CAP = config.get_threshold("protos", "Firefox", "release")
OUT = "spike/_release_post/wrong329.json"
res = json.load(open(OUT)) if os.path.exists(OUT) else {}
pairs = json.load(open("spike/_release_post/pairs329.json"))

def h(k, j, d):
    d[k] = {"error": j["errors"]} if j["errors"] else \
        {"card": j["facets"]["cardinality_proto_signature"]["value"], "reports": j["total"]}

jobs = []
for p in pairs:
    for tag, date in (("d61", ">=2026-07-01"), ("d7", None)):
        k = "%s\t%s\t%s\t%s" % (tag, p["sig"], p["bid"], p["day"])
        if k not in res:
            jobs.append((p, date, k))
print("todo", len(jobs), flush=True)
B = 12
for i in range(0, len(jobs), B):
    qs = []
    for p, date, k in jobs[i:i+B]:
        params = {"product": "Firefox", "release_channel": utils.get_search_channel("release"),
                  "build_id": p["bid"], "signature": "=" + p["sig"], "_results_number": 0,
                  "_facets": "_cardinality.proto_signature", "_facets_size": CAP}
        if date is not None:
            params["date"] = date
        qs.append(Query(socorro.SuperSearch.URL, params=params,
                        handler=functools.partial(h, k), handlerdata=res))
    socorro.SuperSearch(queries=qs).wait()
    if (i//B) % 10 == 0 or i+B >= len(jobs):
        json.dump(res, open(OUT, "w"))
json.dump(res, open(OUT, "w"))
print("done", len(res), "errors", sum(1 for v in res.values() if "error" in v))
