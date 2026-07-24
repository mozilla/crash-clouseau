# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Second-opinion fold into the shipped verdict (orchestrator) + gating + schema round-trip.
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_second_opinion_fold
# run_crash_triage / run_second_opinion are never actually called (mocked), so no SDK/CLI.
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau.agent import orchestrator as orch  # noqa: E402
from crashclouseau.agent.result import CrashTriageResult  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    Candidate,
    Claim,
    Confidence,
    DataFlowHypothesis,
    Decision,
    DiffHunk,
    Dossier,
    SearchfoxCitation,
    SecondOpinion,
    StructLayoutCitation,
    Verdict,
)

_SF = SearchfoxCitation(
    permalink="https://searchfox.org/x#1", symbol_id="_Z1", repo="mozilla-central"
)
_SEED = {"uuid": "u-1", "signature": "S", "channel": "nightly", "stack": "#0 f a:1"}

_ENABLED = {"enabled": True, "model": "opus", "effort": "max", "max_turns": 20,
            "min_confidence": 50}


def _lead(confidence=Confidence.medium, candidate=True):
    # A lead needs a cited anchor or the schema demotes it to abstain at construction. When
    # candidate=False we anchor with a cited hunk instead, so it stays a reportable lead with
    # NO candidate (the generator/mechanism-mode path).
    kwargs = (
        {"candidate": Candidate(node="abc123def456", bug=42)} if candidate
        else {"hunks": [DiffHunk(node="n0deadbeef00", filename="f.cpp", citations=[_SF])]}
    )
    return Dossier(
        verdict=Verdict(
            decision=Decision.lead,
            confidence=confidence,
            needinfo_draft="could you take a look at this crash?",
        ),
        **kwargs,
    )


def _strong(candidate=True):
    return Dossier(
        candidate=Candidate(node="abc123def456", bug=42) if candidate else None,
        verdict=Verdict(
            decision=Decision.strong_evidence,
            confidence=Confidence.high,
            mechanism=Claim(statement="m", citations=[_SF]),
            consistency=Claim(statement="c", citations=[_SF]),
        ),
    )


def _so(mode="verify", corroborates=None, confidence="high", mechanism="mech", refutation=""):
    return SecondOpinion(mode=mode, corroborates=corroborates, confidence=confidence,
                         mechanism=mechanism, refutation=refutation)


class TestFoldBoost(unittest.TestCase):
    def test_corroborated_high_raises_medium_lead_to_probable(self):
        d = _lead(Confidence.medium)
        orch._fold_second_opinion(d, _so(corroborates=True, confidence="high"), _SEED)
        self.assertEqual(d.verdict.confidence, Confidence.probable)
        self.assertTrue(d.corroborations["second_opinion_corroborated"])
        self.assertIsNotNone(d.second_opinion)          # stored regardless
        self.assertTrue(d.second_opinion.corroborates)

    def test_corroborated_medium_confidence_also_boosts(self):
        d = _lead(Confidence.medium)
        orch._fold_second_opinion(d, _so(corroborates=True, confidence="medium"), _SEED)
        self.assertEqual(d.verdict.confidence, Confidence.probable)

    def test_corroborated_low_confidence_is_treated_as_unsure(self):
        # A low-confidence "yes" is not a strong enough agreement to move the band or flag.
        d = _lead(Confidence.medium)
        orch._fold_second_opinion(d, _so(corroborates=True, confidence="low"), _SEED)
        self.assertEqual(d.verdict.confidence, Confidence.medium)
        self.assertNotIn("second_opinion_corroborated", d.corroborations)
        self.assertIsNotNone(d.second_opinion)          # still surfaced in the panel

    def test_probable_lead_stays_probable_but_flagged(self):
        d = _lead(Confidence.probable)
        orch._fold_second_opinion(d, _so(corroborates=True, confidence="high"), _SEED)
        self.assertEqual(d.verdict.confidence, Confidence.probable)
        self.assertTrue(d.corroborations["second_opinion_corroborated"])

    def test_strong_evidence_not_boosted(self):
        d = _strong()
        orch._fold_second_opinion(d, _so(corroborates=True, confidence="high"), _SEED)
        self.assertEqual(d.verdict.decision, Decision.strong_evidence)
        self.assertEqual(d.verdict.confidence, Confidence.high)
        self.assertTrue(d.corroborations["second_opinion_corroborated"])

    def test_precision_downgraded_lead_is_not_reinflated(self):
        # A lead that a precision gate (SF-3 / exposer) demoted from strong-evidence carries
        # downgraded_from_strong; a corroborating SO must NOT re-raise it to probable (an
        # exposer "is related" too). It records the agreement but keeps the band suppressed.
        d = _lead(Confidence.medium)
        d.corroborations = {"downgraded_from_strong": True}
        orch._fold_second_opinion(d, _so(corroborates=True, confidence="high"), _SEED)
        self.assertEqual(d.verdict.confidence, Confidence.medium)      # NOT probable
        self.assertTrue(d.corroborations["second_opinion_corroborated"])


