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

from crashclouseau import sigage  # noqa: E402
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

    def test_batches(self):
        sigs = ["sig{}".format(i) for i in range(sigage._FIRST_DATE_BATCH + 3)]
        with mock.patch.object(sigage.net, "get", return_value=_hits()) as get:
            sigage.first_seen_ever(sigs)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(len(get.call_args_list[0][1]["params"]["signatures"]),
                         sigage._FIRST_DATE_BATCH)
        self.assertEqual(len(get.call_args_list[1][1]["params"]["signatures"]), 3)

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


if __name__ == "__main__":
    unittest.main()
