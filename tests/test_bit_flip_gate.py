# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Hardware bit-flip suppression, and the crash brief that has to carry the evidence for it.
#
# Bug 2061961: crash ff888d42-ce3e-4308-8c2f-b3f060260807 faulted at 0x00000001000000d0 -- one
# flipped bit from 0xd0, a NULL base plus a struct offset. Socorro had already published
# `possible_bit_flips_max_confidence: 62`. Nothing read it, the agent wrote a fully-cited
# use-after-free story, the blind second opinion agreed and boosted medium -> probable (exactly
# the filing threshold), and a developer was needinfo'd about a mechanical refactor of his. Two
# people closed it INVALID in two days citing that one field.
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_bit_flip_gate
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import config  # noqa: E402
from crashclouseau.agent import orchestrator as orch, triage  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    Candidate,
    Claim,
    Confidence,
    Decision,
    Dossier,
    SearchfoxCitation,
    Verdict,
)

_SF = SearchfoxCitation(
    permalink="https://searchfox.org/x#1", symbol_id="_Z1", repo="mozilla-central"
)

# The real payload, from the ProcessedCrash for ff888d42-ce3e-4308-8c2f-b3f060260807.
_FLIP = {
    "address": "0x0000000000000000",
    "confidence": 0.625,
    "details": {"is_null": True, "nearby_registers": 0, "poison_registers": False,
                "was_low": False, "was_non_canonical": False},
    "source_register": "rax",
}
_CRASH_INFO = {
    "address": "0x00000001000000d0",
    "assertion": None,
    "crash_inconsistencies": [],
    "instruction": "mov rax, qword [rax + 0xd0]",
    "memory_accesses": [{"address": "0x00000001000000d0", "size": 8}],
    "possible_bit_flips": [_FLIP],
    "type": "SIGSEGV / SEGV_MAPERR",
}


def _seed(confidence=62, reports=1, **over):
    raw = {"json_dump": {"crash_info": dict(_CRASH_INFO)}}
    if confidence is not None:
        raw["possible_bit_flips_max_confidence"] = confidence
    seed = {"uuid": "u-1", "signature": "S", "channel": "nightly", "is_offstack": False,
            "raw_crash": raw, "signature_report_count": reports}
    seed.update(over)
    return seed


def _lead(confidence=Confidence.probable):
    return Dossier(
        candidate=Candidate(node="c998e317e0cc", bug=2042063),
        verdict=Verdict(decision=Decision.lead, confidence=confidence,
                        needinfo_draft="could you take a look?",
                        mechanism=Claim(text="stale ComputedStyle deref", citations=[_SF])),
    )


def _cfg(**over):
    base = {"enabled": True, "min_confidence": 50, "max_reports": 1}
    base.update(over)
    return base


