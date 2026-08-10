# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Bad-machine suppression: a machine with failing memory scatters unrelated signatures.
#
# Jan de Mooij, closing bug 2062168: "I think this one is bad hardware rather than a regression.
# It's just one crash report and that installation has multiple crashes with distinct
# signatures." That installation produced 21 crashes across 20 distinct signatures in two days on
# one 2011 Sandy Bridge, and we filed TWO bugs out of it the same day (2062168, 2062173).
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_bad_machine_gate
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import config, machine  # noqa: E402
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

_SF = SearchfoxCitation(permalink="https://searchfox.org/x#1", symbol_id="_Z1",
                        repo="mozilla-central")
_DAY = 86400.0


def _cfg(**over):
    base = {"enabled": True, "min_signatures": 10, "max_cpu_infos": 1,
            "min_span_seconds": 1800, "lookback_days": 14}
    base.update(over)
    return base


def _hist(sigs=20, cpus=1, crashes=21, span=2 * _DAY):
    return {"distinct_signatures": sigs, "distinct_cpus": cpus,
            "crashes": crashes, "span_seconds": span}


def _seed(**over):
    s = {"uuid": "u-1", "signature": "S", "channel": "nightly", "install_history": _hist()}
    s.update(over)
    return s


def _lead(confidence=Confidence.probable):
    return Dossier(
        candidate=Candidate(node="abcdef123456", bug=1),
        verdict=Verdict(decision=Decision.lead, confidence=confidence,
                        needinfo_draft="could you take a look?",
                        mechanism=Claim(text="uaf", citations=[_SF])))


class TestBadMachineGate(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(config, "get_agent_bad_machine", return_value=_cfg())
        p.start()
        self.addCleanup(p.stop)

    def test_the_2062168_machine_is_suppressed(self):
        # 21 crashes / 20 signatures / 1 CPU over ~2 days — the real profile.
        d = _lead()
        orch._apply_bad_machine_gate(d, _seed())
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertTrue(d.corroborations["bad_machine_suppressed"])
        self.assertEqual(d.corroborations["machine_distinct_signatures"], 20)
        self.assertIn("DIFFERENT crash signatures", d.verdict.abstain_reason)

    def test_a_suppressed_verdict_drops_the_needinfo(self):
        d = _lead()
        orch._apply_bad_machine_gate(d, _seed())
        self.assertIsNone(d.verdict.needinfo_draft)
        self.assertIsNotNone(d.verdict.mechanism)

    def test_one_machine_repeating_ONE_signature_is_a_bug_not_a_bad_machine(self):
        # Bug 2060924: 5 crashes from one installation, all the same signature — and it is
        # ASSIGNED. Volume is not the predicate; diversity is.
        d = _lead()
        orch._apply_bad_machine_gate(d, _seed(install_history=_hist(sigs=1, crashes=100)))
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.corroborations["machine_crash_count"], 100)
        self.assertNotIn("bad_machine_suppressed", d.corroborations)

    def test_an_aliased_install_time_is_not_one_machine(self):
        # Bug 2061961's install looks like a scattergun (6 crashes, 5 signatures) and carries 4
        # CPUs and 3 operating systems — several machines sharing one install second. The scatter
        # effect vanishes on such ids (+1.0pp, p=0.77), so the CPU count is the mechanism test.
        d = _lead()
        orch._apply_bad_machine_gate(d, _seed(install_history=_hist(sigs=20, cpus=4)))
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.corroborations["machine_distinct_cpus"], 4)
        self.assertNotIn("bad_machine_suppressed", d.corroborations)

    def test_a_cascading_session_is_not_a_failing_machine(self):
        # Bug 2047016 — RESOLVED FIXED, grew to 682 crashes across 23 installs — had its FIRST
        # crash on a machine that emitted 5 signatures in 22 minutes as one Wayland/video stack
        # unwound. Signature count cannot tell a cascade from a scattergun; elapsed time can.
        d = _lead()
        orch._apply_bad_machine_gate(d, _seed(install_history=_hist(span=22 * 60)))
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.corroborations["machine_span_seconds"], 1320)
        self.assertNotIn("bad_machine_suppressed", d.corroborations)

    def test_below_the_signature_threshold_is_recorded_but_does_not_fire(self):
        d = _lead()
        orch._apply_bad_machine_gate(d, _seed(install_history=_hist(sigs=9)))
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.corroborations["machine_distinct_signatures"], 9)

    def test_every_unknown_fails_toward_reporting(self):
        # An absent install_time (3% of nightly crashes), a failed lookup, or an unknown CPU
        # count must each leave the verdict alone. The CPU condition is a POSITIVE requirement
        # precisely so that an unknown cannot satisfy it.
        for hist in ({}, {"distinct_signatures": None},
                     _hist(cpus=None), _hist(span=None),
                     {"distinct_signatures": 20, "distinct_cpus": None, "span_seconds": None}):
            with self.subTest(hist=hist):
                d = _lead()
                orch._apply_bad_machine_gate(d, _seed(install_history=hist))
                self.assertEqual(d.verdict.decision, Decision.lead)
                self.assertNotIn("bad_machine_suppressed", d.corroborations or {})

    def test_offline_seeds_are_a_no_op(self):
        d = _lead()
        orch._apply_bad_machine_gate(d, {"uuid": "u-1"})
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.corroborations or {}, {})

    def test_the_kill_switch_records_nothing(self):
        config.get_agent_bad_machine.return_value = _cfg(enabled=False)
        d = _lead()
        orch._apply_bad_machine_gate(d, _seed())
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.corroborations or {}, {})

    def test_an_existing_abstain_keeps_its_own_reason(self):
        d = Dossier(verdict=Verdict(decision=Decision.abstain, confidence=Confidence.low,
                                    abstain_reason="no candidate in the window"))
        orch._apply_bad_machine_gate(d, _seed())
        self.assertEqual(d.verdict.abstain_reason, "no candidate in the window")
        self.assertEqual(d.corroborations["machine_distinct_signatures"], 20)
        self.assertNotIn("bad_machine_suppressed", d.corroborations)


