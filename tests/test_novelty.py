# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""When "this signature is new" cannot be trusted -- bug 2070554 (2026-09-09).

`shutdownhang | ntdll.dll | kernelbase.dll | mozilla::MaybeLeakRefPtr<T>::~MaybeLeakRefPtr` had its
first report ever on 09-08, four days after 155.0.1 started reporting and after 53% of that
version's crash reports had already arrived; its `ntdll.dll` (10.0.26100.9444) had no symbols on
Socorro. The crash was the years-old `nsHttpConnectionMgr::Shutdown` hang under a name a Windows
update's symbol gap had minted. The run read "new signature, trustworthy window" and named the only
networking change in the dot-release diff -- a pref flip a Nimbus rollout had already applied to
every 155.0 user a week earlier, with zero crashes.

Run: DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
     python -m unittest tests.test_novelty
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import report_bug, sigage  # noqa: E402
from crashclouseau.agent import orchestrator as orch, triage  # noqa: E402
from crashclouseau.agent.schema import Dossier  # noqa: E402

SYMBOL_GAP = "shutdownhang | ntdll.dll | kernelbase.dll | mozilla::MaybeLeakRefPtr<T>::~MaybeLeakRefPtr"
SIBLING = "shutdownhang | mozilla::SpinEventLoopUntil | mozilla::net::nsHttpConnectionMgr::Shutdown"
BUILD = "20260903215306"


def _bucket(day, **versions):
    return {"term": day + "T00:00:00+00:00", "count": sum(versions.values()),
            "facets": {"version": [{"term": v, "count": n} for v, n in versions.items()]}}


def _rates(first_live="2026-09-04", since="2026-07-11"):
    """155.0.1's real per-day crash-report totals, 09-04..09-09, as `version_rates` returns them
    for the symbol-gap signature (6 reports on 09-08, 66 on 09-09, nothing before)."""
    days = ["2026-09-04", "2026-09-05", "2026-09-06", "2026-09-07", "2026-09-08", "2026-09-09"]
    allv = [969, 5522, 7293, 10670, 12765, 10343]
    sig = [0, 0, 0, 0, 6, 66]
    result = {"facets": {"histogram_date": [_bucket(d, **{"155.0.1": n}) for d, n in zip(days, sig)
                                            if n],
                         "version": [{"term": "155.0.1", "facets": {"build_id": []}}]}}
    totals = {"facets": {"histogram_date": [_bucket(d, **{"155.0.1": n})
                                            for d, n in zip(days, allv)], "version": []}}
    return sigage.summarize_version_rates(result, totals=totals, since=since)


class TestModuleFrames(unittest.TestCase):
    def test_the_bare_module_names_in_the_name(self):
        self.assertEqual(sigage.module_frames(SYMBOL_GAP), ["ntdll.dll", "kernelbase.dll"])
        self.assertEqual(sigage.module_frames("libxul.so | mozilla::Foo::Bar"), ["libxul.so"])
        self.assertEqual(sigage.module_frames(
            "shutdownhang | libsystem_kernel.dylib"), ["libsystem_kernel.dylib"])

    def test_a_symbolicated_name_has_none(self):
        self.assertEqual(sigage.module_frames(SIBLING), [])
        self.assertEqual(sigage.module_frames("mozilla::net::DiagnosticRWLock::CrashWithHolder"),
                         [])
        self.assertEqual(sigage.module_frames(""), [])
        self.assertEqual(sigage.module_frames(None), [])

    def test_a_function_that_mentions_a_module_is_not_a_module_frame(self):
        self.assertEqual(sigage.module_frames("mozilla::LoadLibrary(kernel32.dll)"), [])
        self.assertEqual(sigage.module_frames("<unknown in ntdll.pdb>"), [])


