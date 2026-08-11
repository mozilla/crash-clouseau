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


def _numbers_from(series, day_installs=5, first_day=1):
    """`numbers` for a run of consecutive build-days, one buildid each.

    `series` is `[(day_of_month, count), ...]`; a day with count 0 still gets its bucket,
    because a build-day exists as soon as ANY signature crashed on it."""
    days = {}
    for offset, count in enumerate(series):
        d = first_day + offset
        day, bid = _day(d), _bid(d)
        days[day] = {
            "count": count,
            "bids": {bid: count},
            "installs": {bid: day_installs if count else 0},
        }
    return days


class TestEvaluateDays(unittest.TestCase):
    """The decision record, the un-evaluable prefix, and the maturity bar."""

    def test_one_record_per_build_day(self):
        numbers = _numbers_from([0, 0, 0, 8])
        _, _, records = utils.evaluate_days(numbers, 3, 1, 3, 3)
        self.assertEqual(len(records), 4)
        self.assertEqual([r["index"] for r in records], [0, 1, 2, 3])
        self.assertEqual([r["day"] for r in records], sorted(numbers))

    def test_prefix_is_recorded_but_never_selected(self):
        # 4 crashes at index 1: is_spike(4, [0]) is True, but the day is inside the
        # untested `ndays` prefix. This is the places::History::History shape.
        numbers = _numbers_from([0, 4, 0, 0])
        picked, _, records = utils.evaluate_days(numbers, 3, 1, 3, 3)
        self.assertEqual(picked, {})
        self.assertEqual(records[1]["outcome"], utils.UNTESTABLE_PREFIX)
        self.assertFalse(records[1]["evaluable"])
        self.assertTrue(records[1]["spiked"])

    def test_prefix_below_floor_is_not_flagged_as_a_near_miss(self):
        # index 0 has an EMPTY baseline, so the from-zero rule always fires there; without
        # the floor every 1-crash day would be logged as a near miss.
        numbers = _numbers_from([1, 0, 0, 0])
        _, _, records = utils.evaluate_days(numbers, 3, 1, 3, 3)
        self.assertEqual(records[0]["outcome"], utils.NOT_SPIKING)

    def test_mature_day_must_clear_the_floor(self):
        # Day 4 is 10 days before the run: the from-zero rule alone is no longer enough.
        numbers = _numbers_from([0, 0, 0, 2])
        picked, _, records = utils.evaluate_days(
            numbers, 3, 1, 3, 3, today=_day(14), mature_after=5, mature_installs=2
        )
        self.assertEqual(picked, {})
        self.assertEqual(records[3]["outcome"], utils.IMMATURE)

    def test_mature_day_must_carry_two_installations(self):
        # Clears the floor, but one machine produced all of it.
        numbers = _numbers_from([0, 0, 0, 8], day_installs=1)
        picked, _, records = utils.evaluate_days(
            numbers, 3, 1, 3, 3, today=_day(14), mature_after=5, mature_installs=2
        )
        self.assertEqual(picked, {})
        self.assertEqual(records[3]["outcome"], utils.IMMATURE)

    def test_mature_day_that_clears_both_is_selected(self):
        numbers = _numbers_from([0, 0, 0, 8], day_installs=4)
        picked, _, records = utils.evaluate_days(
            numbers, 3, 1, 3, 3, today=_day(14), mature_after=5, mature_installs=2
        )
        self.assertEqual(picked, {_bid(4): 8})
        self.assertEqual(records[3]["outcome"], utils.SELECTED)
        self.assertEqual(records[3]["picked"], _bid(4))

    def test_fresh_day_keeps_the_from_zero_sensitivity(self):
        # Two days old: the maturity bar must not touch a build still filling up, so a
        # single crash from a quiet window is still selected.
        numbers = _numbers_from([0, 0, 0, 1], day_installs=1)
        picked, _, _ = utils.evaluate_days(
            numbers, 3, 1, 3, 3, today=_day(6), mature_after=5, mature_installs=2
        )
        self.assertEqual(picked, {_bid(4): 1})

    def test_today_none_disables_maturity(self):
        numbers = _numbers_from([0, 0, 0, 1], day_installs=1)
        picked, _, _ = utils.evaluate_days(numbers, 3, 1, 3, 3, mature_after=5)
        self.assertEqual(picked, {_bid(4): 1})

    def test_wrapper_agrees_with_evaluate_days(self):
        numbers = _numbers_from([0, 0, 0, 8])
        picked, big, _ = utils.evaluate_days(numbers, 3, 1, 3, 3)
        self.assertEqual(utils.get_new_crashing_bids(numbers, 3, 1, 3, 3), (picked, big))

    def test_install_shortfall_on_a_fresh_day_is_its_own_outcome(self):
        numbers = _numbers_from([0, 0, 0, 8], day_installs=2)
        _, _, records = utils.evaluate_days(numbers, 3, 6, 3, 3)
        self.assertEqual(records[3]["outcome"], utils.BELOW_INSTALL_THRESHOLD)


