# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Stale-signature downweight: a signature that already existed long before its own build cannot
# have been introduced by anything in that build's pushlog window.
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_signature_age_gate
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import config, report_bug, sigage  # noqa: E402
from crashclouseau.agent import orchestrator as orch  # noqa: E402
from crashclouseau.agent.result import CrashTriageResult  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    Candidate,
    Claim,
    Confidence,
    Decision,
    Dossier,
    SearchfoxCitation,
    SecondOpinion,
    Verdict,
)

_SF = SearchfoxCitation(
    permalink="https://searchfox.org/x#1", symbol_id="_Z1", repo="mozilla-central"
)
_SEED = {"uuid": "u-1", "signature": "S", "channel": "nightly", "stack": "#0 f a:1",
         "is_offstack": False}


def _lead(confidence=Confidence.medium):
    return Dossier(
        candidate=Candidate(node="abc123def456", bug=42),
        verdict=Verdict(decision=Decision.lead, confidence=confidence,
                        needinfo_draft="could you take a look?"),
    )


def _strong():
    return Dossier(
        candidate=Candidate(node="abc123def456", bug=42),
        verdict=Verdict(
            decision=Decision.strong_evidence, confidence=Confidence.high,
            needinfo_draft="this looks like the regressor",
            mechanism=Claim(text="null deref of mFoo", citations=[_SF]),
            consistency=Claim(text="matches the stack", citations=[_SF]),
        ),
    )


_FIRST_SEEN = "20260101000000"          # 2026-01-01


