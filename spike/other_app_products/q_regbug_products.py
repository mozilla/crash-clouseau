"""The map's SECOND consumer: report_bug.resolve_product_component inherits the REGRESSOR
bug's product::component. Which products do the 51 filings' regressor bugs live in?"""
import os as _os  # repo-relative paths: this script moved out of /tmp into the repo
_HERE = _os.path.dirname(_os.path.abspath(__file__)) + "/"
_REPO = _os.path.dirname(_os.path.dirname(_HERE.rstrip("/")))
import json, urllib.request, urllib.parse
UA = {"User-Agent": "crash-clouseau"}
meta = json.load(open((_HERE + "filing_meta.json")))
ids = sorted({v["regbug"] for v in meta.values() if v.get("regbug")})
url = "https://bugzilla.mozilla.org/rest/bug?" + urllib.parse.urlencode(
    {"id": ",".join(ids), "include_fields": "id,product,component,summary"})
with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
    bugs = json.load(r)["bugs"]
from collections import Counter
c = Counter(b["product"] for b in bugs)
print("filings with a regressor bug:", len(ids), " resolved:", len(bugs))
print("regressor-bug products:", dict(c))
for b in bugs:
    if b["product"] not in ("Core", "Firefox", "Toolkit", "DevTools", "WebExtensions", "NSS",
                            "Testing", "External Software Affecting Firefox"):
        print("   NON-PLATFORM:", b["id"], b["product"], "::", b["component"], b["summary"][:60])
json.dump(bugs, open(_HERE + "regbug_products.json","w"), indent=1)
