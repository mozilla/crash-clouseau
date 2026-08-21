"""Diff the chosen venue over the 51 filings: shipped map vs three candidate repairs."""
import os as _os  # repo-relative paths: this script moved out of /tmp into the repo
_HERE = _os.path.dirname(_os.path.abspath(__file__)) + "/"
_REPO = _os.path.dirname(_os.path.dirname(_HERE.rstrip("/")))
import json, os, sys
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.chdir(_REPO)
sys.path.insert(0, _REPO)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
from crashclouseau import bugzilla_apply, config, sigage
D = _HERE
rows = json.load(open(D+"venue_candidates.json"))

SHIPPED = {"Thunderbird": ["Thunderbird", "MailNews Core", "Calendar", "Chat Core"],
           "SeaMonkey": ["SeaMonkey"]}
# A: shipped + every other Socorro-reporting application's BMO-exclusive products
A = dict(SHIPPED, **{"Focus": ["Focus"],
                     "Fenix": ["Firefox for Android", "GeckoView"],
                     "MozillaVPN": ["Mozilla VPN"],
                     "Firefox": ["Firefox"]})
# B: A minus GeckoView (GeckoView is the embedding API, arguably shared)
B = dict(A, Fenix=["Firefox for Android"])
# C: the naive "Gecko-app-ness without a Firefox-family notion" map the brief calls the trap:
#    every BMO product that is an APPLICATION (Client Software classification) is foreign.
prods = json.load(open(D+"bmo_products.json"))
C = {"__client_software__": [p["name"] for p in prods
                             if p["classification"] == "Client Software" and p["is_active"]]}

def venue(bugs, product, mapping):
    orig = config._OTHER_APP_PRODUCTS
    config._OTHER_APP_PRODUCTS = mapping
    try:
        ours, theirs = bugzilla_apply._split_by_application(bugs, product)
    finally:
        config._OTHER_APP_PRODUCTS = orig
    return ours, theirs

def pick(ours, r):
    """_bug_for_this_regression with the panel's real pushdate as `landed`."""
    pd = (r.get("pushdate") or [None])[0]
    landed = sigage.to_datetime(pd) if pd else None
    bug_id, predating = bugzilla_apply._bug_for_this_regression(
        ours, landed, 30, candidate_bug=None)
    return bug_id

out = []
for r in rows:
    bugs = r["candidates"] or []
    res = {"bug": r["bug"], "sig": r["signature"],
           "candidates": [(b["id"], b["product"]) for b in bugs]}
    for name, m in (("shipped", SHIPPED), ("A", A), ("B", B), ("C", C)):
        ours, theirs = venue(bugs, r["product"], m)
        res[name] = {"venue": pick(ours, r) if ours else None,
                     "other_app": [b["id"] for b in theirs]}
    out.append(res)

json.dump(out, open(D+"venue_diff.json","w"), indent=1)
for name in ("A","B","C"):
    moved = [r for r in out if r[name]["venue"] != r["shipped"]["venue"]]
    print("map %s: venue changed on %d/51" % (name, len(moved)))
    for r in moved:
        print("   bug %s  %s -> %s   cands=%s" % (r["bug"], r["shipped"]["venue"],
                                                  r[name]["venue"], r["candidates"]))
print()
print("shipped: filings with a foreign candidate:",
      [(r["bug"], r["shipped"]["other_app"], r["candidates"]) for r in out if r["shipped"]["other_app"]])
print("shipped: filings that COMMENT (venue found):",
      sum(1 for r in out if r["shipped"]["venue"]))
