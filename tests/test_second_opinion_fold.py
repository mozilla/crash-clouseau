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

from crashclouseau import config  # noqa: E402
from crashclouseau.agent import orchestrator as orch  # noqa: E402
from crashclouseau.agent.result import CrashTriageResult  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    CONFIDENCE_SCORE,
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
    parse_and_validate,
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
                self.assertEqual(
                    orch._maybe_run_second_opinion(r, _SEED), (None, "skipped_disabled")
                )
                m.assert_not_called()

    def test_abstain_verdict_skips(self):
        v = Verdict(decision=Decision.abstain, abstain_reason="x")
        r = self._result(Dossier(verdict=v))
        with mock.patch.object(orch.config, "get_agent_second_opinion", return_value=_ENABLED):
            with mock.patch("crashclouseau.agent.second_opinion.run_second_opinion") as m:
                self.assertEqual(
                    orch._maybe_run_second_opinion(r, _SEED), (None, "skipped_abstain")
                )
                m.assert_not_called()

    def test_missing_verdict_skips(self):
        r = self._result(Dossier(verdict=None))
        with mock.patch.object(orch.config, "get_agent_second_opinion", return_value=_ENABLED):
            with mock.patch("crashclouseau.agent.second_opinion.run_second_opinion") as m:
                self.assertEqual(
                    orch._maybe_run_second_opinion(r, _SEED), (None, "skipped_no_verdict")
                )
                m.assert_not_called()

    def test_rung_below_min_confidence_skips(self):
        r = self._result(_lead(Confidence.medium))                     # 50
        cfg = {**_ENABLED, "min_confidence": 60}
        with mock.patch.object(orch.config, "get_agent_second_opinion", return_value=cfg):
            with mock.patch("crashclouseau.agent.second_opinion.run_second_opinion") as m:
                self.assertEqual(
                    orch._maybe_run_second_opinion(r, _SEED),
                    (None, "skipped_below_threshold"),
                )
                m.assert_not_called()

    def test_boost_floor_keeps_the_fold_from_being_one_directional(self):
        # Lowering min_confidence to 25 bought MEASUREMENT coverage of the weakest reported
        # leads. It must not also let them be re-ranked: at `low` the refute clamp is a no-op
        # (it never goes below a reportable lead), so a boost there could only move them UP —
        # two rungs, 0.50 -> 0.97 p_worth. Both directions must be inert at the bottom rung.
        boosted = _lead(Confidence.low)
        orch._fold_second_opinion(boosted, _so(corroborates=True, confidence="high"), _SEED,
                                  status="ok")
        self.assertEqual(boosted.verdict.confidence, Confidence.low)
        self.assertTrue(boosted.corroborations["second_opinion_corroborated"])

        refuted = _lead(Confidence.low)
        orch._fold_second_opinion(refuted, _so(corroborates=False, confidence="high"), _SEED,
                                  status="ok")
        self.assertEqual(refuted.verdict.confidence, Confidence.low)

    def test_medium_and_above_still_boosts(self):
        # The floor must not break the behaviour that shipped: a medium lead still rises.
        d = _lead(Confidence.medium)
        orch._fold_second_opinion(d, _so(corroborates=True, confidence="medium"), _SEED,
                                  status="ok")
        self.assertEqual(d.verdict.confidence, Confidence.probable)

    def test_boost_floor_is_at_or_above_medium(self):
        cfg = config.get_agent_second_opinion()
        self.assertGreaterEqual(
            cfg["min_boost_confidence"],
            int(round(CONFIDENCE_SCORE[Confidence.medium] * 100)),
        )
        # ...and strictly above the measurement threshold, or the asymmetry is back.
        self.assertGreater(cfg["min_boost_confidence"], cfg["min_confidence"])

    def test_weakest_reported_lead_is_eligible_under_the_REAL_config(self):
        # Pins the BEHAVIOUR of the min_confidence 50 -> 25 change, not just the constant. A
        # review pass showed the eligibility check could be mutated back to an effective floor
        # of 50 with the whole suite still green, because every other test here patches the
        # config. This one drives the real `config.get_agent_second_opinion()`.
        r = self._result(_lead(Confidence.low))
        called = {"n": 0}

        async def _fake(crash, candidate=None):
            called["n"] += 1
            return _so(corroborates=True, confidence="high")

        with mock.patch.dict(os.environ, {"SECOND_OPINION_ENABLED": "1"}):
            with mock.patch("crashclouseau.agent.second_opinion.run_second_opinion", _fake):
                so, status = orch._maybe_run_second_opinion(r, _SEED)
        self.assertEqual(status, "ok")           # NOT skipped_below_threshold
        self.assertIsNotNone(so)
        self.assertEqual(called["n"], 1)

    def test_default_min_confidence_covers_the_weakest_reported_lead(self):
        # There is no separate report gate — ANY lead is shown — so the shipped default must
        # reach `low` (25), the lowest rung a lead can hold. At the old default of 50 the
        # weakest reported leads silently got no second opinion at all.
        cfg = config.get_agent_second_opinion()
        self.assertLessEqual(
            cfg["min_confidence"],
            int(round(CONFIDENCE_SCORE[Confidence.low] * 100)),
        )

    def test_enabled_reported_lead_runs_with_candidate(self):
        r = self._result(_lead(Confidence.medium, candidate=True))
        seen = {}

        async def _fake(crash, candidate=None):
            seen["candidate"] = candidate
            seen["crash"] = crash
            return _so(corroborates=True, confidence="high")

        with mock.patch.object(orch.config, "get_agent_second_opinion", return_value=_ENABLED):
            with mock.patch("crashclouseau.agent.second_opinion.run_second_opinion", _fake):
                so, status = orch._maybe_run_second_opinion(r, _SEED)
        self.assertIsNotNone(so)
        self.assertEqual(status, "ok")
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
                so, status = orch._maybe_run_second_opinion(r, _SEED)
        self.assertIsNotNone(so)
        self.assertEqual(status, "ok")
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
                so, status = orch._maybe_run_second_opinion(r, seed)
        self.assertIsNotNone(so)
        self.assertEqual(status, "ok")
        self.assertEqual(called["n"], 1)

    def test_run_failure_returns_none(self):
        r = self._result(_lead(Confidence.medium))

        async def _boom(crash, candidate=None):
            raise RuntimeError("SO exploded")

        with mock.patch.object(orch.config, "get_agent_second_opinion", return_value=_ENABLED):
            with mock.patch("crashclouseau.agent.second_opinion.run_second_opinion", _boom):
                self.assertEqual(
                    orch._maybe_run_second_opinion(r, _SEED), (None, "failed")
                )

    def test_silent_none_from_the_run_is_failed_not_skipped(self):
        # ``run_second_opinion`` swallows its own errors and returns None (errored / empty /
        # unparseable). For an ELIGIBLE lead that is a prod break, and it must be
        # distinguishable from a skip — otherwise a broken pass reads as "not applicable".
        r = self._result(_lead(Confidence.medium))

        async def _empty(crash, candidate=None):
            return None

        with mock.patch.object(orch.config, "get_agent_second_opinion", return_value=_ENABLED):
            with mock.patch("crashclouseau.agent.second_opinion.run_second_opinion", _empty):
                self.assertEqual(
                    orch._maybe_run_second_opinion(r, _SEED), (None, "failed")
                )


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

    def test_parse_and_validate_strips_injected_status_and_raw_verdict(self):
        # Same guarantee for the two new outside-the-LLM fields: a forged "ok" status would
        # hide a broken pass, and a forged raw_verdict would make the gates look like a no-op.
        obj = {
            "candidate": {"node": "abc123def456", "bug": 1},
            "verdict": {"decision": "lead", "confidence": "medium", "needinfo_draft": "?"},
            "second_opinion_status": "ok",
            "raw_verdict": {"decision": "lead", "confidence": "probable",
                            "needinfo_draft": "?"},
        }
        d = parse_and_validate(obj)
        self.assertIsNone(d.second_opinion_status)
        self.assertIsNone(d.raw_verdict)
        self.assertEqual(d.verdict.confidence, Confidence.medium)

    def test_new_fields_survive_db_json_round_trip(self):
        d = _lead(Confidence.medium)                       # needs a cited anchor to stay a lead
        d.raw_verdict = Verdict(decision=Decision.lead, confidence=Confidence.probable,
                                needinfo_draft="?")
        d.second_opinion_status = "failed"
        back = Dossier.model_validate(d.model_dump(mode="json"))
        self.assertEqual(back.second_opinion_status, "failed")
        self.assertEqual(back.raw_verdict.confidence, Confidence.probable)
        self.assertEqual(back.verdict.confidence, Confidence.medium)

    def test_old_dossier_without_new_fields_validates_to_none(self):
        back = Dossier.model_validate(
            {"verdict": {"decision": "lead", "confidence": "medium", "needinfo_draft": "?"}}
        )
        self.assertIsNone(back.raw_verdict)
        self.assertIsNone(back.second_opinion_status)


