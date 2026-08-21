"""Are the three would-be-NEW-foreign signatures reachable by the pipeline at all?"""
import os as _os  # repo-relative paths: this script moved out of /tmp into the repo
_HERE = _os.path.dirname(_os.path.abspath(__file__)) + "/"
_REPO = _os.path.dirname(_os.path.dirname(_HERE.rstrip("/")))
import json, os, sys, urllib.request, urllib.parse
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.chdir(_REPO); sys.path.insert(0, ".")
from crashclouseau import inspector
UA = {"User-Agent": "crash-clouseau"}
for sig in ("EMPTY: no frame data available; EmptyMinidump",
            "EMPTY: no frame data available; OK",
            "EMPTY: no frame data available; HeaderMismatch"):
    p = [("product","Firefox"),("release_channel","nightly"),("signature","=%s"%sig),
         ("date",">=2026-08-07"),("_results_number",3),("_columns","uuid")]
    url = "https://crash-stats.mozilla.org/api/SuperSearch/?" + urllib.parse.urlencode(p, doseq=True)
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90) as r:
        hits = json.load(r)["hits"]
    print("==", sig, "n=%d sampled" % len(hits))
    for h in hits[:2]:
        u = h["uuid"]
        url2 = "https://crash-stats.mozilla.org/api/ProcessedCrash/?" + urllib.parse.urlencode({"crash_id": u})
        try:
            with urllib.request.urlopen(urllib.request.Request(url2, headers=UA), timeout=90) as r:
                d = json.load(r)
        except Exception as e:
            print("   ", u, "fetch failed", e); continue
        dump = d.get("json_dump") or {}
        threads = dump.get("threads") or []
        ct = inspector.thread_for_analysis(d)
        frames = threads[ct]["frames"] if (ct is not None and ct < len(threads)) else []
        print("   %s threads=%d thread_for_analysis=%s frames=%d" % (u, len(threads), ct, len(frames)))
