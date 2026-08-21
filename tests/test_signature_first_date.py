# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Socorro's retention-free `SignatureFirstDate`, and the two clocks it makes visible.
#
# Every first-seen in `sigage` came from a SuperSearch bounded by 364 days, which Socorro's own
# Elasticsearch retention truncates further -- probed at ~178 days. Truncation moves first-seen
# FORWARD, so an ancient signature reads as a recent one. Measured on the ten `Core :: JavaScript*`
# bugs the canary filed and a module owner rejected: `js::detail::BumpChunk::assertInvariants` was
# recorded as first seen 2025-12-27 and is really 2017-10-28, and
# `PerformPromiseThenWithoutSettleHandlers` was recorded as three days old and is really 1311.
#
# The fix is NOT to swap the gate's clock -- see `sigage.first_seen_ever`, and test
# `test_gate_clock_is_unchanged` below, which pins that decision. It is to carry the true value
# alongside, for the NOVELTY question, where an unbounded clock is strictly conservative.
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_signature_first_date
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402
from urllib.parse import urlencode  # noqa: E402

from crashclouseau import report_bug, sigage  # noqa: E402
from crashclouseau.agent import triage  # noqa: E402
from crashclouseau.agent import orchestrator as orch  # noqa: E402
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


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP {}".format(self.status_code))

    def json(self):
        return self._payload


def _hits(*pairs):
    return _Resp({"hits": [{"signature": s, "first_build": b, "first_date": "2020-01-01"}
                           for s, b in pairs],
                  "total": len(pairs)})


class TestFirstSeenEver(unittest.TestCase):
    def test_maps_signature_to_build(self):
        with mock.patch.object(sigage.net, "get", return_value=_hits(
                ("js::detail::BumpChunk::assertInvariants", "20171028220326"),
                ("nsAtom::IsStatic", "20180312152746"))) as get:
            out = sigage.first_seen_ever(
                ["js::detail::BumpChunk::assertInvariants", "nsAtom::IsStatic"])
        self.assertEqual(out, {"js::detail::BumpChunk::assertInvariants": "20171028220326",
                               "nsAtom::IsStatic": "20180312152746"})
        self.assertEqual(get.call_args[0][0], sigage.SIGNATURE_FIRST_DATE_URL)
        self.assertEqual(get.call_args[1]["params"]["signatures"],
                         ["js::detail::BumpChunk::assertInvariants", "nsAtom::IsStatic"])

    def test_batches_by_url_bytes_not_by_count(self):
        # Real signatures reach Socorro's own 255-char SigTruncate ceiling, and at that length
        # crash-stats rejects the query string: measured live, 10 per request is 3217 bytes and a
        # 200, 20 is 6559 and a 400, 30 is 9925 and a 414. Batching by count was silently wrong.
        long_sigs = ["A" * 255 for _ in range(20)]
        with mock.patch.object(sigage.net, "get", return_value=_hits()) as get:
            sigage.first_seen_ever(long_sigs)
        for call in get.call_args_list:
            url = sigage.SIGNATURE_FIRST_DATE_URL + "?" + urlencode(
                {"signatures": call[1]["params"]["signatures"]}, doseq=True)
            self.assertLessEqual(len(url), 4094, "a request would be rejected outright")
        self.assertEqual(sum(len(c[1]["params"]["signatures"]) for c in get.call_args_list),
                         len(long_sigs), "every signature must be asked about exactly once")

    def test_short_signatures_still_batch_generously(self):
        # The byte budget must not turn into one-request-per-signature for ordinary names.
        with mock.patch.object(sigage.net, "get", return_value=_hits()) as get:
            sigage.first_seen_ever(["nsAtom::Release"] * 40)
        self.assertLessEqual(get.call_count, 2)

    def test_an_oversized_signature_is_still_asked_about(self):
        # Dropping it would be the exact silent-absence failure the budget exists to prevent:
        # the caller cannot tell "we never asked" from "Socorro has no row".
        huge = "B" * 5000
        with mock.patch.object(sigage.net, "get", return_value=_hits()) as get:
            sigage.first_seen_ever([huge, "nsAtom::Release"])
        asked = [s for c in get.call_args_list for s in c[1]["params"]["signatures"]]
        self.assertIn(huge, asked)
        self.assertIn("nsAtom::Release", asked)

    def test_no_signatures_makes_no_request(self):
        with mock.patch.object(sigage.net, "get") as get:
            self.assertEqual(sigage.first_seen_ever([]), {})
            self.assertEqual(sigage.first_seen_ever(None), {})
        get.assert_not_called()

    def test_failure_is_absence_not_a_new_signature(self):
        # The whole point of the field is to say "this signature is old". A lookup that failed
        # must be indistinguishable from "we did not ask", never from "first seen today".
        for outcome in (RuntimeError("boom"), _Resp({}, status=503), _Resp("not json")):
            with self.subTest(outcome=outcome):
                side = ({"side_effect": outcome} if isinstance(outcome, Exception)
                        else {"return_value": outcome})
                with mock.patch.object(sigage.net, "get", **side):
                    self.assertEqual(sigage.first_seen_ever(["sig"]), {})

    def test_row_without_a_build_is_dropped(self):
        # `first_date` is a CRASH date and can post-date the build by months; every comparison in
        # this module is between buildids, so a row with no `first_build` is no answer at all.
        payload = _Resp({"hits": [{"signature": "sig", "first_build": None,
                                   "first_date": "2020-01-01"}]})
        with mock.patch.object(sigage.net, "get", return_value=payload):
            self.assertEqual(sigage.first_seen_ever(["sig"]), {})


