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

THE SHIPPED TABLE IS THE FULL ARM (2026-08-21). ``--corpus-dir corpus_ship --holdout-folds 0``
fits ``{25: 0.5, 50: 0.5714, 70: 0.7234, 85: 0.7234}``, which is what config/global.json now
carries. It replaces ``{25: 0.5, 50: 0.8, 70: 0.9714, 85: 0.9714}`` — the output of
``--positives-only --holdout-folds 0``: the same fit with the 26 culprit-absent rows deleted
(12 of them scored rung 70+ and all 12 were misses) and no held-out split at all
(``corpus_ship/calibration_table_positives.json``: ``n_negative`` 0, ``n_test`` 0).

    all 90 rows      rung 70  21/27 = 0.778   rung 85  13/20 = 0.650  -> pooled 34/47 = 0.7234
    cal split (62)   rung 70  17/21 = 0.810   rung 85   9/13 = 0.692  -> pooled 26/34 = 0.7647
    TEST split (28)  rung 70   4/6  = 0.667   rung 85   4/7  = 0.571  -> pooled  8/13 = 0.615
    positives-only   rung 70  21/21 = 1.000   rung 85  13/14 = 0.929  -> pooled 34/35 = 0.9714

The held-out 8/13 carries Wilson95 0.3552-0.8229, which covers the shipped 0.7234 — as does the
filer's own adjudicated outcome rate over 52 real filings, which reads 0.65-0.73 across every
way of counting an endorsement: 17/25 = 0.680 (0.4841-0.8280) at its loosest, 15/23 = 0.652
(0.4489-0.8119) once the two rows this patch reclassifies as ``unconfirmed`` are dropped, and
22/30 = 0.733 / 20/28 = 0.714 counting self-duplicates. Every one of those intervals contains
0.7234 and none contains 0.9714, whose Wilson95 LOWER bound is 0.8547. Two independent readings
at ~0.7 is the argument for the shipped number.

``--arm`` FITS EITHER SIDE OF ``corroborations.candidate_in_pushlog_window``, and the two-table
calibration it was written for is a MEASURED NULL RESULT — kept re-runnable, not shipped.
Reported rung 70+ reads 26/26 in-window against 8/21 = 0.381 outside, which looks decisive and is
not: 12 of those 21 are the corpus's culprit-DELETED negatives, whose ``worth`` is False by
construction (0 of 26 negatives score ``worth`` at any rung); the informative rows read
8/9 = 0.889, Fisher exact p = 0.257; and all 12 reported rung-70+ negatives are out-of-window, so
the predicate CONTAINS ``is_negative`` rather than replacing it. corpus_ship predates the flag
(ef0ccd8, 2026-08-10) and could not have recorded it anyway until ``_record_window_membership``
stopped keying on the all-None ``candidate_pushdates`` map, so ``spike/window_arm_null.py``
backfills it from each case's frozen candidate set. ``config.get_agent_calibration`` carries the
production denominators and the two counter-examples (bugs 2062806 and 2062119).

THE TOP TWO RUNGS SHARE ONE VALUE, AND THAT IS THE ANSWER, NOT A BUG (measured 2026-08-04).
``isotonic`` pools them because rung 85 scores WORSE than rung 70 on every cut in the table
above. Non-monotonic in the same direction each time, so a ``strong-evidence``/85 verdict is not
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


def _deceil(bins, fitted):
    """Replace a fitted ``1.0`` with the Wilson95 LOWER bound of the block that produced it.

    A bin with no observed failure fits to exactly 1.0, and 1.0 is not a probability anybody can
    publish to a Bugzilla reviewer: the in-window arm of ``corpus_ship`` is 26/26 at rung 70+,
    which is 26 clean rows, not certainty. That block publishes 0.8713 instead. Adjacent bins
    isotonic already gave the same value are bounded as ONE block (rung 70's 16/16 with rung 85's
    10/10), because bounding them separately gives 0.8064 then 0.7225 — a DECREASING table out of
    a monotonic fit. For the same reason the bound can never fall below the block beneath it.

    A FLOOR ON THE CLAIM, NOT A REFIT: it fires only on a 1.0, so the shipped full arm
    (34/47 = 0.7234) and even the retired positives arm (34/35 = 0.9714) come out untouched —
    ``--positives-only --holdout-folds 0`` still reproduces the old table byte for byte, which is
    the honest behaviour for a guard. What it stops is the NEXT overclaim: with a held-out split
    that same arm fits 1.000 at EVERY rung off 30 clean reported rows, and this turns that into
    0.8865 rather than "100% worth investigating"."""
    out = list(fitted)
    i = 0
    while i < len(out):
        j = i
        while j + 1 < len(out) and abs(out[j + 1] - out[i]) < 1e-12:
            j += 1
        if out[i] >= 1.0 - 1e-12:
            n = sum(b["n"] for b in bins[i:j + 1])
            hits = sum(b["n_hit"] for b in bins[i:j + 1])
            low = M.wilson_ci(hits, n)[0] if n else 0.0
            if i > 0:
                low = max(low, out[i - 1])
            for k in range(i, j + 1):
                out[k] = low
        i = j + 1
    return out


