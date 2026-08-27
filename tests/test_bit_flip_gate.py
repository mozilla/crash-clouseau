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

from crashclouseau import config, report_bug, sigage  # noqa: E402
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


def _seed(confidence=62, reports=1, cpu=None, **over):
    raw = {"json_dump": {"crash_info": dict(_CRASH_INFO)}}
    if confidence is not None:
        raw["possible_bit_flips_max_confidence"] = confidence
    if cpu is not None:
        raw["cpu_info"] = cpu
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
    base = {"enabled": True, "min_confidence": 50, "max_reports": 1,
            "min_signature_reports": 5, "max_bit_flip_rate": 0.2, "max_broken_cpu_rate": 0.7}
    base.update(over)
    return base


def _noise(reports=6, flip=0.5, cpu=0.167, terms=5, share=0.2,
           term="family 23 model 8 stepping 2"):
    """`sigage.hardware_noise` for bug 2064600's signature on Firefox nightly over 364 days,
    measured 2026-08-19: 6 reports, 3 of them flip-annotated -- which is exactly the "about 50%"
    Timothy Nikkel quoted at us. The cpu spread is the same signature re-read 2026-08-21: 5
    distinct models, the commonest an ordinary Zen at 20%.

    Full shape, `sigage.NO_HARDWARE_NOISE` keys and all, because a fixture that is missing a key
    the production caller reads is how a gate test passes on an input production never builds."""
    def _n(rate):
        return None if (rate is None or reports is None) else int(reports * rate)
    out = dict(sigage.NO_HARDWARE_NOISE)
    out.update(reports=reports, bit_flip_rate=flip, broken_cpu_rate=cpu,
               bit_flip_reports=_n(flip), broken_cpu_reports=_n(cpu))
    if reports is not None and terms is not None:
        out.update(cpu_reports=reports, cpu_terms=terms, top_cpu_term=term, top_cpu_share=share)
    return out


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
        # The report count IS recorded (three branches now qualify on it, and counting how often
        # we triage a singleton is worth having); what must be absent is any suppression.
        self.assertEqual(d.corroborations, {"signature_report_count": 1})

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
        self.assertNotIn("possible_bit_flip_confidence", d.corroborations)
        self.assertNotIn("possible_bit_flip_suppressed", d.corroborations)

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

    def _autofile(self, dossier):
        from crashclouseau import bugzilla_apply

        with mock.patch.object(bugzilla_apply.config, "get_agent_autofile", return_value={
                "enabled": True, "min_confidence": 70, "verdicts": ["lead", "culprit"],
                "needinfo": True, "daily_cap": 10, "comment_on_existing": True,
                "comment_max_bug_age_days": 30}), \
             mock.patch.object(bugzilla_apply, "_incomplete_fix_bug") as fix:
            res = bugzilla_apply.autofile_bug(
                "u-1", {"uuid": "u-1", "signature": "S", "channel": "nightly"}, {},
                dossier, "abstain", 70)
        return res, fix

    def test_a_bit_flip_suppression_is_never_filed(self):
        """The whole point of the gate: a probable hardware bit flip must not reach Bugzilla.

        It used to be enough that `autofile_bug` refuses a verdict outside `cfg["verdicts"]`.
        That is no longer the only door — an abstain can now be filed when a bug on the
        signature was fixed and the fix is already in this build — so the suppression is
        asserted directly, and `_incomplete_fix_bug` must not even be CONSULTED: a suppression
        is about this crash, not about the verdict's strength."""
        res, fix = self._autofile({"candidate": {"node": "n"},
                                   "corroborations": {"possible_bit_flip_suppressed": True}})
        self.assertFalse(res["filed"])
        self.assertIn("possible_bit_flip_suppressed", res["skipped"])
        fix.assert_not_called()

    def test_every_suppression_closes_that_door_not_just_this_one(self):
        from crashclouseau import corroborations

        for flag in sorted(corroborations.suppressions()):
            res, fix = self._autofile({"candidate": {"node": "n"},
                                       "corroborations": {flag: True}})
            self.assertFalse(res["filed"], flag)
            self.assertIn(flag, res["skipped"], flag)
            fix.assert_not_called()

    def test_an_unsuppressed_abstain_with_no_incomplete_fix_is_still_not_filed(self):
        res, fix = self._autofile({"candidate": {"node": "n"}})
        fix.return_value = None
        self.assertFalse(res["filed"])


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

    def test_a_candidate_with_no_pushdate_is_still_in_the_window(self):
        # `candidate_pushdates` is the window MINUS every candidate whose landing date is
        # unknown, so keying on it scored a seeded candidate as out-of-window. It also made the
        # flag unrecordable offline: `eval/study_corpus.py` writes "pushdate": None for all of
        # them (0 of corpus_ship's 6873 have one), which is why the two-arm calibration split had
        # to be backfilled by hand instead of read off the corpus.
        d = _lead()
        orch._record_window_membership(
            d, {"candidates": [{"node": "c998e317e0cc", "pushdate": None},
                               {"node": "other", "pushdate": None}]})
        self.assertIs(d.corroborations["candidate_in_pushlog_window"], True)

    def test_a_candidate_outside_the_seeded_set_is_still_false(self):
        d = _lead()
        orch._record_window_membership(
            d, {"candidates": [{"node": "someothernode"}], "candidate_pushdates": {}})
        self.assertIs(d.corroborations["candidate_in_pushlog_window"], False)

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


def _fake_search(response, calls, channel_response=None):
    """A ``socorro.SuperSearch`` stand-in for the two-query form ``signature_history`` uses.

    Records each query's params and replays *response* to the unfiltered one, and
    *channel_response* (defaulting to *response*) to the ``release_channel``-filtered one."""

    class FakeSearch:
        URL = "https://crash-stats.mozilla.org/api/SuperSearch/"

        def __init__(self, queries=None, **kw):
            self.queries = queries or []

        def wait(self):
            for q in self.queries:
                calls.append(q.params)
                filtered = "release_channel" in (q.params or {})
                body = channel_response if (filtered and channel_response is not None) \
                    else response
                q.handler(body, q.handlerdata)
            return None

    return FakeSearch


