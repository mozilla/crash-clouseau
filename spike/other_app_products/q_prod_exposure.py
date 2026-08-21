"""Prod exposure: over a report-weighted sample of Firefox DESKTOP nightly signatures, how
often does the SHIPPED `_open_bugs_for_signature` return a bug in a product the corrected map
would newly call foreign (Firefox for Android / GeckoView / Focus / Mozilla VPN), and how
often in one the CURRENT map already calls foreign (Thunderbird family / SeaMonkey)?"""
import os as _os  # repo-relative paths: this script moved out of /tmp into the repo
_HERE = _os.path.dirname(_os.path.abspath(__file__)) + "/"
_REPO = _os.path.dirname(_os.path.dirname(_HERE.rstrip("/")))
import json, os, sys, urllib.request, urllib.parse, time
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.chdir(_REPO); sys.path.insert(0, ".")
from crashclouseau import bugzilla_apply
D = _HERE
UA = {"User-Agent": "crash-clouseau"}
p = [("product","Firefox"), ("release_channel","nightly"),
     ("date",">=2026-08-07"), ("date","<2026-08-21"),
     ("_results_number",0), ("_facets","signature"), ("_facets_size",1000)]
url = "https://crash-stats.mozilla.org/api/SuperSearch/?" + urllib.parse.urlencode(p, doseq=True)
with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=180) as r:
    d = json.load(r)
facet = d["facets"]["signature"]
print("nightly reports in window:", d["total"], "distinct signatures faceted:", len(facet))
sigs = [f["term"] for f in facet][:300]
json.dump(facet, open(D+"nightly_sig_facet.json","w"), indent=1)

NEW_FOREIGN = {"Firefox for Android", "GeckoView", "Focus", "Mozilla VPN",
               "Firefox for iOS", "Firefox for Android Graveyard"}
OLD_FOREIGN = {"Thunderbird", "MailNews Core", "Calendar", "Chat Core", "SeaMonkey"}
out = []
for i, s in enumerate(sigs):
    try:
        bugs = bugzilla_apply._open_bugs_for_signature(s)
    except Exception as e:
        bugs = None
    out.append({"sig": s, "count": facet[i]["count"],
                "bugs": [(b["id"], b["product"]) for b in (bugs or [])],
                "failed": bugs is None})
    if i % 25 == 0:
        print(i, s[:60], out[-1]["bugs"][:4], flush=True)
    time.sleep(0.35)
json.dump(out, open(D+"prod_exposure.json","w"), indent=1)
n_new = [r for r in out if any(p in NEW_FOREIGN for _, p in r["bugs"])]
n_old = [r for r in out if any(p in OLD_FOREIGN for _, p in r["bugs"])]
n_any = [r for r in out if r["bugs"]]
print()
print("sampled signatures:", len(out), "lookup failures:", sum(1 for r in out if r['failed']))
print("with ANY open bug:", len(n_any))
print("with a CURRENT-foreign bug:", len(n_old), [r["sig"][:50] for r in n_old])
print("with a NEW-foreign bug:", len(n_new), [(r["sig"][:50], r["bugs"]) for r in n_new])
