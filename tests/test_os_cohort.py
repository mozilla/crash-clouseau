# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""OS-version concentration, query scope, prompt rendering and OOM-gate tests."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import oscohort  # noqa: E402
from crashclouseau.agent import orchestrator as orch, triage  # noqa: E402
from crashclouseau.agent.schema import AbstainKind, Decision  # noqa: E402
from tests import test_oom_gate as tog  # noqa: E402

_SINCE = "2026-09-15"
_SIG = "java.lang.OutOfMemoryError: at android.os.Message.obtain(Message.java)"


def _result(daily, installs=500):
    """Build a SuperSearch result from [(Android 37 count, {other OS: count}), ...]."""
    histogram, totals = [], {}
    start = datetime.strptime(_SINCE, "%Y-%m-%d")
    for i, (mine, others) in enumerate(daily):
        terms = {"Android 37": mine, **others}
        for t, n in terms.items():
            totals[t] = totals.get(t, 0) + n
        histogram.append({"term": (start + timedelta(days=i)).strftime("%Y-%m-%dT00:00:00Z"),
                          "count": sum(terms.values()),
                          "facets": {oscohort.FIELD: [{"term": t, "count": n}
                                                      for t, n in terms.items() if n]}})
    rows = [{"term": t, "count": n, "facets": {"cardinality_install_time": {
        "value": installs if t == "Android 37" else n}}} for t, n in totals.items() if n]
    return {"total": sum(totals.values()),
            "facets": {oscohort.FIELD: rows, "histogram_date": histogram}}


_BASELINE = {"Android 37": 1100, "Android 36": 1700, "Android 30": 1200, "Android 28": 1000,
             "Android 33": 1000, "Android 24": 4000}
# Android 37 reports quadruple; other versions stay flat.
_RISING = [(10 if i < 28 else 40, {"Android 36": 2, "Android 24": 1}) for i in range(56)]


def _summarize(daily=_RISING, installs=500):
    return oscohort.summarize(_result(daily, installs), _BASELINE, _SINCE,
                              product="Fenix", channel="release")


def _population(counts):
    """A facet-only response for OS-family comparisons."""
    return {"total": sum(counts.values()), "facets": {oscohort.FIELD: [
        {"term": term, "count": count} for term, count in counts.items()]}}


class TestSummarize(unittest.TestCase):
    def test_skewed_and_rising(self):
        got = _summarize()
        self.assertEqual(got["cohort"], "Android 37")
        self.assertEqual(got["label"], "Android API 37 (Android 17)")
        self.assertTrue(got["skewed"])
        self.assertTrue(got["rising"])
        self.assertEqual(got["halves"], {"cohort": [280, 1120], "rest": [84, 84]})
        self.assertEqual(len(got["weekly"]), 8)
        self.assertEqual(got["weekly"][-1][1:], [280, 21])

    def test_flat_skew_is_not_rising(self):
        flat = [(20, {"Android 36": 2}) for _ in range(56)]
        got = _summarize(flat)
        self.assertTrue(got["skewed"])
        self.assertFalse(got["rising"])

    def test_rest_rising_too_is_not_a_cohort_rise(self):
        both = [(10 if i < 28 else 40, {"Android 36": 3 if i < 28 else 12}) for i in range(56)]
        self.assertFalse(_summarize(both)["rising"])

    def test_general_rise_with_a_mild_skew_is_not_a_cohort_rise(self):
        general = [(2 if i < 28 else 20, {"Android 36": 3 if i < 28 else 14}) for i in range(56)]
        got = _summarize(general)
        self.assertGreater(got["rest_rise"], oscohort.REST_MAX_RISE)
        self.assertFalse(got["rising"])

    def test_unknown_os_is_not_a_cohort(self):
        result = _population({"Unknown": 50})
        self.assertIsNone(oscohort.summarize(result, {"Unknown": 9, "Windows 11": 9},
                                             _SINCE)["cohort"])

    def test_share_matching_the_population_is_not_skewed(self):
        spread = [(2, {"Android 36": 3, "Android 24": 7}) for _ in range(56)]
        self.assertFalse(_summarize(spread)["skewed"])

    def test_one_installation_is_not_skewed(self):
        self.assertFalse(_summarize(installs=1)["skewed"])

    def test_compared_within_the_os_family(self):
        # A Mac-only signature is compared with Mac reports, not all Firefox reports.
        result = _population({"macOS 26": 80, "macOS 15": 20})
        baseline = {"Windows 11": 30000, "Windows 10": 25000, "macOS 26": 700, "macOS 15": 300}
        got = oscohort.summarize(result, baseline, _SINCE)
        self.assertEqual(got["family"], "macOS")
        self.assertAlmostEqual(got["baseline_share"], 0.7)
        self.assertFalse(got["skewed"])

    def test_a_smaller_family_is_not_hidden_by_the_largest(self):
        # macOS 12: 90% of the signature's Mac reports, 10% of the baseline's.
        result = _population({"Windows 11": 500, "Windows 10": 500,
                              "macOS 12": 90, "macOS 26": 10})
        baseline = {"Windows 11": 30000, "Windows 10": 30000, "macOS 12": 100, "macOS 26": 900}
        got = oscohort.summarize(result, baseline, _SINCE)
        self.assertEqual(got["cohort"], "macOS 12")
        self.assertEqual(got["family_reports"], 100)
        self.assertTrue(got["skewed"])

    def test_unskewed_falls_back_to_the_largest_family(self):
        result = _population({"Windows 11": 500, "Windows 10": 500, "macOS 26": 10})
        baseline = {"Windows 11": 30000, "Windows 10": 30000, "macOS 26": 900}
        got = oscohort.summarize(result, baseline, _SINCE)
        self.assertEqual(got["family"], "Windows")
        self.assertFalse(got["skewed"])

    def test_unknown_without_baseline(self):
        self.assertIsNone(oscohort.summarize(_result(_RISING), {}, _SINCE)["cohort"])

    def test_family_and_label(self):
        self.assertEqual(oscohort.family("Windows 11"), "Windows")
        self.assertEqual(oscohort.family("OS X 10.15"), "macOS")
        self.assertEqual(oscohort.family("Ubuntu 26.04.1 LTS"), "Linux")
        self.assertIsNone(oscohort.family("Unknown"))
        self.assertEqual(oscohort.label("Windows 11"), "Windows 11")
        self.assertEqual(oscohort.label("Android 99"), "Android 99")

    def test_query_scope(self):
        def respond(params, handler, handlerdata):
            handler(_population({"Android 37": 50}), handlerdata)
            return mock.Mock()

        for channel, search_channel, major in (("esr153", "esr", 153),
                                               ("release", "release", None)):
            with (
                self.subTest(channel=channel),
                mock.patch.dict(oscohort._BASELINES, clear=True),
                mock.patch.object(oscohort.socorro, "SuperSearch", side_effect=respond) as search,
            ):
                facts = oscohort.os_cohort(_SIG, "Firefox", channel)
                self.assertEqual(facts["cohort"], "Android 37")
                self.assertEqual(facts["reports"], 50)
                self.assertEqual(search.call_count, 2)
                for call in search.call_args_list:
                    params = call.kwargs["params"]
                    self.assertEqual(params["release_channel"], search_channel)
                    if major is None:
                        self.assertNotIn("major_version", params)
                    else:
                        self.assertEqual(params["major_version"], major)

    def test_lookup_failure_is_unknown(self):
        with mock.patch.object(oscohort.socorro, "SuperSearch", side_effect=RuntimeError):
            self.assertEqual(oscohort.os_cohort(_SIG, "Fenix", "release"),
                             oscohort.NO_OS_COHORT)