class TestRawVerdictSnapshot(unittest.TestCase):
    """#4: the gates' NET effect must be auditable — a clamp has to be distinguishable from
    a no-op on an already-medium lead."""

    def _result(self, dossier):
        return CrashTriageResult(num_turns=1, total_cost_usd=0.1, result="ok", dossier=dossier)

    def test_snapshot_captures_the_pre_gate_verdict_when_the_so_clamps(self):
        r = self._result(_lead(Confidence.probable))
        orch.apply_deterministic_gates(
            r, {**_SEED, "is_offstack": False},
            second_opinion=_so(corroborates=False, confidence="high"),
            second_opinion_status="ok",
        )
        # Shipped clamped to medium; the raw snapshot still shows it came in at probable.
        self.assertEqual(r.dossier.verdict.confidence, Confidence.medium)
        self.assertEqual(r.dossier.raw_verdict.confidence, Confidence.probable)

    def test_snapshot_equals_shipped_when_no_gate_moves_it(self):
        r = self._result(_lead(Confidence.medium))
        orch.apply_deterministic_gates(
            r, {**_SEED, "is_offstack": False},
            second_opinion=_so(corroborates=False, confidence="high"),
            second_opinion_status="ok",
        )
        # A confident refute on an ALREADY-medium lead is a no-op — the pair proves it.
        self.assertEqual(r.dossier.verdict.confidence, Confidence.medium)
        self.assertEqual(r.dossier.raw_verdict.confidence, Confidence.medium)

    def test_snapshot_does_not_alias_the_shipped_verdict(self):
        r = self._result(_lead(Confidence.medium))
        orch.apply_deterministic_gates(
            r, {**_SEED, "is_offstack": False},
            second_opinion=_so(corroborates=True, confidence="high"),
            second_opinion_status="ok",
        )
        self.assertEqual(r.dossier.verdict.confidence, Confidence.probable)   # boosted
        self.assertEqual(r.dossier.raw_verdict.confidence, Confidence.medium)  # snapshot kept
        self.assertIsNot(r.dossier.raw_verdict, r.dossier.verdict)