def calibration_table(cal_rows):
    """{rung_score(str) -> calibrated P(worth-investigating)} from the calibration split."""
    bins = M.reliability_bins(cal_rows)
    fitted = _deceil(bins, isotonic(bins))
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
              positives_only=False, arm="all"):
    rows = load_rows(os.path.join(corpus_dir, "results.jsonl"))
    if positives_only:
        # DIAGNOSTIC ONLY -- this has not been the shipped fit since 2026-08-21, and half the
        # justification that used to sit here was measured BACKWARDS. It said the culprit-absent
        # negatives are "rare in prod": 16 of the 31 filings since ef0ccd8 (51.6%, Wilson95
        # 0.348-0.680) print the out-of-window caveat, against 26/90 = 28.9% in this corpus, so
        # production is MORE culprit-absent than the arm that was dropped for being rare.
        # Dropping it is what produced 0.9714 = 34/35, the precision left after deleting 12
        # rung-70+ rows that were misses to a row.
        #
        # THE OTHER HALF STILL STANDS and is why `--arm out-of-window` is not a clean read
        # either: a negative case has no `regressor_nodes`, so its `worth` is False whatever the
        # agent said (0 of 26, at every rung) -- including when it correctly rediscovers the
        # removed regressor from searchfox/history. Those rows measure the false-investigate
        # RATE, which this function already reports separately; they are not zero-labelled
        # observations about the leads they are pooled with.
        rows = [r for r in rows if not r.get("is_negative")]
    if arm in ("in-window", "out-of-window"):
        # `corroborations.candidate_in_pushlog_window`, tri-state, with None ("nobody recorded
        # it") on the out-of-window side exactly as `report_bug.is_suspected_regression` and
        # `config.get_agent_calibration` read it. Keying the SHIPPED table on this was measured
        # and refuted (see the module docstring); the flag is kept fittable so the null result
        # can be re-run on a corpus that carries it -- corpus_ship does not, hence the count
        # printed below, and `spike/window_arm_null.py`, which backfills it.
        def _in_window(row):
            return (row.get("corroborations") or {}).get("candidate_in_pushlog_window") is True
        known = sum(1 for r in rows
                    if "candidate_in_pushlog_window" in (r.get("corroborations") or {}))
        print("arm={}: {} of {} rows carry candidate_in_pushlog_window".format(
            arm, known, len(rows)))
        rows = [r for r in rows if _in_window(r) == (arm == "in-window")]
    cal, test = split(rows, holdout_folds=holdout_folds)
    table, cal_bins = calibration_table(cal)
    test_bins = M.reliability_bins(test)
    thr = pick_threshold(cal, target_precision=target_precision)

    n_neg = sum(1 for r in rows if r.get("is_negative"))
    fi = sum(1 for r in rows if r.get("is_negative") and r.get("reported"))
    ci = M.wilson_ci(fi, n_neg)

    result = {
        # What this fit IS, in the file itself. `corpus_ship/calibration_table_positives.json`
        # was byte-identical to the shipped table and recorded nothing about having deleted the
        # negative arm, so nothing ever flagged that the published 0.9714 was positives-only.
        "arm": arm, "positives_only": bool(positives_only),
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
                        help="DIAGNOSTIC: fit only the culprit-present arm, dropping the "
                             "culprit-absent negatives. This is how the RETIRED table was fit")
    parser.add_argument("--arm", choices=("all", "in-window", "out-of-window"), default="all",
                        help="fit one side of corroborations.candidate_in_pushlog_window "
                             "(None counts as out-of-window). Kept so the two-arm null result "
                             "stays re-measurable; the shipped table is --arm all")
    args = parser.parse_args(argv)
    calibrate(args.corpus_dir, out=args.out,
              target_precision=args.target_precision, holdout_folds=args.holdout_folds,
              positives_only=args.positives_only, arm=args.arm)


if __name__ == "__main__":
    main()