class TestSignatureHistoryIsUntouched(unittest.TestCase):
    """`signature_history` batches its two SuperSearches into ONE round-trip, and says so. The
    new lookup is a different endpoint with its own failure mode, so it is issued from
    `build_seed` instead -- folding it in here would break that contract silently, and would
    make every test that mocks `socorro.SuperSearch` start reaching the network."""

    def test_no_first_date_call_from_signature_history(self):
        with mock.patch.object(sigage, "first_seen_ever") as ever, \
                mock.patch.object(sigage.net, "get") as get, \
                mock.patch.object(sigage.socorro, "SuperSearch") as ss:
            ss.return_value.wait.return_value = None
            history = sigage.signature_history("sig")
        ever.assert_not_called()
        get.assert_not_called()
        self.assertNotIn("first_seen_ever", history)


class TestSignatureAgeDays(unittest.TestCase):
    def test_age_at_the_build(self):
        self.assertEqual(sigage.signature_age_days("20260801000000", "20260808000000"), 7.0)

    def test_unknown_either_side_is_none(self):
        for first, build in ((None, "20260808000000"), ("20260801000000", None),
                             ("", ""), ("nonsense", "20260808000000")):
            with self.subTest(first=first, build=build):
                self.assertIsNone(sigage.signature_age_days(first, build))

    def test_the_two_clocks_disagree_by_years(self):
        # The real numbers from bug 2062173: the gate saw a 221-day-old signature; it is 3205.
        build = "20260810000000"
        self.assertLess(sigage.signature_age_days("20251227210510", build), 300)
        self.assertGreater(sigage.signature_age_days("20171028220326", build), 3000)


def _dossier(**corr):
    return Dossier(
        crash={"uuid": "u", "signature": "sig", "frames": []},
        verdict=Verdict(
            decision=Decision.lead,
            confidence=Confidence.probable,
            mechanism=Claim(summary="s", citations=[_SF]),
        ),
        candidate=Candidate(node="a" * 12, bug=1, author="A", channel="nightly"),
        corroborations=dict(corr),
    )


