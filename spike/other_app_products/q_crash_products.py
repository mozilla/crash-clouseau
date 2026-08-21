"""Socorro product for each of the 51 filings' crash uuids."""
import os as _os  # repo-relative paths: this script moved out of /tmp into the repo
_HERE = _os.path.dirname(_os.path.abspath(__file__)) + "/"
_REPO = _os.path.dirname(_os.path.dirname(_HERE.rstrip("/")))
import json, urllib.request, urllib.parse, concurrent.futures as cf
UA = {"User-Agent": "crash-clouseau"}
panel = json.load(open((_HERE + "filings_enriched.json")))
def one(x):
    url = "https://crash-stats.mozilla.org/api/ProcessedCrash/?" + urllib.parse.urlencode(
        {"crash_id": x["uuid"]})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
            d = json.load(r)
        return x["bug"], {"uuid": x["uuid"], "product": d.get("product"),
                          "channel": d.get("release_channel"),
                          "signature": d.get("signature"), "panel_sig": x["sig"]}
    except Exception as e:
        return x["bug"], {"uuid": x["uuid"], "error": str(e)}
out = {}
with cf.ThreadPoolExecutor(6) as ex:
    for b, v in ex.map(one, panel):
        out[b] = v
json.dump(out, open("crash_products.json","w"), indent=1)
from collections import Counter
print(Counter(v.get("product") for v in out.values()))
print(Counter(v.get("channel") for v in out.values()))
print("sig mismatch:", [b for b,v in out.items() if v.get("signature") != v.get("panel_sig")])
print("errors:", [b for b,v in out.items() if "error" in v])
