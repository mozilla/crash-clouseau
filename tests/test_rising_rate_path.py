# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""The rate path: an EXISTING signature whose daily rate rose is selected without a spike day.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        uv run python -m unittest tests.test_rising_rate_path

``utils.evaluate_days`` asks whether one build-day is 3x the loudest of the three before it. A
rise spread over days never produces that day: ``QuotaManager::Shutdown::<T>::operator()``
tripled on nightly between 2026-07-16 and 07-21 with a loudest build-day of 2.25x, while the
7-day exposure-normalised rate read 3.7-4.7x for a week. Replayed over the 30 days to 2026-09-03,
~1.9 of nightly's ~6.7 daily rising episodes had not been spike-selected within a week, and ~6.5
of beta's ~6.6 -- so the path is BUDGETED per day and ranked, not thresholded.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import date, datetime, timedelta, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import models, sigtrend, utils  # noqa: E402
from tests.test_lambda_signatures import BIDS, D, T, no_rate_path, run_selector  # noqa: E402


class TestPickLatestBuild(unittest.TestCase):
    def test_newest_build_with_users_wins(self):
        numbers = {
            datetime(2026, 8, 13): {"count": 5, "bids": {"b13": 5}, "installs": {"b13": 5}},
            datetime(2026, 8, 14): {"count": 6, "bids": {"b14": 6}, "installs": {"b14": 6}},
        }
        self.assertEqual(utils.pick_latest_build(numbers, 1), (datetime(2026, 8, 14), "b14", 6))

    def test_a_build_below_the_install_threshold_is_skipped(self):
        numbers = {
            datetime(2026, 8, 13): {"count": 5, "bids": {"b13": 5}, "installs": {"b13": 5}},
            datetime(2026, 8, 14): {"count": 2, "bids": {"b14": 2}, "installs": {"b14": 1}},
        }
        # Newest build has one install: fall back to the one before it, or to nothing.
        self.assertEqual(utils.pick_latest_build(numbers, 5)[1], "b13")
        self.assertIsNone(utils.pick_latest_build(numbers, 6))


def _steady(sig="mozilla::Old::Hang", counts=(5, 5, 5, 6)):
    return {bid: {sig: (n, n)} for bid, n in zip(BIDS, counts)}


def _facts(ratio=3.5, installs=40, score=1e-6):
    return {"signature_trend_ratio": ratio, "signature_trend_installs": installs,
            "signature_trend_window_days": 7, "signature_trend_score": score}


def _rate_path(candidates, taken=0, covered=frozenset()):
    return (
        mock.patch.object(sigtrend, "rising_candidates", return_value=candidates),
        mock.patch.object(models.Selection, "taken_today", return_value=taken),
        mock.patch.object(models.Selection, "covered_recently", return_value=set(covered)),
    )


class TestTheRatePath(unittest.TestCase):
    """A steady 5-6 a day never spikes; a rising 7-day rate selects it anyway, within a budget."""

    def test_a_steady_signature_is_not_a_spike(self):
        data, _, _ = run_selector(_steady(), extra=no_rate_path())
        self.assertEqual(data, {})

    def test_rising_selects_the_newest_build_and_says_why(self):
        sig = "mozilla::Old::Hang"
        data, selection, _ = run_selector(
            _steady(sig), extra=_rate_path([(sig, [sig], _facts())]))
        newest = utils.get_build_date(BIDS[-1])
        self.assertEqual(data, {sig: {"bids": {newest: 6}, "protos": {newest: []},
                                      "installs": {newest: 0}}})
        rec = next(r for r in selection if r["outcome"] == utils.RISING_RATE)
        self.assertEqual(rec["signature"], sig)
        self.assertEqual(rec["picked"], newest)
        self.assertEqual(rec["trend_ratio"], 3.5)
        self.assertEqual(rec["baseline"], [])
        row = models.Selection._row(rec, "Firefox", "nightly", datetime.now(timezone.utc))
        self.assertTrue(row["ever_selected"])
        self.assertEqual(row["outcome"], "rising_rate")

    def test_the_proto_cap_is_the_rate_paths_own(self):
        sig = "mozilla::Old::Hang"
        _, _, proto_small = run_selector(_steady(sig),
                                         extra=_rate_path([(sig, [sig], _facts())]))
        self.assertEqual(proto_small.call_args.kwargs.get("proto_cap"), 3)

    def test_the_daily_budget_binds_best_first(self):
        a, b = "mozilla::A", "mozilla::B"
        pop = {bid: {a: (5, 5), b: (5, 5)} for bid in BIDS}
        # nightly budget is 3; two already taken today -> room for ONE, and the list is ranked.
        cands = [(b, [b], _facts(score=1e-9)), (a, [a], _facts(score=1e-3))]
        data, selection, _ = run_selector(pop, extra=_rate_path(cands, taken=2))
        self.assertEqual(set(data), {b})
        self.assertEqual([r["signature"] for r in selection
                          if r["outcome"] == utils.RISING_RATE], [b])

    def test_a_family_already_covered_is_not_paid_for_again(self):
        sig = "mozilla::Old::Hang"
        extra = _rate_path([(sig, [sig], _facts())], covered={sig})
        data, _, _ = run_selector(_steady(sig), extra=extra)
        self.assertEqual(data, {})

    def test_a_rise_with_nothing_current_is_skipped_and_costs_no_budget(self):
        sig = "mozilla::Old::Hang"
        gone = "mozilla::Gone"                       # rising in the rollup, absent from the window
        cands = [(gone, [gone], _facts(score=1e-9)), (sig, [sig], _facts())]
        data, _, _ = run_selector(_steady(sig), extra=_rate_path(cands, taken=2))
        self.assertEqual(set(data), {sig})

    def test_a_split_lambda_rising_as_a_family_picks_both_halves(self):
        pop = {bid: {T: (5, 5), D: (2, 2)} for bid in BIDS}
        data, selection, _ = run_selector(pop, extra=_rate_path([(T, [D, T], _facts())]))
        self.assertEqual(set(data), {T, D})
        recs = {r["signature"]: r for r in selection if r["outcome"] == utils.RISING_RATE}
        self.assertEqual(recs[T]["merged_with"], [D])

    def test_a_channel_with_no_budget_never_reads_the_database(self):
        extra = (
            mock.patch.object(models.Selection, "taken_today",
                              side_effect=AssertionError("must not be called")),
            mock.patch.object(sigtrend, "rising_candidates",
                              side_effect=AssertionError("must not be called")),
        )
        data, _, _ = run_selector(_steady(), channel="release", extra=extra)
        self.assertEqual(data, {})

    def test_a_broken_rollup_cannot_stop_the_spike_test(self):
        pop = _steady()
        pop[BIDS[-1]]["mozilla::New::Crash"] = (4, 4)
        data, _, _ = run_selector(pop, extra=(
            mock.patch.object(sigtrend, "rising_candidates", side_effect=RuntimeError("db")),
            mock.patch.object(models.Selection, "taken_today", return_value=0),
        ))
        self.assertEqual(set(data), {"mozilla::New::Crash"})