class TestRecordSignatureAgeFacts(unittest.TestCase):
    def test_records_both_clocks_and_both_ages(self):
        d = _dossier()
        orch._record_signature_age_facts(d, {
            "buildid": "20260810000000",
            "signature_first_seen_buildid": "20251227210510",
            "signature_first_seen_ever": "20171028220326",
        })
        self.assertEqual(d.corroborations["signature_first_seen_windowed"], "20251227210510")
        self.assertEqual(d.corroborations["signature_first_seen_ever"], "20171028220326")
        self.assertLess(d.corroborations["signature_age_days_windowed"], 300)
        self.assertGreater(d.corroborations["signature_age_days_ever"], 3000)

    def test_an_unanswered_clock_is_absent_not_zero(self):
        d = _dossier()
        orch._record_signature_age_facts(d, {
            "buildid": "20260810000000", "signature_first_seen_buildid": None,
            "signature_first_seen_ever": None,
        })
        for key in ("signature_first_seen_windowed", "signature_first_seen_ever",
                    "signature_age_days_windowed", "signature_age_days_ever"):
            self.assertNotIn(key, d.corroborations)

    def test_unknown_buildid_still_records_the_first_seen(self):
        # The first-seen itself is worth having even when the age cannot be computed.
        d = _dossier()
        orch._record_signature_age_facts(d, {
            "buildid": None, "signature_first_seen_ever": "20171028220326"})
        self.assertEqual(d.corroborations["signature_first_seen_ever"], "20171028220326")
        self.assertNotIn("signature_age_days_ever", d.corroborations)

    def test_records_on_an_abstain_too(self):
        # Most of the corpus abstains, and "was this signature ever new?" is exactly the question
        # those runs can answer. A recorder that only ran on reported verdicts would be blind to
        # the population it is meant to measure.
        d = _dossier()
        d.verdict = Verdict(decision=Decision.abstain, confidence=Confidence.low,
                            abstain_reason="nothing found")
        orch._record_signature_age_facts(d, {
            "buildid": "20260810000000", "signature_first_seen_ever": "20260808000000"})
        self.assertEqual(d.corroborations["signature_age_days_ever"], 2.0)

    def test_moves_no_rung(self):
        d = _dossier()
        orch._record_signature_age_facts(d, {
            "buildid": "20260810000000", "signature_first_seen_ever": "20171028220326"})
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.verdict.confidence, Confidence.probable)

    def test_never_raises_on_a_junk_seed(self):
        for seed in (None, {}, {"buildid": object()}):
            with self.subTest(seed=seed):
                orch._record_signature_age_facts(_dossier(), seed)
        orch._record_signature_age_facts(None, {"buildid": "20260810000000"})


class TestRenameSuspected(unittest.TestCase):
    """A signature is only NEW if nobody renamed an old crash onto it.

    `SignatureFirstDate` claims an all-time minimum; Elasticsearch reaches back ~178 days. ES
    holding an OLDER build is impossible unless those documents were re-signatured after their
    window closed -- Socorro's cron walks a rolling window of `date_processed`, which is the
    SUBMITTED timestamp and is never refreshed on reprocessing. Live example measured this session:
    `rlbox::detail::dynamic_check | rlbox::rlbox_sandbox<T>::register_callback<T>` reports
    first_build 20260609153453 (age 0) while ES holds 12 reports back to build 20251106194447."""

    def _facts(self, windowed, ever, buildid="20260810000000"):
        d = _dossier()
        orch._record_signature_age_facts(d, {
            "buildid": buildid, "signature_first_seen_buildid": windowed,
            "signature_first_seen_ever": ever})
        return d.corroborations

    def test_fires_when_es_holds_an_older_build_than_the_all_time_minimum(self):
        facts = self._facts(windowed="20251106194447", ever="20260609153453")
        self.assertTrue(facts["signature_rename_suspected"])
        self.assertLess(facts["signature_clock_drift_days"], -_thirty())

    def test_cron_lag_is_not_a_rename(self):
        # The only one of 450 non-novel controls to invert at all did so by a single day.
        facts = self._facts(windowed="20260809000000", ever="20260810000000")
        self.assertNotIn("signature_rename_suspected", facts)

    def test_the_normal_direction_is_not_a_rename(self):
        # The unbounded clock being OLDER than the windowed one is the expected case -- it is the
        # whole reason `first_seen_ever` exists -- and must never be read as a rename.
        facts = self._facts(windowed="20251227210510", ever="20171028220326")
        self.assertNotIn("signature_rename_suspected", facts)
        self.assertGreater(facts["signature_clock_drift_days"], 0)

    def test_needs_both_clocks(self):
        for windowed, ever in (("20260809000000", None), (None, "20260810000000"), (None, None)):
            with self.subTest(windowed=windowed, ever=ever):
                self.assertNotIn("signature_rename_suspected", self._facts(windowed, ever))

    def test_it_only_suppresses_novelty_it_never_moves_a_rung(self):
        d = _dossier()
        orch._record_signature_age_facts(d, {
            "buildid": "20260810000000", "signature_first_seen_buildid": "20251106194447",
            "signature_first_seen_ever": "20260609153453"})
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.verdict.confidence, Confidence.probable)


