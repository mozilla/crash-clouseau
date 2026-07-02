"""Classify Phase-0 misses: searchfox-hole vs no-call-path (signals only).

Reads a run's results + corpus and, for each case where the neighborhood did NOT
reach the regressor, prints signals to judge whether it's a searchfox call-graph
**hole** (edge should exist in-subsystem but is missing) or a genuine
**no-call-path** (generic-abort crash whose stack doesn't reflect the culprit,
cross-subsystem regressor, or a loose ``regressed_by``). Also prints the recall
summary and the verified hits.

Usage:
    python -m spike.classify_misses [--results spike/results.big.json]
                                    [--corpus  spike/corpus.big.json]
"""

from __future__ import annotations

import argparse
import json

# crash signatures whose stack usually does NOT point at the culprit
GENERIC = ("ipc::fatalerror", "oom", "moz_crash", "asyncshutdown", "shutdownhang",
           "runwatchdog", "mozalloc_abort", "| unknown", "js::gc")


def _top2(p: str) -> str:
    a = p.split("/")
    return "/".join(a[:2]) if len(a) >= 2 else p


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="classify Phase-0 misses (hole vs no-call-path)")
    ap.add_argument("--results", default="spike/results.big.json")
    ap.add_argument("--corpus", default="spike/corpus.big.json")
    args = ap.parse_args(argv)

    r = json.load(open(args.results))
    corpus = {c["clouseau_bug"]: c for c in json.load(open(args.corpus))}
    scored = [c for c in r["cases"] if not c.get("skipped")]
    regs = {c["regressor_bug"] for c in scored}
    print("scored", len(scored), "| distinct regressors", len(regs),
          "| skipped", sum(1 for c in r["cases"] if c.get("skipped")))
    print("aggregate", r["aggregate"])

    print("\n--- HITS (verified reaches) ---")
    for c in scored:
        m = c["modes"]["mechanical"]
        if m["neighborhood_hit"]:
            print(" HIT", c["clouseau_bug"],
                  "off_stack" if not c["stack_only_hit"] else "on_stack",
                  "| matched", list(m.get("matched", {}).items())[:2])

    print("\n--- MISSES (hole vs no-call-path signals) ---")
    for c in scored:
        m = c["modes"]["mechanical"]
        if m["neighborhood_hit"]:
            continue
        resolved = sum(1 for s in m["steps"] if s["returned"] > 0)
        sig = corpus.get(c["clouseau_bug"], {}).get("signature", "") or ""
        generic = any(g in sig.lower() for g in GENERIC)
        rdirs = {_top2(f) for f in c.get("regressor_files", [])}
        fdirs = {_top2(f) for f in c.get("frame_files", [])}
        overlap = rdirs & fdirs
        # heuristic lean (confirm by hand): generic-abort crash or NO subsystem
        # overlap => likely no-call-path; overlap + genuinely explored but missed
        # => likely a searchfox hole.
        if generic or not overlap:
            lean = "no-call-path?"
        elif resolved > 3 and m["neighborhood_size"] > 100:
            lean = "hole?"
        else:
            lean = "unclear"
        print(f" MISS {c['clouseau_bug']} [{lean}] | nbhd {m['neighborhood_size']}"
              f" resolved {resolved} stopped {m['stopped']}"
              f" | generic={generic} dir_overlap={bool(overlap)} {sorted(overlap)[:2]}")
        print(f"      sig {sig[:44]} | regr_dirs {sorted(rdirs)[:3]}"
              f" | funcs {c.get('regressor_funcs', [])[:3]}")


if __name__ == "__main__":
    main()
