import os as _os  # repo-relative paths: this script moved out of /tmp into the repo
_HERE = _os.path.dirname(_os.path.abspath(__file__)) + "/"
_REPO = _os.path.dirname(_os.path.dirname(_HERE.rstrip("/")))
import json, urllib.request, urllib.parse, datetime, sys

UA = {"User-Agent": "crash-clouseau"}
BASE = "https://crash-stats.mozilla.org/api/SuperSearch/"

def get(params):
    url = BASE + "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)

today = datetime.date.today()
start = today - datetime.timedelta(days=7)
p = [("date", ">=%s" % start.isoformat()), ("date", "<%s" % today.isoformat()),
     ("_results_number", 0), ("_facets", "product"), ("_facets_size", 200)]
d = get(p)
out = {"window": [start.isoformat(), today.isoformat()], "total": d["total"],
       "product_facet": d["facets"]["product"]}
print(json.dumps(out, indent=1))

# per-product x channel
res = {}
for f in d["facets"]["product"]:
    prod = f["term"]
    pp = [("date", ">=%s" % start.isoformat()), ("date", "<%s" % today.isoformat()),
          ("product", prod),
          ("_results_number", 0), ("_facets", "release_channel"), ("_facets_size", 50)]
    dd = get(pp)
    res[prod] = {"total": dd["total"],
                 "channels": {x["term"]: x["count"] for x in dd["facets"]["release_channel"]}}
out["by_product_channel"] = res
json.dump(out, open("socorro_products.json", "w"), indent=1)
print(json.dumps(res, indent=1))