class TestBitFlipGate(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(config, "get_agent_bit_flip", return_value=_cfg())
        p.start()
        self.addCleanup(p.stop)

    def test_the_2061961_case_is_suppressed(self):
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed())
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertTrue(d.corroborations["possible_bit_flip_suppressed"])
        self.assertEqual(d.corroborations["possible_bit_flip_confidence"], 62)
        self.assertIn("BIT FLIP", d.verdict.abstain_reason)

    def test_a_suppressed_verdict_carries_no_needinfo(self):
        # An abstain must not ship the action the lead was going to take -- and
        # `Verdict._consistency_rule` rejects that combination outright, so a `model_copy`
        # here would raise rather than suppress.
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed())
        self.assertIsNone(d.verdict.needinfo_draft)
        self.assertIsNotNone(d.verdict.mechanism)

    def test_a_busy_signature_is_never_suppressed_on_the_score_alone(self):
        # The same score is common on high-volume signatures, where it means one flaky machine
        # among many. Confidence alone would suppress real, busy crashes.
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed(reports=42))
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.corroborations["possible_bit_flip_confidence"], 62)
        self.assertNotIn("possible_bit_flip_suppressed", d.corroborations)

    def test_a_single_crash_alone_is_never_suppressed(self):
        # NOT a volume gate in disguise: 16 of the canary's first 20 filings were single-crash,
        # and bug 2062119 named the WRONG changeset on a one-report signature and still got a
        # real fix written. Volume only ever qualifies the flip signal.
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed(confidence=None, reports=1))
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.corroborations or {}, {})

    def test_a_baseline_score_is_recorded_but_does_not_fire(self):
        # 25 is rust-minidump's floor -- "some single-bit variant happens to be mapped", which
        # on a 64-bit heap is close to noise.
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed(confidence=25))
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.corroborations["possible_bit_flip_confidence"], 25)

    def test_an_unknown_report_count_does_not_suppress(self):
        # `None` means the Socorro lookup failed. It must not read as "a singleton".
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed(reports=None))
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.corroborations["possible_bit_flip_confidence"], 62)
        self.assertNotIn("signature_report_count", d.corroborations)

    def test_an_absent_field_is_not_a_zero(self):
        # Socorro OMITS the field when the stackwalker found no candidate; it is never 0. An
        # absent field must leave the verdict, and the corroborations, untouched.
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed(confidence=None))
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.corroborations or {}, {})

    def test_offline_seeds_are_a_no_op(self):
        # The eval corpus's frozen crashes are stubs with no `crash_info`, so the gate must be
        # a natural no-op there rather than an exception on the shared gate ladder.
        d = _lead()
        orch._apply_bit_flip_gate(d, {"uuid": "u-1", "raw_crash": {}})
        self.assertEqual(d.verdict.decision, Decision.lead)
        orch._apply_bit_flip_gate(d, {"uuid": "u-1"})
        self.assertEqual(d.verdict.decision, Decision.lead)

    def test_the_kill_switch_stops_it_recording_anything(self):
        config.get_agent_bit_flip.return_value = _cfg(enabled=False)
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed())
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.corroborations or {}, {})

    def test_an_existing_abstain_keeps_its_own_reason(self):
        d = Dossier(verdict=Verdict(decision=Decision.abstain, confidence=Confidence.low,
                                    abstain_reason="no candidate in the window"))
        orch._apply_bit_flip_gate(d, _seed())
        self.assertEqual(d.verdict.abstain_reason, "no candidate in the window")
        self.assertEqual(d.corroborations["possible_bit_flip_confidence"], 62)
        self.assertNotIn("possible_bit_flip_suppressed", d.corroborations)

    def test_a_non_numeric_confidence_is_ignored(self):
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed(confidence="banana"))
        self.assertEqual(d.verdict.decision, Decision.lead)


class TestGateRunsLast(unittest.TestCase):
    """It has to outrank the second-opinion boost, which is what filed bug 2061961: the raw
    verdict was `medium` (50) and the fold raised it to `probable` (70), exactly
    `autofile.min_confidence`."""

    def test_a_second_opinion_boost_cannot_rescue_a_bit_flip(self):
        from crashclouseau.agent.result import CrashTriageResult

        with mock.patch.object(config, "get_agent_bit_flip", return_value=_cfg()), \
             mock.patch.object(orch, "_fold_second_opinion") as fold:
            # Stand in for the fold: a bare lead corroborated -> raised to probable.
            def boost(dossier, so, seed, status=None):
                dossier.verdict = dossier.verdict.model_copy(
                    update={"confidence": Confidence.probable})
                dossier.corroborations = {**(dossier.corroborations or {}),
                                          "second_opinion_boosted": True}
            fold.side_effect = boost
            result = CrashTriageResult(num_turns=1, total_cost_usd=0.1, result="ok",
                                       dossier=_lead(confidence=Confidence.medium))
            orch.apply_deterministic_gates(result, _seed())

        self.assertTrue(result.dossier.corroborations["second_opinion_boosted"])
        self.assertEqual(result.dossier.verdict.decision, Decision.abstain)
        self.assertTrue(result.dossier.corroborations["possible_bit_flip_suppressed"])
        # The pre-gate snapshot still records what the model actually said.
        self.assertEqual(result.dossier.raw_verdict.decision, Decision.lead)

    def test_an_abstained_verdict_is_never_filed(self):
        # The whole point: `autofile_bug` refuses a verdict outside `cfg["verdicts"]`, so the
        # abstain above is what stops the Bugzilla write. Asserted here rather than trusting it.
        from crashclouseau import bugzilla_apply

        with mock.patch.object(bugzilla_apply.config, "get_agent_autofile", return_value={
                "enabled": True, "min_confidence": 70, "verdicts": ["lead", "culprit"],
                "needinfo": True, "daily_cap": 10, "comment_on_existing": True,
                "comment_max_bug_age_days": 30}):
            res = bugzilla_apply.autofile_bug(
                "u-1", {"uuid": "u-1", "signature": "S", "channel": "nightly"}, {},
                {"candidate": {"node": "n"}}, "abstain", 70)
        self.assertFalse(res["filed"])
        self.assertIn("not fileable", res["skipped"])