class TestItOutranksTheRest(unittest.TestCase):
    """It runs LAST, so no earlier gate or fold can rescue a broken machine's crash."""

    def test_a_second_opinion_boost_cannot_rescue_a_bad_machine(self):
        from crashclouseau.agent.result import CrashTriageResult

        def boost(dossier, so, seed, status=None):
            dossier.verdict = dossier.verdict.model_copy(
                update={"confidence": Confidence.probable})

        with mock.patch.object(config, "get_agent_bad_machine", return_value=_cfg()), \
             mock.patch.object(orch, "_fold_second_opinion", side_effect=boost):
            result = CrashTriageResult(num_turns=1, total_cost_usd=0.1, result="ok",
                                       dossier=_lead(confidence=Confidence.medium))
            orch.apply_deterministic_gates(result, _seed())
        self.assertEqual(result.dossier.verdict.decision, Decision.abstain)
        self.assertTrue(result.dossier.corroborations["bad_machine_suppressed"])

    def test_a_downweight_would_NOT_have_stopped_bug_2062173(self):
        """Why the effect is an abstain and not the stale-gate's one-rung clamp.

        Bug 2062173 shipped at strong-evidence/97%. One rung down is `probable` (70), which is
        exactly `autofile.min_confidence` — so a downweight files the bug anyway. Pinned because
        "downweight, it's less drastic" is the obvious review suggestion and it is wrong here.
        """
        self.assertEqual(config.get_agent_autofile()["min_confidence"], 70)
        from crashclouseau.agent.schema import CONFIDENCE_SCORE

        one_rung_down_from_strong = CONFIDENCE_SCORE[Confidence.probable] * 100
        self.assertGreaterEqual(one_rung_down_from_strong,
                                config.get_agent_autofile()["min_confidence"])


