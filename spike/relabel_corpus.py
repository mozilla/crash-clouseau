# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Apply the fixed label validation to an EXISTING frozen corpus.

`eval/corpus.py` now validates regressor landings at freeze time, but that only helps corpora
frozen from here on. `corpus_ship` still carries the labels the old code produced: 23% of its
`regressor_node` values are unusable (27% of individual landing nodes) because the old labeller
took `sorted(nodes)[0]` — changeset-HASH order, i.e. effectively at random — from a pool of
landings that included Lando auto-format commits and changesets that landed after the crashing
build.

This re-derives the labels in place from data already on disk, so it costs nothing but hg
lookups:

  * validate every node in `regressor_nodes` against the crash's own build
  * keep the survivors, ordered by pushdate ascending
  * `regressor_node` becomes the EARLIEST survivor — a meaningful representative of when the
    regression was introduced
  * record what was dropped, and why, in `label_rejects`

`regressor_bugs` is untouched: it comes straight from Bugzilla `regressed_by` and was never
affected. That is also why the CALIBRATION is unaffected either way — `metrics._hit` falls back
to the bug match, and an audit found 0 of 54 recorded hits tainted
(`spike/audit_all_regressor_nodes.py`). The point of this script is to make `regressor_node`
trustworthy for FUTURE experiments that feed it as "the" regressor, as the second-opinion
sensitivity measurement did.

Cases left with no usable node keep their bug labels and are reported: they are still valid for
bug-level scoring, just not for node-level.

  DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
    uv run python spike/relabel_corpus.py --corpus corpus_ship --out spike/RELABEL_REPORT.json
Add --write to actually modify the corpus (default is a dry run).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import crashclouseau.net  # noqa: E402,F401  (allowlisted UA for hg.mozilla.org)
from crashclouseau.eval import corpus as C  # noqa: E402
from crashclouseau.eval.models import CorpusCase  # noqa: E402


def _relabel(case):
    """Re-derive one case's node labels. Returns a report dict; does not write."""
    out = {
        "uuid": case.uuid,
        "was_regressor_node": case.regressor_node,
        "n_nodes_before": len(case.regressor_nodes or []),
    }
    try:
        with open(case.crash_json_path) as handle:
            build = json.load(handle).get("build")
    except (OSError, json.JSONDecodeError, TypeError):
        build = None
    build_dt = C._build_date(build)
    out["crash_buildid"] = build
    if build_dt is None:
        # Without the build date the strongest check (landed-after-build) cannot run. Say so
        # rather than silently relabelling on the weaker checks alone.
        out["warning"] = "no crash buildid; timing check skipped"
    bugs = set(case.regressor_bugs or ([case.regressor_bug] if case.regressor_bug else []))
    landings = [{"node": n, "bug": bugs or None} for n in (case.regressor_nodes or [])]
    kept, rejected = C._validate_landings(landings, case.channel, build_dt)
    out["kept"] = [k["node"] for k in kept]
    out["rejected"] = [{"node": r["node"], "reason": r["reason"]} for r in rejected]
    out["now_regressor_node"] = kept[0]["node"] if kept else ""
    out["changed"] = out["now_regressor_node"] != case.regressor_node
    out["lost_all_nodes"] = not kept and bool(case.regressor_nodes)
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--corpus", default="corpus_ship")
    p.add_argument("--out", default="spike/RELABEL_REPORT.json")
    p.add_argument("--write", action="store_true",
                   help="write the corrected labels back (default: dry run)")
    p.add_argument("--workers", type=int, default=8)
    args = p.parse_args()

    cases = []
    for path in sorted(glob.glob(os.path.join(args.corpus, "*", "case.json"))):
        with open(path) as handle:
            case = CorpusCase.model_validate_json(handle.read())
        if case.regressor_nodes and not case.is_negative:
            cases.append((path, case))
    print("relabelling {} labelled positives in {}{}".format(
        len(cases), args.corpus, "" if args.write else "  (DRY RUN)"))

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        reports = list(pool.map(lambda pc: _relabel(pc[1]), cases))

    changed = [r for r in reports if r["changed"]]
    lost = [r for r in reports if r["lost_all_nodes"]]
    n_rejected = sum(len(r["rejected"]) for r in reports)
    summary = {
        "corpus": args.corpus,
        "n_cases": len(reports),
        "n_labels_changed": len(changed),
        "n_nodes_rejected": n_rejected,
        "n_cases_left_with_no_node": len(lost),
        "wrote": bool(args.write),
    }

    if args.write:
        by_uuid = {r["uuid"]: r for r in reports}
        for path, case in cases:
            rep = by_uuid[case.uuid]
            case.regressor_nodes = rep["kept"]
            case.regressor_node = rep["now_regressor_node"]
            case.label_rejects = rep["rejected"]
            with open(path, "w") as handle:
                handle.write(case.model_dump_json(indent=2))
        print("wrote {} case.json files".format(len(cases)))

    with open(args.out, "w") as handle:
        json.dump({"summary": summary, "reports": reports}, handle, indent=2)
    print(json.dumps(summary, indent=2))
    print()
    for r in changed:
        print("  {} {!r} -> {!r}".format(r["uuid"][:13], r["was_regressor_node"],
                                         r["now_regressor_node"]))
        for rj in r["rejected"]:
            print("      dropped {} ({})".format(rj["node"], rj["reason"]))
    if lost:
        print("\n  CASES LEFT WITH NO USABLE NODE (still valid for bug-level scoring):")
        for r in lost:
            print("    {}  bugs kept, all {} node(s) rejected".format(
                r["uuid"][:13], r["n_nodes_before"]))
    print("\nwrote {}".format(args.out))


if __name__ == "__main__":
    main()
