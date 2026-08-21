# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""The two-arm worth-investigating calibration: a MEASURED NULL RESULT, kept reproducible.

WHAT WAS PROPOSED. `config/global.json` ships ONE calibration table. The obvious repair for the
positives-only fit it replaced (`config.get_agent_calibration`) was two tables, selected by
`corroborations.candidate_in_pushlog_window` -- the observable fact
`report_bug.is_suspected_regression` already reads to decide whether the prose may say "Suspected
regressor". High number in-window, low number outside.

WHY IT IS NOT SHIPPED. On corpus_ship, reported rung 70+ reads 26/26 in-window against
8/21 = 0.381 out of it, and that gap is an artefact of the corpus's construction:

  * 12 of the 21 out-of-window rows are the corpus's culprit-DELETED negatives. A negative case
    has no `regressor_nodes`, so `worth` is False whatever the agent said -- 0 of 26 negatives
    score worth at ANY rung. 0.381 is 57% unconditional zeros.
  * On the rows whose label can carry information, out-of-window reads 8/9 = 0.889 against
    in-window's 26/26. Fisher exact p = 0.257. The predicate does not separate.
  * ALL 12 reported rung-70+ negatives are out-of-window (12/12), so the "observable" predicate
    is a strict SUPERSET of the unobservable `is_negative` label it claimed to replace -- which
    makes the in-window arm the positives-only fit under a new name.
  * And the corpus cannot express a bug-2062806 shape: an out-of-window candidate that was the
    true cause (hzhao confirmed the mechanism, backed the named changeset out, RESOLVED FIXED).
    Publishing 0.381 for that population puts the cleanest confirmed fix of all 52 filings in the
    lowest bin.

Settling the question needs the 16 out-of-window filings labelled one by one, not a refit.

WHY THE FLAG HAS TO BE BACKFILLED. It landed on 2026-08-10 (ef0ccd8), two weeks after corpus_ship
was run, and until 2026-08-21 `_record_window_membership` keyed on `seed["candidate_pushdates"]`,
which `eval/study_corpus.py` fills with None for every candidate (0 of corpus_ship's 6873 have a
pushdate) -- so the offline harness could never have recorded it. All 90 rows of results.jsonl
carry only {call_path_verified, exposer_*, offstack_observe_only}. This recomputes the flag the
way the fixed gate now does: is the CITED node one of the case's frozen window candidates?
A corpus run after that fix carries the flag itself, and `eval.calibrate --arm` reads it directly.

Offline, no network, deterministic. corpus_* is gitignored, so the corpus is a local artifact and
this script is the reproducible part.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379 \\
        uv run python spike/window_arm_null.py corpus_ship
"""
import argparse
import json
import os
import sys
from math import comb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau.eval import calibrate as CAL  # noqa: E402
from crashclouseau.eval import metrics as M  # noqa: E402

_FLAG = "candidate_in_pushlog_window"


def _window_nodes(corpus_dir, uuid):
    """One case's frozen pushlog window: every candidate node `build_seed` had seeded."""
    path = os.path.join(corpus_dir, uuid, "case.json")
    if not os.path.exists(path):
        return set()
    with open(path) as handle:
        case = json.load(handle)
    return {c.get("node") for c in (case.get("candidates") or []) if c.get("node")}


def backfill(corpus_dir, rows):
    """Set `corroborations.candidate_in_pushlog_window` on rows that predate the flag.

    The shipped predicate: the CITED node against the seeded candidate set (`cited_node` is
    already short-rev'd by `eval.metrics`, as the case candidates are). A row whose case is
    missing, or that cited nothing, keeps the flag ABSENT -- which the arm filter reads as
    out-of-window, exactly as production does."""
    filled = 0
    for row in rows:
        corr = dict(row.get("corroborations") or {})
        if _FLAG not in corr:
            nodes = _window_nodes(corpus_dir, row["uuid"])
            cited = row.get("cited_node")
            if nodes and cited:
                corr[_FLAG] = cited in nodes
                filled += 1
        row["corroborations"] = corr
    return filled


def fisher_exact(a, b, c, d):
    """Two-sided Fisher exact p for [[a, b], [c, d]]. No scipy in this project."""
    n = a + b + c + d
    if n == 0:
        return 1.0

    def p_of(x):
        y, z, w = a + b - x, a + c - x, d - (x - a)
        if min(y, z, w) < 0:
            return 0.0
        return comb(a + b, x) * comb(c + d, z) / comb(n, a + c)

    p0 = p_of(a)
    return sum(p_of(x) for x in range(0, min(a + b, a + c) + 1) if p_of(x) <= p0 + 1e-12)


def _worth(row):
    return bool(row.get("worth")) if "worth" in row else bool(
        row.get("hit") or row.get("person_hit"))


def _twin_regressors(corpus_dir, uuid):
    """The culprit a `-neg` case had DELETED, read off its positive twin's case.json.

    The negative's OWN case.json wipes `regressor_nodes` to [], which is the point of the arm --
    so the only way to ask "did the agent find the removed regressor anyway?" is the twin, and
    the twin survives for only some of them. `None` = not checkable."""
    if not uuid.endswith("-neg"):
        return None
    path = os.path.join(corpus_dir, uuid[:-4], "case.json")
    if not os.path.exists(path):
        return None
    with open(path) as handle:
        return set(json.load(handle).get("regressor_nodes") or []) or None


