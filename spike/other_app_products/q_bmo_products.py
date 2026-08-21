import os as _os  # repo-relative paths: this script moved out of /tmp into the repo
_HERE = _os.path.dirname(_os.path.abspath(__file__)) + "/"
_REPO = _os.path.dirname(_os.path.dirname(_HERE.rstrip("/")))
import json, urllib.request, urllib.parse
UA = {"User-Agent": "crash-clouseau"}
url = ("https://bugzilla.mozilla.org/rest/product?type=accessible&"
       + urllib.parse.urlencode({"include_fields": "name,is_active,classification,description"}))
with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=180) as r:
    d = json.load(r)
prods = d["products"]
json.dump(prods, open("bmo_products.json","w"), indent=1)
print("total products:", len(prods), "active:", sum(1 for p in prods if p.get("is_active")))
cands = ["Thunderbird","MailNews Core","Calendar","Chat Core","SeaMonkey","Focus","Fenix",
         "GeckoView","Core","Firefox","Toolkit","DevTools","WebExtensions","NSS",
         "External Software Affecting Firefox","Firefox for Android","Firefox for iOS",
         "Mozilla VPN","Reference Browser","Android Background Services","Core Graveyard",
         "Firefox for Android Graveyard","Thunderbird Graveyard","Focus-iOS","Focus-Android"]
by = {p["name"]: p for p in prods}
for c in cands:
    p = by.get(c)
    print("%-40s %s" % (c, ("is_active=%s  class=%s" % (p["is_active"], p["classification"])) if p else "NOT FOUND / not accessible"))
print()
print("--- all active products, by classification ---")
from collections import defaultdict
g = defaultdict(list)
for p in prods:
    if p.get("is_active"):
        g[p["classification"]].append(p["name"])
for k in sorted(g):
    print("%s (%d): %s" % (k, len(g[k]), ", ".join(sorted(g[k]))))
