"""Mechanical vs Haiku head-to-head for a --mode both run (Phase-0 spike).

Reads a single results file that has both `modes.mechanical` and `modes.llm`
per case and reports, on the off-stack subset (the go/no-go test):
  * off-stack recall for each mode,
  * per-case hit/miss + neighborhood size + queries + est cost,
  * flips (llm reached where mechanical missed, or vice versa),
  * query-efficiency: on cases BOTH reached, did Haiku use fewer queries than
    mechanical's brute-force expansion?

Usage:
  python -m spike.head_to_head --results spike/results.both.json
"""

from __future__ import annotations

import argparse
import json


def _leg(case, mode):
    m = (case.get("modes") or {}).get(mode) or {}
    return {
        "hit": bool(m.get("neighborhood_hit")),
        "nbhd": m.get("neighborhood_size"),
        "queries": m.get("queries"),
        "cost": m.get("est_cost_usd") or 0.0,
        "stopped": m.get("stopped"),
        "matched": list((m.get("matched") or {}).keys()),
    }


def _status(c):
    if c.get("skipped"):
        return "SKIP"
    return "onstack" if c.get("stack_only_hit") else "offstack"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="spike/results.both.json")
    args = ap.parse_args()

    r = json.load(open(args.results))
    cases = r["cases"]

    print(f"{'bug':>8} {'status':>8} | {'MECH':>4} {'m.nbhd':>6} {'m.q':>4} "
          f"| {'HAIKU':>5} {'h.nbhd':>6} {'h.q':>4} {'h.$':>7} | note")
    print("-" * 92)

    off = {"mech_hit": 0, "haiku_hit": 0, "total": 0}
    flips_haiku_only, flips_mech_only, both_hit = [], [], []
    total_cost = 0.0
    for c in cases:
        st = _status(c)
        me = _leg(c, "mechanical")
        ha = _leg(c, "llm")
        total_cost += ha["cost"]
        note = ""
        if st == "offstack":
            off["total"] += 1
            off["mech_hit"] += me["hit"]
            off["haiku_hit"] += ha["hit"]
            if ha["hit"] and not me["hit"]:
                note = "HAIKU-ONLY ***"; flips_haiku_only.append(c["clouseau_bug"])
            elif me["hit"] and not ha["hit"]:
                note = "mech-only !!!"; flips_mech_only.append(c["clouseau_bug"])
            elif me["hit"] and ha["hit"]:
                note = f"both (haiku {ha['queries']}q vs mech {me['queries']}q)"
                both_hit.append((c["clouseau_bug"], me["queries"], ha["queries"]))
        print(f"{c['clouseau_bug']:>8} {st:>8} | {str(me['hit']):>4} {str(me['nbhd']):>6} {str(me['queries']):>4} "
              f"| {str(ha['hit']):>5} {str(ha['nbhd']):>6} {str(ha['queries']):>4} {ha['cost']:>7.4f} | {note}")

    n = off["total"] or 1
    print("\n=== OFF-STACK SUBSET (the go/no-go test) ===")
    print(f"  cases: {off['total']}")
    print(f"  mechanical recall: {off['mech_hit']}/{off['total']} = {off['mech_hit']/n:.2f}")
    print(f"  HAIKU recall:      {off['haiku_hit']}/{off['total']} = {off['haiku_hit']/n:.2f}")
    print(f"  Haiku reached where mechanical MISSED: {flips_haiku_only or 'none'}")
    print(f"  mechanical reached where Haiku missed: {flips_mech_only or 'none'}")
    print("\n=== QUERY EFFICIENCY (cases both reached) ===")
    for bug, mq, hq in both_hit:
        verdict = "Haiku fewer" if (hq or 0) < (mq or 0) else ("tie" if hq == mq else "Haiku more")
        print(f"  {bug}: mech {mq}q  vs  haiku {hq}q  -> {verdict}")
    print(f"\n=== total Haiku API cost this run: ${total_cost:.4f} ===")


if __name__ == "__main__":
    main()
