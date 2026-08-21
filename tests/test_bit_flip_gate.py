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

from crashclouseau import config, sigage  # noqa: E402
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


def _noise(reports=6, flip=0.5, cpu=0.167):
    """`sigage.hardware_noise` for bug 2064600's signature on Firefox nightly over 364 days,
    measured 2026-08-19: 6 reports, 3 of them flip-annotated -- which is exactly the "about 50%"
    Timothy Nikkel quoted at us."""
    def _n(rate):
        return None if (rate is None or reports is None) else int(reports * rate)
    return {"reports": reports, "bit_flip_rate": flip, "broken_cpu_rate": cpu,
            "bit_flip_reports": _n(flip), "broken_cpu_reports": _n(cpu)}


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
        self.assertIn("50% carry a Socorro bit-flip annotation (crash population: 2%)", lines)
        self.assertIn("17% come from a known-defective Intel Raptor Lake", lines)
        self.assertIn("crash population: 4%", lines)  # nightly Raptor Lake background
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
        out, _ = self._run({"total": 100, "facets": {"cpu_info": []}})
        self.assertIsNone(out["bit_flip_rate"])
        self.assertEqual(out["broken_cpu_rate"], 0.0)

    def test_a_failed_lookup_never_raises(self):
        with mock.patch.object(sigage.socorro, "SuperSearch", side_effect=RuntimeError("boom")):
            self.assertEqual(sigage.hardware_noise("S")["reports"], None)
        self.assertEqual(sigage.hardware_noise("")["reports"], None)