def _response(total, per_channel, build_id="20260325210205"):
    return {"hits": [{"build_id": build_id}], "total": total,
            "facets": {"release_channel": [{"term": t, "count": c}
                                           for t, c in per_channel.items()]}}


class TestSignatureHistory(unittest.TestCase):
    """One SuperSearch answers both gates' questions — at DIFFERENT channel scopes.

    ``first_seen`` spans every channel (the origin question is not per-channel), ``total``
    stays on the requested one (the bit-flip gate's population rates are nightly-measured).
    That split is the whole point of the facet, so these pin both halves."""

    def test_first_seen_and_total_come_from_one_request(self):
        from crashclouseau import sigage

        calls = []
        with mock.patch.object(sigage.socorro, "SuperSearch",
                               _fake_search(_response(30, {"nightly": 7, "release": 23}), calls)):
            got = sigage.signature_history("S", "Firefox", "nightly")
        self.assertEqual(got["first_seen"], "20260325210205")
        self.assertEqual(got["total"], 7)
        self.assertEqual(got["total_other_channels"], 23)
        # Two queries, ONE wait: the channel's own oldest build cannot be read off the
        # unfiltered response, and it is the fallback below the floor.
        self.assertEqual(len(calls), 2)

    def test_the_request_is_NOT_channel_filtered(self):
        # The bug this fixes: scoped to nightly, a signature ten months old on release looked
        # nine days old, the stale gate stayed silent, and bug 2062934 was filed at 97%.
        from crashclouseau import sigage

        calls = []
        with mock.patch.object(sigage.socorro, "SuperSearch",
                               _fake_search(_response(30, {"nightly": 7}), calls)):
            sigage.signature_history("S", "Firefox", "nightly")
        self.assertNotIn("release_channel", calls[0])
        self.assertEqual(calls[0]["_facets"], "release_channel")
        # ...and the SECOND query is the channel-scoped fallback.
        self.assertEqual(calls[1]["release_channel"], "nightly")
        self.assertNotIn("_facets", calls[1])

    def test_total_is_the_channel_slice_not_the_whole_population(self):
        from crashclouseau import sigage

        with mock.patch.object(sigage.socorro, "SuperSearch",
                               _fake_search(_response(500, {"nightly": 3, "release": 497}), [])):
            got = sigage.signature_history("S", "Firefox", "nightly")
        # 500 would tell the bit-flip gate this is a busy signature; on nightly it is a
        # near-singleton, and nightly is the population its rates are measured against.
        self.assertEqual(got["total"], 3)

    def test_beta_is_summed_over_aurora_too(self):
        from crashclouseau import sigage

        with mock.patch.object(sigage.socorro, "SuperSearch",
                               _fake_search(_response(90, {"beta": 40, "aurora": 20,
                                                           "release": 30}), [])):
            self.assertEqual(sigage.signature_history("S", "Firefox", "beta")["total"], 60)

    def test_no_channel_asked_for_means_the_whole_population(self):
        from crashclouseau import sigage

        with mock.patch.object(sigage.socorro, "SuperSearch",
                               _fake_search(_response(90, {"nightly": 90}), [])):
            self.assertEqual(sigage.signature_history("S", "Firefox", None)["total"], 90)

    def test_a_channel_with_no_reports_is_zero_not_none(self):
        from crashclouseau import sigage

        with mock.patch.object(sigage.socorro, "SuperSearch",
                               _fake_search(_response(23, {"release": 23}), [])):
            self.assertEqual(sigage.signature_history("S", "Firefox", "nightly")["total"], 0)

    def test_a_missing_facet_on_a_NON_empty_result_is_unknown(self):
        # Not zero: "we could not find out" must not read as "a singleton" to the bit-flip gate.
        from crashclouseau import sigage

        with mock.patch.object(sigage.socorro, "SuperSearch",
                               _fake_search({"hits": [{"build_id": "20260101000000"}],
                                             "total": 12}, [])):
            got = sigage.signature_history("S", "Firefox", "nightly")
        self.assertEqual(got["first_seen"], "20260101000000")
        self.assertIsNone(got["total"])

    def test_below_the_floor_the_answer_is_the_CHANNELS_own_first_seen(self):
        # Purely additive: with too little off-channel history to be evidence, the answer is
        # byte-for-byte what this returned before, so no firing the gate already had is lost.
        from crashclouseau import sigage

        with mock.patch.object(sigage.socorro, "SuperSearch", _fake_search(
                _response(12, {"nightly": 9, "release": 3}, build_id="20250101000000"), [],
                channel_response=_response(9, {"nightly": 9}, build_id="20260801000000"))):
            got = sigage.signature_history("S", "Firefox", "nightly", other_channel_floor=20)
        self.assertEqual(got["total_other_channels"], 3)
        self.assertEqual(got["first_seen"], "20260801000000")
        self.assertEqual(got["first_seen_channel"], "20260801000000")

    def test_at_the_floor_the_other_channels_history_is_admitted(self):
        # bug 2062934's shape: three nightly reports, twenty-nine across channels, and a
        # first-seen ten months older than nightly knows about.
        from crashclouseau import sigage

        with mock.patch.object(sigage.socorro, "SuperSearch", _fake_search(
                _response(29, {"nightly": 3, "release": 26}, build_id="20251009121631"), [],
                channel_response=_response(3, {"nightly": 3}, build_id="20260811085340"))):
            got = sigage.signature_history("S", "Firefox", "nightly", other_channel_floor=20)
        self.assertEqual(got["total_other_channels"], 26)
        self.assertEqual(got["first_seen"], "20251009121631")
        self.assertEqual(got["first_seen_channel"], "20260811085340")

    def test_an_unknown_off_channel_count_is_not_a_cleared_floor(self):
        # A lookup we could not resolve must not be the thing that widens the gate.
        from crashclouseau import sigage

        with mock.patch.object(sigage.socorro, "SuperSearch", _fake_search(
                {"hits": [{"build_id": "20250101000000"}], "total": 400}, [],
                channel_response=_response(3, {"nightly": 3}, build_id="20260801000000"))):
            got = sigage.signature_history("S", "Firefox", "nightly", other_channel_floor=20)
        self.assertIsNone(got["total_other_channels"])
        self.assertEqual(got["first_seen"], "20260801000000")

    def test_an_empty_result_set_is_a_real_zero(self):
        from crashclouseau import sigage

        with mock.patch.object(sigage.socorro, "SuperSearch",
                               _fake_search({"hits": [], "total": 0, "facets": {}}, [])):
            self.assertEqual(sigage.signature_history("S", "Firefox", "nightly"),
                             {"first_seen": None, "first_seen_channel": None,
                              "first_seen_any": None,
                              "total": 0, "total_other_channels": 0})

    def test_a_failed_lookup_is_none_not_zero(self):
        # `total: 0` would read as "a signature nobody has ever hit" and suppress every verdict
        # carrying a flip score.
        from crashclouseau import sigage

        class Boom:
            URL = "https://crash-stats.mozilla.org/api/SuperSearch/"

            def __init__(self, **kw):
                raise RuntimeError("socorro down")

        with mock.patch.object(sigage.socorro, "SuperSearch", Boom):
            got = sigage.signature_history("S")
        self.assertEqual(got, {"first_seen": None, "first_seen_channel": None,
                               "first_seen_any": None,
                               "total": None, "total_other_channels": None})

    def test_first_seen_buildid_still_answers_the_old_question(self):
        from crashclouseau import sigage

        with mock.patch.object(sigage, "signature_history",
                               return_value={"first_seen": "20260101000000", "total": 3}):
            self.assertEqual(sigage.first_seen_buildid("S"), "20260101000000")