class TestBrief(unittest.TestCase):
    def test_skewed_cohort_is_printed(self):
        facts = _summarize()
        text = "\n".join(triage._os_cohort_lines({"os_cohort": facts}))
        self.assertIn("OS VERSION CONCENTRATION: Fenix release, last 56 days.", text)
        self.assertIn("are on Android API 37 (Android 17) from 500 installations", text)
        self.assertIn("reports on Android API 37 (Android 17) grew x4.0 and the rest of the "
                      "signature x1.0", text)
        self.assertIn("targetSdkVersion", text)
        self.assertNotIn("This report is on", text)

    def test_report_on_the_cohort_and_outside_it(self):
        facts = _summarize()
        on = "\n".join(triage._os_cohort_lines(
            {"os_cohort": facts, "raw_crash": {"os_pretty_version": "Android 37"}}))
        self.assertIn("This report is on Android API 37 (Android 17).", on)
        self.assertIn("targetSdkVersion", on)
        off = "\n".join(triage._os_cohort_lines(
            {"os_cohort": facts, "raw_crash": {"os_pretty_version": "Android 36"}}))
        self.assertIn("This report is on Android API 36 (Android 16), outside that version.", off)
        self.assertNotIn("targetSdkVersion", off)
        self.assertIn("specific to this OS version", off)

    def test_flat_skew_gets_the_neutral_note(self):
        flat = [(20, {"Android 36": 2}) for _ in range(56)]
        facts = _summarize(flat)
        text = "\n".join(triage._os_cohort_lines({"os_cohort": facts}))
        self.assertIn("specific to this OS version", text)
        self.assertNotIn("targetSdkVersion", text)

    def test_nothing_unless_skewed(self):
        self.assertEqual(triage._os_cohort_lines({"os_cohort": {"cohort": "Windows 11",
                                                                "skewed": False}}), [])
        self.assertEqual(triage._os_cohort_lines({}), [])


class TestOomGateWaiver(unittest.TestCase):
    def _seed(self, cohort, os_version="Android 37"):
        raw = dict(tog._java_raw(_SIG), os_pretty_version=os_version)
        return dict(tog._seed(raw), product="Fenix", os_cohort=cohort)

    def test_rising_cohort_keeps_an_actionable_java_oom(self):
        dossier = tog._dossier()
        orch._apply_oom_gate(dossier, self._seed(
            {"cohort": "Android 37", "skewed": True, "rising": True, "share": 0.84,
             "ratio": 12.6}))
        self.assertEqual(dossier.verdict.decision, Decision.actionable)
        self.assertEqual(dossier.corroborations["oom_gate_waived"]["cohort"], "Android 37")

    def test_waiver_requires_a_rising_cohort_and_a_matching_report(self):
        for rising, os_version in ((True, "Android 36"), (False, "Android 37")):
            with self.subTest(rising=rising, os_version=os_version):
                dossier = tog._dossier()
                orch._apply_oom_gate(dossier, self._seed(
                    {"cohort": "Android 37", "skewed": True, "rising": rising}, os_version))
                self.assertEqual(dossier.verdict.decision, Decision.abstain)
                self.assertEqual(dossier.verdict.abstain_kind, AbstainKind.resource_exhaustion)
                self.assertNotIn("oom_gate_waived", dossier.corroborations)

    def test_recorded_without_the_weekly_series(self):
        dossier = tog._dossier()
        orch._record_os_cohort(dossier, {"os_cohort": {"cohort": "Android 37", "weekly": [[1]]}})
        self.assertEqual(dossier.corroborations["os_cohort"], {"cohort": "Android 37"})
        dossier = tog._dossier()
        orch._record_os_cohort(dossier, {"os_cohort": {"cohort": None}})
        self.assertNotIn("os_cohort", dossier.corroborations or {})


if __name__ == "__main__":
    unittest.main()
