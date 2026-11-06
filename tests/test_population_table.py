# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Spike population queries, counts and bug-table rendering."""
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau import spike_report, spikes  # noqa: E402
from crashclouseau.agent import spike_escalation as se  # noqa: E402
from tests import REAL_POPULATION_TABLE  # noqa: E402
from tests.test_spike_escalation import _FilerBase, _esc  # noqa: E402

BEFORE = {"label": "The 56 days before", "reports": 102, "installs": 58, "installs_floor": False,
          "busiest": 6, "raptor_lake": 73}
DURING = {"label": "2026-11-03 to 2026-11-09", "reports": 34, "installs": 6,
          "installs_floor": False, "busiest": 17, "raptor_lake": 34}
TABLE = {"channel": "beta", "columns": [BEFORE, DURING]}


class TestScopes(unittest.TestCase):
    def test_a_build_day_compares_with_its_builds_before(self):
        esc = _esc(created=datetime(2026, 9, 5, tzinfo=timezone.utc),
                   payload={"spike": {"kind": "build_day", "history_days": 21}})
        (label, during), (blabel, before), _end = se._spike_scopes(esc, "mozilla::Foo::Bar")
        self.assertEqual((label, blabel), ("Builds of 2026-09-03", "Builds of the 21 days before"))
        self.assertEqual(during["build_id"], [">=20260903000000", "<20260904000000"])
        self.assertEqual(before["build_id"], [">=20260813000000", "<20260903000000"])
        self.assertEqual(before["date"], [">=2026-08-13", "<2026-09-05T00:00:00+00:00"])

    def test_a_rate_spike_compares_with_its_baseline_days(self):
        esc = _esc(payload={"spike": {"kind": "rate", "window_start": "2026-11-03",
                                      "window_end": "2026-11-09", "baseline_days": 56}})
        (label, during), (blabel, before), _end = se._spike_scopes(esc, "mozilla::Foo::Bar")
        self.assertEqual((label, blabel), ("2026-11-03 to 2026-11-09", "The 56 days before"))
        self.assertEqual(during["date"], [">=2026-11-03", "<2026-11-10"])
        self.assertEqual(before["date"], [">=2026-09-08", "<2026-11-03"])

    def test_a_version_step_compares_with_the_previous_major(self):
        spike = {"kind": "version_step", "major": 159, "from_major": 158,
                 "window_start": "2026-10-28", "window_end": "2026-11-04",
                 "from_release_day": "2026-10-14"}
        esc = _esc(channel="release", payload={"spike": spike})
        (label, during), (blabel, before), _end = se._spike_scopes(esc, "mozilla::Foo::Bar")
        self.assertEqual((label, blabel), ("Firefox 159 (2026-10-28 to 2026-11-04)",
                                           "Firefox 158 (2026-10-14 to 2026-10-27)"))
        self.assertEqual((during["major_version"], before["major_version"]), ("159", "158"))
        self.assertEqual(before["date"], [">=2026-10-14", "<2026-10-28"])
        spike.pop("from_release_day")
        self.assertIsNone(se._spike_scopes(esc, "mozilla::Foo::Bar")[1])