if __name__ == "__main__":
    unittest.main()


class TestSignatureIsMostlyHardware(unittest.TestCase):
    """Bug 2064600. Clouseau filed `mozilla::ActiveScrolledRoot::GetNearestScrollASR` at 97%
    worth-investigating; Timothy Nikkel replied 20 minutes later: "About 50% of the crashes with
    this signature have non-zero bit flip probability. That might be something you want to
    include in your llm prompt to consider. And there is also several of the known buggy family 6
    model 183 stepping 1 without a bit flip annotation."

    Neither fact was reachable from what the gate read. The triaged report
    (92ce80ce-3c58-4fc3-ae1f-8ffde0260819) has NO flip annotation and an ordinary Rocket Lake
    CPU, so every per-report check passes it; the signature it sits on is 29% bit flips and 42%
    Raptor Lake over 180 days, against a crash-population background of 7.6% and 3.8%."""

    def setUp(self):
        p = mock.patch.object(config, "get_agent_bit_flip", return_value=_cfg())
        p.start()
        self.addCleanup(p.stop)

    def test_the_2064600_case_is_suppressed(self):
        # The exact seed of the crash we filed: clean report, compromised signature.
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed(confidence=None, reports=6, hardware_noise=_noise(),
                                           cpu="family 6 model 167 stepping 1"))
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertTrue(d.corroborations["hardware_noise_signature_suppressed"])
        self.assertIs(d.corroborations["report_on_broken_cpu"], False)
        self.assertEqual(d.corroborations["signature_bit_flip_rate"], 0.5)
        self.assertIn("mostly hardware error", d.verdict.abstain_reason)
        self.assertIsNone(d.verdict.needinfo_draft)

    def test_the_thresholds_are_bugbots(self):
        # mozilla/bugbot skips a signature at >= 0.2 bit flips or >= 0.7 broken CPU
        # (bugbot/crash/analyzer.py). Either alone is enough; just under either is not.
        cases = [(0.20, 0.0, True), (0.19, 0.0, False), (0.0, 0.70, True), (0.0, 0.69, False)]
        for flip, cpu, fires in cases:
            with self.subTest(flip=flip, cpu=cpu):
                d = _lead()
                orch._apply_bit_flip_gate(d, _seed(confidence=None, reports=99,
                                                   hardware_noise=_noise(reports=99, flip=flip,
                                                                         cpu=cpu)))
                self.assertEqual(d.verdict.decision,
                                 Decision.abstain if fires else Decision.lead)

    def test_a_small_sample_can_never_fire(self):
        # A brand-new nightly regression has a handful of reports and 1-of-3 is "33%". Below the
        # floor the rule is off, which is what stops this eating the pipeline's whole purpose.
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed(confidence=None, reports=4,
                                           hardware_noise=_noise(reports=4, flip=0.75, cpu=1.0)))
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertNotIn("hardware_noise_signature_suppressed", d.corroborations)
        # Measured anyway, so the threshold can be scored against outcomes later.
        self.assertEqual(d.corroborations["signature_hardware_sample"], 4)

    def test_an_unknown_share_never_suppresses(self):
        # The lookup failing must not read as "clean" OR as "hardware" -- the rate tests are
        # positive requirements precisely so a None cannot satisfy them.
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed(confidence=None, reports=99,
                                           hardware_noise=_noise(reports=None, flip=None,
                                                                 cpu=None)))
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertNotIn("signature_bit_flip_rate", d.corroborations)

    def test_a_busy_healthy_signature_is_untouched(self):
        # `IPCError-browser | ShutDownKill`, measured: 50,101 reports, 0% flips, 2% Raptor Lake.
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed(confidence=None, reports=50101,
                                           hardware_noise=_noise(reports=50101, flip=0.0,
                                                                 cpu=0.02)))
        self.assertEqual(d.verdict.decision, Decision.lead)

    def test_the_nightly_denominator_spares_bug_2062219(self):
        # THE CASE THAT SETTLED THE DENOMINATOR. `nsAtom::IsStatic` (bug 2062219, RESOLVED FIXED)
        # runs 49% bit flips across all products and channels over 180 days and 13% on Firefox
        # nightly over a year. The wider rate suppresses a bug that was real and got FIXED; the
        # nightly rate leaves it alone. Measured over the canary's first 47 filings, the nightly
        # denominator kills 0 of the 18 FIXED/DUPLICATE/ASSIGNED ones and the wider one kills this.
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed(confidence=None, reports=255,
                                           hardware_noise=_noise(reports=255, flip=0.13,
                                                                 cpu=0.31)))
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.corroborations["signature_bit_flip_rate"], 0.13)

    def test_it_does_not_reopen_the_cluster(self):
        # THE DISTINCTION THAT MATTERS. `_INSTANCE_SUPPRESSED` keeps a proto-signature cluster
        # OPEN when a verdict was killed for a reason peculiar to one report, so the next crash
        # still gets looked at. "This signature is 29% bit flips" is not such a reason -- it is
        # equally true of every report in the cluster -- so it must close it, exactly as the
        # backout gate does, or we re-pay ~$3 a report to re-derive the same answer forever.
        from crashclouseau import models
        self.assertNotIn("hardware_noise_signature_suppressed", models._INSTANCE_SUPPRESSED)
        self.assertIn("broken_cpu_suppressed", models._INSTANCE_SUPPRESSED)
        self.assertIn("possible_bit_flip_suppressed", models._INSTANCE_SUPPRESSED)


