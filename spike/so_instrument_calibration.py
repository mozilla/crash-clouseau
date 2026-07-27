# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Is the blind second opinion (#SO) a MEASURING INSTRUMENT or just a skeptic?

In the first three prod days the SO refuted 17 of 23 verify-mode leads (74%), 10 of them at
`high` confidence. That number is uninterpretable on its own, because two very different
worlds produce it:

  (a) the primary pipeline's lead precision really is ~26%, or
  (b) the SO refutes almost anything — it has STRICTLY LESS evidence than the primary (the
      tight allowlist denies it the pushlog by design) and is prompted anti-confirmation
      ("it may be unrelated — say so"), so "no" is its cheap default.

Under (b) the asymmetric fold is moving ~40% of reported leads on noise.

This harness separates them with the study corpus's GROUND TRUTH. Two arms, same prompt, same
tools, same model:

  POSITIVE arm  — feed the SO the crash plus its KNOWN-TRUE regressor as the candidate.
                  Every refute here is a FALSE refute. This rate IS the instrument's bias.
  NEGATIVE arm  — feed the SO a crash whose true regressor is NOT in the window
                  (`is_negative`), plus a real-but-wrong changeset from that window.
                  Every refute here is a TRUE refute.

The GAP between the two rates is the instrument's discriminative power. If they are close,
the SO cannot tell a real regressor from an unrelated changeset and the 74% says nothing
about the pipeline. Because only a HIGH-confidence refute moves the band, the high-confidence
rates are reported separately — those are the ones with teeth.

`--effort` also settles the still-open high-vs-max question (both arms, same cases).

Usage (needs the SDK + network; ~$1/call, so start small):
  DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
    uv run python spike/so_instrument_calibration.py --n-pos 2 --n-neg 2 --out /tmp/pilot.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

# Run as `uv run python spike/so_instrument_calibration.py` -> sys.path[0] is spike/, so the
# repo root (and with it the `crashclouseau` package) is not importable without this.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau import config                                       # noqa: E402
from crashclouseau.eval.corpus import load_corpus                      # noqa: E402
from crashclouseau.eval.runner import _case_to_crash                   # noqa: E402


def _pick_wrong_candidate(case):
    """A real changeset from this crash's window that is NOT the regressor. For an
    `is_negative` case the true regressor is not in the window at all, so every candidate
    qualifies; prefer a live, non-noise one and pick DETERMINISTICALLY (sorted by node) so
    re-runs and the effort A/B compare like for like."""
    cands = [
        c for c in (case.candidates or [])
        if (c.get("node") if isinstance(c, dict) else getattr(c, "node", None))
    ]

    def _get(c, key, default=None):
        return c.get(key, default) if isinstance(c, dict) else getattr(c, key, default)

    live = [c for c in cands if not _get(c, "backedout") and not _get(c, "noise")] or cands
    if not live:
        return None
    best = sorted(live, key=lambda c: str(_get(c, "node") or ""))[0]
    return {"node": _get(best, "node"), "bug": _get(best, "bug"),
            "desc": _get(best, "desc", "") or ""}


def _build_rows(cases, n_pos, n_neg):
    """One row per SO call: the crash, the candidate to judge, and what the answer SHOULD be.
    Cases are taken in sorted-uuid order (not sampled) so a re-run is reproducible."""
    positives, negatives = [], []
    for case in sorted(cases, key=lambda c: c.uuid):
        crash = _case_to_crash(case)
        # A crash whose frozen dump renders NO stack gives the SO almost nothing to reason
        # from; refuting there measures the missing input, not the instrument. Skip it.
        if not (crash.get("stack") or "").strip():
            continue
        if getattr(case, "is_negative", False):
            cand = _pick_wrong_candidate(case)
            if cand:
                negatives.append((case, crash, cand, False))
        elif case.regressor_node:
            positives.append((
                case, crash,
                {"node": case.regressor_node, "bug": case.regressor_bug, "desc": ""},
                True,
            ))
    rows = []
    for case, crash, cand, truth in positives[:n_pos] + negatives[:n_neg]:
        rows.append({
            "uuid": case.uuid,
            "arm": "positive" if truth else "negative",
            "signature": case.signature,
            "is_offstack": bool(getattr(case, "is_offstack", False)),
            "candidate": cand,
            # Ground truth: should the SO corroborate this candidate?
            "should_corroborate": truth,
            "crash": crash,
        })
    return rows


async def _run_row(row, sem, spent, max_cost):
    from crashclouseau.agent.second_opinion import run_second_opinion

    async with sem:
        if max_cost is not None and spent["usd"] >= max_cost:
            return {**_meta(row), "skipped": "cost cap reached"}
        started = time.time()
        try:
            so = await run_second_opinion(row["crash"], row["candidate"])
        except Exception as exc:                                        # noqa: BLE001
            return {**_meta(row), "error": "{}: {}".format(type(exc).__name__, exc)}
        elapsed = round(time.time() - started, 1)
        if so is None:
            return {**_meta(row), "error": "no result (errored/empty/unparseable)",
                    "elapsed_s": elapsed}
        spent["usd"] += float(so.cost_usd or 0.0)
        return {
            **_meta(row),
            "mode": so.mode,
            "corroborates": so.corroborates,
            "confidence": (so.confidence or "").strip().lower(),
            "mechanism": so.mechanism,
            "refutation": so.refutation,
            "cost_usd": so.cost_usd,
            "elapsed_s": elapsed,
        }


