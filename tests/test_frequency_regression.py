# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""A crash that got MORE FREQUENT is not refuted by how old its signature is.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        uv run python -m unittest tests.test_frequency_regression

2026-08-15, nightly, ``shutdownhang | ... QuotaManager::Observer::Observe``: the principal named
bug 2058982's nightly-only rescan as extra I/O-thread work that shutdown has to wait for. The
blind second opinion refuted it: "first seen 273 days BEFORE the change, so the change cannot have
introduced it" and "touches no shutdown code and cannot itself hang". The module owner confirmed
the same change as the regressor the next day (bug 2063892). Both arguments are right for a fault
and inverted for a watchdog, and for any signature whose rate is rising.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from crashclouseau import utils  # noqa: E402
from crashclouseau.agent import orchestrator as orch, second_opinion, triage  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    Candidate,
    Confidence,
    Decision,
    Dossier,
    Verdict,
)

T = "mozilla::dom::quota::QuotaManager::Shutdown::<T>::operator()"
HANG = ("shutdownhang | mozilla::SpinEventLoopUntil<T> | "
        "mozilla::dom::quota::QuotaManager::Observer::Observe")


class TestWatchdogCrash(unittest.TestCase):
    def test_the_three_signature_prefixes(self):
        for sig in (HANG, "AsyncShutdownTimeout | profile-before-change | Foo", "hang | Bar"):
            self.assertTrue(utils.is_watchdog_crash(sig), sig)

    def test_the_report_type_and_the_reason(self):
        self.assertTrue(utils.is_watchdog_crash(T, report_type="hang"))
        self.assertTrue(utils.is_watchdog_crash(
            T, moz_crash_reason="Quota manager shutdown timed out"))
        self.assertTrue(utils.is_watchdog_crash(
            "mozilla::Foo",
            moz_crash_reason="Shutdown hanging at step AppShutdownQM. Something is blocking "
                             "the main-thread."))

    def test_a_fault_is_not_one(self):
        self.assertFalse(utils.is_watchdog_crash("mozilla::Foo::Bar", "crash",
                                                 "MOZ_RELEASE_ASSERT(isSome())"))
        self.assertFalse(utils.is_watchdog_crash(None, None, None))


_FIRST_SEEN = "20251115092723"


def _facts(ratio=3.5, installs=40):
    return {"signature_trend_ratio": ratio, "signature_trend_installs": installs,
            "signature_trend_window_days": 7}


