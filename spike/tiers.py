"""Three-tier navigator comparison: mechanical vs Haiku vs Sonnet vs Opus.

Reads the mechanical baseline plus one results file per LLM tier (each a
``--mode llm`` run) and reports, on the off-stack subset (the go/no-go test):
  * off-stack recall per tier,
  * per-case hit/miss + queries + stop reason (does a stronger model persist
    past Haiku's early ``no-proposals`` give-up?),
  * union recall (mechanical + all LLM tiers combined),
  * true API cost per tier (recomputed with correct per-model rates; the stored
    est_cost_usd always uses Haiku rates).

Usage:
  python -m spike.tiers --mechanical spike/results.big2.json \
      --tier haiku=spike/results.both.json \
      --tier sonnet=spike/results.sonnet-5.json \
      --tier opus=spike/results.opus-4-8.json
"""

from __future__ import annotations

import argparse
import json

# ($/MTok in, $/MTok out). Sonnet 5 intro pricing through 2026-08-31.
RATES = {"haiku": (1.0, 5.0), "sonnet": (2.0, 10.0), "opus": (5.0, 25.0)}


def _load(path):
    return {c["clouseau_bug"]: c for c in json.load(open(path))["cases"]}


def _leg(case, mode):
    m = (case.get("modes") or {}).get(mode) or {}
    return {
        "hit": bool(m.get("neighborhood_hit")),
        "q": m.get("queries"),
        "nbhd": m.get("neighborhood_size"),
        "stopped": m.get("stopped"),
        "in": m.get("input_tokens") or 0,
        "out": m.get("output_tokens") or 0,
    }


def _cost(tier, leg):
    ri, ro = RATES.get(tier, (1.0, 5.0))
    return leg["in"] / 1e6 * ri + leg["out"] / 1e6 * ro


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mechanical", default="spike/results.big2.json")
    ap.add_argument("--tier", action="append", default=[],
                    help="name=path (LLM --mode llm results); repeatable")
    args = ap.parse_args()

    mech = _load(args.mechanical)
    tiers = {}
    for spec in args.tier:
        name, path = spec.split("=", 1)
        tiers[name] = _load(path)

    bugs = sorted(mech)
    off = [b for b in bugs if not mech[b].get("skipped") and not mech[b].get("stack_only_hit")]

    names = list(tiers)
    hdr = f"{'bug':>8} | {'MECH':>5}"
    for n in names:
        hdr += f" | {n[:6]+'.hit':>10} {n[:4]+'.q':>6} {'stop':>13}"
    print("OFF-STACK SUBSET (the go/no-go test)\n" + hdr)
    print("-" * len(hdr))

    recall = {"mechanical": 0}
    for n in names:
        recall[n] = 0
    union = 0
    for b in off:
        mh = _leg(mech[b], "mechanical")["hit"]
        recall["mechanical"] += mh
        row = f"{b:>8} | {str(mh):>5}"
        any_hit = mh
        for n in names:
            c = tiers[n].get(b)
            lg = _leg(c, "llm") if c else {"hit": False, "q": None, "stopped": "-"}
            recall[n] += lg["hit"]
            any_hit = any_hit or lg["hit"]
            row += f" | {str(lg['hit']):>10} {str(lg['q']):>6} {str(lg['stopped']):>13}"
        union += any_hit
        print(row)

    n_off = len(off) or 1
    print(f"\n=== OFF-STACK RECALL (n={len(off)}) ===")
    print(f"  mechanical : {recall['mechanical']}/{len(off)} = {recall['mechanical']/n_off:.2f}")
    for n in names:
        print(f"  {n:<10} : {recall[n]}/{len(off)} = {recall[n]/n_off:.2f}")
    print(f"  UNION (mech + all LLM tiers): {union}/{len(off)} = {union/n_off:.2f}")

    print("\n=== TRUE API COST (all scored cases, correct per-model rates) ===")
    for n in names:
        tot = sum(_cost(n, _leg(c, "llm")) for c in tiers[n].values() if not c.get("skipped"))
        print(f"  {n:<10}: ${tot:.4f}")

    print("\n=== PERSISTENCE (llm stop reasons across ALL scored cases) ===")
    for n in names:
        from collections import Counter
        stc = Counter(_leg(c, "llm")["stopped"] for c in tiers[n].values() if not c.get("skipped"))
        print(f"  {n:<10}: {dict(stc)}")


if __name__ == "__main__":
    main()