class TestWindowMembership(unittest.TestCase):
    """Whether the candidate came from this build's pushlog window is the only recency evidence
    the pipeline has, and it decides whether the filed bug may say "regression" at all. Measured
    over the first 22 filings the premise held 3 times; bug 2062119 named a changeset from
    2022-12-13 and the run's own skeptic was recording "not a new regression"."""

    def test_a_seeded_candidate_is_in_the_window(self):
        d = _lead()
        orch._record_window_membership(
            d, {"candidate_pushdates": {"abcdef123456": 1, "other": 2}})
        self.assertIs(d.corroborations["candidate_in_pushlog_window"], False)
        d = Dossier(candidate=Candidate(node="abcdef123456"),
                    verdict=Verdict(decision=Decision.abstain, confidence=Confidence.low,
                                    abstain_reason="x"))
        orch._record_window_membership(d, {"candidate_pushdates": {"abcdef123456": 1}})
        self.assertIs(d.corroborations["candidate_in_pushlog_window"], True)

    def test_a_blame_found_candidate_is_out_of_the_window(self):
        d = _lead()
        orch._record_window_membership(d, {"candidate_pushdates": {"someothernode": 1}})
        self.assertIs(d.corroborations["candidate_in_pushlog_window"], False)

    def test_no_map_records_nothing_rather_than_false(self):
        # Offline seeds and old runs carry no map. `report_bug.is_suspected_regression` reads an
        # absent flag as "no", so recording a bare False here would be indistinguishable from a
        # measured out-of-window -- and this flag is the thing we want to COUNT.
        for seed in ({}, {"candidate_pushdates": {}}, {"candidate_pushdates": None}):
            with self.subTest(seed=seed):
                d = _lead()
                orch._record_window_membership(d, seed)
                self.assertEqual(d.corroborations or {}, {})

    def test_it_moves_no_rung(self):
        d = _lead()
        orch._record_window_membership(d, {"candidate_pushdates": {"nope": 1}})
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.verdict.confidence, Confidence.probable)


