"""For each open BMO bug carrying a crash signature in a candidate 'other application'
product, does that signature ALSO occur in the Firefox DESKTOP nightly crash population?

A yes means: a desktop nightly crash on that signature reaches `_split_by_application` with
that bug as a venue candidate today."""
import os as _os  # repo-relative paths: this script moved out of /tmp into the repo
_HERE = _os.path.dirname(_os.path.abspath(__file__)) + "/"
_REPO = _os.path.dirname(_os.path.dirname(_HERE.rstrip("/")))
import json, urllib.request, urllib.parse, re, concurrent.futures as cf, time
UA = {"User-Agent": "crash-clouseau"}
D = _HERE

PRODUCTS = ["Firefox for Android", "GeckoView", "Focus", "Application Services",
            "Thunderbird", "MailNews Core", "Calendar", "Chat Core", "SeaMonkey",
            "Firefox", "Toolkit", "External Software Affecting Firefox", "WebExtensions"]

def bz(params):
    url = "https://bugzilla.mozilla.org/rest/bug?" + urllib.parse.urlencode(params, doseq=True)
    for _ in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
                return json.load(r)
        except Exception as e:
            print("bz retry", e); time.sleep(6)
    raise SystemExit("bz failed")

bugs = {}
for p in PRODUCTS:
    d = bz([("product", p), ("f1","cf_crash_signature"),("o1","isnotempty"),
            ("resolution","---"),
            ("include_fields","id,product,component,summary,cf_crash_signature,creation_time")])
    bugs[p] = d["bugs"]
    print(p, len(d["bugs"]))
json.dump(bugs, open(D+"open_sig_bugs.json","w"), indent=1)

# split cf_crash_signature into individual signatures
def sigs_of(v):
    out = []
    for m in re.finditer(r"\[@([^\]]*)\]", v or ""):
        s = m.group(1).strip()
        if s:
            out.append(s)
    if not out and (v or "").strip():
        out.append(v.strip())
    return out

allsigs = {}
for p, bl in bugs.items():
    for b in bl:
        for s in sigs_of(b.get("cf_crash_signature")):
            allsigs.setdefault(s, []).append((p, b["id"]))
print("distinct signatures:", len(allsigs))

BASE = "https://crash-stats.mozilla.org/api/SuperSearch/"
def desktop_count(sig):
    p = [("product","Firefox"), ("signature","=%s" % sig),
         ("date",">=2026-02-21"), ("_results_number",0), ("_facets","release_channel"),
         ("_facets_size",30)]
    url = BASE + "?" + urllib.parse.urlencode(p, doseq=True)
    for _ in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
                d = json.load(r)
            return {"total": d["total"],
                    "channels": {x["term"]: x["count"] for x in d["facets"]["release_channel"]}}
        except Exception as e:
            time.sleep(4)
    return {"error": True}

res = {}
sl = list(allsigs)
with cf.ThreadPoolExecutor(6) as ex:
    for s, v in zip(sl, ex.map(desktop_count, sl)):
        res[s] = v
json.dump({"sig_owners": {k: v for k, v in allsigs.items()}, "desktop": res},
          open(D+"collision.json","w"), indent=1)

from collections import defaultdict
per = defaultdict(lambda: {"bugs": set(), "colliding_bugs": set(), "nightly_bugs": set()})
for s, owners in allsigs.items():
    r = res.get(s, {})
    tot = r.get("total", 0)
    ntly = (r.get("channels") or {}).get("nightly", 0)
    for p, bid in owners:
        per[p]["bugs"].add(bid)
        if tot:
            per[p]["colliding_bugs"].add(bid)
        if ntly:
            per[p]["nightly_bugs"].add(bid)
print()
print("%-38s %6s %10s %10s" % ("product","open","desktopFx","desktop-nightly"))
for p in PRODUCTS:
    v = per[p]
    print("%-38s %6d %10d %10d" % (p, len(v["bugs"]), len(v["colliding_bugs"]),
                                   len(v["nightly_bugs"])))
json.dump({p: {k: sorted(x) for k, x in v.items()} for p, v in per.items()},
          open(D+"collision_by_product.json","w"), indent=1)
