"""How many BMO bugs carry a cf_crash_signature, by product? Open-only and all-time.

This is the denominator for 'could a bug in product X ever be a venue candidate for a
signature search'."""
import os as _os  # repo-relative paths: this script moved out of /tmp into the repo
_HERE = _os.path.dirname(_os.path.abspath(__file__)) + "/"
_REPO = _os.path.dirname(_os.path.dirname(_HERE.rstrip("/")))
import json, urllib.request, urllib.parse, time
UA = {"User-Agent": "crash-clouseau"}
prods = json.load(open("bmo_products.json"))
names = [p["name"] for p in prods]
def count(params):
    url = "https://bugzilla.mozilla.org/rest/bug?" + urllib.parse.urlencode(params, doseq=True)
    for _ in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
                return json.load(r)["bug_count"]
        except Exception as e:
            print("retry", e); time.sleep(5)
    return None
res = {}
for n in names:
    allc = count([("product", n), ("f1","cf_crash_signature"),("o1","isnotempty"),
                  ("count_only","1")])
    if not allc:
        continue
    openc = count([("product", n), ("f1","cf_crash_signature"),("o1","isnotempty"),
                   ("resolution","---"),("count_only","1")])
    res[n] = {"all": allc, "open": openc,
              "is_active": next(p["is_active"] for p in prods if p["name"]==n)}
    print("%-45s all=%-7s open=%-6s active=%s" % (n, allc, openc, res[n]["is_active"]))
json.dump(res, open("bmo_sig_census.json","w"), indent=1)