def _thirty():
    return sigage.RENAME_DRIFT_DAYS - 0.5


class TestGateClockIsUnchanged(unittest.TestCase):
    """The decision `sigage.first_seen_ever` documents, pinned.

    Substituting the unbounded clock into `_apply_signature_age_gate` would clamp eight of the
    sixteen filings a human acted on -- `FindSafeLength` (first seen 2010-10-26, FIXED) and
    `nsJARProtocolHandler::MimeService` (2014-01-23, FIXED) among them. A signature being old
    does not stop a new patch from causing the crash, so that comparison stays on the windowed
    clock until somebody back-tests a replacement."""

    def test_gate_reads_the_windowed_clock_only(self):
        d = _dossier()
        node = "a" * 12
        seed = {
            "uuid": "u", "channel": "nightly", "buildid": "20260810000000",
            # FindSafeLength's real first appearance. If the gate ever reads this key, a
            # candidate that landed last week looks 5767 days late and the lead is clamped.
            "signature_first_seen_ever": "20101026000000",
            "signature_first_seen_buildid": "20260808000000",
            "candidate_pushdates": {node: "20260809000000"},
        }
        with mock.patch.object(orch.config, "get_agent_signature_age",
                               return_value={"enabled": True, "min_age_days": 7,
                                             "other_channel_floor": 20}):
            orch._apply_signature_age_gate(d, seed)
        self.assertEqual(d.verdict.confidence, Confidence.probable)
        self.assertNotIn("stale_signature", d.corroborations)


# ---------------------------------------------------------------------------------------------
# Saying it out loud. Until this, the age reached no prompt, no bug comment and no human -- we
# fetched it every run and told nobody, while nine of the ten `Core :: JavaScript*` filings a
# module owner rejected accused a changeset landing 283 to 3205 days after the signature was
# already crashing. This repo has shipped three recovery paths that never fired in prod, so the
# tests below pin REACHABILITY (does it get into the prompt / the comment at all?) as hard as
# they pin the wording.

# bug 2062173, the canonical case: the windowed clock said 2025-12-27 and the truth is 2017-10-28.
_BUG_2062173 = {
    "buildid": "20260810000000",
    "signature_first_seen_buildid": "20251227210510",
    "signature_first_seen_ever": "20171028220326",
}


def _brief(**seed):
    return "\n".join(triage._signature_age_lines(seed))


def _note(buildid=None, **seed):
    facts = sigage.age_facts(
        buildid or seed.get("buildid"),
        seed.get("signature_first_seen_buildid"),
        seed.get("signature_first_seen_ever"),
        observed=seed.get("signature_first_seen_any") or seed.get("signature_first_seen_buildid"),
    )
    return report_bug.build_signature_age_note(facts, buildid or seed.get("buildid"))


class TestAgeFactsIsOneComputation(unittest.TestCase):
    """The recorder, the crash brief and the filed bug all state the same numbers to a model, to
    a reviewer and to the archive. Three copies of this arithmetic would eventually disagree, and
    the disagreement would be invisible -- so there is one function and the recorder uses it."""

    def test_the_recorder_writes_exactly_age_facts(self):
        d = _dossier()
        orch._record_signature_age_facts(d, _BUG_2062173)
        self.assertEqual(d.corroborations, sigage.age_facts(
            _BUG_2062173["buildid"],
            _BUG_2062173["signature_first_seen_buildid"],
            _BUG_2062173["signature_first_seen_ever"]))

    def test_neither_clock_answering_is_an_empty_record(self):
        self.assertEqual(sigage.age_facts("20260810000000", None, None), {})

    def test_buildid_day_is_readable_or_empty(self):
        self.assertEqual(sigage.buildid_day("20171028220326"), "2017-10-28")
        for junk in (None, "", "tip", 12):
            self.assertEqual(sigage.buildid_day(junk), "")