class TestAppliedMoveIsDistinguishable(unittest.TestCase):
    """A review pass proved `raw_verdict` alone cannot attribute a band move to the SO: the
    corroboration gate runs FIRST and can move the same lead, so a clamp-after-bump persists
    raw == shipped == `medium`, byte-identical to the SO doing nothing. The applied-move flags
    (`second_opinion_boosted` / `second_opinion_clamped`) resolve it, because
    `second_opinion_corroborated` / `_refuted` only record the SO's OPINION."""

    def _result(self, dossier):
        return CrashTriageResult(num_turns=1, total_cost_usd=0.1, result="ok", dossier=dossier)

    def _corroborated_lead(self, confidence):
        # A fault-address <-> struct-offset match makes _apply_corroboration_gate bump the lead
        # to `probable` BEFORE the fold runs.
        return Dossier(
            candidate=Candidate(node="abc123def456", bug=42),
            data_flow=DataFlowHypothesis(
                summary="null-deref of mLength", operation="null",
                citations=[StructLayoutCitation(type_name="T", field="mLength", offset=8)],
            ),
            verdict=Verdict(decision=Decision.lead, confidence=confidence, needinfo_draft="?"),
        )

    _FAULT_SEED = {**_SEED, "is_offstack": False,
                   "raw_crash": {"json_dump": {"crash_info": {"address": "0x8"}}}}

    def test_clamp_after_a_corroboration_bump_is_still_visible(self):
        r = self._result(self._corroborated_lead(Confidence.medium))
        orch.apply_deterministic_gates(
            r, self._FAULT_SEED,
            second_opinion=_so(corroborates=False, confidence="high"),
            second_opinion_status="ok",
        )
        d = r.dossier
        # The raw/shipped pair collapses -- both `medium` -- exactly as the review showed.
        self.assertEqual(d.raw_verdict.confidence, Confidence.medium)
        self.assertEqual(d.verdict.confidence, Confidence.medium)
        # ...so the applied-move flag is what proves the clamp fired.
        self.assertTrue(d.corroborations["second_opinion_clamped"])

    def test_no_op_refute_does_not_claim_a_clamp(self):
        r = self._result(_lead(Confidence.medium))       # already medium, nothing to clamp
        orch.apply_deterministic_gates(
            r, {**_SEED, "is_offstack": False},
            second_opinion=_so(corroborates=False, confidence="high"),
            second_opinion_status="ok",
        )
        d = r.dossier
        self.assertEqual(d.raw_verdict.confidence, Confidence.medium)
        self.assertEqual(d.verdict.confidence, Confidence.medium)
        self.assertTrue(d.corroborations["second_opinion_refuted"])       # opinion recorded
        self.assertNotIn("second_opinion_clamped", d.corroborations)      # but nothing applied

    def test_corroboration_gate_boost_is_not_credited_to_the_second_opinion(self):
        r = self._result(self._corroborated_lead(Confidence.medium))
        orch.apply_deterministic_gates(
            r, self._FAULT_SEED,
            second_opinion=_so(corroborates=True, confidence="high"),
            second_opinion_status="ok",
        )
        d = r.dossier
        self.assertEqual(d.verdict.confidence, Confidence.probable)
        self.assertTrue(d.corroborations["second_opinion_corroborated"])  # opinion recorded
        # The corroboration gate had already raised it, so the SO boost was inert.
        self.assertNotIn("second_opinion_boosted", d.corroborations)

    def test_real_second_opinion_boost_is_credited(self):
        r = self._result(_lead(Confidence.medium))       # no fault-offset corroboration
        orch.apply_deterministic_gates(
            r, {**_SEED, "is_offstack": False},
            second_opinion=_so(corroborates=True, confidence="high"),
            second_opinion_status="ok",
        )
        d = r.dossier
        self.assertEqual(d.raw_verdict.confidence, Confidence.medium)
        self.assertEqual(d.verdict.confidence, Confidence.probable)
        self.assertTrue(d.corroborations["second_opinion_boosted"])

    def test_suppressed_boost_is_not_credited(self):
        d = _lead(Confidence.medium)
        d.corroborations = {"downgraded_from_strong": True}
        orch._fold_second_opinion(d, _so(corroborates=True, confidence="high"), _SEED,
                                  status="ok")
        self.assertEqual(d.verdict.confidence, Confidence.medium)        # stays suppressed
        self.assertTrue(d.corroborations["second_opinion_corroborated"])
        self.assertNotIn("second_opinion_boosted", d.corroborations)


