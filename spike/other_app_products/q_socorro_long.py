import os as _os  # repo-relative paths: this script moved out of /tmp into the repo
_HERE = _os.path.dirname(_os.path.abspath(__file__)) + "/"
_REPO = _os.path.dirname(_os.path.dirname(_HERE.rstrip("/")))
import json, urllib.request, urllib.parse, datetime
UA = {"User-Agent": "crash-clouseau"}
BASE = "https://crash-stats.mozilla.org/api/SuperSearch/"
def get(params):
    url = BASE + "?" + urllib.parse.urlencode(params, doseq=True)
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=180) as r:
        return json.load(r)
today = datetime.date.today()
out = {}
for days in (30, 90, 180, 365):
    start = today - datetime.timedelta(days=days)
    p = [("date", ">=%s" % start.isoformat()), ("date", "<%s" % today.isoformat()),
         ("_results_number", 0), ("_facets", "product"), ("_facets_size", 500)]
    d = get(p)
    out[days] = {"window_start": start.isoformat(), "total": d["total"],
                 "products": {x["term"]: x["count"] for x in d["facets"]["product"]}}
    print(days, out[days]["total"], out[days]["products"])
json.dump(out, open("socorro_products_long.json","w"), indent=1)