class TestReportIsOnADefectiveCpu(unittest.TestCase):
    """The per-report half of Nikkel's second point: this crash came from an Intel Raptor Lake,
    whose documented instability corrupts computation on correct software (meta bug 1975808)."""

    def setUp(self):
        p = mock.patch.object(config, "get_agent_bit_flip", return_value=_cfg())
        p.start()
        self.addCleanup(p.stop)

    def test_a_singleton_on_a_broken_cpu_is_suppressed(self):
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed(confidence=None, reports=1,
                                           cpu="family 6 model 183 stepping 1"))
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertTrue(d.corroborations["broken_cpu_suppressed"])
        self.assertIn("Raptor Lake", d.verdict.abstain_reason)

    def test_a_broken_cpu_alone_never_suppresses(self):
        # 3.8% of ALL crash reports come from one of these machines, so suppressing on the CPU
        # alone would throw away roughly one real bug in twenty-six. The conjunction with
        # "nobody else has ever hit this" is load-bearing, exactly as it is for the flip score.
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed(confidence=None, reports=42,
                                           cpu="family 6 model 183 stepping 1"))
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertIs(d.corroborations["report_on_broken_cpu"], True)

    def test_a_healthy_cpu_is_recorded_and_left_alone(self):
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed(confidence=None, reports=1,
                                           cpu="family 6 model 167 stepping 1"))
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertIs(d.corroborations["report_on_broken_cpu"], False)

    def test_an_unknown_cpu_is_not_a_broken_one(self):
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed(confidence=None, reports=1))
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertNotIn("report_on_broken_cpu", d.corroborations)

    def test_the_flip_score_wins_when_both_apply(self):
        # Ordering is not cosmetic: the reason lands in the filed/abstained verdict and in
        # `models.Feedback`, so the more specific finding has to be the one reported.
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed(confidence=62, reports=1,
                                           cpu="family 6 model 183 stepping 1"))
        self.assertTrue(d.corroborations["possible_bit_flip_suppressed"])
        self.assertNotIn("broken_cpu_suppressed", d.corroborations)


class TestTheBriefCarriesTheHardware(unittest.TestCase):
    """Nikkel's actual request was about the prompt: "That might be something you want to include
    in your llm prompt to consider." """

    def test_cpu_info_reaches_the_prompt_at_all(self):
        # REGRESSION TEST. The fact was `("CPU arch", _first_present(cpu_arch, ..., cpu_info,
        # ...))` and `cpu_arch` is set on essentially every crash, so the `cpu_info` fallbacks
        # behind it were unreachable and no agent had ever seen which processor a crash came
        # from -- the exact fact Nikkel says he checks every time.
        facts = triage._crash_facts(
            {"raw_crash": {"cpu_arch": "amd64", "cpu_info": "family 6 model 60 stepping 3"}})
        cpu = [f for f in facts if f.startswith("CPU")]
        self.assertEqual(len(cpu), 1)
        self.assertIn("amd64", cpu[0])
        self.assertIn("family 6 model 60 stepping 3", cpu[0])

    def test_a_defective_cpu_is_called_out_and_survives_truncation(self):
        facts = triage._crash_facts(
            {"raw_crash": {"cpu_arch": "amd64", "cpu_info": "family 6 model 183 stepping 1"}})
        cpu = [f for f in facts if f.startswith("CPU")][0]
        self.assertIn("KNOWN-DEFECTIVE", cpu)
        self.assertIn("1975808", cpu)
        # `_short_value` truncates at 300; the warning is the part that must survive.
        self.assertNotIn("...", cpu)

    def test_the_signature_share_is_stated_against_the_population(self):
        # A bare "50%" is unreadable: the model cannot know it is alarming without the 2.5%.
        lines = "\n".join(triage._crash_facts({"raw_crash": {}, "hardware_noise": _noise()}))
        # The population is NAMED now, because it is per channel: beta's bit-flip rate is 6.75%
        # against nightly's 2.55%, so "crash population" alone was a number with no denominator
        # attached to it (`sigage.population_bit_flip_rate`).
        self.assertIn(
            "50% carry a Socorro bit-flip annotation (Firefox-nightly population: 2%)", lines)
        self.assertIn("17% come from a known-defective Intel Raptor Lake", lines)
        self.assertIn("Firefox-nightly population: 4%", lines)  # Raptor Lake background
        self.assertIn("NOT hardware error", lines)    # Nikkel's "next step"

    def test_the_blind_second_opinion_is_told_too(self):
        # The opposite of `_archetype_lines`, and deliberately: an archetype is a suggested
        # DIRECTION, so priming both models correlates their mistakes, whereas this is a FACT
        # both were blind to. On bug 2061961 the SO shared the blind spot and BOOSTED the rung.
        from crashclouseau.agent import second_opinion
        prompt = second_opinion._user_prompt(
            {"signature": "S", "raw_crash": {}, "hardware_noise": _noise()}, None)
        self.assertIn("HARDWARE-ERROR SHARE OF THIS SIGNATURE", prompt)

    def test_nothing_is_said_when_nothing_was_measured(self):
        self.assertEqual(triage._hardware_noise_lines({}), [])
        self.assertEqual(triage._hardware_noise_lines({"hardware_noise": _noise(reports=None)}),
                         [])


