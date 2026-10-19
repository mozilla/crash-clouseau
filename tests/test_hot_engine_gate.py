# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Suppression and reopening rules for old engine signatures."""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import config, corroborations, models, sigtrend  # noqa: E402
from crashclouseau.agent import orchestrator as orch  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    AbstainKind,
    Candidate,
    Claim,
    Confidence,
    Decision,
    Dossier,
    SearchfoxCitation,
    Verdict,
)

_SF = SearchfoxCitation(permalink="https://searchfox.org/x#547", symbol_id="_Z1",
                        repo="mozilla-central")
_ON = {"enabled": True, "paths": ("js/src/",)}


def _seed(**over):
    s = {"uuid": "f1d30bd8", "signature": "(anonymous namespace)::BytecodeParser::simulateOp",
         "channel": "nightly", "product": "Firefox", "buildid": "20261019073400",
         "signature_first_seen_ever": "20190619235516", "signature_trend": {},
         "frames": [{"stackpos": 0, "filename": "js/src/vm/BytecodeUtil.cpp", "line": 856},
                    {"stackpos": 1, "filename": "js/src/vm/BytecodeUtil.cpp", "line": 2425}]}
    s.update(over)
    return s


def _actionable():
    return Dossier(
        candidate=Candidate(node="8cde5fbfa29f", bug=1670481),
        verdict=Verdict(decision=Decision.actionable, confidence=Confidence.probable,
                        title="MOZ_RELEASE_ASSERT in BytecodeParser::simulateOp",
                        mechanism=Claim(statement="the release assert fires", citations=[_SF])))


class _Base(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(config, "get_agent_hot_engine", return_value=dict(_ON)),
                  mock.patch.object(sigtrend, "rate_unchanged", return_value=True),
                  mock.patch.object(orch, "_spike_reopens", return_value=False),
                  mock.patch.object(orch, "_incomplete_fix_may_file", return_value=False)):
            p.start()
            self.addCleanup(p.stop)


class TestHotEngineGate(_Base):
    def _gate(self, seed=None, dossier=None):
        d = dossier or _actionable()
        orch._apply_hot_engine_gate(d, seed or _seed())
        return d

    def test_the_1670481_crash_is_suppressed(self):
        d = self._gate()
        v = d.verdict
        self.assertEqual((v.decision, v.confidence, v.abstain_kind),
                         (Decision.abstain, Confidence.low, AbstainKind.hardware))
        self.assertIn("js/src/vm/BytecodeUtil.cpp", v.abstain_reason)
        self.assertIn("first seen 2678 days before this build", v.abstain_reason)
        self.assertEqual(v.mechanism.statement, "the release assert fires")
        self.assertTrue(d.corroborations["hot_engine_suppressed"])
        sigtrend.rate_unchanged.assert_called_once_with(
            "Firefox", "nightly", "(anonymous namespace)::BytecodeParser::simulateOp")

    def test_a_rate_change_spares_it(self):
        with mock.patch.object(orch, "_frequency_regression_reasons", return_value=["rate"]):
            self.assertEqual(self._gate().verdict.decision, Decision.actionable)
        orch._spike_reopens.return_value = True
        d = self._gate()
        self.assertEqual(d.verdict.decision, Decision.actionable)
        self.assertNotIn("hot_engine_suppressed", d.corroborations)

    def test_an_unmeasured_rate_is_not_a_flat_one(self):
        for answer in (None, False):
            with self.subTest(answer=answer):
                sigtrend.rate_unchanged.return_value = answer
                self.assertEqual(self._gate().verdict.decision, Decision.actionable)
        sigtrend.rate_unchanged.side_effect = RuntimeError("db")
        with mock.patch.object(orch, "_rollback_quietly") as rollback:
            self.assertEqual(self._gate().verdict.decision, Decision.actionable)
        rollback.assert_called_once_with()

    def test_a_new_or_undated_signature_is_not_touched(self):
        for ever in ("20261013073400", None):
            with self.subTest(ever=ever):
                d = self._gate(_seed(signature_first_seen_ever=ever))
                self.assertEqual(d.verdict.decision, Decision.actionable)
        sigtrend.rate_unchanged.assert_not_called()
        orch._spike_reopens.assert_not_called()

    def test_only_the_first_frame_with_a_file_counts(self):
        outside = [{"filename": "dom/media/mediasource/MediaSource.cpp"},
                   {"filename": "js/src/vm/Interpreter.cpp"}]
        self.assertEqual(self._gate(_seed(frames=outside)).verdict.decision, Decision.actionable)
        unnamed = [{"filename": ""}, {"filename": None}, {"filename": "js/src/jit/BaselineIC.cpp"}]
        self.assertEqual(self._gate(_seed(frames=unnamed)).verdict.decision, Decision.abstain)
        self.assertEqual(self._gate(_seed(frames=[])).verdict.decision, Decision.actionable)

    def test_an_oom_is_left_to_its_own_gate(self):
        seed = _seed(signature="OOM | unknown | js::AutoEnterOOMUnsafeRegion::crash_impl")
        self.assertEqual(self._gate(seed).verdict.decision, Decision.actionable)

    def test_abstains_and_the_kill_switch(self):
        abstain = Dossier(verdict=Verdict(decision=Decision.abstain, confidence=Confidence.low,
                                          abstain_reason="nothing in the window"))
        self.assertEqual(self._gate(dossier=abstain).verdict.abstain_reason,
                         "nothing in the window")
        config.get_agent_hot_engine.return_value = dict(_ON, enabled=False)
        self.assertEqual(self._gate().verdict.decision, Decision.actionable)