class TestNoveltyFacts(unittest.TestCase):
    def test_the_2070554_signature_fails_both_tests(self):
        facts = sigage.novelty_facts(SYMBOL_GAP, "2026-09-08", "155.0.1", _rates(), "release")
        self.assertEqual(facts["signature_module_frames"], ["ntdll.dll", "kernelbase.dll"])
        self.assertEqual(facts["signature_first_report_date"], "2026-09-08")
        self.assertEqual(facts["signature_first_report_lag_days"], 4)
        # (969 + 5522 + 7293 + 10670) / 47562 of 155.0.1's reports had already arrived.
        self.assertAlmostEqual(facts["version_reports_before_first_report"], 0.514, places=3)
        self.assertEqual(facts["signature_novelty_unreliable"], "module_frames,late_first_report")

    def test_a_signature_that_came_with_the_build_is_left_alone(self):
        facts = sigage.novelty_facts(SIBLING, "2026-09-04", "155.0.1", _rates(), "release")
        self.assertEqual(facts["version_reports_before_first_report"], 0.0)
        self.assertEqual(facts["signature_first_report_lag_days"], 0)
        self.assertNotIn("signature_novelty_unreliable", facts)
        self.assertNotIn("signature_module_frames", facts)

    def test_a_day_two_first_report_is_still_with_the_build(self):
        # 969 of 47,562 (2%) had arrived: well under LATE_FIRST_REPORT_SHARE.
        facts = sigage.novelty_facts(SIBLING, "2026-09-05", "155.0.1", _rates(), "release")
        self.assertAlmostEqual(facts["version_reports_before_first_report"], 0.02, places=2)
        self.assertNotIn("signature_novelty_unreliable", facts)

    def test_only_the_module_frames_test_runs_on_nightly(self):
        # A nightly "version" is a whole cycle of builds, so "before the version's first
        # reports" means nothing there.
        facts = sigage.novelty_facts(SYMBOL_GAP, "2026-09-08", "158.0a1", _rates(), "nightly")
        self.assertEqual(facts["signature_novelty_unreliable"], "module_frames")
        self.assertNotIn("version_reports_before_first_report", facts)

    def test_a_version_older_than_the_window_has_no_visible_first_day(self):
        facts = sigage.novelty_facts(SIBLING, "2026-09-08", "155.0.1",
                                     _rates(since="2026-09-04"), "release")
        self.assertNotIn("version_reports_before_first_report", facts)
        self.assertNotIn("signature_novelty_unreliable", facts)

    def test_unknowns_say_nothing(self):
        self.assertEqual(sigage.novelty_facts(SIBLING, None, "155.0.1", _rates(), "release"), {})
        self.assertEqual(sigage.novelty_facts(SIBLING, "2026-09-08", "155.0", _rates(),
                                              "release"), {})
        self.assertEqual(sigage.novelty_facts(SIBLING, "2026-09-08", "155.0.1", None,
                                              "release"), {})
        self.assertEqual(sigage.novelty_facts(SIBLING, "2026-09-08", "155.0.1",
                                              dict(sigage.NO_VERSION_RATES), "release"), {})
        self.assertEqual(sigage.novelty_facts(None, None, None, None, None), {})


class TestFirstSeenEverCarriesTheDate(unittest.TestCase):
    class _Resp:
        status_code = 200

        def __init__(self, hits):
            self._hits = hits

        def raise_for_status(self):
            pass

        def json(self):
            return {"hits": self._hits, "total": len(self._hits)}

    def test_facts_and_the_build_only_view(self):
        hits = [{"signature": SYMBOL_GAP, "first_build": BUILD,
                 "first_date": "2026-09-08T00:03:36+00:00"},
                {"signature": "nobuild", "first_build": None, "first_date": "2026-09-08"}]
        with mock.patch.object(sigage.net, "get", return_value=self._Resp(hits)):
            facts = sigage.first_seen_ever_facts([SYMBOL_GAP, "nobuild"])
            builds = sigage.first_seen_ever([SYMBOL_GAP, "nobuild"])
        self.assertEqual(facts, {SYMBOL_GAP: {"first_build": BUILD,
                                              "first_date": "2026-09-08"}})
        self.assertEqual(builds, {SYMBOL_GAP: BUILD})


def _crash(signature=SYMBOL_GAP, first_report="2026-09-08", **over):
    c = {"uuid": "76d884cd", "signature": signature, "channel": "release", "version": "155.0.1",
         "buildid": BUILD, "signature_first_seen_buildid": BUILD,
         "signature_first_seen_ever": BUILD, "signature_first_report_date": first_report,
         "version_rates": _rates()}
    c.update(over)
    return c


