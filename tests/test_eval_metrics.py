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
    CallEdge,
    CallPath,
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


def _result(node=None, strong=False, lead=False, cost=0.1, out_tokens=0, in_tokens=0,
            bug=None, mech_text="m"):
    hunks = []
    candidate = None
    call_path = None
    if node:
        dl = DiffLineCitation(node=node, filename="f.cpp", line=10, side="added", content="x")
        hunks = [DiffHunk(node=node, filename="f.cpp", lines=[dl], citations=[dl])]
        candidate = Candidate(node=node, bug=bug)
    if strong:
        verdict = Verdict(
            decision=Decision.strong_evidence, confidence=Confidence.high,
            mechanism=Claim(statement=mech_text, citations=[_SF]),
            consistency=Claim(statement="c", citations=[_SF]),
        )
    elif lead:
        verdict = Verdict(
            decision=Decision.lead, confidence=Confidence.medium,
            mechanism=Claim(statement=mech_text, citations=[_SF]),
        )
        if not node:
            # A lead naming no changeset ("mechanism lead"): sustained by a cited
            # call-path edge (the lead-anchor gate), but it references no hg node.
            call_path = CallPath(edges=[CallEdge(
                caller_symbol="A::f", callee_symbol="B::g", via="calls-from",
                citations=[_SF])])
    else:
        verdict = Verdict(decision=Decision.abstain, abstain_reason="x")
    return CrashTriageResult(
        num_turns=1, total_cost_usd=cost,
        input_tokens=in_tokens, output_tokens=out_tokens,
        dossier=Dossier(candidate=candidate, hunks=hunks, call_path=call_path, verdict=verdict),
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


class TestLeadPrecision(unittest.TestCase):
    def test_lead_referencing_regressor_is_precise(self):
        case = _case("u1", on_stack_label=False)
        r = M.lead_precision([case], {"u1": _result(node=REG, lead=True)})
        self.assertEqual(r["lead_precision"], 1.0)
        self.assertEqual(r["n_lead"], 1)

    def test_lead_missing_regressor_is_imprecise(self):
        case = _case("u1", on_stack_label=False)
        r = M.lead_precision([case], {"u1": _result(node="0000deadbeef", lead=True)})
        self.assertEqual(r["lead_precision"], 0.0)
        self.assertEqual(r["n_lead"], 1)

    def test_mechanism_lead_without_node_counts_as_miss(self):
        case = _case("u1", on_stack_label=False)
        r = M.lead_precision([case], {"u1": _result(lead=True)})   # no changeset named
        self.assertEqual(r["lead_precision"], 0.0)
        self.assertEqual(r["n_lead"], 1)

    def test_strong_and_abstain_are_not_leads(self):
        case = _case("u1", on_stack_label=False)
        self.assertEqual(
            M.lead_precision([case], {"u1": _result(node=REG, strong=True)})["n_lead"], 0
        )
        self.assertEqual(
            M.lead_precision([case], {"u1": _result(strong=False)})["n_lead"], 0
        )


REGBUG = 2021892


class TestGroundTruthMatching(unittest.TestCase):
    """A run hits the ground truth by NODE or by BUG — the bug match is alias-free, so it
    catches the case where the dossier's mozilla-central rev != the regressor's autoland
    landing rev."""

    def test_bug_match_via_candidate_bug(self):
        # Wrong node, but the candidate names the regressor bug -> hit.
        case = _case("u1", reg="", regressor_nodes=["aaaaaaaaaaaa"],
                     regressor_bugs=[REGBUG], on_stack_label=False)
        r = M.lead_precision([case], {"u1": _result(node="0000deadbeef", bug=REGBUG, lead=True)})
        self.assertEqual(r["lead_precision"], 1.0)

    def test_bug_match_via_mechanism_text(self):
        case = _case("u1", reg="", regressor_bugs=[REGBUG], on_stack_label=False)
        r = M.lead_precision(
            [case], {"u1": _result(lead=True, mech_text="regressed by bug %d" % REGBUG)}
        )
        self.assertEqual(r["lead_precision"], 1.0)

    def test_node_match_against_the_set(self):
        # regressor_nodes has several; the dossier citing any one is a hit.
        case = _case("u1", reg="", regressor_nodes=["aaaaaaaaaaaa", REG], on_stack_label=False)
        r = M.lead_precision([case], {"u1": _result(node=REG, lead=True)})
        self.assertEqual(r["lead_precision"], 1.0)

    def test_no_match_when_neither_node_nor_bug(self):
        case = _case("u1", reg="", regressor_nodes=["aaaaaaaaaaaa"],
                     regressor_bugs=[REGBUG], on_stack_label=False)
        r = M.lead_precision([case], {"u1": _result(node="0000deadbeef", lead=True)})
        self.assertEqual(r["lead_precision"], 0.0)

    def test_offstack_recall_counts_bug_match(self):
        case = _case("u1", reg="", regressor_bugs=[REGBUG],
                     seed_nodes=[], on_stack_label=False)
        r = M.offstack_recall([case], {"u1": _result(node="0000deadbeef", bug=REGBUG, strong=True)})
        self.assertEqual(r["offstack_recall"], 1.0)


class TestCostSummary(unittest.TestCase):
    def test_mean_and_total(self):
        results = {
            "u1": _result(cost=0.2, out_tokens=100, in_tokens=1000),
            "u2": _result(node=REG, strong=True, cost=0.4, out_tokens=300, in_tokens=3000),
        }
        c = M.cost_summary(results)
        self.assertAlmostEqual(c["total_cost_usd"], 0.6)
        self.assertAlmostEqual(c["mean_cost_usd"], 0.3)
        self.assertEqual(c["mean_output_tokens"], 200)
        self.assertEqual(c["mean_input_tokens"], 2000)
        self.assertEqual(c["n_scored"], 2)

    def test_ignores_failed_none_results(self):
        c = M.cost_summary({"u1": _result(cost=0.5), "boom": None})
        self.assertEqual(c["n_scored"], 1)
        self.assertAlmostEqual(c["total_cost_usd"], 0.5)

    def test_empty(self):
        c = M.cost_summary({})
        self.assertEqual(c["n_scored"], 0)
        self.assertEqual(c["mean_cost_usd"], 0.0)


class TestThreeWayCalibration(unittest.TestCase):
    def test_lead_cells(self):
        cases = [
            _case("u1", on_stack_label=True, seed_nodes=[REG]),   # findable
            _case("u2", on_stack_label=False, seed_nodes=[]),     # unfindable
        ]
        results = {"u1": _result(node=REG, lead=True), "u2": _result(lead=True)}
        conf = M.abstain_calibration(cases, results)
        self.assertEqual(conf["lead_findable"], 1)
        self.assertEqual(conf["lead_unfindable"], 1)
        self.assertEqual(conf["strong_findable"], 0)
        self.assertEqual(conf["abstain_unfindable"], 0)


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

    def test_compute_metrics_includes_lead_and_cost(self):
        cases = [_case("u1", on_stack_label=False, seed_nodes=[])]
        results = {"u1": _result(node=REG, lead=True, cost=0.5, out_tokens=250, in_tokens=5000)}
        m = M.compute_metrics(cases, results, corpus_hash="h")
        self.assertEqual(m.n_lead, 1)
        self.assertEqual(m.lead_precision, 1.0)
        self.assertAlmostEqual(m.mean_cost_usd, 0.5)
        self.assertAlmostEqual(m.total_cost_usd, 0.5)
        self.assertEqual(m.mean_output_tokens, 250)

    def test_compare_surfaces_false_abstain_and_cost_info(self):
        # A findable case that abstains is a false abstain; cost rose 0.2 -> 0.5.
        cases = [_case("u1", on_stack_label=True, seed_nodes=[REG])]
        m = M.compute_metrics(cases, {"u1": _result(strong=False, cost=0.5)})
        with tempfile.TemporaryDirectory() as d:
            base = os.path.join(d, "b.json")
            with open(base, "w") as fh:
                json.dump({
                    "offstack_recall": 0.0, "evidence_precision": 0.0, "lead_precision": 0.0,
                    "mean_cost_usd": 0.2, "abstain_calibration": {"abstain_findable": 0},
                }, fh)
            out = M.compare_to_baseline(m, base)
            self.assertEqual(out["status"], "pass")            # no quality regression
            self.assertEqual(out["info"]["abstain_findable"], 1)   # one more false abstain
            self.assertAlmostEqual(out["info"]["mean_cost_usd"], 0.3)

    def test_lead_precision_regression_gates(self):
        cases = [_case("u1", on_stack_label=False, seed_nodes=[])]
        m = M.compute_metrics(cases, {"u1": _result(node="0000deadbeef", lead=True)})
        with tempfile.TemporaryDirectory() as d:
            base = os.path.join(d, "b.json")
            with open(base, "w") as fh:
                json.dump({"offstack_recall": 0.0, "evidence_precision": 0.0,
                           "lead_precision": 1.0}, fh)
            self.assertEqual(M.compare_to_baseline(m, base)["status"], "regress")


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
        cfg = R._sweep_llm_cfg({"roles": {"skeptic": "sonnet"}, "principal_model": "haiku",
                                "effort": "max"})
        self.assertEqual(cfg["roles"]["skeptic"]["model"], "sonnet")
        self.assertEqual(cfg["principal"]["model"], "haiku")
        self.assertEqual(cfg["principal"]["effort"], "max")                      # -> principal
        self.assertTrue(all(r.get("effort") == "max" for r in cfg["roles"].values()))  # -> every role

    def test_sweep_model_and_effort_reach_subagents(self):
        # Regression: build_roles must honor the swept llm_cfg, not the base config, so a
        # sweep's per-role model/effort actually reaches the subagent AgentDefinition.
        from crashclouseau.agent import roles
        cfg = R._sweep_llm_cfg({"principal_model": "opus", "roles": {"skeptic": "opus"},
                                "effort": "max"})
        ad = roles.make_role("skeptic", cfg)
        self.assertEqual(ad.model, "opus")
        self.assertEqual(ad.effort, "max")
        base = roles.make_role("skeptic")            # no llm_cfg -> base config tier, no effort
        self.assertEqual(base.model, "haiku")
        self.assertIsNone(base.effort)

    def test_case_to_crash_carries_raw_crash_for_facts(self):
        # The frozen processed crash must reach the prompt as raw_crash, else eval reruns
        # render an empty "Crash facts" block and can't measure that prompt input.
        from crashclouseau.agent import triage
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "processed_crash.json")
            with open(path, "w") as fh:
                json.dump({
                    "product": "Firefox", "build": "20260708000000", "version": "141.0a1",
                    "moz_crash_reason": "MOZ_CRASH(x)",
                    "json_dump": {
                        "crash_info": {"type": "SIGSEGV", "address": "0x0",
                                       "crashing_thread": 0},
                        "threads": [],
                    },
                }, fh)
            crash = R._case_to_crash(
                CorpusCase(uuid="u1", signature="S::f", channel="nightly",
                           crash_json_path=path)
            )
        self.assertEqual(crash["buildid"], "20260708000000")   # from `build`
        self.assertEqual(crash["raw_crash"]["moz_crash_reason"], "MOZ_CRASH(x)")
        p = triage._user_prompt(crash)
        self.assertIn("Crash facts:", p)
        self.assertIn("MOZ_CRASH_REASON: MOZ_CRASH(x)", p)
        self.assertIn("Crash type: SIGSEGV", p)

    def test_case_to_crash_carries_seed_candidates(self):
        # Frozen candidates must reach the crash dict so the agent gets a seed (closes
        # the no-candidates fidelity gap); the user prompt then lists them.
        case = CorpusCase(uuid="u1", candidates=[{"node": "abc123def456", "bug": 42, "score": 3}])
        crash = R._case_to_crash(case)
        self.assertEqual(crash["candidates"], [{"node": "abc123def456", "bug": 42, "score": 3}])
        from crashclouseau.agent import triage
        self.assertIn("abc123def456", triage._user_prompt(crash))


