# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Fit + validate the worth-investigating calibration (Phase-2, STEP 5).

Reads a corpus's ``results.jsonl`` (the labeled ``(score, hit, is_negative)`` rows persisted
by ``eval.run``), fits a monotonic rung -> P(worth-investigating) map by isotonic regression
(pool-adjacent-violators) on a CALIBRATION split, picks a PRECISION-FIRST report threshold
(above the highest score the agent gives any culprit-absent negative window), and reports ECE +
a Wilson CI on the negative arm from a held-out TEST split. Writes ``calibration_table.json``
(its ``calibration_table`` sub-key is consumed later — via ``config.get_agent_calibration`` — by
``orchestrator.apply_deterministic_gates`` to populate ``Verdict.p_worth_investigating``) and
prints a text reliability diagram.

Deterministic + offline: the cal/test split keys on a stable hash of the uuid (no RNG), and
refitting reads only ``results.jsonl`` — it never re-runs the agent.

THE TOP TWO RUNGS SHARE ONE VALUE, AND THAT IS THE ANSWER, NOT A BUG (measured 2026-08-04).
``corpus_ship`` fits ``{25: 0.5, 50: 0.8, 70: 0.9714, 85: 0.9714}`` and the 0.9714 is exactly
34/35 — the POOLED 70+85 bin. ``isotonic`` pools them because rung 85 scores WORSE than rung 70:

    positives-only    rung 70  21/21 = 1.000     rung 85  13/14 = 0.929
    with negatives    rung 70  17/21 = 0.810     rung 85   9/13 = 0.692

Non-monotonic in the same direction on every cut, so a ``strong-evidence``/85 verdict is not
empirically more likely to be worth investigating than a ``probable``/70 lead. Refitting the same
rows will pool them again; the ladder above 70 carries no signal. Two consequences worth knowing
before touching this:

* A gate that moves a verdict from 85 to 70 (``orchestrator._apply_is_backout_gate``, the
  second-opinion refute fold) cannot move the badge. That is correct behaviour, not a wiring bug.
* Conditioning ``p_worth`` on the deterministic flags instead of the rung — which is the only way
  to express "97%, unless the signature is 283 days older than the candidate" — CANNOT be fit
  from this corpus. The flags are online-only, and ``sigage.first_seen_buildid`` searches Socorro
  within 364 days of *today*, so for a historical corpus the fact is simply gone: backfilling
  ``corpus_ship`` recovered it for 24 of 55 reported-with-node rows, of which 2 were stale
  (``spike/enrich_corpus_gate_facts.py``). n=2. The labelled data cannot carry the flags
  retroactively and the flagged data (prod, since 2026-07-27) has no labels; closing that needs
  flags frozen at triage time plus a forward labelling loop, not a refit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os

from crashclouseau.eval import metrics as M
from crashclouseau.logger import logger


def load_rows(path):
    rows = []
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _fold(uuid, folds=10):
    """Stable [0, folds) bucket for a uuid (sha256, no RNG — reproducible splits)."""
    return int(hashlib.sha256(uuid.encode()).hexdigest(), 16) % folds


def split(rows, holdout_folds=3, folds=10):
    """Deterministic calibration/test split: the last ``holdout_folds`` of ``folds`` buckets
    are TEST. Negatives are split the same way so both arms appear on each side."""
    cal, test = [], []
    for r in rows:
        (test if _fold(r["uuid"], folds) >= folds - holdout_folds else cal).append(r)
    return cal, test


def isotonic(bins):
    """Pool-adjacent-violators: fit a non-decreasing P(hit) over the rung bins (sorted by
    score asc), weighted by bin count. Returns one fitted probability per input bin."""
    if not bins:
        return []
    blocks = [
        {"lo": i, "hi": i, "val": b["p_hit"], "w": max(b["n"], 1)}
        for i, b in enumerate(bins)
    ]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i]["val"] > blocks[i + 1]["val"] + 1e-12:
            a, b = blocks[i], blocks[i + 1]
            w = a["w"] + b["w"]
            blocks[i] = {
                "lo": a["lo"], "hi": b["hi"],
                "val": (a["val"] * a["w"] + b["val"] * b["w"]) / w, "w": w,
            }
            del blocks[i + 1]
            i = max(i - 1, 0)  # back up: the merge may violate the prior block
        else:
            i += 1
    fitted = [0.0] * len(bins)
    for blk in blocks:
        for j in range(blk["lo"], blk["hi"] + 1):
            fitted[j] = blk["val"]
    return fitted


def calibration_table(cal_rows):
    """{rung_score(str) -> calibrated P(worth-investigating)} from the calibration split."""
    bins = M.reliability_bins(cal_rows)
    fitted = isotonic(bins)
    return {str(b["score"]): round(p, 4) for b, p in zip(bins, fitted)}, bins