def _fit(name, rows, holdout_folds):
    table, bins = CAL.calibration_table(rows)
    print("\n=== %s (%d rows) ===" % (name, len(rows)))
    print("  fit:", table)
    for b in bins:
        print("   rung %3s  n=%-3d worth=%-3d neg=%-3d p=%.4f"
              % (b["score"], b["n"], b["n_hit"], b["n_negative"], b["p_hit"]))
    n = sum(b["n"] for b in bins if b["score"] >= 70)
    hits = sum(b["n_hit"] for b in bins if b["score"] >= 70)
    if n:
        lo, hi = M.wilson_ci(hits, n)
        print("  rung 70+: %d/%d = %.4f  Wilson95 %.4f-%.4f" % (hits, n, hits / n, lo, hi))
    cal, test = CAL.split(rows, holdout_folds=holdout_folds)
    tbins = M.reliability_bins(test)
    tn = sum(b["n"] for b in tbins if b["score"] >= 70)
    th = sum(b["n_hit"] for b in tbins if b["score"] >= 70)
    print("  held out (%d/10 folds): cal fit %s; TEST rung 70+ %d/%d"
          % (holdout_folds, CAL.calibration_table(cal)[0], th, tn))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python spike/window_arm_null.py")
    parser.add_argument("corpus_dir")
    parser.add_argument("--holdout-folds", type=int, default=3)
    args = parser.parse_args(argv)

    rows = CAL.load_rows(os.path.join(args.corpus_dir, "results.jsonl"))
    print("%d rows, %d backfilled from case.json" % (len(rows), backfill(args.corpus_dir, rows)))

    def arm(want):
        return [r for r in rows
                if ((r.get("corroborations") or {}).get(_FLAG) is True) == want]

    _fit("ALL  (the SHIPPED fit)", rows, args.holdout_folds)
    _fit("IN-WINDOW  (not shipped)", arm(True), args.holdout_folds)
    _fit("OUT-OF-WINDOW  (not shipped)", arm(False), args.holdout_folds)
    _fit("POSITIVES-ONLY  (the RETIRED table)",
         [r for r in rows if not r.get("is_negative")], args.holdout_folds)

    top = [r for r in rows if r.get("reported") and (r.get("score") or 0) >= 70]
    inw = [r for r in top if (r.get("corroborations") or {}).get(_FLAG) is True]
    out = [r for r in top if (r.get("corroborations") or {}).get(_FLAG) is not True]
    negs = [r for r in top if r.get("is_negative")]
    outp = [r for r in out if not r.get("is_negative")]
    inwp = [r for r in inw if not r.get("is_negative")]
    a, c = sum(map(_worth, inwp)), sum(map(_worth, outp))
    print("\n=== WHY THE SPLIT IS NOT SHIPPED (reported, rung 70+) ===")
    print("  in-window            %d/%d" % (sum(map(_worth, inw)), len(inw)))
    print("  out-of-window        %d/%d   of which culprit-DELETED negatives: %d"
          % (sum(map(_worth, out)), len(out), sum(1 for r in out if r.get("is_negative"))))
    print("  negatives scoring worth at ANY rung: %d of %d"
          % (sum(1 for r in rows if r.get("is_negative") and _worth(r)),
             sum(1 for r in rows if r.get("is_negative"))))
    print("  reported rung-70+ negatives that are out-of-window: %d of %d"
          % (sum(1 for r in negs
                 if (r.get("corroborations") or {}).get(_FLAG) is not True), len(negs)))
    print("  culprit-PRESENT only: in %d/%d vs out %d/%d   Fisher exact p = %.4f"
          % (a, len(inwp), c, len(outp),
             fisher_exact(a, len(inwp) - a, c, len(outp) - c)))

    # ...and the same objection, turned on the SHIPPED fit. 12 of the 47 rows behind
    # 34/47 = 0.7234 are those same by-construction zeros. Pooling them is defensible -- a lead
    # named in a culprit-less window IS a false investigate -- but where the twin survives, most
    # of them turn out to have named the very changeset the negative build deleted. See
    # `config.get_agent_calibration`.
    checkable = rediscovered = 0
    print("\n=== WHAT THE SHIPPED FIT RESTS ON (the same zeros, pooled IN) ===")
    for r in negs:
        reg = _twin_regressors(args.corpus_dir, r["uuid"])
        if reg is None:
            continue
        checkable += 1
        if r.get("cited_node") in reg:
            rediscovered += 1
            print("   %s cited %s -- the DELETED culprit" % (r["uuid"], r.get("cited_node")))
    hits70 = sum(map(_worth, top))
    print("  by-construction zeros among the shipped fit's rung-70+ rows: %d of %d"
          % (len(negs), len(top)))
    print("  of the %d whose positive twin survives, %d cited the deleted culprit"
          % (checkable, rediscovered))
    if top:
        print("  relabel just those: %d/%d = %.4f   (shipped fit is %d/%d = %.4f, a LOWER bound)"
              % (hits70 + rediscovered, len(top), (hits70 + rediscovered) / len(top),
                 hits70, len(top), hits70 / len(top)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
