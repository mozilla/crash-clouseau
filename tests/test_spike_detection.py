# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# DATABASE_URL=sqlite:// python -m unittest tests.test_spike_detection
import unittest
from datetime import datetime

from crashclouseau import utils


def _old_rule(n, before):
    """The original strict step rule, kept here to pin down that the new union rule is a
    strict EXTENSION of it (everything it caught still fires)."""
    return bool(n and all(x == 0 for x in before))


class TestIsSpike(unittest.TestCase):
    """Union of the original hard-zero rule and an added relative-spike rule."""

    def test_keeps_hard_zero_brand_new(self):
        # Unchanged from the original: appears from a fully-quiet window at N >= 1.
        self.assertTrue(utils.is_spike(1, [0, 0, 0], floor=3, ratio=3))
        self.assertTrue(utils.is_spike(2, [0, 0, 0], floor=3, ratio=3))

    def test_stray_blip_then_spike(self):
        # 0,0,1,0 -> N: window is [0, 1, 0] (baseline 1). Fires at N >= 3, not before.
        self.assertFalse(_old_rule(3, [0, 1, 0]))                       # old rule missed it
        self.assertTrue(utils.is_spike(3, [0, 1, 0], floor=3, ratio=3))
        self.assertFalse(utils.is_spike(2, [0, 1, 0], floor=3, ratio=3))

    def test_worsening_existing_signature(self):
        # 10,20,10 -> 150: baseline 20, bar = max(3, 3*20) = 60.
        self.assertFalse(_old_rule(150, [10, 20, 10]))                  # old rule missed it
        self.assertTrue(utils.is_spike(150, [10, 20, 10], floor=3, ratio=3))
        self.assertFalse(utils.is_spike(40, [10, 20, 10], floor=3, ratio=3))  # 40 < 60

    def test_uses_max_not_mean(self):
        # One busy prior day must veto a modest bump; mean([50,0,0])=16.7 would pass 60 at
        # ratio 3, max=50 rejects it (needs >= 150).
        self.assertFalse(utils.is_spike(60, [50, 0, 0], floor=3, ratio=3))
        self.assertTrue(utils.is_spike(200, [50, 0, 0], floor=3, ratio=3))

    def test_floor_gates_tiny_baseline_spike(self):
        # baseline 1 clears the ratio at n=3, so the floor is what stops a 2-crash "spike".
        self.assertFalse(utils.is_spike(2, [1, 0, 0], floor=3, ratio=3))

    def test_is_strict_superset_of_old_rule(self):
        windows = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [10, 20, 10], [5, 5, 5]]
        for before in windows:
            for n in range(0, 40):
                if _old_rule(n, before):
                    self.assertTrue(
                        utils.is_spike(n, before, floor=3, ratio=3),
                        msg="old rule fired but union did not: n=%d before=%s" % (n, before),
                    )


class TestGetSpikeIndices(unittest.TestCase):
    def test_documented_example(self):
        nums = [0, 0, 0, 2, 0, 0, 0, 8, 1, 0, 0, 30]
        self.assertEqual(
            list(utils.get_spike_indices(nums, 3, floor=3, ratio=3)), [3, 7, 11]
        )
        # Contrast: the old rule stopped at [3, 7] -- a lone 1 blocked index 11.
        old = [i for i in range(3, len(nums)) if _old_rule(nums[i], nums[i - 3:i])]
        self.assertEqual(old, [3, 7])


def _day(d):
    return datetime(2026, 1, d)


def _bid(d, h=9):
    return datetime(2026, 1, d, h)


class TestGetNewCrashingBids(unittest.TestCase):
    """End-to-end over the per-day structure `get_new_signatures` builds. The candidate
    spike day is the last in `series` and carries a single buildid."""

    def _numbers(self, series, spike_installs=5):
        days, b = {}, None
        for idx, c in enumerate(series, start=1):
            day = _day(idx)
            if idx == len(series):
                b = _bid(idx)
                days[day] = {"count": c, "bids": {b: c}, "installs": {b: spike_installs}}
            else:
                days[day] = {"count": c, "bids": {}, "installs": {}}
        return days, b

    def test_hard_zero_spike_returned(self):
        numbers, b = self._numbers([0, 0, 0, 8])
        res, big = utils.get_new_crashing_bids(numbers, 3, threshold=1, floor=3, ratio=3)
        self.assertEqual(res, {b: 8})
        self.assertFalse(big)

    def test_worsening_existing_returned(self):
        numbers, b = self._numbers([10, 20, 10, 150])
        res, _ = utils.get_new_crashing_bids(numbers, 3, threshold=1, floor=3, ratio=3)
        self.assertEqual(res, {b: 150})

    def test_churn_returns_nothing(self):
        numbers, _ = self._numbers([5, 5, 5, 7])   # 7 < max(3, 3*5)=15
        res, _ = utils.get_new_crashing_bids(numbers, 3, threshold=1, floor=3, ratio=3)
        self.assertEqual(res, {})

    def test_install_threshold_filters_the_bid(self):
        numbers, _ = self._numbers([0, 0, 0, 8], spike_installs=2)
        res, _ = utils.get_new_crashing_bids(numbers, 3, threshold=6, floor=3, ratio=3)
        self.assertEqual(res, {})

    def test_big_flag_for_high_volume(self):
        numbers, _ = self._numbers([0, 0, 0, 600], spike_installs=50)
        _, big = utils.get_new_crashing_bids(numbers, 3, threshold=1, floor=3, ratio=3)
        self.assertTrue(big)


if __name__ == "__main__":
    unittest.main()
