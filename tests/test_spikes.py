# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""What a REAL crash spike is -- the predicate that files a bug by itself -- and where it is kept.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
        uv run python -m unittest tests.test_spikes

`crashclouseau/spikes.py` decides what the pipeline TELLS PEOPLE, the way the crash-spikes
dashboard decides minus its seasonal model: the channel's crash floor, several distinct
installations, the selector's 3x ratio and a Poisson excess at the dashboard's `major` alert rate.
`utils.is_spike` decides what it SPENDS on and fires on 0 -> 1; this must not. Pure arithmetic and
the table's shape; the escalation that acts on it is tested in tests/test_spike_escalation.py.
"""
import os
import unittest
from datetime import date
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau import config, models, spikes, utils  # noqa: E402

NIGHTLY = dict(floor=3, ratio=3, min_installs=3, z_min=spikes.z_threshold(0.00015))


def _row(number, baseline, installs, outcome=utils.SELECTED, signature="mozilla::Foo::Bar",
         build_day="2026-09-03", picked="20260903093145", ever_selected=True):
    return {"signature": signature, "product": "Firefox", "channel": "nightly",
            "build_day": build_day, "outcome": outcome, "number": number, "position": 5,
            "evaluable": True, "baseline": list(baseline),
            "bids": {picked: {"count": number, "installs": installs}}, "picked": picked,
            "ever_selected": ever_selected}


class TestThePredicate(unittest.TestCase):
    def test_zero_to_one_is_a_selection_not_a_spike(self):
        for n in range(1, 6):
            self.assertFalse(spikes.is_real_spike(n, [0, 0, 0], installs=n, **NIGHTLY), n)

    def test_from_zero_needs_six_crashes_and_three_machines_on_nightly(self):
        self.assertTrue(spikes.is_real_spike(6, [0, 0, 0], installs=3, **NIGHTLY))
        self.assertFalse(spikes.is_real_spike(6, [0, 0, 0], installs=2, **NIGHTLY))
        self.assertFalse(spikes.is_real_spike(60, [0, 0, 0], installs=1, **NIGHTLY),
                         "one machine crashing sixty times is one machine")

    def test_over_a_baseline_the_excess_test_and_the_ratio_both_bind(self):
        # baseline 1: 3x is 3, the excess test asks for 9.
        self.assertFalse(spikes.is_real_spike(8, [1, 0, 0], installs=8, **NIGHTLY))
        self.assertTrue(spikes.is_real_spike(9, [1, 0, 0], installs=8, **NIGHTLY))
        # baseline 3: 13.
        self.assertFalse(spikes.is_real_spike(12, [3, 0, 0], installs=10, **NIGHTLY))
        self.assertTrue(spikes.is_real_spike(13, [3, 0, 0], installs=10, **NIGHTLY))
        # baseline 10: the 3x ratio is the binding bar (29 fails it, 30 passes).
        self.assertFalse(spikes.is_real_spike(29, [10, 8, 9], installs=20, **NIGHTLY))
        self.assertTrue(spikes.is_real_spike(30, [10, 8, 9], installs=20, **NIGHTLY))

    def test_the_loudest_prior_day_is_the_baseline(self):
        self.assertFalse(spikes.is_real_spike(40, [50, 0, 0], installs=30, **NIGHTLY))

    def test_the_z_threshold_is_the_dashboard_alert_rate(self):
        self.assertAlmostEqual(spikes.z_threshold(0.00015), 3.615, places=2)   # major
        self.assertAlmostEqual(spikes.z_threshold(0.0015), 2.968, places=2)    # spike
        # A typo cannot open or close the gate.
        self.assertLess(spikes.z_threshold(0), 7)
        self.assertGreater(spikes.z_threshold(1.0), -0.1)

    def test_the_channels_have_their_own_floors(self):
        self.assertIsNotNone(spikes.judge_build_day(8, [0, 0, 0], 5, "Firefox", "nightly"))
        # Beta: floor 10 crashes, 6 installs. Release: 50 / 50.
        self.assertIsNone(spikes.judge_build_day(8, [0], 5, "Firefox", "beta"))
        self.assertIsNone(spikes.judge_build_day(12, [0], 5, "Firefox", "beta"))
        self.assertIsNotNone(spikes.judge_build_day(12, [0], 6, "Firefox", "beta"))
        self.assertIsNone(spikes.judge_build_day(49, [0], 60, "Firefox", "release"))
        self.assertIsNotNone(spikes.judge_build_day(60, [0], 60, "Firefox", "release"))

    def test_the_facts_carry_every_number_the_decision_used(self):
        facts = spikes.judge_build_day(32, [1, 0, 2], 21, "Firefox", "nightly")
        self.assertEqual(facts["kind"], "build_day")
        self.assertEqual((facts["count"], facts["installs"], facts["baseline_max"]), (32, 21, 2))
        self.assertEqual(facts["ratio"], 16.0)
        self.assertGreater(facts["z"], facts["z_min"])
        self.assertEqual(facts["floor"], 3)
        self.assertEqual(facts["min_installs"], 3)

    def test_day_installs_sums_the_builds_of_the_day(self):
        self.assertEqual(spikes.day_installs({"a": {"count": 3, "installs": 2},
                                              "b": {"count": 1, "installs": 1},
                                              "c": {"count": 0}}), 3)
        self.assertEqual(spikes.day_installs(None), 0)

    def test_a_rate_pick_is_judged_on_installs_against_its_own_rate(self):
        weak = {"signature_trend_ratio": 3.1, "signature_trend_installs": 5,
                "signature_trend_expected_installs": 1.6, "signature_trend_window_days": 7,
                "signature_trend_baseline_days": 56, "signature_trend_reports": 6}
        self.assertIsNone(spikes.judge_rate(weak, "Firefox", "nightly"))
        strong = dict(weak, signature_trend_ratio=18.0, signature_trend_installs=45,
                      signature_trend_expected_installs=2.5, signature_trend_reports=67)
        facts = spikes.judge_rate(strong, "Firefox", "nightly")
        self.assertEqual(facts["kind"], "rate")
        self.assertEqual(facts["installs"], 45)
        self.assertGreater(facts["z"], facts["z_min"])
        self.assertIsNone(spikes.judge_rate({}, "Firefox", "nightly"),
                          "a rate we could not measure is not a spike")

    def test_judge_selection_dispatches_on_the_outcome(self):
        with mock.patch.object(spikes, "build_history", return_value=[]):
            self.assertIsNotNone(spikes.judge_selection(_row(32, [1, 0, 2], 21), "Firefox", "nightly"))
            self.assertIsNone(spikes.judge_selection(_row(1, [0, 0, 0], 1), "Firefox", "nightly"))
            declined = _row(40, [0, 0, 0], 30, outcome=utils.BELOW_INSTALL_THRESHOLD,
                            ever_selected=False)
            self.assertIsNone(spikes.judge_selection(declined, "Firefox", "nightly"),
                              "a pair the pipeline never analysed has nothing to investigate")
        rising = _row(6, [], 6, outcome=utils.RISING_RATE)
        self.assertIsNone(spikes.judge_selection(rising, "Firefox", "nightly", trend_facts={}))
        strong = {"signature_trend_ratio": 18.0, "signature_trend_installs": 45,
                  "signature_trend_expected_installs": 2.5, "signature_trend_window_days": 7,
                  "signature_trend_baseline_days": 56}
        self.assertEqual(
            spikes.judge_selection(rising, "Firefox", "nightly", trend_facts=strong)["kind"], "rate")

    def test_the_signatures_own_builds_are_the_baseline(self):
        """Bug 2070317: 10 reports of a 522-day-old OOM signature on 156.0b4, judged against the
        selector's one preceding build-day (0, the crash wore a sibling signature on 156.0b3)
        and filed as an appearance from zero, while the beta builds of the three weeks before
        carried 90-221 reports each. The loudest of the signature's own builds is the baseline."""
        loud = [{"buildid": "20260826090609", "count": 221},
                {"buildid": "20260831122253", "count": 3},
                {"buildid": "20260902090331", "count": 90}]
        self.assertIsNone(spikes.judge_build_day(10, [0], 6, "Firefox", "beta", history=loud))
        # The same row with a genuinely empty history is what the selector saw, and is a spike.
        facts = spikes.judge_build_day(10, [0], 6, "Firefox", "beta", history=[])
        self.assertIsNotNone(facts)
        self.assertEqual((facts["history"], facts["history_max"], facts["history_days"]),
                         ([], 0, 21))
        # A quiet history joins the selector's baseline; the loudest of either is the bar.
        facts = spikes.judge_build_day(32, [1, 0, 2], 21, "Firefox", "nightly",
                                       history=[{"buildid": "20260820090000", "count": 4}])
        self.assertEqual((facts["baseline"], facts["baseline_max"], facts["history_max"]),
                         ([1, 0, 2], 4, 4))
        self.assertEqual(facts["ratio"], 8.0)
        # Without a history the arithmetic is the selector's alone (the pure entry point).
        self.assertNotIn("history", spikes.judge_build_day(32, [1, 0, 2], 21, "Firefox", "nightly"))

    def test_an_unreadable_history_is_not_a_spike(self):
        with mock.patch.object(spikes, "build_history", return_value=None):
            self.assertIsNone(spikes.judge_selection(_row(32, [1, 0, 2], 21), "Firefox", "nightly"))

    def test_judge_selection_reads_the_history_of_the_siblings_before_the_picked_build(self):
        calls = []

        def history(signatures, product, channel, buildid, days):
            calls.append((list(signatures), product, channel, buildid, days))
            return []
        row = dict(_row(32, [1, 0, 2], 21, signature="Q::<T>::operator()"),
                   signatures=["Q::<T>::operator()", "Q::$::operator()"])
        with mock.patch.object(spikes, "build_history", side_effect=history):
            self.assertIsNotNone(spikes.judge_selection(row, "Firefox", "beta"))
        self.assertEqual(calls, [(["Q::<T>::operator()", "Q::$::operator()"], "Firefox", "beta",
                                  "20260903093145", 21)])

    def test_the_history_query_is_the_signatures_builds_of_the_horizon_on_the_channel(self):
        params = spikes._history_params(["A::a", "A::b"], "Firefox", "beta", "20260907090530", 21)
        self.assertEqual(params["signature"], ["=A::a", "=A::b"])
        self.assertEqual(params["release_channel"], ["beta", "aurora"])
        self.assertEqual(params["build_id"], [">=20260817090530", "<20260907090530"],
                         "21 days before the build, up to but excluding the build itself")
        self.assertEqual(params["date"], ">=2026-08-17")
        self.assertEqual((params["_results_number"], params["_facets"]), (0, "build_id"))

    def test_build_history_is_sorted_by_build_and_none_when_socorro_fails(self):
        facets = {"build_id": [{"term": 20260902090331, "count": 90},
                               {"term": 20260826090609, "count": 221},
                               {"term": 20260831122253, "count": 3}]}
        with mock.patch.object(spikes, "_search", return_value={"total": 314, "facets": facets}):
            hist = spikes.build_history(["S"], "Firefox", "beta", "20260907090530", 21)
        self.assertEqual([(h["buildid"], h["count"]) for h in hist],
                         [("20260826090609", 221), ("20260831122253", 3), ("20260902090331", 90)])
        with mock.patch.object(spikes, "_search", return_value={"total": 0, "facets": {"build_id": []}}):
            self.assertEqual(spikes.build_history(["S"], "Firefox", "beta", "20260907090530", 21), [])
        with mock.patch.object(spikes, "_search", side_effect=Exception("503")):
            self.assertIsNone(spikes.build_history(["S"], "Firefox", "beta", "20260907090530", 21))
        with mock.patch.object(spikes, "_search", return_value=None):
            self.assertIsNone(spikes.build_history(["S"], "Firefox", "beta", "20260907090530", 21))
        self.assertIsNone(spikes.build_history([], "Firefox", "beta", "20260907090530", 21))

    def test_describe_states_the_history(self):
        base = {"kind": "build_day", "count": 32, "installs": 21, "baseline": [1, 0, 2],
                "history_days": 21}
        text = spikes.describe(dict(base, history=[{"buildid": "a", "count": 4},
                                                   {"buildid": "b", "count": 1},
                                                   {"buildid": "c", "count": 0}],
                                    history_max=4), "nightly", "2026-09-03", None)
        self.assertIn("the preceding 3 build-days had 1, 0, 2 reports", text)
        self.assertIn("the loudest of its 3 earlier builds in the 21 days before had 4", text)
        text = spikes.describe(dict(base, baseline=[0], history=[], history_max=0),
                               "beta", "2026-09-07", "20260907090530")
        self.assertIn("none of the preceding 1 build-day had any, and no build in the 21 days "
                      "before had any report of it", text)

    def test_describe_states_the_numbers_once(self):
        text = spikes.describe({"kind": "build_day", "count": 32, "installs": 21,
                                "baseline": [1, 0, 2]}, "nightly", "2026-09-03", "20260903093145")
        self.assertIn("32 reports from 21 distinct installations", text)
        self.assertIn("1, 0, 2", text)
        text = spikes.describe({"kind": "build_day", "count": 8, "installs": 5,
                                "baseline": [0, 0, 0]}, "beta", "2026-09-03", None)
        self.assertIn("none of the preceding 3 build-days had any", text)