class TestSecondOpinionStatusPersisted(unittest.TestCase):
    """#2: a null second_opinion must say WHY — the pass is best-effort, so a prod break
    would otherwise be indistinguishable from an ineligible verdict."""

    def _result(self, dossier):
        return CrashTriageResult(num_turns=1, total_cost_usd=0.1, result="ok", dossier=dossier)

    def test_fold_stores_ok_alongside_the_second_opinion(self):
        d = _lead(Confidence.medium)
        orch._fold_second_opinion(d, _so(corroborates=True, confidence="high"), _SEED,
                                  status="ok")
        self.assertEqual(d.second_opinion_status, "ok")
        self.assertIsNotNone(d.second_opinion)

    def test_failed_is_distinguishable_from_skipped(self):
        failed = _lead(Confidence.medium)
        orch._fold_second_opinion(failed, None, _SEED, status="failed")
        skipped = _lead(Confidence.medium)
        orch._fold_second_opinion(skipped, None, _SEED, status="skipped_below_threshold")
        self.assertIsNone(failed.second_opinion)
        self.assertIsNone(skipped.second_opinion)
        self.assertNotEqual(failed.second_opinion_status, skipped.second_opinion_status)

    def test_status_is_authoritative_over_a_model_injected_value(self):
        d = _lead(Confidence.medium)
        d.second_opinion_status = "ok"          # as if the model had smuggled it through
        orch._fold_second_opinion(d, None, _SEED, status="failed")
        self.assertEqual(d.second_opinion_status, "failed")

    def test_eval_path_records_no_status(self):
        # The offline eval runner never runs the gate; it must not look like a skip or a fail.
        r = self._result(_lead(Confidence.medium))
        orch.apply_deterministic_gates(r, {**_SEED, "is_offstack": False})
        self.assertIsNone(r.dossier.second_opinion_status)

    def test_status_reaches_the_dossier_through_the_gates(self):
        r = self._result(_lead(Confidence.medium))
        orch.apply_deterministic_gates(
            r, {**_SEED, "is_offstack": False},
            second_opinion=None, second_opinion_status="failed",
        )
        self.assertEqual(r.dossier.second_opinion_status, "failed")
        self.assertIsNone(r.dossier.second_opinion)
        # A failed pass must not move the band.
        self.assertEqual(r.dossier.verdict.confidence, Confidence.medium)


if __name__ == "__main__":
    unittest.main()