class TestPlacesHistoryRegression(unittest.TestCase):
    """The case this was built for.

    `mozilla::places::History::History`, run of 2026-08-07: all crashes sit on the
    2026-07-31 build, which by then held 4 reports over 4 installations. Under the old
    8-day window that build-day was the 2nd bucket in the series, so it fell inside the
    untested `ndays` prefix and the spike -- already true -- was discarded. A 21-day
    window puts 14 build-days ahead of it, and it is selected."""

    RUN_DAY = 7          # 2026-08-07, modelled inside one month for readability
    TARGET_OFFSET = 0    # the build-day carrying the crashes

    def _series(self, lead_days):
        """`lead_days` quiet build-days, then the 4-crash target, then quiet days up to
        the run day. Returns (numbers, target_bid)."""
        series = [0] * lead_days + [4] + [0] * 3
        numbers = _numbers_from(series, day_installs=4, first_day=1)
        target = _bid(1 + lead_days)
        # only the target day has crashes, so give the rest zero installs
        return numbers, target

    def _run(self, lead_days):
        numbers, target = self._series(lead_days)
        today = _day(1 + lead_days + 7)          # the target build is 7 days old
        picked, _, records = utils.evaluate_days(
            numbers, 3, 1, 3, 3, today=today, mature_after=5, mature_installs=2
        )
        record = next(r for r in records if r["day"] == _day(1 + lead_days))
        return picked, record, target

    def test_old_eight_day_window_misses_it(self):
        # One earlier build-day ahead of the target: index 1, inside the ndays prefix.
        picked, record, _ = self._run(lead_days=1)
        self.assertEqual(picked, {})
        self.assertEqual(record["index"], 1)
        self.assertFalse(record["evaluable"])
        self.assertTrue(record["spiked"])
        self.assertEqual(record["outcome"], utils.UNTESTABLE_PREFIX)

    def test_wider_window_selects_it(self):
        picked, record, target = self._run(lead_days=14)
        self.assertEqual(record["index"], 14)
        self.assertTrue(record["evaluable"])
        self.assertEqual(record["outcome"], utils.SELECTED)
        self.assertEqual(picked, {target: 4})

    def test_wider_window_still_rejects_a_single_machine(self):
        # Same timeline, but every crash from one installation: the maturity bar is what
        # keeps a lone flooding machine out of a 21-day window.
        numbers, _ = self._series(14)
        for day in numbers:
            numbers[day]["installs"] = {b: (1 if n else 0)
                                        for b, n in numbers[day]["bids"].items()}
        picked, _, records = utils.evaluate_days(
            numbers, 3, 1, 3, 3, today=_day(22), mature_after=5, mature_installs=2
        )
        self.assertEqual(picked, {})
        self.assertEqual(records[14]["outcome"], utils.IMMATURE)


if __name__ == "__main__":
    unittest.main()