class TestErroredBucketing(unittest.TestCase):
    """A failed run (None result) buckets as ``errored_*`` and counts in ``n_errored`` —
    it must NOT masquerade as a deliberate abstain (which would flatter the false-abstain
    cell, the exact confound seen in the 2026-07-09 A/B)."""

    def test_failed_run_is_errored_not_abstain(self):
        cases = [
            _case("u1", on_stack_label=False, seed_nodes=[]),    # unfindable
            _case("u2", on_stack_label=True, seed_nodes=[REG]),  # findable
        ]
        conf = M.abstain_calibration(cases, {"u1": None, "u2": None})
        self.assertEqual(conf["errored_unfindable"], 1)
        self.assertEqual(conf["errored_findable"], 1)
        self.assertEqual(conf["abstain_unfindable"], 0)   # NOT counted as abstain
        self.assertEqual(conf["abstain_findable"], 0)

    def test_compute_metrics_counts_errored(self):
        cases = [_case("u1", on_stack_label=False), _case("u2", on_stack_label=False)]
        m = M.compute_metrics(cases, {"u1": _result(node=REG, strong=True), "u2": None})
        self.assertEqual(m.n_errored, 1)

    def test_compare_surfaces_errored_delta(self):
        cases = [_case("u1", on_stack_label=False, seed_nodes=[])]
        m = M.compute_metrics(cases, {"u1": None})   # one failed run
        with tempfile.TemporaryDirectory() as d:
            base = os.path.join(d, "b.json")
            with open(base, "w") as fh:
                json.dump({"offstack_recall": 0.0, "evidence_precision": 0.0,
                           "lead_precision": 0.0, "n_errored": 0}, fh)
            self.assertEqual(M.compare_to_baseline(m, base)["info"]["n_errored"], 1)


if __name__ == "__main__":
    unittest.main()
