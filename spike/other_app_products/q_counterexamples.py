"""The two named counter-examples, plus the Fenix-day symmetry, run through the SHIPPED
_split_by_application under each candidate map."""
import os as _os  # repo-relative paths: this script moved out of /tmp into the repo
_HERE = _os.path.dirname(_os.path.abspath(__file__)) + "/"
_REPO = _os.path.dirname(_os.path.dirname(_HERE.rstrip("/")))
import json, os, sys
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.chdir(_REPO); sys.path.insert(0, ".")
from crashclouseau import bugzilla_apply, config
D = _HERE
prods = json.load(open(D+"bmo_products.json"))

SHIPPED = {"Thunderbird": ["Thunderbird", "MailNews Core", "Calendar", "Chat Core"],
           "SeaMonkey": ["SeaMonkey"]}
A = dict(SHIPPED, Focus=["Focus"], Fenix=["Firefox for Android", "GeckoView"],
         MozillaVPN=["Mozilla VPN"], Firefox=["Firefox"])
B = dict(SHIPPED, Focus=["Focus"], Fenix=["Firefox for Android"],
         MozillaVPN=["Mozilla VPN"], Firefox=["Firefox"])
# B2 = B but with the ANDROID FAMILY collapsed (Fenix and Focus are one triage surface)
ANDROID = ["Firefox for Android", "GeckoView", "Focus"]
B2 = dict(SHIPPED, Fenix=ANDROID, Focus=ANDROID, ReferenceBrowser=ANDROID,
          MozillaVPN=["Mozilla VPN"], Firefox=["Firefox"])
C = {"__client_software__": [p["name"] for p in prods
                             if p["classification"] == "Client Software" and p["is_active"]]}
MAPS = {"shipped": SHIPPED, "A": A, "B": B, "B2(family)": B2, "C(classification)": C}

def split(bugs, product, m):
    orig = config._OTHER_APP_PRODUCTS
    config._OTHER_APP_PRODUCTS = m
    try:
        return bugzilla_apply._split_by_application(bugs, product)
    finally:
        config._OTHER_APP_PRODUCTS = orig

def b(i, p): return {"id": i, "product": p, "creation_time": "2026-01-01T00:00:00Z"}

print("%-18s | %-9s | %s" % ("map", "crash", "verdict"))
print("-"*100)
CASES = [
    ("CE1 2057980 MailNews Core must be EXCLUDED", b(2057980, "MailNews Core"), "Firefox", "excluded"),
    ("CE2 test:359 Core",        b(12345, "Core"),        "Firefox", "venue"),
    ("CE2 test:359 Firefox",     b(12345, "Firefox"),     "Firefox", "venue"),
    ("CE2 test:359 Toolkit",     b(12345, "Toolkit"),     "Firefox", "venue"),
    ("CE2 test:359 GeckoView",   b(12345, "GeckoView"),   "Firefox", "venue"),
    ("CE2 test:359 'Fenix'",     b(12345, "Fenix"),       "Firefox", "venue"),
    ("CE3 Application Services (shared, bug 2056116)", b(2056116, "Application Services"), "Firefox", "venue"),
    ("CE4 External Software (shared, 54 collide)", b(1, "External Software Affecting Firefox"), "Firefox", "venue"),
    ("FENIX-DAY Firefox::Installer 1681745 must be EXCLUDED for a Fenix crash",
     b(1681745, "Firefox"), "Fenix", "excluded"),
    ("FENIX-DAY Firefox for Android must be a VENUE for a Fenix crash",
     b(1, "Firefox for Android"), "Fenix", "venue"),
    ("FENIX-DAY Core must be a VENUE for a Fenix crash", b(1, "Core"), "Fenix", "venue"),
    ("FOCUS-DAY Firefox for Android must be a VENUE for a Focus crash",
     b(1, "Firefox for Android"), "Focus", "venue"),
]
rows = []
for label, bug, product, want in CASES:
    line = []
    for name, m in MAPS.items():
        ours, theirs = split([bug], product, m)
        got = "venue" if ours else "excluded"
        line.append("%s%s" % (got, "" if got == want else "  <-- WRONG"))
    rows.append((label, want, line))
hdr = "%-72s %-9s " % ("case", "want") + " ".join("%-22s" % k for k in MAPS)
print(hdr)
for label, want, line in rows:
    print("%-72s %-9s " % (label[:72], want) + " ".join("%-22s" % x for x in line))
