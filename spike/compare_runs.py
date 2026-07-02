"""Compare two run_spike result files case-by-case (Phase-0 spike helper).

Purpose: the first option-(a) run was contaminated by transient searchfox 5xx
errors (each failed query silently shrinks the neighborhood -> false misses).
This diffs the contaminated run against the retry-fixed re-run so we can see:
  * which cases flipped MISS->HIT (first run was contaminated), and
  * which cases are STILL a miss while having failed queries (candidate genuine
    searchfox compute-limit holes worth a targeted lower-depth re-query).

Usage:
  python -m spike.compare_runs --a spike/results.big.json --b spike/results.big2.json
"""

from __future__ import annotations

import argparse
import json


def _load(path):
    r = json.load(open(path))
    return {c["clouseau_bug"]: c for c in r["cases"]}, r.get("aggregate", {})


def _mech(case):
    m = (case.get("modes") or {}).get("mechanical") or {}
    steps = m.get("steps") or []
    failed = sum(1 for s in steps if s.get("ok") is False)
    return {
        "hit": bool(m.get("neighborhood_hit")),
        "nbhd": m.get("neighborhood_size"),
        "queries": m.get("queries"),
        "failed_q": failed,
        "stopped": m.get("stopped"),
    }


def _status(case):
    if case.get("skipped"):
        return "SKIP"
    return "onstack" if case.get("stack_only_hit") else "offstack"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", default="spike/results.big.json", help="baseline (contaminated)")
    ap.add_argument("--b", default="spike/results.big2.json", help="retry re-run")
    args = ap.parse_args()

    A, aggA = _load(args.a)
    B, aggB = _load(args.b)
    bugs = sorted(set(A) | set(B))

    print(f"{'bug':>8} {'status':>8} | {'A.hit':>5} {'A.nbhd':>7} {'A.failQ':>7} "
          f"| {'B.hit':>5} {'B.nbhd':>7} {'B.failQ':>7} | flip")
    print("-" * 88)
    flips_up, flips_down, still_miss_failed = [], [], []
    for bug in bugs:
        ca, cb = A.get(bug), B.get(bug)
        st = _status(cb or ca)
        ma = _mech(ca) if ca else {}
        mb = _mech(cb) if cb else {}
        ha = ma.get("hit"); hb = mb.get("hit")
        flip = ""
        if ca and cb and st != "SKIP":
            if not ha and hb:
                flip = "MISS->HIT ***"; flips_up.append(bug)
            elif ha and not hb:
                flip = "HIT->MISS !!!"; flips_down.append(bug)
            if not hb and mb.get("failed_q"):
                still_miss_failed.append(bug)
        print(f"{bug:>8} {st:>8} | {str(ha):>5} {str(ma.get('nbhd')):>7} {str(ma.get('failed_q')):>7} "
              f"| {str(hb):>5} {str(mb.get('nbhd')):>7} {str(mb.get('failed_q')):>7} | {flip}")

    print("\n=== aggregate A (contaminated) ===")
    print(json.dumps(aggA, indent=1))
    print("=== aggregate B (retry) ===")
    print(json.dumps(aggB, indent=1))

    print("\n=== deltas ===")
    print("MISS->HIT (A contaminated, B recovered):", flips_up or "none")
    print("HIT->MISS (regression -- investigate):  ", flips_down or "none")
    print("still MISS in B *with* failed queries (candidate genuine hole / retarget):",
          still_miss_failed or "none")


if __name__ == "__main__":
    main()