class TestFoldRefute(unittest.TestCase):
    def test_confident_refute_downgrades_strong_to_lead_with_anchor(self):
        d = _strong(candidate=True)                     # candidate = the surviving anchor
        orch._fold_second_opinion(
            d, _so(corroborates=False, confidence="high", mechanism="",
                   refutation="the assert is debug-only"), _SEED)
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertTrue(d.corroborations["second_opinion_refuted"])

    def test_confident_refute_abstains_strong_without_anchor(self):
        d = _strong(candidate=False)                    # nothing cited to hand over
        orch._fold_second_opinion(
            d, _so(corroborates=False, confidence="high"), _SEED)
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertTrue(d.corroborations["second_opinion_refuted"])

    def test_confident_refute_clamps_probable_lead_to_medium(self):
        d = _lead(Confidence.probable)
        orch._fold_second_opinion(
            d, _so(corroborates=False, confidence="high"), _SEED)
        self.assertEqual(d.verdict.decision, Decision.lead)   # recall-safe: still a lead
        self.assertEqual(d.verdict.confidence, Confidence.medium)
        self.assertTrue(d.corroborations["second_opinion_refuted"])

    def test_confident_refute_keeps_medium_lead_reportable(self):
        # A medium lead cannot drop further (never below a reportable lead), but is flagged.
        d = _lead(Confidence.medium)
        orch._fold_second_opinion(
            d, _so(corroborates=False, confidence="high"), _SEED)
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.verdict.confidence, Confidence.medium)
        self.assertTrue(d.corroborations["second_opinion_refuted"])

    def test_non_confident_refute_leaves_verdict(self):
        # corroborates=False but only medium confidence -> uncertain, no change / no flag.
        d = _lead(Confidence.probable)
        orch._fold_second_opinion(
            d, _so(corroborates=False, confidence="medium"), _SEED)
        self.assertEqual(d.verdict.confidence, Confidence.probable)
        self.assertNotIn("second_opinion_refuted", d.corroborations)


class TestFoldNoOp(unittest.TestCase):
    def test_mechanism_mode_never_moves_band(self):
        d = _lead(Confidence.medium)
        orch._fold_second_opinion(
            d, _so(mode="mechanism", corroborates=None, confidence="high"), _SEED)
        self.assertEqual(d.verdict.confidence, Confidence.medium)
        self.assertEqual(d.corroborations, {})
        self.assertIsNotNone(d.second_opinion)          # still surfaced
        self.assertEqual(d.second_opinion.mode, "mechanism")

    def test_none_second_opinion_is_noop(self):
        d = _lead(Confidence.medium)
        orch._fold_second_opinion(d, None, _SEED)
        self.assertEqual(d.verdict.confidence, Confidence.medium)
        self.assertIsNone(d.second_opinion)

    def test_no_verdict_is_noop(self):
        # No verdict to move: the band logic must not run (and must not raise). The SO is still
        # stored authoritatively, but there is no confidence band to change.
        d = Dossier(verdict=None)
        orch._fold_second_opinion(d, _so(corroborates=True), _SEED)   # must not raise
        self.assertIsNone(d.verdict)

    def test_model_injected_second_opinion_cleared_when_none(self):
        # second_opinion is a defined Dossier field, so the primary model could inject one.
        # The fold is authoritative: with no real SO it clears the field (blindness guarantee).
        d = _lead(Confidence.medium)
        d.second_opinion = SecondOpinion(mode="verify", corroborates=True, confidence="high",
                                         mechanism="the model fabricated this")
        orch._fold_second_opinion(d, None, _SEED)
        self.assertIsNone(d.second_opinion)                           # injected value cleared
        self.assertEqual(d.verdict.confidence, Confidence.medium)     # band untouched


