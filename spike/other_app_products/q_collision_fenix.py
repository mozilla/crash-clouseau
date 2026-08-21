"""The symmetric question, for the day plans/16 lands: which open signature-bugs collide with
the FENIX (and Focus) crash populations?"""
import os as _os  # repo-relative paths: this script moved out of /tmp into the repo
_HERE = _os.path.dirname(_os.path.abspath(__file__)) + "/"
_REPO = _os.path.dirname(_os.path.dirname(_HERE.rstrip("/")))
import json, urllib.request, urllib.parse, re, concurrent.futures as cf, time
UA = {"User-Agent": "crash-clouseau"}
D = _HERE
bugs = json.load(open(D+"open_sig_bugs.json"))
def sigs_of(v):
    out = [m.group(1).strip() for m in re.finditer(r"\[@([^\]]*)\]", v or "")]
    out = [s for s in out if s]
    if not out and (v or "").strip():
        out = [v.strip()]
    return out
allsigs = {}
for p, bl in bugs.items():
    for b in bl:
        for s in sigs_of(b.get("cf_crash_signature")):
            allsigs.setdefault(s, []).append((p, b["id"]))
BASE = "https://crash-stats.mozilla.org/api/SuperSearch/"
def count(sig, product):
    p = [("product", product), ("signature","=%s" % sig), ("date",">=2026-02-21"),
         ("_results_number",0), ("_facets","release_channel"), ("_facets_size",30)]
    url = BASE + "?" + urllib.parse.urlencode(p, doseq=True)
    for _ in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
                d = json.load(r)
            return {"total": d["total"],
                    "channels": {x["term"]: x["count"] for x in d["facets"]["release_channel"]}}
        except Exception:
            time.sleep(4)
    return {"error": True}
out = {}
for product in ("Fenix", "Focus", "Thunderbird"):
    sl = list(allsigs)
    res = {}
    with cf.ThreadPoolExecutor(6) as ex:
        for s, v in zip(sl, ex.map(lambda s: count(s, product), sl)):
            res[s] = v
    out[product] = res
    print("done", product, sum(1 for v in res.values() if v.get("total")))
json.dump({"sig_owners": allsigs, "pops": out}, open(D+"collision_multi.json","w"), indent=1)

from collections import defaultdict
PRODUCTS = list(bugs)
print()
hdr = "%-38s %6s" % ("BMO product (open sig bugs)", "open")
for pp in ("Fenix","Focus","Thunderbird"):
    hdr += " %12s" % pp
print(hdr)
summary = {}
for p in PRODUCTS:
    ids = {b["id"] for b in bugs[p]}
    line = "%-38s %6d" % (p, len(ids))
    summary[p] = {"open": len(ids)}
    for pp in ("Fenix","Focus","Thunderbird"):
        hit = set()
        for s, owners in allsigs.items():
            if out[pp].get(s, {}).get("total"):
                hit |= {bid for q, bid in owners if q == p}
        line += " %12d" % len(hit)
        summary[p][pp] = sorted(hit)
    print(line)
json.dump(summary, open(D+"collision_multi_summary.json","w"), indent=1)