class TestTheKnobs(unittest.TestCase):
    def test_the_history_horizon_holds_the_channels_previous_build(self):
        # Nightly and beta ship several builds a week; release and ESR one every four, so their
        # horizon has to span a cycle or the signature's previous build is not in it.
        self.assertEqual(config.get_spike("history_days", "Firefox", "nightly"), 21)
        self.assertEqual(config.get_spike("history_days", "Firefox", "beta"), 21)
        for channel in ("release", "esr", "esr153"):
            self.assertGreaterEqual(config.get_spike("history_days", "Firefox", channel), 35, channel)

    def test_the_install_floor_is_more_than_one_machine_on_every_channel(self):
        for channel in ("nightly", "beta", "release"):
            self.assertGreaterEqual(config.get_spike("real_installs", "Firefox", channel),
                                    config.get_threshold("installs", "Firefox", channel))
            self.assertGreater(config.get_spike("real_installs", "Firefox", channel), 1,
                               "one machine must never be a spike")
        # The alert rate is the dashboard's `major` level; a stripped config errs quieter.
        self.assertEqual(config.get_spike("real_alert_rate", "Firefox", "nightly"), 0.00015)
        self.assertGreaterEqual(config._SPIKE_DEFAULTS["real_installs"],
                                config.get_spike("real_installs", "Firefox", "nightly"))