class TestApplyGatesFold(unittest.TestCase):
    """The fold reaches the SHIPPED verdict through apply_deterministic_gates."""

    def _result(self, dossier):
        return CrashTriageResult(num_turns=1, total_cost_usd=0.1, result="ok", dossier=dossier)

    def test_gates_fold_corroboration_boosts_lead(self):
        r = self._result(_lead(Confidence.medium))
        orch.apply_deterministic_gates(
            r, {**_SEED, "is_offstack": False},
            second_opinion=_so(corroborates=True, confidence="high"))
        self.assertEqual(r.dossier.verdict.confidence, Confidence.probable)
        self.assertTrue(r.dossier.corroborations["second_opinion_corroborated"])

    def test_gates_without_second_opinion_leave_lead(self):
        r = self._result(_lead(Confidence.medium))
        orch.apply_deterministic_gates(r, {**_SEED, "is_offstack": False})
        self.assertEqual(r.dossier.verdict.confidence, Confidence.medium)
        self.assertIsNone(r.dossier.second_opinion)

    def test_gates_clear_model_injected_second_opinion(self):
        d = _lead(Confidence.medium)
        d.second_opinion = SecondOpinion(mode="verify", corroborates=True, confidence="high")
        r = self._result(d)
        orch.apply_deterministic_gates(r, {**_SEED, "is_offstack": False})  # no SO passed
        self.assertIsNone(r.dossier.second_opinion)

    def test_exposer_downgraded_lead_not_reinflated_end_to_end(self):
        # Finding #1, end to end: a strong-evidence verdict + a poison fault -> the exposer
        # classifier downgrades it to a medium lead (downgraded_from_strong); a corroborating
        # SO must NOT re-inflate it to probable through the full gate pipeline.
        r = self._result(_strong(candidate=True))
        seed = {**_SEED, "is_offstack": False,
                "raw_crash": {"json_dump": {"crash_info": {"address": "0xe5e5e5e5"}}}}
        orch.apply_deterministic_gates(
            r, seed, second_opinion=_so(corroborates=True, confidence="high"))
        self.assertEqual(r.dossier.verdict.decision, Decision.lead)      # exposer downgrade
        self.assertEqual(r.dossier.verdict.confidence, Confidence.medium)  # NOT re-inflated
        self.assertTrue(r.dossier.corroborations["downgraded_from_strong"])
        self.assertTrue(r.dossier.corroborations["exposer_strong"])
        self.assertTrue(r.dossier.corroborations["second_opinion_corroborated"])