class TestPeriodCounts(unittest.TestCase):
    END = datetime(2026, 11, 9, tzinfo=timezone.utc)

    def test_installs_busiest_and_raptor_lake_on_either_rendering(self):
        found = {"total": 34, "facets": {
            "install_time": [{"term": "1793668339", "count": 17}, {"term": "1780000000", "count": 9},
                             {"term": "1770000000", "count": 8}],
            "cpu_info": [{"term": "family 6 model 183 stepping 1", "count": 30},
                         {"term": "GenuineIntel family 6 model 183 stepping 1", "count": 4}]}}
        with mock.patch.object(spikes, "_search", return_value=found) as search:
            got = se._period_counts({"signature": ["=x"]}, "Firefox", self.END)
        self.assertEqual(got, {"reports": 34, "installs": 3, "installs_floor": False,
                               "busiest": 17, "raptor_lake": 34})
        self.assertEqual(search.call_args.args[0]["_facets"], ["install_time", "cpu_info"])

    def test_reports_without_usable_install_times_leave_installations_unknown(self):
        found = {"total": 34, "facets": {"install_time": [{"term": "0", "count": 34}],
                                         "cpu_info": []}}
        with mock.patch.object(spikes, "_search", return_value=found):
            got = se._period_counts({}, "Firefox", self.END)
        self.assertEqual((got["reports"], got["installs"], got["busiest"]), (34, None, None))
        empty = {"total": 0, "facets": {"install_time": [], "cpu_info": []}}
        with mock.patch.object(spikes, "_search", return_value=empty):
            got = se._period_counts({}, "Firefox", self.END)
        self.assertEqual((got["reports"], got["installs"], got["busiest"]), (0, 0, 0))

    def test_a_failed_or_malformed_read_is_none(self):
        for found in (None, {"facets": {}}, {"total": 3, "facets": {"install_time": []}}):
            with mock.patch.object(spikes, "_search", return_value=found):
                self.assertIsNone(se._period_counts({}, "Firefox", self.END))

    def test_the_table_needs_both_periods(self):
        esc = _esc(payload={"spike": {"kind": "rate", "window_start": "2026-11-03",
                                      "window_end": "2026-11-09"}})
        counts = [dict(BEFORE), dict(DURING)]
        with mock.patch.object(se, "_period_counts", side_effect=counts):
            table = REAL_POPULATION_TABLE(esc, "x")
        self.assertEqual([c["label"] for c in table["columns"]],
                         ["The 56 days before", "2026-11-03 to 2026-11-09"])
        with mock.patch.object(se, "_period_counts", side_effect=[dict(BEFORE), None]):
            self.assertIsNone(REAL_POPULATION_TABLE(esc, "x"))


class TestRendering(unittest.TestCase):
    def test_the_2089254_table(self):
        self.assertEqual(spike_report.population_table({"population_table": TABLE}), "\n".join([
            "Crash-stats counts for this signature on beta when this was posted:",
            "",
            "| | The 56 days before | 2026-11-03 to 2026-11-09 |",
            "|:---|:---:|:---:|",
            "| Reports | 102 | 34 |",
            "| Distinct install times | 58 | 6 |",
            "| Reports from the busiest install time | 6 (6%) | 17 (50%) |",
            "| Reports from Intel Raptor Lake CPUs (family 6 model 183 stepping 1) | 73 (72%) | 34 (100%) |",
        ]))

    def test_unknown_installations_are_not_shown_as_zero(self):
        cols = [BEFORE, dict(DURING, installs=None, busiest=None)]
        text = spike_report.population_table({"population_table": dict(TABLE, columns=cols)})
        self.assertIn("| Distinct install times | 58 | unknown |", text)
        self.assertIn("| Reports from the busiest install time | 6 (6%) | unknown |", text)
        self.assertIn("| Reports | 102 | 34 |", text)

    def test_no_raptor_lake_row_without_raptor_lake_and_a_floor_says_so(self):
        cols = [dict(BEFORE, raptor_lake=0), dict(DURING, raptor_lake=0, installs_floor=True)]
        text = spike_report.population_table({"population_table": dict(TABLE, columns=cols)})
        self.assertNotIn("Raptor Lake", text)
        self.assertIn("| Distinct install times | 58 | at least 6 |", text)
        self.assertIsNone(spike_report.population_table({}))
        zero = [dict(BEFORE, reports=0, busiest=0), DURING]
        self.assertIn("| Reports from the busiest install time | 0 | 17 (50%) |",
                      spike_report.population_table({"population_table": {"columns": zero}}))


class TestInTheBug(_FilerBase):
    def test_the_table_follows_the_volume_paragraph(self):
        with mock.patch.object(se, "_population_table", return_value=TABLE):
            out = se.file_spike_bug(_esc(), self.brief, self.findings)
        self.assertTrue(out["filed"])
        self.assertEqual(out["population_table"], TABLE)
        body = self.created[0]["description"]
        volume = body.index("**Filed because this signature's crash volume spiked.**")
        table = body.index("| Reports | 102 | 34 |")
        analysis = body.index("Clouseau analysis")
        self.assertLess(volume, table)
        self.assertLess(table, analysis)

    def test_no_table_without_both_periods(self):
        out = se.file_spike_bug(_esc(), self.brief, self.findings)
        self.assertNotIn("population_table", out)
        self.assertNotIn("Crash-stats counts", self.created[0]["description"])