class TestTheBriefWithdrawsTheNoveltyClaim(unittest.TestCase):
    def test_the_2070554_brief_says_why_the_name_is_not_a_clock(self):
        text = "\n".join(triage._signature_age_lines(_crash()))
        self.assertIn("first seen anywhere in build 20260903215306", text)
        self.assertIn("BUT THE NAME IS NOT TRUSTWORTHY", text)
        self.assertIn("`ntdll.dll`, `kernelbase.dll`", text)
        self.assertIn("dates a SYMBOL GAP, not a crash", text)
        self.assertIn("BUT IT DID NOT COME WITH THE BUILD", text)
        self.assertIn("dated 2026-09-08, 4 days after 155.0.1 began reporting", text)
        self.assertIn("51% of that version's crash reports had already arrived", text)
        self.assertIn("do NOT treat the pushlog window below as trustworthy", text)
        self.assertIn("window membership, which is noise", text)
        self.assertNotIn("genuinely trustworthy", text)

    def test_a_genuinely_new_signature_keeps_the_trustworthy_window(self):
        text = "\n".join(triage._signature_age_lines(_crash(signature=SIBLING,
                                                            first_report="2026-09-04")))
        self.assertIn("pushlog window below is genuinely trustworthy", text)
        self.assertNotIn("BUT", text)

    def test_the_symbol_gap_alone_is_enough(self):
        text = "\n".join(triage._signature_age_lines(_crash(first_report="2026-09-04")))
        self.assertIn("BUT THE NAME IS NOT TRUSTWORTHY", text)
        self.assertNotIn("BUT IT DID NOT COME WITH THE BUILD", text)
        self.assertNotIn("genuinely trustworthy", text)

    def test_an_undated_signature_with_module_frames_is_warned_too(self):
        text = "\n".join(triage._signature_age_lines(_crash(signature_first_seen_ever=None)))
        self.assertIn("SIGNATURE AGE: not established", text)
        self.assertIn("BUT THE NAME IS NOT TRUSTWORTHY", text)
        self.assertIn("do NOT treat the pushlog window below as trustworthy", text)

    def test_the_second_opinion_sees_the_same_lines(self):
        text = "\n".join(triage._crash_facts(_crash(raw_crash={"json_dump": {}})))
        self.assertIn("BUT THE NAME IS NOT TRUSTWORTHY", text)


class TestTheRecorderAndTheBug(unittest.TestCase):
    def test_the_facts_reach_the_corroborations(self):
        d = Dossier(crash={"uuid": "u", "signature": SYMBOL_GAP, "frames": []})
        orch._record_signature_age_facts(d, _crash())
        c = d.corroborations
        self.assertEqual(c["signature_novelty_unreliable"], "module_frames,late_first_report")
        self.assertEqual(c["signature_module_frames"], ["ntdll.dll", "kernelbase.dll"])
        self.assertEqual(c["signature_first_report_date"], "2026-09-08")
        self.assertEqual(c["signature_first_report_lag_days"], 4)
        self.assertEqual(c["signature_age_days_ever"], 0.0)

    def test_the_filed_bug_takes_new_back(self):
        d = Dossier(crash={"uuid": "u", "signature": SYMBOL_GAP, "frames": []})
        orch._record_signature_age_facts(d, _crash())
        note = report_bug.build_signature_age_note(d.corroborations, BUILD)
        self.assertIn("This signature is new", note)
        self.assertIn("unsymbolicated module frames (`ntdll.dll`, `kernelbase.dll`)", note)
        self.assertIn("dates a symbol gap", note)
        self.assertIn("its first report is dated 2026-09-08, 4 days after this version began "
                      "reporting, when 51% of the version's crash reports had already arrived",
                      note)

    def test_a_trustworthy_new_signature_gets_no_caveat(self):
        d = Dossier(crash={"uuid": "u", "signature": SIBLING, "frames": []})
        orch._record_signature_age_facts(d, _crash(signature=SIBLING, first_report="2026-09-04"))
        note = report_bug.build_signature_age_note(d.corroborations, BUILD)
        self.assertIn("This signature is new", note)
        self.assertNotIn("symbol gap", note)
        self.assertNotIn("did not appear with the build", note)


if __name__ == "__main__":
    unittest.main()