class TestTheTable(unittest.TestCase):
    def test_registered_as_a_post_deploy_table(self):
        self.assertIn("spike_escalations", models._ADDED_TABLES)
        cols = {c.name for c in models.db.metadata.tables["spike_escalations"].columns}
        self.assertEqual(cols, {"id", "signature", "product", "channel", "build_day", "buildid",
                                "uuid", "kind", "status", "attempts", "payload", "cost_usd",
                                "input_tokens", "output_tokens", "cache_read_tokens", "created",
                                "updated"})

    def test_to_dict(self):
        row = models.SpikeEscalation("sig", "Firefox", "nightly", date(2026, 9, 3),
                                     buildid="20260903093145", uuid="u-1",
                                     payload={"spike": {"kind": "build_day"},
                                              "filing": {"filed": True, "bug": 1}})
        d = row.to_dict()
        self.assertEqual((d["signature"], d["build_day"], d["filing"]["bug"]),
                         ("sig", "2026-09-03", 1))
        self.assertIsNone(d["cost_usd"])

    def test_to_dict_carries_what_the_tasks_page_needs(self):
        """A `done` row with no cost and no filing is unreadable without the sweep's reason;
        the ordinary Bug column falls back to a spike filing by signature AND sibling."""
        row = models.SpikeEscalation("sig", "Firefox", "nightly", date(2026, 9, 3),
                                     payload={"spike": {"kind": "build_day"},
                                              "spike_sentence": "32 reports from 21 installs",
                                              "siblings": ["sig", "sig<T>"],
                                              "skipped": "the ordinary triage filed bug 7 for this spike"})
        d = row.to_dict()
        self.assertEqual(d["spike_sentence"], "32 reports from 21 installs")
        self.assertEqual(d["siblings"], ["sig", "sig<T>"])
        self.assertEqual(d["skipped"], "the ordinary triage filed bug 7 for this spike")
        self.assertIsNone(models.SpikeEscalation("s", "Firefox", "nightly", date(2026, 9, 3))
                          .to_dict()["skipped"])


if __name__ == "__main__":
    unittest.main()
