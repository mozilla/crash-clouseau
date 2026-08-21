"""Prototype of the shippable repair, run against LIVE data (not committed to the repo).

(1) an audit that turns the docstring's completeness CLAIM into a check;
(2) the agent-facing prose GENERATED from the same map.
"""
import os as _os  # repo-relative paths: this script moved out of /tmp into the repo
_HERE = _os.path.dirname(_os.path.abspath(__file__)) + "/"
_REPO = _os.path.dirname(_os.path.dirname(_HERE.rstrip("/")))
import json, os, sys, urllib.request, urllib.parse
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.chdir(_REPO); sys.path.insert(0, ".")
from crashclouseau import config
UA = {"User-Agent": "crash-clouseau"}

def bmo_products():
    url = ("https://bugzilla.mozilla.org/rest/product?type=accessible&"
           + urllib.parse.urlencode({"include_fields": "name,is_active"}))
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
        return {p["name"]: p["is_active"] for p in json.load(r)["products"]}

def socorro_products(days=30):
    import datetime
    t = datetime.date.today(); s = t - datetime.timedelta(days=days)
    p = [("date", ">=%s" % s), ("date", "<%s" % t), ("_results_number", 0),
         ("_facets", "product"), ("_facets_size", 500)]
    url = "https://crash-stats.mozilla.org/api/SuperSearch/?" + urllib.parse.urlencode(p, doseq=True)
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=180) as r:
        return {x["term"]: x["count"] for x in json.load(r)["facets"]["product"]}

bmo = bmo_products()
soc = socorro_products()
m = config._OTHER_APP_PRODUCTS

print("--- CHECK 1: every BMO product name in the map exists on BMO and is active")
bad = []
for app, prods in m.items():
    for p in prods:
        if p not in bmo:
            bad.append((app, p, "NOT A BMO PRODUCT"))
        elif not bmo[p]:
            bad.append((app, p, "inactive"))
print("   violations:", bad or "none")
print("   (the same check applied to a hypothetical `Fenix: [\"Fenix\"]` entry:",
      "'Fenix' in bmo ->", "Fenix" in bmo, ")")

print()
print("--- CHECK 2: every Socorro-reporting application other than ours is a KEY in the map")
ours = set(config.get_products()) if hasattr(config, "get_products") else {"Firefox"}
print("   config products:", sorted(ours))
missing = sorted(set(soc) - set(m) - ours)
print("   Socorro products (30d):", {k: v for k, v in soc.items()})
print("   reporting applications with NO map entry:", missing)
dead = sorted(set(m) - set(soc))
print("   map keys that report NOTHING to Socorro in 30d:", dead)

print()
print("--- CHECK 3: the agent prose, generated")
def describe(product=None):
    apps = sorted(a for a in m if a != product)
    bits = []
    for a in apps:
        ps = m[a]
        bits.append("%s (%s)" % (a, ", ".join("``%s``" % x for x in ps))
                    if ps != [a] else "%s" % a)
    return " and ".join([", ".join(bits[:-1]), bits[-1]]) if len(bits) > 1 else bits[0]
print("   ", describe("Firefox"))
print()
print("   full sentence:")
print("   every application built on mozilla-central ({}) shares Gecko's crash signatures"
      .format(describe("Firefox")))