class TestTheCrashBriefStatesTheAge(unittest.TestCase):
    def test_it_states_the_unbounded_clock_as_the_answer(self):
        # The windowed date may appear, but only as the error being corrected -- never as the age.
        text = _brief(**_BUG_2062173)
        self.assertIn("20171028220326", text)
        self.assertIn("2017-10-28", text)
        self.assertIn("3207 days before the build", text)
        self.assertNotIn("225 days before the build", text)

    def test_it_explains_the_disagreement_so_crash_stats_does_not_contradict_us(self):
        text = _brief(**_BUG_2062173)
        self.assertIn("would call this signature 225 days old", text)
        self.assertIn("the older figure is the true one", text)

    def test_agreeing_clocks_get_no_second_date(self):
        text = _brief(buildid="20260810000000",
                      signature_first_seen_buildid="20250810000000",
                      signature_first_seen_ever="20250810000000")
        self.assertIn("365 days before the build", text)
        self.assertNotIn("Do not be misled", text)

    def test_a_new_signature_is_told_the_window_is_trustworthy(self):
        text = _brief(buildid="20260820120000",
                      signature_first_seen_buildid="20260819210808",
                      signature_first_seen_ever="20260819210808")
        self.assertIn("pushlog window below is genuinely trustworthy", text)
        self.assertNotIn("landed long after", text)

    def test_an_old_signature_does_not_clear_the_candidate(self):
        # The `nightly_vs_beta` lesson and FIXED 2061960: a 326-day-old signature whose named
        # developer pushed the fix. This block must never read as "stale signature, no regressor".
        text = _brief(**_BUG_2062173)
        self.assertIn("does NOT clear the changeset", text)
        self.assertIn("2061960", text)

    def test_an_undatable_signature_is_never_called_new(self):
        text = _brief(buildid="20260820000000",
                      signature_first_seen_buildid="20260819210808")
        self.assertIn("not established", text)
        self.assertIn("is not 'it is new'", text)
        self.assertNotIn("genuinely trustworthy", text)

    def test_a_rename_withdraws_novelty_and_never_asserts_it(self):
        text = _brief(buildid="20260810000000",
                      signature_first_seen_any="20251106194447",
                      signature_first_seen_ever="20260609153453")
        self.assertIn("re-signatured onto this name", text)
        self.assertIn("the crash did not necessarily start", text)
        self.assertNotIn("genuinely trustworthy", text)

    def test_same_build_is_only_claimed_when_it_is_the_same_build(self):
        same = _brief(buildid="20260819210808",
                      signature_first_seen_buildid="20260819210808",
                      signature_first_seen_ever="20260819210808")
        self.assertIn("which is this crash's own build", same)
        near = _brief(buildid="20260820120000",
                      signature_first_seen_buildid="20260819210808",
                      signature_first_seen_ever="20260819210808")
        self.assertIn("less than a day before the build", near)
        self.assertNotIn("own build", near)

    def test_it_never_says_1_days(self):
        # A crash brief that says "1 days" reads as a template, and the point of the block is to
        # be read. `_days_phrase` is the one place that decides, so test it directly.
        self.assertEqual(triage._days_phrase(0.0), "less than a day")
        self.assertEqual(triage._days_phrase(0.4), "less than a day")
        self.assertEqual(triage._days_phrase(1.0), "1 day")
        self.assertEqual(triage._days_phrase(1.9), "1 day")
        self.assertEqual(triage._days_phrase(2.0), "2 days")
        text = _brief(buildid="20260820210808", signature_first_seen_buildid="20260819210808",
                      signature_first_seen_ever="20260819210808")
        self.assertIn("1 day before the build", text)

    def test_nothing_known_says_nothing(self):
        self.assertEqual(triage._signature_age_lines({"buildid": "20260810000000"}), [])
        self.assertEqual(triage._signature_age_lines({}), [])