class TestCrashBriefCarriesTheEvidence(unittest.TestCase):
    """The gate is the backstop; this is the fix for the reasoning. `_crash_facts` feeds the
    principal, its five subagents AND the blind second opinion (`second_opinion._user_prompt`
    calls it), so one edit un-blinds all of them at once."""

    def test_the_bit_flip_and_the_instruction_reach_the_model(self):
        facts = "\n".join(triage._crash_facts(
            {"raw_crash": {"json_dump": {"crash_info": dict(_CRASH_INFO)}}}))
        self.assertIn("mov rax, qword [rax + 0xd0]", facts)
        self.assertIn("POSSIBLE BIT FLIP", facts)
        self.assertIn("rax should have been 0x0000000000000000", facts)
        self.assertIn("conf 62%", facts)
        self.assertIn("NULL", facts)

    def test_the_second_opinion_sees_exactly_the_same_facts(self):
        # It is billed as an independent check and it is -- in REASONING. It was never
        # independent in EVIDENCE, which is why it corroborated a hardware fault.
        from crashclouseau.agent import second_opinion

        crash = {"signature": "S", "channel": "nightly", "stack": "#0 f a:1",
                 "raw_crash": {"json_dump": {"crash_info": dict(_CRASH_INFO)}}}
        prompt = second_opinion._user_prompt(crash, {"node": "c998e317e0cc", "bug": 2042063})
        self.assertIn("POSSIBLE BIT FLIP", prompt)

    def test_a_poison_register_is_flagged_as_arguing_the_other_way(self):
        # A poison value means a use-after-free -- SOFTWARE. rust-minidump halves the score for
        # it, and the model must not read the remaining number as "hardware".
        info = dict(_CRASH_INFO, possible_bit_flips=[
            {**_FLIP, "confidence": 0.31,
             "details": {**_FLIP["details"], "poison_registers": True}}])
        facts = "\n".join(triage._crash_facts({"raw_crash": {"json_dump": {"crash_info": info}}}))
        self.assertIn("POISON", facts)
        self.assertIn("UAF", facts)

    def test_no_bit_flip_data_adds_no_line(self):
        for info in ({}, {"possible_bit_flips": []}, {"possible_bit_flips": None},
                     {"possible_bit_flips": "nonsense"}):
            with self.subTest(info=info):
                facts = "\n".join(triage._crash_facts(
                    {"raw_crash": {"json_dump": {"crash_info": info}}}))
                self.assertNotIn("BIT FLIP", facts)

    def test_the_summary_survives_the_300_char_truncation(self):
        # Every fact goes through `_short_value(value, limit=300)`. The flags are the part that
        # has to survive, so the renderer caps the candidate list rather than the text.
        info = dict(_CRASH_INFO, possible_bit_flips=[
            {**_FLIP, "source_register": "r{}".format(i)} for i in range(8)])
        summary = triage._bit_flip_summary(info)
        self.assertLessEqual(len(summary), 300)
        self.assertIn("r0", summary)
        self.assertNotIn("r7", summary)


class TestSignatureHistory(unittest.TestCase):
    """One SuperSearch now answers both gates' questions."""

    def test_first_seen_and_total_come_from_one_request(self):
        from crashclouseau import sigage

        calls = []

        class FakeSearch:
            def __init__(self, params=None, handler=None, handlerdata=None):
                calls.append(params)
                handler({"hits": [{"build_id": "20260325210205"}], "total": 7}, handlerdata)

            def wait(self):
                return None

        with mock.patch.object(sigage.socorro, "SuperSearch", FakeSearch):
            got = sigage.signature_history("S", "Firefox", "nightly")
        self.assertEqual(got, {"first_seen": "20260325210205", "total": 7})
        self.assertEqual(len(calls), 1)

    def test_a_failed_lookup_is_none_not_zero(self):
        # `total: 0` would read as "a signature nobody has ever hit" and suppress every verdict
        # carrying a flip score.
        from crashclouseau import sigage

        class Boom:
            def __init__(self, **kw):
                raise RuntimeError("socorro down")

        with mock.patch.object(sigage.socorro, "SuperSearch", Boom):
            got = sigage.signature_history("S")
        self.assertEqual(got, {"first_seen": None, "total": None})

    def test_first_seen_buildid_still_answers_the_old_question(self):
        from crashclouseau import sigage

        with mock.patch.object(sigage, "signature_history",
                               return_value={"first_seen": "20260101000000", "total": 3}):
            self.assertEqual(sigage.first_seen_buildid("S"), "20260101000000")


if __name__ == "__main__":
    unittest.main()