class TestHardwareNoiseLookup(unittest.TestCase):
    """`sigage.hardware_noise` -- one SuperSearch, both shares."""

    def _run(self, payload):
        with mock.patch.object(sigage.socorro, "SuperSearch") as ss:
            def ctor(params=None, handler=None, handlerdata=None, **kw):
                handler(payload, handlerdata)
                return mock.Mock(wait=mock.Mock())
            ss.side_effect = ctor
            out = sigage.hardware_noise("S")
            return out, ss.call_args.kwargs["params"]

    def test_it_sums_both_facets(self):
        out, params = self._run({
            "total": 100,
            "facets": {"possible_bit_flips_max_confidence": [{"term": 25, "count": 20},
                                                             {"term": 62, "count": 10}],
                       "cpu_info": [{"term": "family 6 model 183 stepping 1", "count": 40},
                                    {"term": "family 6 model 60 stepping 3", "count": 30}]}})
        self.assertEqual(out["reports"], 100)
        self.assertEqual(out["bit_flip_reports"], 30)
        self.assertEqual(out["broken_cpu_reports"], 40)
        self.assertAlmostEqual(out["bit_flip_rate"], 0.3)
        self.assertAlmostEqual(out["broken_cpu_rate"], 0.4)

    def test_it_asks_only_about_this_product_and_channel(self):
        # Not a detail: the same thresholds on an all-products/all-channels rate suppress bug
        # 2062219, which was FIXED. Release carries years of failing consumer hardware on a hot
        # signature and it says nothing about whether a nightly crash is real.
        _, params = self._run({"total": 1, "facets": {}})
        self.assertEqual(params["product"], "Firefox")
        self.assertEqual(params["release_channel"], "nightly")
        self.assertEqual(params["signature"], "=S")
        self.assertEqual(params["date"][:2], ">=")

    def test_zero_rows_is_unknown_not_clean(self):
        # An empty response is equally what a malformed or throttled query returns, and this
        # feeds a suppression.
        out, _ = self._run({"total": 0, "facets": {}})
        self.assertIsNone(out["reports"])
        self.assertIsNone(out["bit_flip_rate"])

    def test_a_missing_facet_is_unknown_not_zero(self):
        # This used to assert `broken_cpu_rate == 0.0` under this exact name. An empty `cpu_info`
        # facet is not a signature with no Raptor Lakes, it is a signature Socorro has no CPU
        # string for -- 2,552 of 15,329 Firefox-nightly macOS reports carry one (16.6%) against
        # 99.8% on Windows, and 3 of the canary's 52 filings (2062806 FIXED, 2063002 DUPLICATE,
        # 2062335) plus 3 of 200 background signatures had the whole facet empty. The old 0.0 was
        # a hardware clean bill printed to a model and to a filed bug on no measurement at all.
        out, _ = self._run({"total": 100, "facets": {"cpu_info": []}})
        self.assertIsNone(out["bit_flip_rate"])
        self.assertIsNone(out["broken_cpu_rate"])
        self.assertIsNone(out["broken_cpu_reports"])
        self.assertIsNone(out["top_cpu_share"])

    def test_an_empty_flip_facet_is_a_real_zero(self):
        # The asymmetry, and why `_sum` takes `if_empty` instead of a constant: Socorro sets
        # `possible_bit_flips_max_confidence` only when the stackwalker found a candidate, so no
        # rows there means no report has one. `cpu_info` is simply absent on some reports.
        out, _ = self._run({
            "total": 100,
            "facets": {"possible_bit_flips_max_confidence": [],
                       "cpu_info": [{"term": "family 6 model 60 stepping 3", "count": 100}]}})
        self.assertEqual(out["bit_flip_rate"], 0.0)
        self.assertEqual(out["broken_cpu_rate"], 0.0)
        self.assertEqual(out["top_cpu_share"], 1.0)

    def test_a_failed_lookup_never_raises(self):
        with mock.patch.object(sigage.socorro, "SuperSearch", side_effect=RuntimeError("boom")):
            self.assertEqual(sigage.hardware_noise("S")["reports"], None)
        self.assertEqual(sigage.hardware_noise("")["reports"], None)

    def test_it_keeps_the_cpu_rows_it_already_paid_for(self):
        # BUG 2065373, verbatim from Socorro on 2026-08-21: 58 reports, ONE `cpu_info` row. The
        # gate saw `broken_cpu_rate` 0.0 -- a hardware clean bill computed from the same rows
        # that say the whole population is one AMD model -- and threw the rows away.
        out, _ = self._run({
            "total": 58,
            "facets": {"cpu_info": [{"term": "family 25 model 117 stepping 2", "count": 58}]}})
        self.assertEqual(out["broken_cpu_rate"], 0.0)
        self.assertEqual(out["cpu_reports"], 58)
        self.assertEqual(out["cpu_terms"], 1)
        self.assertEqual(out["top_cpu_term"], "family 25 model 117 stepping 2")
        self.assertEqual(out["top_cpu_share"], 1.0)

    def test_the_share_is_of_the_reports_that_have_a_cpu_string(self):
        # NOT of `total`. Socorro has a cpu_info for 16.6% of Firefox-nightly macOS reports, so
        # denominating on `total` would report a mac-heavy signature as unconcentrated when it
        # is only unmeasured. 6 of the 10 known CPUs is 60%, not 6%.
        out, _ = self._run({
            "total": 100,
            "facets": {"cpu_info": [{"term": "family 6 model 60 stepping 3", "count": 6},
                                    {"term": "family 6 model 158 stepping 9", "count": 4}]}})
        self.assertEqual(out["cpu_reports"], 10)
        self.assertAlmostEqual(out["top_cpu_share"], 0.6)

    def test_the_top_row_is_the_biggest_one_not_the_first(self):
        # Socorro returns facets count-ordered, but nothing in the contract says so and the
        # `build_id` trap at the top of this module is the same mistake in the other direction.
        out, _ = self._run({
            "total": 10,
            "facets": {"cpu_info": [{"term": "a", "count": 3}, {"term": "b", "count": 7}]}})
        self.assertEqual(out["top_cpu_term"], "b")
        self.assertAlmostEqual(out["top_cpu_share"], 0.7)

    def test_every_answer_has_the_same_keys_as_the_unknown_one(self):
        # `orchestrator._hardware_noise` has to hand back this shape when the gate is off, and it
        # used to do that from a second hand-written literal that would silently fall behind.
        out, _ = self._run({"total": 1, "facets": {}})
        self.assertEqual(set(out), set(sigage.NO_HARDWARE_NOISE))
        self.assertEqual(set(sigage.hardware_noise("")), set(sigage.NO_HARDWARE_NOISE))
        with mock.patch.object(config, "get_agent_bit_flip", return_value=_cfg(enabled=False)):
            self.assertEqual(set(orch._hardware_noise({"signature": "S"}, "nightly")),
                             set(sigage.NO_HARDWARE_NOISE))

    def test_beta_is_asked_for_as_beta_plus_aurora(self):
        # Socorro files a third of beta under `aurora`: a raw "beta" returns 154,768 of the
        # 264,278 Firefox beta+aurora reports in a 364-day window (2026-08-21), so the rate is
        # computed on 59% of the channel. A no-op on nightly, which is all that runs today.
        with mock.patch.object(sigage.socorro, "SuperSearch") as ss:
            def ctor(params=None, handler=None, handlerdata=None, **kw):
                handler({"total": 1, "facets": {}}, handlerdata)
                return mock.Mock(wait=mock.Mock())
            ss.side_effect = ctor
            sigage.hardware_noise("S", channel="beta")
            self.assertEqual(ss.call_args.kwargs["params"]["release_channel"],
                             ["beta", "aurora"])
            sigage.hardware_noise("S", channel="nightly")
            self.assertEqual(ss.call_args.kwargs["params"]["release_channel"], "nightly")