def _meta(row):
    return {
        "uuid": row["uuid"],
        "arm": row["arm"],
        "signature": row["signature"],
        "is_offstack": row["is_offstack"],
        "candidate_node": row["candidate"]["node"],
        "candidate_bug": row["candidate"].get("bug"),
        "should_corroborate": row["should_corroborate"],
    }


def summarize(results):
    """Sensitivity/specificity of the SO as an instrument, plus the high-confidence-only
    view (the only refutes that actually move the shipped band)."""
    out = {}
    for arm in ("positive", "negative"):
        rows = [r for r in results if r["arm"] == arm and "error" not in r and "skipped" not in r]
        refuted = [r for r in rows if r["corroborates"] is False]
        out[arm] = {
            "n": len(rows),
            "corroborated": sum(1 for r in rows if r["corroborates"] is True),
            "refuted": len(refuted),
            "unsure": sum(1 for r in rows if r["corroborates"] is None),
            "refuted_high_conf": sum(1 for r in refuted if r["confidence"] == "high"),
            "refute_rate": round(len(refuted) / len(rows), 3) if rows else None,
            "refute_rate_high_conf": (
                round(sum(1 for r in refuted if r["confidence"] == "high") / len(rows), 3)
                if rows else None
            ),
            "cost_usd": round(sum(float(r.get("cost_usd") or 0) for r in rows), 2),
            "mean_elapsed_s": (
                round(sum(r.get("elapsed_s") or 0 for r in rows) / len(rows), 1)
                if rows else None
            ),
        }
    pos, neg = out["positive"], out["negative"]
    # The headline. A useful instrument refutes wrong candidates far more than right ones;
    # a biased one refutes both alike and the gap collapses toward zero.
    if pos["refute_rate"] is not None and neg["refute_rate"] is not None:
        out["discrimination"] = {
            "false_refute_rate": pos["refute_rate"],
            "true_refute_rate": neg["refute_rate"],
            "gap": round(neg["refute_rate"] - pos["refute_rate"], 3),
            "false_refute_rate_high_conf": pos["refute_rate_high_conf"],
            "true_refute_rate_high_conf": neg["refute_rate_high_conf"],
            "gap_high_conf": round(
                neg["refute_rate_high_conf"] - pos["refute_rate_high_conf"], 3
            ),
        }
    out["errors"] = [r for r in results if "error" in r or "skipped" in r]
    return out


async def main_async(args):
    cases, _ = load_corpus(args.corpus)
    rows = _build_rows(cases, args.n_pos, args.n_neg)
    print("running {} SO calls ({} positive / {} negative), effort={} model={}".format(
        len(rows), sum(1 for r in rows if r["arm"] == "positive"),
        sum(1 for r in rows if r["arm"] == "negative"), args.effort, args.model))

    base = config.get_agent_second_opinion
    overridden = {**base(), "enabled": True, "effort": args.effort, "model": args.model,
                  "max_turns": args.max_turns}
    config.get_agent_second_opinion = lambda: dict(overridden)
    try:
        sem = asyncio.Semaphore(args.concurrency)
        spent = {"usd": 0.0}
        results = await asyncio.gather(
            *(_run_row(r, sem, spent, args.max_cost) for r in rows)
        )
    finally:
        config.get_agent_second_opinion = base

    results = list(results)
    summary = summarize(results)
    payload = {
        "config": {"corpus": args.corpus, "effort": args.effort, "model": args.model,
                   "max_turns": args.max_turns, "n_pos": args.n_pos, "n_neg": args.n_neg},
        "summary": summary,
        "results": results,
    }
    with open(args.out, "w") as handle:
        json.dump(payload, handle, indent=2)
    print(json.dumps(summary, indent=2))
    print("\nwrote {}".format(args.out))
    return payload


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--corpus", default="corpus_ship")
    p.add_argument("--n-pos", type=int, default=2,
                   help="known-TRUE-regressor cases (measures the FALSE refute rate)")
    p.add_argument("--n-neg", type=int, default=2,
                   help="wrong-changeset cases (measures the TRUE refute rate)")
    p.add_argument("--effort", default="max", choices=["low", "medium", "high", "max"])
    p.add_argument("--model", default="opus")
    p.add_argument("--max-turns", type=int, default=20)
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--max-cost", type=float, default=None,
                   help="stop starting new calls once this much has been spent (usd)")
    p.add_argument("--out", default="spike/SO_INSTRUMENT_CALIBRATION.json")
    args = p.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
