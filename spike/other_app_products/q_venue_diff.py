"""Re-run the SHIPPED venue chain over the 51 filings and diff the chosen venue
under the current _OTHER_APP_PRODUCTS map vs candidate corrected maps."""
import os as _os  # repo-relative paths: this script moved out of /tmp into the repo
_HERE = _os.path.dirname(_os.path.abspath(__file__)) + "/"
_REPO = _os.path.dirname(_os.path.dirname(_HERE.rstrip("/")))
import json, os, sys, datetime
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
sys.path.insert(0, _REPO)
from crashclouseau import bugzilla_apply, config

panel = json.load(open((_HERE + "filings_enriched.json")))
crashes = json.load(open(_HERE + "crash_products.json"))

# The shipped map, for reference.
CURRENT = dict(config._OTHER_APP_PRODUCTS)
print("shipped map:", json.dumps(CURRENT))
print("foreign(Firefox) today:", sorted(config.get_other_app_products("Firefox")))

rows = []
for x in panel:
    bug = x["bug"]
    c = crashes[str(bug)]
    sig = c["signature"]
    bugs = bugzilla_apply._open_bugs_for_signature(sig)
    rows.append({"bug": bug, "uuid": x["uuid"], "product": c["product"],
                 "signature": sig, "candidates": bugs,
                 "node": x.get("node"), "pushdate": x.get("pushdate"),
                 "created": x.get("created")})
    print("%s  %-70s  %s" % (bug, sig[:70],
          [(b["id"], b["product"]) for b in (bugs or [])]))
json.dump(rows, open(_HERE + "venue_candidates.json","w"), indent=1)