class TestInstallHistory(unittest.TestCase):
    """The lookup. One SuperSearch returning rows, not facets."""

    def _search(self, hits, captured=None):
        class Fake:
            def __init__(self, params=None, handler=None, handlerdata=None):
                if captured is not None:
                    captured.append(params)
                handler({"hits": hits, "total": len(hits)}, handlerdata)

            def wait(self):
                return None
        return Fake

    def test_it_counts_diversity_cpus_and_span(self):
        hits = [{"signature": "A", "cpu_info": "c1", "date": "2026-08-08T00:00:00+00:00"},
                {"signature": "B", "cpu_info": "c1", "date": "2026-08-09T00:00:00+00:00"},
                {"signature": "A", "cpu_info": "c1", "date": "2026-08-10T00:00:00+00:00"}]
        with mock.patch.object(machine.socorro, "SuperSearch", self._search(hits)):
            got = machine.install_history(1786140350)
        self.assertEqual(got["distinct_signatures"], 2)
        self.assertEqual(got["distinct_cpus"], 1)
        self.assertEqual(got["crashes"], 3)
        self.assertEqual(got["span_seconds"], 2 * _DAY)

    def test_the_window_is_bounded_at_the_crash_so_it_is_never_hindsight(self):
        # Without an upper bound an offline replay would score the rule against crashes that had
        # not happened when the verdict was made.
        captured = []
        with mock.patch.object(machine.socorro, "SuperSearch",
                               self._search([], captured)):
            machine.install_history(1, before="2026-08-10T12:00:00+00:00", days=14)
        dates = captured[0]["date"]
        self.assertTrue(any(d.startswith(">=2026-07-27") for d in dates), dates)
        self.assertTrue(any(d.startswith("<=2026-08-10T12:00:00") for d in dates), dates)

    def test_an_empty_response_is_unknown_not_a_quiet_machine(self):
        # A failed or malformed query returns no rows too, and this feeds a suppression.
        with mock.patch.object(machine.socorro, "SuperSearch", self._search([])):
            got = machine.install_history(1)
        self.assertEqual(got, {"distinct_signatures": None, "distinct_cpus": None,
                               "crashes": None, "span_seconds": None})

    def test_a_missing_cpu_column_is_unknown_not_one_cpu(self):
        hits = [{"signature": "A", "date": "2026-08-08T00:00:00+00:00"},
                {"signature": "B", "date": "2026-08-10T00:00:00+00:00"}]
        with mock.patch.object(machine.socorro, "SuperSearch", self._search(hits)):
            got = machine.install_history(1)
        self.assertEqual(got["distinct_signatures"], 2)
        self.assertIsNone(got["distinct_cpus"])

    def test_no_install_time_and_a_failed_lookup_are_both_unknown(self):
        self.assertIsNone(machine.install_history(None)["distinct_signatures"])

        class Boom:
            def __init__(self, **kw):
                raise RuntimeError("socorro down")

        with mock.patch.object(machine.socorro, "SuperSearch", Boom):
            self.assertIsNone(machine.install_history(1)["distinct_signatures"])

    def test_truncation_can_only_undercount(self):
        # The row cap makes the count a LOWER bound, which is the safe direction for a rule that
        # fires on the count being HIGH.
        self.assertGreaterEqual(machine._MAX_ROWS, 200)


class TestProtoClusterIsNotClosed(unittest.TestCase):
    """The flag that stops a false negative being permanent."""

    def test_instance_suppressions_are_listed(self):
        from crashclouseau import models

        # Crash-report-specific suppressions: "this REPORT is noise" says nothing about the next
        # report of the same signature from a healthy machine.
        self.assertIn("bad_machine_suppressed", models._INSTANCE_SUPPRESSED)
        self.assertIn("possible_bit_flip_suppressed", models._INSTANCE_SUPPRESSED)
        # The backout gate suppresses on the CANDIDATE being gone from the tree, which is equally
        # true for every crash in the cluster — it SHOULD close it.
        self.assertNotIn("candidate_backedout_suppressed", models._INSTANCE_SUPPRESSED)


if __name__ == "__main__":
    unittest.main()