class TestCpuConcentrationIsReportedNeverGated(unittest.TestCase):
    """The rank-8 audit result: the cpu_info concentration is a FACT the pipeline already paid
    for, and it is NOT a suppressor. Measured 2026-08-21 on the canary's 52 filings (19
    FIXED/DUPLICATE/ASSIGNED controls) at the gate's own `min_signature_reports` floor: every
    threshold from 0.40 to 0.95 eats at least one control, 0.50 eats five, AUC is 0.333 on 3 bad
    against 13 controls, and the shape is present in 13-35% of the triaged population."""

    # (bug, resolution, reports, flip rate, cpu rate, cpu terms, top share) -- real values,
    # `sigage.hardware_noise` on Firefox nightly over 364 days, 2026-08-21.
    CONTROLS = [
        (2062052, "FIXED", 6, 0.0, 0.0, 1, 1.000),      # ScreenOrientation::Create, 6/6 one CPU
        (2063678, "FIXED", 1111, 0.0, 0.0, 2, 0.973),   # libc.so.6 | cuEGLApiInit, Mageia/NVIDIA
        (2063809, "FIXED", 109, 0.0, 0.0, 5, 0.541),    # ff_vk_exec_add_dep_frame, AMD Vulkan
        (2061180, "DUPLICATE", 1251, 0.001, 0.0, 8, 0.767),   # libvulkan_radeon.so
        (2063864, "DUPLICATE", 18, 0.0, 0.0, 2, 0.833),       # setsockopt_syscall
    ]

    def setUp(self):
        p = mock.patch.object(config, "get_agent_bit_flip", return_value=_cfg())
        p.start()
        self.addCleanup(p.stop)

    def test_the_five_controls_a_concentration_rule_would_have_eaten(self):
        for bug, res, n, flip, cpu, terms, share in self.CONTROLS:
            with self.subTest(bug=bug, resolution=res):
                d = _lead()
                orch._apply_bit_flip_gate(d, _seed(
                    confidence=None, reports=n,
                    hardware_noise=_noise(reports=n, flip=flip, cpu=cpu, terms=terms,
                                          share=share)))
                self.assertEqual(d.verdict.decision, Decision.lead)
                self.assertEqual(d.corroborations["signature_top_cpu_share"], round(share, 3))

    def test_bug_2065373_is_measured_and_left_alone(self):
        # 58 reports, all on `family 25 model 117 stepping 2`, no bit flip, no Raptor Lake.
        # :jstutte called that filing "meaningful, but with some details to correct" and it is
        # still NEW, so a rule tuned to eat it would have eaten a filing the reviewer wanted.
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed(
            confidence=None, reports=58,
            hardware_noise=_noise(reports=58, flip=0.0, cpu=0.0, terms=1, share=1.0,
                                  term="family 25 model 117 stepping 2")))
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.corroborations["signature_top_cpu_share"], 1.0)
        self.assertEqual(d.corroborations["signature_cpu_terms"], 1)
        self.assertEqual(d.corroborations["signature_top_cpu_term"],
                         "family 25 model 117 stepping 2")
        self.assertEqual(d.corroborations["signature_cpu_reports"], 58)

    def test_an_unmeasured_spread_records_nothing(self):
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed(
            confidence=None, reports=99,
            hardware_noise=_noise(reports=99, flip=0.0, cpu=None, terms=None)))
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertNotIn("signature_top_cpu_share", d.corroborations)
        self.assertNotIn("signature_broken_cpu_rate", d.corroborations)

    def test_the_prompt_states_the_spread_with_its_background(self):
        lines = triage._crash_facts({"raw_crash": {}, "hardware_noise": _noise(
            reports=58, flip=0.0, cpu=0.0, terms=1, share=1.0,
            term="family 25 model 117 stepping 2")})
        spread = [ln for ln in lines if "CPU-MODEL SPREAD" in ln]
        self.assertEqual(len(spread), 1)
        self.assertIn("100% are on family 25 model 117 stepping 2", spread[0])
        self.assertIn("Of the 58 reports", spread[0])
        self.assertIn("32%", spread[0])          # the population median it must be read against
        self.assertIn("13% of them", spread[0])  # 26 of 200 sampled signatures
        self.assertIn("ORDINARY", spread[0])

    def test_the_spread_is_outside_the_suppression_paragraph(self):
        # THE COUNTER-EXAMPLE AGAINST MY OWN REPORTING FIX. "One cpu_info value" folded into the
        # "the higher these are, the likelier a failing-hardware artefact" paragraph is the
        # suppressor this audit killed, in prose: it would push a model to abstain on bugs 2056116
        # (the off-stack pref-flip archetype), 1993828 and 2063678, all real, all one-model.
        lines = triage._crash_facts({"raw_crash": {}, "hardware_noise": _noise(
            reports=58, flip=0.0, cpu=0.0, terms=1, share=1.0)})
        artefact = [ln for ln in lines if "failing-hardware artefact" in ln]
        self.assertEqual(len(artefact), 1)
        self.assertNotIn("CPU-MODEL SPREAD", artefact[0])
        spread = [ln for ln in lines if "CPU-MODEL SPREAD" in ln][0]
        self.assertNotIn("failing-hardware artefact", spread)
        self.assertIn("SCOPE", spread)

    def test_nothing_is_said_when_socorro_has_no_cpu_string(self):
        self.assertEqual(triage._cpu_spread_line({}), "")
        self.assertEqual(triage._cpu_spread_line(_noise(terms=None)), "")

    def test_the_filed_bug_prints_the_same_number(self):
        # The whole note on bug 2065373: no bit flip, no Raptor Lake, and the one fact about the
        # signature nothing else printed.
        note = report_bug.build_hardware_note({
            "signature_hardware_sample": 58, "signature_bit_flip_rate": 0.0,
            "signature_broken_cpu_rate": 0.0, "signature_cpu_reports": 58,
            "signature_cpu_terms": 1, "signature_top_cpu_share": 1.0,
            "signature_top_cpu_term": "family 25 model 117 stepping 2"})
        self.assertIn("CPU-model spread", note)
        self.assertIn("100% of the 58 reports", note)
        self.assertIn("family 25 model 117 stepping 2", note)
        self.assertIn("32%", note)
        self.assertIn("SCOPE", note)
        self.assertNotIn("Raptor Lake", note)
        self.assertNotIn("Hardware-error share", note)

    def test_one_report_is_not_a_spread(self):
        # THE SHAPE PRODUCTION ACTUALLY PRODUCES. One report carries one `cpu_info` string, so
        # `top_cpu_share` is 1.00 by arithmetic and clears any lift -- 18 of the canary's 52
        # filings have exactly one report and 17 of them read 1.00. Without the floor the crash
        # brief and every such bug comment would state "100% of the 1 reports are on one model,
        # and 13% of the population does that", against a background measured only on
        # signatures with >=5 reports. The share is still RECORDED; only the prose is silent.
        noise = _noise(reports=1, flip=0.0, cpu=0.0, terms=1, share=1.0,
                       term="family 6 model 94 stepping 3")
        self.assertEqual(triage._cpu_spread_line(noise), "")
        lines = triage._crash_facts({"raw_crash": {}, "hardware_noise": noise})
        self.assertEqual([ln for ln in lines if "CPU-MODEL SPREAD" in ln], [])
        # ... while the hardware-share paragraph it sits next to is unaffected.
        self.assertEqual(len([ln for ln in lines if "failing-hardware artefact" in ln]), 1)
        self.assertEqual(report_bug.build_hardware_note({
            "signature_hardware_sample": 1, "signature_bit_flip_rate": 0.0,
            "signature_broken_cpu_rate": 0.0, "signature_cpu_reports": 1,
            "signature_cpu_terms": 1, "signature_top_cpu_share": 1.0,
            "signature_top_cpu_term": "family 6 model 94 stepping 3"}), "")
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed(confidence=None, reports=1, hardware_noise=noise))
        self.assertEqual(d.corroborations["signature_top_cpu_share"], 1.0)

    def test_the_floor_is_the_share_s_own_denominator(self):
        # 4 known CPU strings out of 200 reports is still a share of four things. `cpu_reports`,
        # not `reports`, is what the percentage is computed from -- on macOS Socorro carries a
        # cpu_info for 16.6% of reports, so a busy signature can have a tiny one.
        noise = _noise(reports=200, flip=0.0, cpu=0.0, terms=1, share=1.0)
        noise.update(cpu_reports=4)
        self.assertEqual(triage._cpu_spread_line(noise), "")
        noise.update(cpu_reports=5)
        self.assertIn("Of the 5 reports", triage._cpu_spread_line(noise))

    def test_the_prompt_states_neither_direction(self):
        # The measurement says concentration is ANTI-correlated with carrying a real bug: 9 of
        # the 26 background signatures at 100% do, against 118 of 200 overall. So the line may
        # not tell the model a concentrated signature "usually has a code path behind it" --
        # that is the killed suppressor with its sign flipped, and it inflates leads.
        spread = triage._cpu_spread_line(_noise(reports=58, flip=0.0, cpu=0.0, terms=1,
                                                share=1.0))
        self.assertIn("NEITHER direction", spread)
        self.assertIn("LESS often", spread)
        self.assertNotIn("usually has a code path", spread)

    def test_an_ordinary_spread_is_not_mentioned(self):
        # The median nightly signature is at 32% and `_HARDWARE_NOTE_LIFT` puts the line at 0.64
        # -- 58 of 200 sampled signatures, 29%. Saying "41% are on one model" of a population
        # whose median is 32% is noise in a bug comment.
        note = report_bug.build_hardware_note({
            "signature_hardware_sample": 54, "signature_broken_cpu_rate": 0.24,
            "signature_cpu_reports": 54, "signature_cpu_terms": 14,
            "signature_top_cpu_share": 0.41, "signature_top_cpu_term": "family 6 model 154"})
        self.assertNotIn("CPU-model spread", note)

    def test_the_spread_rides_with_the_rates_when_both_fire(self):
        note = report_bug.build_hardware_note({
            "signature_hardware_sample": 19, "signature_bit_flip_rate": 0.0,
            "signature_broken_cpu_rate": 0.789, "signature_cpu_reports": 19,
            "signature_cpu_terms": 5, "signature_top_cpu_share": 0.789,
            "signature_top_cpu_term": "family 6 model 183 stepping 1"})
        self.assertIn("Hardware-error share", note)
        self.assertIn("79% come from the known-buggy Intel Raptor Lake", note)
        self.assertIn("CPU-model spread", note)