class TestBeforeTheRun(_Base):
    def test_a_flat_hot_signature_is_not_analysed(self):
        result = orch._hot_engine_decided_before_run(_seed())
        self.assertEqual((result.num_turns, result.total_cost_usd), (0, 0.0))
        v = result.dossier.verdict
        self.assertEqual((v.decision, v.abstain_kind), (Decision.abstain, AbstainKind.hardware))
        self.assertIn("js/src/vm/BytecodeUtil.cpp", v.abstain_reason)
        self.assertTrue(result.dossier.corroborations["hot_engine_suppressed"])

    def test_anything_else_runs(self):
        sigtrend.rate_unchanged.return_value = None
        self.assertIsNone(orch._hot_engine_decided_before_run(_seed()))
        sigtrend.rate_unchanged.return_value = True
        orch._incomplete_fix_may_file.return_value = True
        self.assertIsNone(orch._hot_engine_decided_before_run(_seed()))

    def test_the_cluster_stays_open_so_a_later_rise_is_seen(self):
        self.assertIn("hot_engine_suppressed", corroborations.suppressions())
        self.assertIn("hot_engine_suppressed", models._INSTANCE_SUPPRESSED)


class TestItRunsAfterTheFold(_Base):
    def test_a_second_opinion_boost_cannot_rescue_it(self):
        from crashclouseau.agent.result import CrashTriageResult

        def boost(dossier, so, seed, status=None):
            dossier.verdict = dossier.verdict.model_copy(
                update={"confidence": Confidence.probable})

        with mock.patch.object(orch, "_fold_second_opinion", side_effect=boost), \
                mock.patch.object(orch, "_apply_compiled_out_gate"):
            result = CrashTriageResult(num_turns=1, total_cost_usd=0.1, result="ok",
                                       dossier=_actionable())
            orch.apply_deterministic_gates(result, _seed())
        self.assertEqual(result.dossier.verdict.decision, Decision.abstain)
        self.assertTrue(result.dossier.corroborations["hot_engine_suppressed"])


class TestTheConfig(unittest.TestCase):
    def test_defaults_and_the_kill_switch(self):
        with mock.patch.object(config, "get_agent", return_value={}):
            self.assertEqual(config.get_agent_hot_engine(), _ON)
            with mock.patch.dict(os.environ, {"HOT_ENGINE_GATE_ENABLED": "0"}):
                self.assertFalse(config.get_agent_hot_engine()["enabled"])


if __name__ == "__main__":
    unittest.main()