def _days(asof, n, per_day):
    return {asof - timedelta(days=i): (per_day, per_day) for i in range(n)}


class TestRisingCandidates(unittest.TestCase):
    """The scan behind the rate path: merged over families, ranked, existing signatures only."""

    ASOF = date(2026, 8, 21)

    def _scan(self, series, exclude=()):
        exposure = _days(self.ASOF, 63, 500)
        with mock.patch.object(models.ChannelDaily, "series", return_value=exposure), \
                mock.patch.object(models.SignatureDaily, "series_all", return_value=series):
            return sigtrend.rising_candidates("Firefox", "nightly", asof=self.ASOF,
                                              exclude=exclude)

    def _series(self, window_per_day, baseline_total):
        s = _days(self.ASOF, 7, window_per_day)
        base_days = [self.ASOF - timedelta(days=7 + i) for i in range(56)]
        for i, d in enumerate(base_days):
            s[d] = (1, 1) if i < baseline_total else (0, 0)
        return s

    def test_two_halves_below_the_floor_rise_together(self):
        # Each half: 3 installs in the window against an expected 0.5 -- a 6x that `is_rising`
        # refuses on the 5-install floor. Merged: 6 against 1, and it speaks.
        half = self._series(window_per_day=0, baseline_total=4)
        for i in range(3):
            half[self.ASOF - timedelta(days=i)] = (1, 1)
        out = self._scan({T: dict(half), D: dict(half), "Flat": self._series(1, 56)})
        self.assertEqual([(f, m) for f, m, _ in out], [(T, [D, T])])
        facts = out[0][2]
        self.assertEqual(facts["signature_trend_installs"], 6)
        self.assertGreaterEqual(facts["signature_trend_ratio"], 3.0)

    def test_a_flat_signature_and_a_new_one_are_not_candidates(self):
        new = _days(self.ASOF, 7, 10)                       # no baseline at all: no ratio
        out = self._scan({"Flat": self._series(1, 56), "New": new})
        self.assertEqual(out, [])

    def test_ranked_rarest_tail_first(self):
        loud = self._series(window_per_day=10, baseline_total=56)     # 70 vs 7 expected
        quiet = self._series(window_per_day=1, baseline_total=10)     # 7 vs 1.25 expected
        out = self._scan({"Loud": loud, "Quiet": quiet})
        self.assertEqual([f for f, _, _ in out], ["Loud", "Quiet"])

    def test_a_family_the_spike_test_took_is_excluded(self):
        loud = self._series(window_per_day=10, baseline_total=56)
        self.assertEqual(self._scan({T: loud, D: dict(loud)}, exclude=[D]), [])

    def test_no_exposure_is_no_answer(self):
        with mock.patch.object(models.ChannelDaily, "series", return_value={}):
            self.assertEqual(sigtrend.rising_candidates("Firefox", "nightly", asof=self.ASOF),
                             [])


if __name__ == "__main__":
    unittest.main()