def pick_threshold(rows, target_precision=0.9):
    """Precision-first tau: the lowest rung STRICTLY ABOVE the highest score the agent gives
    any REPORTED culprit-absent negative (the operational 'keep FP near the study 0-FP'). Also
    returns the precision-target tau (lowest tau whose reported precision >= target) for
    comparison. ``tau_precision_first=None`` means even the top rung fired on a negative."""
    reported = [r for r in rows if r.get("reported") and r.get("score") is not None]
    rungs = sorted({r["score"] for r in reported})
    neg_scores = [r["score"] for r in reported if r.get("is_negative")]
    max_neg = max(neg_scores) if neg_scores else None
    if max_neg is None:
        tau_pf = min(rungs) if rungs else None      # no negative ever reported -> report all
    else:
        above = [s for s in rungs if s > max_neg]
        tau_pf = min(above) if above else None       # None: no clean rung exists
    tau_target = None
    for row in sorted(M.threshold_sweep(rows), key=lambda r: r["tau"]):
        if row["precision"] >= target_precision:
            tau_target = row["tau"]
            break
    return {
        "tau_precision_first": tau_pf,
        "max_negative_score": max_neg,
        "tau_precision_target": tau_target,
        "target_precision": target_precision,
    }


def _reliability_diagram(bins, title):
    lines = ["  {}".format(title),
             "  rung |   n | neg | P(hit) | bar"]
    for b in bins:
        bar = "#" * int(round(b["p_hit"] * 20))
        lines.append("  {:>4} | {:>3} | {:>3} |  {:>4.2f}  | {}".format(
            b["score"], b["n"], b["n_negative"], b["p_hit"], bar))
    return "\n".join(lines) if bins else "  {} (no reported rows)".format(title)


def calibrate(corpus_dir, out=None, target_precision=0.9, holdout_folds=3,
              positives_only=False):
    rows = load_rows(os.path.join(corpus_dir, "results.jsonl"))
    if positives_only:
        # Fit only on the CULPRIT-PRESENT arm (the production condition: build_seed feeds the
        # first-bad window, which contains the regressor ~100% of the time). The synthetic
        # culprit-absent negatives are rare in prod AND their reported-rate over-counts the
        # agent correctly REDISCOVERING the removed regressor via searchfox/history (uncreditable
        # here), so mixing them deflates the table. See [[clouseau-phase2-calibration]].
        rows = [r for r in rows if not r.get("is_negative")]
    cal, test = split(rows, holdout_folds=holdout_folds)
    table, cal_bins = calibration_table(cal)
    test_bins = M.reliability_bins(test)
    thr = pick_threshold(cal, target_precision=target_precision)

    n_neg = sum(1 for r in rows if r.get("is_negative"))
    fi = sum(1 for r in rows if r.get("is_negative") and r.get("reported"))
    ci = M.wilson_ci(fi, n_neg)

    result = {
        "n_rows": len(rows), "n_cal": len(cal), "n_test": len(test),
        "n_negative": n_neg, "n_false_investigate": fi,
        "false_investigate_rate": (fi / n_neg) if n_neg else 0.0,
        "false_investigate_wilson95": [round(ci[0], 4), round(ci[1], 4)],
        "calibration_table": table,
        "threshold": thr,
        "ece_cal_uncalibrated": round(M.ece(cal_bins), 4),
        "ece_test_uncalibrated": round(M.ece(test_bins), 4),
        "reliability_cal": cal_bins,
        "reliability_test": test_bins,
        "threshold_sweep": M.threshold_sweep(rows),
    }
    out = out or os.path.join(corpus_dir, "calibration_table.json")
    with open(out, "w") as handle:
        json.dump(result, handle, indent=2)

    logger.info("calibrate: %d rows (%d cal / %d test); wrote %s",
                len(rows), len(cal), len(test), out)
    print("\n=== worth-investigating calibration ===")
    print("rows={} cal={} test={}  negatives={} false-investigate={} (Wilson95 <= {:.1%})".format(
        len(rows), len(cal), len(test), n_neg, fi, ci[1]))
    print("calibrated rung -> P(worth-investigating):", table)
    print("precision-first tau = {} (max reported-negative score = {})".format(
        thr["tau_precision_first"], thr["max_negative_score"]))
    print(_reliability_diagram(cal_bins, "CALIBRATION split reliability"))
    print(_reliability_diagram(test_bins, "TEST split reliability"))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m crashclouseau.eval.calibrate")
    parser.add_argument("--corpus-dir", required=True,
                        help="corpus dir containing results.jsonl")
    parser.add_argument("--out", default=None, help="calibration_table.json output path")
    parser.add_argument("--target-precision", type=float, default=0.9)
    parser.add_argument("--holdout-folds", type=int, default=3,
                        help="test = this many of 10 stable uuid-hash folds (0 = fit on all rows, "
                             "for the shipped table)")
    parser.add_argument("--positives-only", action="store_true",
                        help="fit only the culprit-present arm (the prod condition); drop the "
                             "synthetic culprit-absent negatives")
    args = parser.parse_args(argv)
    calibrate(args.corpus_dir, out=args.out,
              target_precision=args.target_precision, holdout_folds=args.holdout_folds,
              positives_only=args.positives_only)


if __name__ == "__main__":
    main()
