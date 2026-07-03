# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# DATABASE_URL=sqlite:// python -m unittest tests.test_eval_metrics
import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

from crashclouseau.agent.result import CrashTriageResult
from crashclouseau.agent.schema import (
    Candidate,
    Claim,
    Confidence,
    Decision,
    DiffHunk,
    DiffLineCitation,
    Dossier,
    SearchfoxCitation,
    Verdict,
)
from crashclouseau.eval import metrics as M
from crashclouseau.eval import runner as R
from crashclouseau.eval.models import CorpusCase

REG = "abc123def456"
_SF = SearchfoxCitation(permalink="p#1", symbol_id="_Z", repo="mozilla-central")


def _result(node=None, strong=False):
    hunks = []
    candidate = None
    if node:
        dl = DiffLineCitation(node=node, filename="f.cpp", line=10, side="added", content="x")
        hunks = [DiffHunk(node=node, filename="f.cpp", lines=[dl], citations=[dl])]
        candidate = Candidate(node=node)
    if strong:
        verdict = Verdict(
            decision=Decision.strong_evidence, confidence=Confidence.high,
            mechanism=Claim(statement="m", citations=[_SF]),
            consistency=Claim(statement="c", citations=[_SF]),
        )
    else:
        verdict = Verdict(decision=Decision.abstain, abstain_reason="x")
    return CrashTriageResult(
        num_turns=1, total_cost_usd=0.1,
        dossier=Dossier(candidate=candidate, hunks=hunks, verdict=verdict),
    )


def _case(uuid, **kw):
    return CorpusCase(uuid=uuid, regressor_node=kw.pop("reg", REG), **kw)


class TestOffstackRecall(unittest.TestCase):
    def test_reached(self):
        case = _case("u1", on_stack_label=False, seed_nodes=[])
        r = M.offstack_recall([case], {"u1": _result(node=REG)})
        self.assertEqual(r["offstack_recall"], 1.0)
        self.assertEqual(r["stackonly_recall"], 0.0)
        self.assertEqual(r["n_offstack"], 1)

    def test_missed(self):
        case = _case("u1", on_stack_label=False, seed_nodes=[])
        r = M.offstack_recall([case], {"u1": _result(node="0000deadbeef")})
        self.assertEqual(r["offstack_recall"], 0.0)

    def test_stackonly_baseline_counts_seed_hit(self):
        case = _case("u1", on_stack_label=False, seed_nodes=[REG])
        r = M.offstack_recall([case], {"u1": _result(node="0000deadbeef")})
        self.assertEqual(r["stackonly_recall"], 1.0)
        self.assertEqual(r["offstack_recall"], 0.0)  # agent didn't reach it


class TestEvidencePrecision(unittest.TestCase):
    def test_named_right_changeset(self):
        case = _case("u1", on_stack_label=False)
        r = M.evidence_correctness([case], {"u1": _result(node=REG, strong=True)})
        self.assertEqual(r["evidence_precision"], 1.0)
        self.assertEqual(r["n_strong"], 1)

    def test_named_wrong_changeset(self):
        case = _case("u1", on_stack_label=False)
        r = M.evidence_correctness([case], {"u1": _result(node="0000deadbeef", strong=True)})
        self.assertEqual(r["evidence_precision"], 0.0)

    def test_diff_checker_can_veto(self):
        case = _case("u1", on_stack_label=False)
        r = M.evidence_correctness(
            [case], {"u1": _result(node=REG, strong=True)},
            diff_checker=lambda c, d: False,
        )
        self.assertEqual(r["evidence_precision"], 0.0)

    def test_abstain_not_counted(self):
        case = _case("u1", on_stack_label=False)
        r = M.evidence_correctness([case], {"u1": _result(node=REG, strong=False)})
        self.assertEqual(r["n_strong"], 0)


class TestAbstainCalibration(unittest.TestCase):
    def test_confusion_matrix(self):
        cases = [
            _case("u1", on_stack_label=True, seed_nodes=[REG]),   # findable
            _case("u2", on_stack_label=False, seed_nodes=[]),     # unfindable
        ]
        results = {"u1": _result(node=REG, strong=True), "u2": _result(strong=False)}
        conf = M.abstain_calibration(cases, results)
        self.assertEqual(conf["strong_findable"], 1)
        self.assertEqual(conf["abstain_unfindable"], 1)
        self.assertEqual(conf["strong_unfindable"], 0)


class TestComputeAndCompare(unittest.TestCase):
    def test_compute_metrics(self):
        cases = [_case("u1", on_stack_label=False, seed_nodes=[])]
        m = M.compute_metrics(cases, {"u1": _result(node=REG, strong=True)}, corpus_hash="h")
        self.assertEqual(m.offstack_recall, 1.0)
        self.assertEqual(m.evidence_precision, 1.0)
        self.assertEqual(m.n_cases, 1)
        self.assertEqual(m.corpus_hash, "h")

    def test_compare_to_baseline(self):
        cases = [_case("u1", on_stack_label=False, seed_nodes=[])]
        m = M.compute_metrics(cases, {"u1": _result(node=REG, strong=True)})
        with tempfile.TemporaryDirectory() as d:
            base = os.path.join(d, "baseline.json")
            with open(base, "w") as fh:
                json.dump({"offstack_recall": 0.5, "evidence_precision": 0.5}, fh)
            out = M.compare_to_baseline(m, base)
            self.assertEqual(out["status"], "pass")  # 1.0 > 0.5
            with open(base, "w") as fh:
                json.dump({"offstack_recall": 1.0, "evidence_precision": 1.0}, fh)
            worse = M.compute_metrics(cases, {"u1": _result(node="0000deadbeef", strong=False)})
            self.assertEqual(M.compare_to_baseline(worse, base)["status"], "regress")

    def test_compare_missing_baseline(self):
        m = M.compute_metrics([], {})
        self.assertEqual(M.compare_to_baseline(m, "/no/such/file.json")["status"], "no-baseline")


class TestRerunCorpus(unittest.TestCase):
    def test_keys_by_uuid_and_isolates_failures(self):
        async def fake_triage(*, crash, tools_cfg=None, llm_cfg=None, recorder=None, extra=None):
            if crash["uuid"] == "boom":
                raise RuntimeError("triage exploded")
            return _result(strong=False)

        cases = [CorpusCase(uuid="u1"), CorpusCase(uuid="boom")]
        with mock.patch("crashclouseau.agent.triage.run_crash_triage", fake_triage):
            results = asyncio.run(R.rerun_corpus(cases))
        self.assertIsNotNone(results["u1"])
        self.assertIsNone(results["boom"])  # failure degraded, not raised

    def test_sweep_overrides_llm_cfg(self):
        cfg = R._sweep_llm_cfg({"roles": {"skeptic": "sonnet"}, "principal_model": "haiku"})
        self.assertEqual(cfg["roles"]["skeptic"]["model"], "sonnet")
        self.assertEqual(cfg["principal"]["model"], "haiku")


if __name__ == "__main__":
    unittest.main()