def _seed(landed_after_days, **extra):
    seen = datetime.strptime(_FIRST_SEEN, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    node = "abc123def456"
    return {"uuid": "u-1", "signature": "mozilla::Foo::Bar", "channel": "nightly",
            "stack": "#0 f a:1", "is_offstack": False,
            "signature_first_seen_buildid": _FIRST_SEEN,
            "candidate_pushdates": {node: seen + timedelta(days=landed_after_days)},
            **extra}


def _lead(confidence=Confidence.probable):
    return Dossier(candidate=Candidate(node="abc123def456", bug=42),
                   verdict=Verdict(decision=Decision.lead, confidence=confidence,
                                   needinfo_draft="could you take a look?"))


class TestTheAgeGateWaivesAFrequencyRegression(unittest.TestCase):
    def test_a_plain_fault_is_clamped_as_before(self):
        d = _lead()
        orch._apply_signature_age_gate(d, _seed(273.0))
        self.assertEqual(d.verdict.confidence, Confidence.medium)
        self.assertTrue(d.corroborations["stale_signature_clamped"])
        self.assertNotIn("stale_signature_waived", d.corroborations)

    def test_a_rising_rate_records_the_timing_and_moves_nothing(self):
        d = _lead()
        orch._apply_signature_age_gate(d, _seed(273.0, signature_trend=_facts()))
        self.assertEqual(d.verdict.confidence, Confidence.probable)
        self.assertTrue(d.corroborations["stale_signature"])
        self.assertEqual(d.corroborations["candidate_landed_after_first_seen_days"], 273.0)
        self.assertEqual(d.corroborations["stale_signature_waived"], "rate")
        self.assertNotIn("stale_signature_clamped", d.corroborations)

    def test_a_watchdog_signature_alone_is_no_longer_waived(self):
        # 2026-09-09. The waiver's argument -- "a hang regresses by getting more frequent" --
        # needs the crash to BE more frequent. Waived on the hang alone it filed bugs 2069191,
        # 2069353 and 2070489, none confirmed; the case that motivated it (2063892) had a rising
        # rate and is waived by `rate` on its own.
        d = _lead()
        orch._apply_signature_age_gate(d, _seed(273.0, signature=HANG))
        self.assertEqual(d.verdict.confidence, Confidence.medium)
        self.assertTrue(d.corroborations["stale_signature_clamped"])
        self.assertTrue(d.corroborations["stale_signature_watchdog_unwaived"])
        self.assertNotIn("stale_signature_waived", d.corroborations)

    def test_a_fault_is_not_counted_as_an_unwaived_watchdog(self):
        d = _lead()
        orch._apply_signature_age_gate(d, _seed(273.0))
        self.assertNotIn("stale_signature_watchdog_unwaived", d.corroborations)

    def test_the_report_type_and_the_reason_count_too(self):
        for raw in ({"report_type": "hang"},
                    {"moz_crash_reason": "Quota manager shutdown timed out"},
                    {"json_dump": {"moz_crash_reason": "Shutdown hanging at step AppShutdownQM"}}):
            d = _lead()
            orch._apply_signature_age_gate(d, _seed(273.0, raw_crash=raw,
                                                    signature_trend=_facts()))
            self.assertEqual(d.corroborations.get("stale_signature_waived"), "rate,watchdog", raw)

    def test_a_boundary_step_on_this_version_is_a_frequency_signal(self):
        step = {"version": "155.0.1", "from_version": "155.0", "ratio": 7.5, "kind": "boundary"}
        d = _lead()
        orch._apply_signature_age_gate(d, _seed(
            273.0, signature=HANG, version="155.0.1", version_rates={"step": step}))
        self.assertEqual(d.verdict.confidence, Confidence.probable)
        self.assertEqual(d.corroborations["stale_signature_waived"], "step,watchdog")
        # A fault with a boundary step is waived by the step alone, like `rate`.
        d = _lead()
        orch._apply_signature_age_gate(d, _seed(
            273.0, version="155.0.1", version_rates={"step": step}))
        self.assertEqual(d.corroborations["stale_signature_waived"], "step")

    def test_a_step_on_another_version_or_a_date_event_is_not_a_reason(self):
        step = {"version": "155.0.1", "from_version": "155.0", "ratio": 7.5, "kind": "boundary"}
        d = _lead()
        orch._apply_signature_age_gate(d, _seed(
            273.0, signature=HANG, version="155.0", version_rates={"step": step}))
        self.assertEqual(d.verdict.confidence, Confidence.medium)
        d = _lead()
        orch._apply_signature_age_gate(d, _seed(
            273.0, signature=HANG, version="155.0.1",
            version_rates={"step": None, "date_event": {"version": "155.0.1", "day": "2026-09-08"}}))
        self.assertEqual(d.verdict.confidence, Confidence.medium)
        self.assertTrue(d.corroborations["stale_signature_watchdog_unwaived"])

    def test_both_reasons_are_named(self):
        d = _lead()
        orch._apply_signature_age_gate(d, _seed(273.0, signature=HANG, signature_trend=_facts()))
        self.assertEqual(d.corroborations["stale_signature_waived"], "rate,watchdog")

    def test_a_flat_rate_is_not_a_reason(self):
        d = _lead()
        orch._apply_signature_age_gate(d, _seed(273.0, signature_trend=_facts(ratio=1.2)))
        self.assertEqual(d.verdict.confidence, Confidence.medium)

    def test_a_candidate_that_predates_the_crash_records_nothing(self):
        d = _lead()
        orch._apply_signature_age_gate(d, _seed(-30.0, signature=HANG))
        self.assertEqual(d.corroborations, {})


class TestBothModelsAreToldWhatAWatchdogIs(unittest.TestCase):
    def _crash(self, **raw):
        return {"uuid": "u-1", "signature": raw.pop("signature", "mozilla::Foo::Bar"),
                "channel": "nightly", "product": "Firefox", "stack": "0 Foo::Bar foo.cpp:1",
                "raw_crash": raw}

    def test_the_facts_carry_the_block_for_a_hang(self):
        text = "\n".join(triage._crash_facts(self._crash(signature=HANG)))
        self.assertIn("WATCHDOG / TIMEOUT CRASH", text)
        self.assertIn("age is NOT a refutation", text)

    def test_the_reason_alone_is_enough(self):
        text = "\n".join(triage._crash_facts(
            self._crash(moz_crash_reason="Quota manager shutdown timed out")))
        self.assertIn("WATCHDOG / TIMEOUT CRASH", text)

    def test_a_fault_gets_no_block(self):
        text = "\n".join(triage._crash_facts(
            self._crash(moz_crash_reason="MOZ_RELEASE_ASSERT(x)")))
        self.assertNotIn("WATCHDOG", text)

    def test_the_second_opinion_sees_the_same_block(self):
        p = second_opinion._user_prompt(self._crash(signature=HANG),
                                        {"node": "deadbeef", "bug": 42})
        self.assertIn("WATCHDOG / TIMEOUT CRASH", p)
        self.assertIn("deadbeef", p)

    def test_the_second_opinion_is_told_age_is_not_a_refutation(self):
        self.assertIn("NOT by itself a refutation", second_opinion._SYSTEM)
        self.assertIn("adds work, I/O or blocking", second_opinion._SYSTEM)

    def test_a_rising_rate_says_so_about_the_first_seen_build(self):
        lines = triage._signature_trend_lines({"signature_trend": {
            **_facts(), "signature_trend_reports": 42, "signature_trend_expected_installs": 11.4,
            "signature_trend_baseline_days": 56}})
        self.assertIn("does NOT rule a candidate out", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
