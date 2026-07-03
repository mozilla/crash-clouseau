# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Eval CLI (#13): ``python -m crashclouseau.eval.run mine|label|rerun|score|all``.

``all`` chains mine -> label -> rerun -> score and writes a stable metrics.json.
``rerun``/``score`` re-run the frozen corpus through the agent and score in one pass
(results are in-memory CrashTriageResults, not persisted between steps)."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from dateutil.relativedelta import relativedelta

from crashclouseau import config
from crashclouseau.eval import corpus as corpus_mod
from crashclouseau.eval import labels as labels_mod
from crashclouseau.eval import metrics as metrics_mod
from crashclouseau.eval import runner as runner_mod
from crashclouseau.eval.models import SweepConfig
from crashclouseau.logger import logger


def _default_window():
    end = datetime.now(timezone.utc)
    start = end - relativedelta(days=config.get_ndays_of_data())
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _load_sweep(path):
    if not path:
        return None
    with open(path) as handle:
        return SweepConfig.model_validate_json(handle.read()).model_dump()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m crashclouseau.eval.run")
    parser.add_argument("cmd", choices=["mine", "label", "rerun", "score", "all"])
    parser.add_argument("--corpus-dir", default=None)
    parser.add_argument("--sweep", default=None, help="path to a SweepConfig JSON")
    parser.add_argument("--baseline", default=None)
    parser.add_argument("--out", default="metrics.json")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    args = parser.parse_args(argv)

    corpus_dir = args.corpus_dir or config.get_eval().get("corpus_dir", "corpus")
    sweep = _load_sweep(args.sweep)

    if args.cmd in ("mine", "all"):
        start, end = (args.start, args.end)
        if not (start and end):
            start, end = _default_window()
        corpus_mod.freeze(corpus_mod.mine_clouseau_bugs(start, end), corpus_dir)

    cases, corpus_hash = corpus_mod.load_corpus(corpus_dir)

    if args.cmd in ("label", "all"):
        for case in cases:
            case.on_stack_label = labels_mod.derive_onstack_label(case)
            corpus_mod.save_case(case, corpus_dir)

    if args.cmd in ("rerun", "score", "all"):
        results = asyncio.run(runner_mod.rerun_corpus(cases, sweep))
        metrics = metrics_mod.compute_metrics(
            cases, results, sweep_config=sweep, corpus_hash=corpus_hash
        )
        with open(args.out, "w") as handle:
            handle.write(metrics.model_dump_json(indent=2))
        logger.info("eval: wrote %s (%d cases)", args.out, metrics.n_cases)
        if args.baseline:
            logger.info("eval: %s", metrics_mod.compare_to_baseline(metrics, args.baseline))


if __name__ == "__main__":
    main()