def _seed(landed_after_days, first_seen=_FIRST_SEEN, node="abc123def456"):
    """A seed whose candidate landed `landed_after_days` after the signature was first seen.
    Negative = the candidate predates the crash's first appearance (the healthy case)."""
    seen = datetime.strptime(first_seen, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return {**_SEED,
            "signature_first_seen_buildid": first_seen,
            "candidate_pushdates": {node: seen + timedelta(days=landed_after_days)}}


class TestSignatureAgeGate(unittest.TestCase):
    def test_stale_signature_clamps_a_lead_one_rung(self):
        d = _lead(Confidence.probable)
        orch._apply_signature_age_gate(d, _seed(178.0))
        self.assertEqual(d.verdict.confidence, Confidence.medium)
        self.assertTrue(d.corroborations["stale_signature"])
        self.assertTrue(d.corroborations["stale_signature_clamped"])
        self.assertEqual(d.corroborations["candidate_landed_after_first_seen_days"], 178.0)
        self.assertEqual(d.corroborations["signature_first_seen_buildid"], _FIRST_SEEN)

    def test_clamp_is_one_rung_only(self):
        d = _lead(Confidence.medium)
        orch._apply_signature_age_gate(d, _seed(178.0))
        self.assertEqual(d.verdict.confidence, Confidence.low)

    def test_never_below_a_reportable_lead(self):
        # `low` is the floor: an old signature must not silently delete a report. Recall-safety.
        d = _lead(Confidence.low)
        orch._apply_signature_age_gate(d, _seed(400.0))
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.verdict.confidence, Confidence.low)
        self.assertTrue(d.corroborations["stale_signature"])          # still flagged
        self.assertNotIn("stale_signature_clamped", d.corroborations)  # but nothing moved

    def test_candidate_outside_the_seed_falls_back_to_hg(self):
        # The agent chose a node the seed never priced (it found it via blame), so the gate has
        # to resolve the landing date itself instead of skipping. Skipping is what let a
        # 126-day-stale lead ship at 80% worth-investigating in prod.
        seen = datetime.strptime(_FIRST_SEEN, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        landed = seen + timedelta(days=126.6)
        d = _lead(Confidence.probable)
        with mock.patch.object(sigage, "pushdate_for_node",
                               return_value=landed.timestamp()) as pd:
            orch._apply_signature_age_gate(d, _seed(178.0, node="99999999beef"))
        pd.assert_called_once_with("abc123def456", "nightly")
        self.assertEqual(d.verdict.confidence, Confidence.medium)
        self.assertTrue(d.corroborations["stale_signature_clamped"])
        self.assertEqual(d.corroborations["candidate_landed_after_first_seen_days"], 126.6)

    def test_seeded_pushdate_wins_no_lookup(self):
        # The pre-computed pushdate is authoritative: no hg request when the seed has one.
        d = _lead(Confidence.probable)
        with mock.patch.object(sigage, "pushdate_for_node") as pd:
            orch._apply_signature_age_gate(d, _seed(178.0))
        pd.assert_not_called()
        self.assertEqual(d.verdict.confidence, Confidence.medium)

    def test_unresolvable_pushdate_is_a_no_op(self):
        # hg could not date the changeset either -> unknown timing must not penalise a verdict.
        d = _lead(Confidence.probable)
        with mock.patch.object(sigage, "pushdate_for_node", return_value=None):
            orch._apply_signature_age_gate(d, _seed(178.0, node="99999999beef"))
        self.assertEqual(d.verdict.confidence, Confidence.probable)
        self.assertEqual(d.corroborations, {})

    def test_no_lookup_before_first_seen_is_known(self):
        # Offline purity: an eval seed has no first-seen buildid, so the gate must return
        # BEFORE it would ever reach for the network.
        d = _lead(Confidence.probable)
        with mock.patch.object(sigage, "pushdate_for_node") as pd:
            orch._apply_signature_age_gate(d, {**_SEED})
        pd.assert_not_called()

    def test_no_candidate_is_a_no_op(self):
        d = Dossier(
            hunks=[],
            candidate=None,
            verdict=Verdict(decision=Decision.abstain, abstain_reason="no candidate"),
        )
        orch._apply_signature_age_gate(d, _seed(178.0))
        self.assertEqual(d.corroborations, {})

    def test_candidate_landing_just_after_is_untouched(self):
        d = _lead(Confidence.probable)
        orch._apply_signature_age_gate(d, _seed(2.0))
        self.assertEqual(d.verdict.confidence, Confidence.probable)
        self.assertEqual(d.corroborations, {})

    def test_candidate_predating_first_seen_is_untouched(self):
        # The candidate landed BEFORE the crash was ever seen: it can perfectly well be the
        # origin. The healthy case, and it must never be penalised.
        d = _lead(Confidence.probable)
        orch._apply_signature_age_gate(d, _seed(-3.0))
        self.assertEqual(d.verdict.confidence, Confidence.probable)
        self.assertEqual(d.corroborations, {})

    def test_unknown_first_seen_is_a_no_op(self):
        # Offline / lookup failed. Must not penalise the verdict on missing data.
        d = _lead(Confidence.probable)
        orch._apply_signature_age_gate(d, {**_SEED})
        self.assertEqual(d.verdict.confidence, Confidence.probable)
        self.assertEqual(d.corroborations, {})

    def test_strong_evidence_is_flagged_but_not_downgraded(self):
        d = _strong()
        orch._apply_signature_age_gate(d, _seed(178.0))
        self.assertEqual(d.verdict.decision, Decision.strong_evidence)
        self.assertEqual(d.verdict.confidence, Confidence.high)
        self.assertTrue(d.corroborations["stale_signature"])
        self.assertNotIn("stale_signature_clamped", d.corroborations)

    def test_abstain_with_a_candidate_is_flagged_but_not_touched(self):
        d = Dossier(
            candidate=Candidate(node="abc123def456", bug=42),
            verdict=Verdict(decision=Decision.abstain, abstain_reason="nothing"),
        )
        orch._apply_signature_age_gate(d, _seed(178.0))
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertTrue(d.corroborations["stale_signature"])
        self.assertNotIn("stale_signature_clamped", d.corroborations)

    def test_disabled_is_a_no_op(self):
        d = _lead(Confidence.probable)
        with mock.patch.object(orch.config, "get_agent_signature_age",
                               return_value={"enabled": False, "min_age_days": 7}):
            orch._apply_signature_age_gate(d, _seed(178.0))
        self.assertEqual(d.verdict.confidence, Confidence.probable)
        self.assertEqual(d.corroborations, {})

    def test_threshold_boundary(self):
        # min_age_days=7 is exclusive: exactly 7 days is NOT stale (a build's own window plus
        # slack for a late-reported crash must not trip it).
        at = _lead(Confidence.probable)
        orch._apply_signature_age_gate(at, _seed(7.0))
        self.assertEqual(at.verdict.confidence, Confidence.probable)
        over = _lead(Confidence.probable)
        orch._apply_signature_age_gate(over, _seed(7.1))
        self.assertEqual(over.verdict.confidence, Confidence.medium)

    def test_never_raises_on_a_missing_verdict(self):
        orch._apply_signature_age_gate(Dossier(verdict=None), _seed(178.0))   # must not raise


class TestThroughTheGates(unittest.TestCase):
    def _result(self, dossier):
        return CrashTriageResult(num_turns=1, total_cost_usd=0.1, result="ok", dossier=dossier)

    def test_clamp_reaches_the_shipped_verdict_and_p_worth(self):
        r = self._result(_lead(Confidence.probable))
        orch.apply_deterministic_gates(r, _seed(178.0))
        self.assertEqual(r.dossier.verdict.confidence, Confidence.medium)
        # The calibrated badge must follow the clamped rung, not the raw one.
        self.assertEqual(r.dossier.raw_verdict.confidence, Confidence.probable)
        self.assertLess(r.dossier.verdict.p_worth_investigating, 0.97)

    def test_eval_seeds_without_the_key_are_unaffected(self):
        # The offline eval runner passes a seed with no signature_age_days: calibration must keep
        # scoring the same pipeline it did before this gate existed.
        r = self._result(_lead(Confidence.probable))
        orch.apply_deterministic_gates(r, {**_SEED})
        self.assertEqual(r.dossier.verdict.confidence, Confidence.probable)
        self.assertNotIn("stale_signature", r.dossier.corroborations)

    def test_clamp_survives_a_corroboration_bump(self):
        # The gate runs AFTER _apply_corroboration_gate on purpose, so the timing clamp gets the
        # last word on the rung rather than being handed back to the bump.
        from crashclouseau.agent.schema import DataFlowHypothesis, StructLayoutCitation
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
        seed = {**_seed(178.0),
                "raw_crash": {"json_dump": {"crash_info": {"address": "0x8"}}},
                # The gate fails closed: without the searchfox answer there is no bump to
                # clamp, and this test would pass for the wrong reason.
                "struct_layout": {
                    "fault": 8, "status": "verified", "refuted": [], "unresolved": [],
                    "verified": [{"type": "T", "field": "mLength", "offset": 8,
                                  "actual": "mLength"}]}}
        orch.apply_deterministic_gates(r, seed)
        # corroboration raised medium -> probable, then the stale clamp took it back to medium.
        self.assertTrue(r.dossier.corroborations["fault_address_offset_match"])
        self.assertTrue(r.dossier.corroborations["stale_signature_clamped"])
        self.assertEqual(r.dossier.verdict.confidence, Confidence.medium)

    def test_the_clamp_and_the_restore_both_reach_the_reader(self):
        # bug 2062219's shape, end to end: 202.5 days stale, clamped probable -> medium, then an
        # independent blind review agrees and puts it back. Both halves used to be invisible —
        # the dossier ships p_worth 0.9714, byte-identical to a clean rung-70 lead, and neither
        # the filed bug nor the chip said the timing evidence had run against the changeset. The
        # clamp is DELIBERATELY re-inflatable (`_SO_BOOST_POLICY`: it rules on origin, the second
        # opinion on mechanism); this pins the other obligation, that we say so.
        r = self._result(_lead(Confidence.probable))
        orch.apply_deterministic_gates(
            r, _seed(202.5),
            second_opinion=SecondOpinion(mode="verify", corroborates=True, confidence="high",
                                         mechanism="the patch drops the last ref"),
            second_opinion_status="ok")
        corr = r.dossier.corroborations
        self.assertTrue(corr["stale_signature_clamped"])
        self.assertTrue(corr["second_opinion_boosted"])
        self.assertEqual(r.dossier.verdict.confidence, Confidence.probable)
        self.assertAlmostEqual(r.dossier.verdict.p_worth_investigating, 0.9714, places=4)
        note = report_bug.build_stale_signature_note(corr)
        self.assertIn("202 days before the changeset", note)
        self.assertIn("independent blind re-analysis", note)
        self.assertIn("may still be relevant", note)          # never "not the cause"


class TestSigageLookup(unittest.TestCase):
    def test_positive_when_the_candidate_landed_after_first_seen(self):
        self.assertAlmostEqual(
            sigage.days_landed_after_first_seen("20260101000000", "20260701000000"),
            181.0, places=0)

    def test_negative_when_the_candidate_predates_first_seen(self):
        self.assertLess(
            sigage.days_landed_after_first_seen("20260701000000", "20260101000000"), 0)

    def test_unknown_sides_give_none(self):
        self.assertIsNone(sigage.days_landed_after_first_seen(None, "20260701000000"))
        self.assertIsNone(sigage.days_landed_after_first_seen("20260101000000", None))
        self.assertIsNone(sigage.days_landed_after_first_seen("nonsense", "also-nonsense"))

    def test_accepts_every_pushdate_shape_the_seed_can_carry(self):
        # on-stack: a tz-aware DB datetime | off-stack: hg's [epoch, tzoffset] | plus epoch,
        # ISO string and buildid. All must land on the same instant.
        target = datetime(2026, 7, 1, tzinfo=timezone.utc)
        epoch = target.timestamp()
        for shape in (target, target.replace(tzinfo=None), [epoch, 0], (epoch, 0), epoch,
                      int(epoch), "2026-07-01T00:00:00+00:00", "2026-07-01 00:00:00",
                      "20260701000000"):
            got = sigage.to_datetime(shape)
            self.assertIsNotNone(got, shape)
            self.assertEqual(got, target, shape)

    def test_junk_pushdate_shapes_give_none(self):
        for shape in (None, "", "not-a-date", {}, object()):
            self.assertIsNone(sigage.to_datetime(shape), shape)

    def test_empty_signature_short_circuits_without_a_lookup(self):
        self.assertIsNone(sigage.first_seen_buildid(""))

    def test_window_is_clamped_below_socorro_hard_limit(self):
        # Socorro rejects a range over 365 days outright; the implicit "to now" bound makes an
        # exact 365 a 400. Assert the request we would send stays legal.
        seen = {}

        class _FakeSearch:
            URL = "https://crash-stats.mozilla.org/api/SuperSearch/"

            def __init__(self, queries=None, **kw):
                self._q = queries or []

            def wait(self):
                for q in self._q:
                    seen.setdefault("params", q.params)
                    seen.setdefault("all", []).append(q.params)
                    q.handler({"hits": [{"build_id": 20260101000000}]}, q.handlerdata)

        with mock.patch.object(sigage.socorro, "SuperSearch", _FakeSearch):
            sigage.first_seen_buildid("S", days=10_000)
        from datetime import datetime, timezone
        since = datetime.strptime(seen["params"]["date"][2:], "%Y-%m-%d").replace(
            tzinfo=timezone.utc)
        span = (datetime.now(timezone.utc) - since).days
        self.assertLessEqual(span, 365)
        # And it must sort ascending by build_id rather than page a count-ordered facet.
        self.assertEqual(seen["params"]["_sort"], "build_id")
        self.assertEqual(seen["params"]["_results_number"], 1)
        # No channel filter: first-seen is the ORIGIN question and spans every channel. The
        # `release_channel` facet is what still scopes the bit-flip gate's count.
        self.assertNotIn("release_channel", seen["params"])
        self.assertEqual(seen["params"]["_facets"], "release_channel")

    def test_lookup_failure_returns_none_instead_of_raising(self):
        class _Boom:
            URL = "https://crash-stats.mozilla.org/api/SuperSearch/"

            def __init__(self, **kw):
                pass

            def wait(self):
                raise RuntimeError("socorro down")

        with mock.patch.object(sigage.socorro, "SuperSearch", _Boom):
            self.assertIsNone(sigage.first_seen_buildid("S"))


class TestPushdateForNode(unittest.TestCase):
    """sigage.json_rev + its two readers. The gate's fallback when the seed never priced the
    candidate the agent chose, and the source of the bug comment's git link. hg's json-rev takes
    a SHORT rev and returns both `pushdate` [epoch, tzoffset] and `git_commit` -- ONE slow
    (8-13s) request, so both readers share one cache."""

    def setUp(self):
        sigage._JSON_REV_CACHE.clear()

    def _resp(self, payload):
        r = mock.Mock()
        r.json.return_value = payload
        return r

    def test_both_readers_share_one_request(self):
        r = self._resp({"node": "c90adbc8b3bf1234", "pushdate": [1784318610, 0],
                        "git_commit": "9d7faea5127c"})
        with mock.patch("crashclouseau.net.get", return_value=r) as get:
            self.assertEqual(sigage.pushdate_for_node("c90adbc8b3bf", "nightly"),
                             [1784318610, 0])
            self.assertEqual(sigage.git_commit_for_node("c90adbc8b3bf", "nightly"),
                             "9d7faea5127c")
            self.assertEqual(sigage.pushdate_for_node("c90adbc8b3bf", "nightly"),
                             [1784318610, 0])
        # the endpoint costs 8-13s: pushdate + git sha + a repeat must be ONE request
        self.assertEqual(get.call_count, 1)
        self.assertIn("json-rev/c90adbc8b3bf", get.call_args[0][0])

    def test_result_feeds_the_day_computation(self):
        # The [epoch, tz] shape json-rev returns must be one sigage.to_datetime understands,
        # or the gate would resolve a pushdate and still compute None days.
        r = self._resp({"pushdate": [1784318610, 0]})
        with mock.patch("crashclouseau.net.get", return_value=r):
            pd = sigage.pushdate_for_node("abc", "nightly")
        self.assertIsNotNone(sigage.days_landed_after_first_seen(_FIRST_SEEN, pd))

    def test_missing_fields(self):
        with mock.patch("crashclouseau.net.get", return_value=self._resp({"node": "abc"})):
            self.assertIsNone(sigage.pushdate_for_node("abc", "nightly"))
            self.assertEqual(sigage.git_commit_for_node("abc", "nightly"), "")

    def test_failure_and_empty_inputs_return_none(self):
        with mock.patch("crashclouseau.net.get", side_effect=RuntimeError("hg down")):
            self.assertIsNone(sigage.pushdate_for_node("zzz", "nightly"))
            self.assertEqual(sigage.git_commit_for_node("zzz", "nightly"), "")
        self.assertIsNone(sigage.pushdate_for_node("", "nightly"))
        self.assertIsNone(sigage.pushdate_for_node("abc", ""))   # no repo for the channel


class TestResolveCandidateGitCommit(unittest.TestCase):
    """orchestrator._resolve_candidate_git_commit: the candidate's git sha AND author email
    are resolved ONCE in the worker from a single json-rev and stored, so no page render pays
    hg's 8-13s cost for the (gh) link or the needinfo."""

    REV = {"git_commit": "9d7faea5127c", "user": "Jon Coppeard <jcoppeard@mozilla.com>"}

    def test_stores_the_sha_and_the_author_email(self):
        d = _lead()
        with mock.patch.object(sigage, "json_rev", return_value=self.REV) as g:
            orch._resolve_candidate_git_commit(d, _SEED)
        g.assert_called_once_with("abc123def456", "nightly")
        self.assertEqual(d.candidate.git_commit, "9d7faea5127c")
        self.assertEqual(d.candidate.author_email, "jcoppeard@mozilla.com")

    def test_a_cached_sha_still_resolves_a_missing_email(self):
        # The email was added later, so dossiers carry a sha and no address; one must not
        # suppress the other or the needinfo silently never appears.
        d = _lead()
        d.candidate = d.candidate.model_copy(update={"git_commit": "cached1"})
        with mock.patch.object(sigage, "json_rev", return_value=self.REV) as g:
            orch._resolve_candidate_git_commit(d, _SEED)
        g.assert_called_once()
        self.assertEqual(d.candidate.git_commit, "cached1")       # not overwritten
        self.assertEqual(d.candidate.author_email, "jcoppeard@mozilla.com")

    def test_no_op_when_both_known_or_candidate_absent(self):
        d = _lead()
        d.candidate = d.candidate.model_copy(
            update={"git_commit": "cached1", "author_email": "a@b.c"})
        with mock.patch.object(sigage, "json_rev") as g:
            orch._resolve_candidate_git_commit(d, _SEED)
            orch._resolve_candidate_git_commit(None, _SEED)
            orch._resolve_candidate_git_commit(
                Dossier(candidate=None, verdict=Verdict(decision=Decision.abstain,
                                                        abstain_reason="none")), _SEED)
        g.assert_not_called()
        self.assertEqual(d.candidate.git_commit, "cached1")

    def test_unresolved_and_failure_leave_it_empty(self):
        d = _lead()
        with mock.patch.object(sigage, "json_rev", return_value={}):
            orch._resolve_candidate_git_commit(d, _SEED)
        self.assertEqual((d.candidate.git_commit, d.candidate.author_email), ("", ""))
        with mock.patch.object(sigage, "json_rev", side_effect=RuntimeError("hg down")):
            orch._resolve_candidate_git_commit(d, _SEED)   # must not raise
        self.assertEqual((d.candidate.git_commit, d.candidate.author_email), ("", ""))

    def test_only_a_bracketed_address_is_accepted_as_an_email(self):
        # A bare name or a bare address must not become a needinfo target: anything merely
        # containing an "@" would eventually ping a string that is not a person.
        self.assertEqual(orch._email_from_hg_user("Jan de Mooij <jdemooij@mozilla.com>"),
                         "jdemooij@mozilla.com")
        for bad in ("stransky", "", None, "no-brackets@example.com", "<not an email>"):
            with self.subTest(bad=bad):
                self.assertEqual(orch._email_from_hg_user(bad), "")

    def test_the_model_cannot_choose_who_gets_pinged(self):
        from crashclouseau.agent.schema import parse_and_validate
        d = parse_and_validate({
            "candidate": {"node": "abc123def456", "author_email": "attacker@example.com"},
            "verdict": {"decision": "abstain", "abstain_reason": "x"},
        })
        self.assertEqual(d.candidate.author_email, "")

    def test_gates_do_not_resolve_it(self):
        # It must stay OUT of apply_deterministic_gates: that is shared with the offline eval
        # runner, where one 8-13s hg call per corpus crash would wreck runtime + determinism.
        result = CrashTriageResult(num_turns=1, total_cost_usd=0.1, result="ok",
                                   dossier=_lead())
        with mock.patch.object(sigage, "git_commit_for_node") as g:
            orch.apply_deterministic_gates(result, _seed(-3.0))
        g.assert_not_called()


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = config.get_agent_signature_age()
        self.assertTrue(cfg["enabled"])       # a genuine improvement ships live, not flagged
        self.assertEqual(cfg["min_age_days"], 7)

    def test_env_kill_switch(self):
        with mock.patch.dict(os.environ, {"SIGNATURE_AGE_ENABLED": "0"}):
            self.assertFalse(config.get_agent_signature_age()["enabled"])


if __name__ == "__main__":
    unittest.main()