class TestTheVendorPrefixedRendering(unittest.TestCase):
    """Socorro renders `cpu_info` WITH a vendor prefix on 32-bit builds, and every comparison in
    the tree used to be an exact match against the amd64 rendering alone -- so the Raptor Lake
    detector was blind on x86 in all three places at once: the suppression, the signature-level
    rate, and the crash brief the agent reads.

    emilio, closing bug 2065969 INVALID: "The two crashes are indeed raptor lake, they're just
    x86, not amd64." The comment we filed had told him the signature carried 0% of them. Measured
    2026-08-24 on Firefox nightly: all 12 of the top `cpu_arch=x86` terms are prefixed and none of
    the top 12 amd64 terms is, and Raptor Lake is 25.5% of x86 reports against 8.7% of amd64."""

    X86 = "GenuineIntel family 6 model 183 stepping 1"
    AMD64 = "family 6 model 183 stepping 1"

    def setUp(self):
        p = mock.patch.object(config, "get_agent_bit_flip", return_value=_cfg())
        p.start()
        self.addCleanup(p.stop)

    def test_the_two_renderings_are_the_same_silicon(self):
        self.assertEqual(sigage.cpu_model(self.X86), self.AMD64)
        self.assertEqual(sigage.cpu_model("AuthenticAMD family 25 model 116 stepping 1"),
                         "family 25 model 116 stepping 1")
        self.assertIn(sigage.cpu_model(self.X86), sigage.BROKEN_CPU_MODELS)

    def test_it_only_ever_strips_a_token_before_a_family(self):
        # ARM reports carry no `family` at all, and mangling one into a bogus "model" would be a
        # silent false negative in the other direction.
        arm = "ARMv7 ARM part(0x4100c070) features: swp,half,thumb,fastmult"
        self.assertEqual(sigage.cpu_model(arm), arm)
        self.assertEqual(sigage.cpu_model("family 6 model 183 stepping 1"), self.AMD64)
        self.assertEqual(sigage.cpu_model(""), "")
        self.assertEqual(sigage.cpu_model(None), "")

    def test_a_32_bit_raptor_lake_singleton_is_suppressed(self):
        # THE BUG 2065969 SHAPE, at the gate. Read False before 2026-08-24.
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed(confidence=None, reports=1, cpu=self.X86))
        self.assertIs(d.corroborations["report_on_broken_cpu"], True)
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertTrue(d.corroborations["broken_cpu_suppressed"])

    def test_the_flag_still_records_what_socorro_actually_said(self):
        # The comparison normalises; the EVIDENCE does not. A reader of the dossier has to be able
        # to see which rendering the report carried.
        d = _lead()
        orch._apply_bit_flip_gate(d, _seed(confidence=None, reports=42, cpu=self.X86))
        self.assertEqual(d.corroborations["cpu_info"], self.X86)

    def test_the_brief_warns_the_agent_on_a_prefixed_cpu(self):
        facts = triage._crash_facts(
            {"raw_crash": {"cpu_arch": "x86", "cpu_info": self.X86}})
        cpu = [f for f in facts if f.startswith("CPU")][0]
        self.assertIn("KNOWN-DEFECTIVE", cpu)
        self.assertIn("1975808", cpu)

    def test_the_signature_rate_sums_both_renderings(self):
        with mock.patch.object(sigage.socorro, "SuperSearch") as ss:
            payload = {"total": 100,
                       "facets": {"cpu_info": [{"term": self.AMD64, "count": 30},
                                               {"term": self.X86, "count": 10},
                                               {"term": "family 6 model 60 stepping 3",
                                                "count": 60}]}}

            def ctor(params=None, handler=None, handlerdata=None, **kw):
                handler(payload, handlerdata)
                return mock.Mock(wait=mock.Mock())
            ss.side_effect = ctor
            out = sigage.hardware_noise("S")
        # 30 + 10, not 30: the x86 rows used to be thrown away by the exact match.
        self.assertEqual(out["broken_cpu_reports"], 40)
        self.assertAlmostEqual(out["broken_cpu_rate"], 0.4)
        # And the spread groups them too, or the rate would call them one processor while the
        # spread called them two and split its own denominator.
        self.assertEqual(out["cpu_terms"], 2)
        self.assertEqual(out["cpu_reports"], 100)
        self.assertEqual(out["top_cpu_term"], "family 6 model 60 stepping 3")
        self.assertAlmostEqual(out["top_cpu_share"], 0.6)