class TestTheAgeReachesBothModels(unittest.TestCase):
    """`_crash_facts` is shared with the blind second opinion, and that is the point rather than
    an accident. It is the bit-flip precedent, not the archetype one: an age is a FACT both models
    are blind to, not a suggested direction, and the blindness is measured -- the SO corroborated
    11 of 11 JS filings, because a nine-year-old signature does not change the answer to "is this
    mechanism plausible?" while it settles "did this patch create this crash?"."""

    def _seed(self):
        return {"uuid": "u", "signature": "sig", "channel": "nightly", "stack": "#0 f", **_BUG_2062173}

    def test_the_triage_prompt_carries_it(self):
        self.assertIn("SIGNATURE AGE:", triage._user_prompt(self._seed()))

    def test_the_blind_second_opinion_carries_it_too(self):
        from crashclouseau.agent import second_opinion

        self.assertIn("SIGNATURE AGE:", second_opinion._user_prompt(self._seed(), None))


class TestTheFiledBugStatesTheAge(unittest.TestCase):
    """Onset anchoring: 12 of 12 archive bugs that named a regressor said which build the
    signature started in, and when it was old, 7 of 7 stopped naming a regressor at all."""

    def test_it_states_the_unbounded_clock(self):
        note = _note(**_BUG_2062173)
        self.assertIn("This signature is not new", note)
        self.assertIn("20171028220326 (2017-10-28)", note)
        self.assertIn("3207 days before the build above", note)

    def test_it_pre_empts_the_reader_checking_crash_stats(self):
        self.assertIn("only reaches 2025-12-27", _note(**_BUG_2062173))

    def test_agreeing_clocks_get_no_parenthetical(self):
        note = _note(buildid="20260810000000",
                     signature_first_seen_buildid="20250810000000",
                     signature_first_seen_ever="20250810000000")
        self.assertNotIn("364-day", note)

    def test_new_is_said_only_when_the_dates_say_so(self):
        self.assertIn("This signature is new", _note(
            buildid="20260819210808", signature_first_seen_buildid="20260819210808",
            signature_first_seen_ever="20260819210808"))

    def test_an_undatable_signature_makes_no_claim_it_cannot_support(self):
        # No `SignatureFirstDate` row AND the windowed first-seen is an earlier build: we do not
        # know, so the bug says nothing rather than guessing at the reader's expense.
        self.assertEqual(_note(buildid="20260820120000",
                               signature_first_seen_buildid="20260819210808"), "")

    def test_an_undatable_signature_first_seen_in_this_build_says_what_is_checkable(self):
        note = _note(buildid="20260819210808", signature_first_seen_buildid="20260819210808")
        self.assertIn("no report of this signature on any earlier build", note)
        self.assertIn("as far as we can tell", note)

    def test_a_rename_is_stated_as_a_withdrawal(self):
        note = _note(buildid="20260810000000",
                     signature_first_seen_any="20251106194447",
                     signature_first_seen_ever="20260609153453")
        self.assertIn("re-signatured onto this name", note)
        self.assertIn("The name is new; the crash may not be.", note)
        self.assertNotIn("This signature is new", note)

    def test_nothing_known_emits_no_section(self):
        for corr in (None, {}, {"candidate_in_pushlog_window": True}):
            self.assertEqual(report_bug.build_signature_age_note(corr, "20260810000000"), "")

    def test_the_note_is_in_the_filed_comment_after_the_volume_sentence(self):
        dossier = {"candidate": {"node": "abc123"}, "verdict": {},
                   "corroborations": sigage.age_facts(
                       _BUG_2062173["buildid"],
                       _BUG_2062173["signature_first_seen_buildid"],
                       _BUG_2062173["signature_first_seen_ever"])}
        stack = {"frames": [{"stackpos": 0, "function": "Foo::bar", "filename": "dom/Foo.cpp",
                             "line": 51, "module": "xul.dll"}]}
        comment = report_bug.build_bug_comment(
            {"uuid": "u-1", "channel": "nightly", "buildid": "20260810000000"},
            stack, dossier, stats={"count": 2, "installs": 2}, first=True, version="155.0a1")
        self.assertIn("This signature is not new", comment)
        self.assertLess(comment.find("There are 2 crashes"),
                        comment.find("This signature is not new"))


if __name__ == "__main__":
    unittest.main()
