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

    def test_describe_states_the_numbers_once(self):
        text = spikes.describe({"kind": "build_day", "count": 32, "installs": 21,
                                "baseline": [1, 0, 2]}, "nightly", "2026-09-03", "20260903093145")
        self.assertIn("32 reports from 21 distinct installations", text)
        self.assertIn("1, 0, 2", text)
        text = spikes.describe({"kind": "build_day", "count": 8, "installs": 5,
                                "baseline": [0, 0, 0]}, "beta", "2026-09-03", None)
        self.assertIn("none of the preceding 3 build-days had any", text)


class TestTheKnobs(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