class TestMaybeRunSecondOpinion(unittest.TestCase):
    def _result(self, dossier):
        return CrashTriageResult(num_turns=1, total_cost_usd=0.1, result="ok", dossier=dossier)

    def test_disabled_returns_none_without_calling(self):
        r = self._result(_lead())
        disabled = {**_ENABLED, "enabled": False}
        with mock.patch.object(orch.config, "get_agent_second_opinion", return_value=disabled):
            with mock.patch("crashclouseau.agent.second_opinion.run_second_opinion") as m:
                self.assertIsNone(orch._maybe_run_second_opinion(r, _SEED))
                m.assert_not_called()

    def test_abstain_verdict_skips(self):
        v = Verdict(decision=Decision.abstain, abstain_reason="x")
        r = self._result(Dossier(verdict=v))
        with mock.patch.object(orch.config, "get_agent_second_opinion", return_value=_ENABLED):
            with mock.patch("crashclouseau.agent.second_opinion.run_second_opinion") as m:
                self.assertIsNone(orch._maybe_run_second_opinion(r, _SEED))
                m.assert_not_called()

    def test_rung_below_min_confidence_skips(self):
        r = self._result(_lead(Confidence.medium))                     # 50
        cfg = {**_ENABLED, "min_confidence": 60}
        with mock.patch.object(orch.config, "get_agent_second_opinion", return_value=cfg):
            with mock.patch("crashclouseau.agent.second_opinion.run_second_opinion") as m:
                self.assertIsNone(orch._maybe_run_second_opinion(r, _SEED))
                m.assert_not_called()

    def test_enabled_reported_lead_runs_with_candidate(self):
        r = self._result(_lead(Confidence.medium, candidate=True))
        seen = {}

        async def _fake(crash, candidate=None):
            seen["candidate"] = candidate
            seen["crash"] = crash
            return _so(corroborates=True, confidence="high")

        with mock.patch.object(orch.config, "get_agent_second_opinion", return_value=_ENABLED):
            with mock.patch("crashclouseau.agent.second_opinion.run_second_opinion", _fake):
                so = orch._maybe_run_second_opinion(r, _SEED)
        self.assertIsNotNone(so)
        self.assertTrue(so.corroborates)
        self.assertEqual(seen["candidate"], {"node": "abc123def456", "bug": 42})
        self.assertIs(seen["crash"], _SEED)

    def test_enabled_no_candidate_runs_generator_mode(self):
        r = self._result(_lead(Confidence.medium, candidate=False))
        seen = {}

        async def _fake(crash, candidate=None):
            seen["candidate"] = candidate
            return _so(mode="mechanism", corroborates=None)

        with mock.patch.object(orch.config, "get_agent_second_opinion", return_value=_ENABLED):
            with mock.patch("crashclouseau.agent.second_opinion.run_second_opinion", _fake):
                so = orch._maybe_run_second_opinion(r, _SEED)
        self.assertIsNotNone(so)
        self.assertIsNone(seen["candidate"])            # generator/mechanism mode

    def test_corroboration_promotable_sub_threshold_lead_still_runs(self):
        # Finding #2: a raw medium lead (50) below a min_confidence of 60 is NOT skipped when a
        # deterministic corroboration (fault-offset match) will promote it to probable (70) —
        # that promoted lead is reported and must still get a second opinion.
        d = Dossier(
            candidate=Candidate(node="abc123def456", bug=42),
            data_flow=DataFlowHypothesis(
                summary="null-deref of mLength", operation="null",
                citations=[StructLayoutCitation(type_name="T", field="mLength", offset=8)],
            ),
            verdict=Verdict(decision=Decision.lead, confidence=Confidence.medium,
                            needinfo_draft="?"),
        )
        r = self._result(d)
        seed = {**_SEED, "raw_crash": {"json_dump": {"crash_info": {"address": "0x8"}}}}
        cfg = {**_ENABLED, "min_confidence": 60}
        called = {"n": 0}

        async def _fake(crash, candidate=None):
            called["n"] += 1
            return _so(corroborates=True, confidence="high")

        with mock.patch.object(orch.config, "get_agent_second_opinion", return_value=cfg):
            with mock.patch("crashclouseau.agent.second_opinion.run_second_opinion", _fake):
                so = orch._maybe_run_second_opinion(r, seed)
        self.assertIsNotNone(so)
        self.assertEqual(called["n"], 1)

    def test_run_failure_returns_none(self):
        r = self._result(_lead(Confidence.medium))

        async def _boom(crash, candidate=None):
            raise RuntimeError("SO exploded")

        with mock.patch.object(orch.config, "get_agent_second_opinion", return_value=_ENABLED):
            with mock.patch("crashclouseau.agent.second_opinion.run_second_opinion", _boom):
                self.assertIsNone(orch._maybe_run_second_opinion(r, _SEED))


class TestSchemaRoundTrip(unittest.TestCase):
    def test_second_opinion_survives_db_json_round_trip(self):
        d = Dossier(
            verdict=Verdict(decision=Decision.lead, confidence=Confidence.probable,
                            needinfo_draft="?"),
            second_opinion=SecondOpinion(mode="verify", corroborates=True,
                                         confidence="high", mechanism="UAF of mFoo",
                                         cost_usd=0.42),
        )
        payload = d.model_dump(mode="json")
        self.assertEqual(payload["second_opinion"]["mechanism"], "UAF of mFoo")
        back = Dossier.model_validate(payload)
        self.assertEqual(back.second_opinion.mode, "verify")
        self.assertTrue(back.second_opinion.corroborates)
        self.assertEqual(back.second_opinion.cost_usd, 0.42)

    def test_old_dossier_without_field_validates_to_none(self):
        old = {"verdict": {"decision": "lead", "confidence": "medium",
                           "needinfo_draft": "?"}}
        back = Dossier.model_validate(old)
        self.assertIsNone(back.second_opinion)

    def test_parse_and_validate_strips_model_injected_internal_fields(self):
        # corroborations + second_opinion are computed OUTSIDE the LLM; a model that injects
        # them into its handoff JSON must not have them survive the parse boundary (they'd
        # spoof a chip / suppress the boost / fake an independent second opinion).
        from crashclouseau.agent.schema import parse_and_validate
        obj = {
            "candidate": {"node": "abc123def456", "bug": 1},
            "verdict": {"decision": "lead", "confidence": "medium", "needinfo_draft": "?"},
            "corroborations": {"downgraded_from_strong": True,
                               "fault_address_offset_match": True},
            "second_opinion": {"mode": "verify", "corroborates": True, "confidence": "high"},
        }
        d = parse_and_validate(obj)
        self.assertEqual(d.corroborations, {})            # injected flags stripped
        self.assertIsNone(d.second_opinion)               # injected SO stripped
        self.assertEqual(d.verdict.decision, Decision.lead)  # the real verdict is untouched


if __name__ == "__main__":
    unittest.main()
