# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""A rising signature's ON-STACK candidate window reaches back ten days, not three.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        uv run python -m unittest tests.test_rising_onstack_window

`update.put_report` bounds the on-stack window (`Changeset.find`, filtered to files on the crash
stack) at `buildid - get_ndays()` on nightly and at the previous build's push on beta. `0aa2e02`
widened the OFF-stack pushlog window to 24 h on a rise and left this one alone because its
binding constraint is the file filter. It is bounded by the clock too: the July 2026 nightly rise
of `QuotaManager::Shutdown::<T>::operator()` was caused by a 07-16 batch touching
`dom/quota/ActorsParent.cpp` -- ON the stack -- and the rate read 3.7-4.7x from 07-21, so a 3-day
window from any build the rate path (`b065e9f`) would have picked could not contain it. Detection
was fixed there; this is attribution.

What these tests pin: the widening happens only on a rise; it is `WINDOW_DAYS + get_ndays()` and
not a new number; it is never NARROWER than the deployed bound, beta's previous-build bound
included; a report without a signature, or with a broken rollup, scores exactly as before; and the
serial chain hands `put_report` the signature it needs.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from dateutil.relativedelta import relativedelta  # noqa: E402

from crashclouseau import config, sigtrend, update  # noqa: E402

# A nightly built the day after the July rise cleared the ratio (07-21), and the batch that caused
# it: bugs 1998600/1998624/1998648, "Drop UnloadQuota's outer transaction", pushed 2026-07-16.
BUILD = datetime(2026, 7, 22, 10, 4, 59, tzinfo=timezone.utc)
BATCH = datetime(2026, 7, 16, 9, 30, 0, tzinfo=timezone.utc)
SIG = "mozilla::dom::quota::QuotaManager::Shutdown::<T>::operator()"


def _rising(ratio=3.7, installs=23):
    """A `trend_facts`-shaped dict that `is_rising` accepts -- the July numbers."""
    return {"signature_trend_window_days": 7, "signature_trend_installs": installs,
            "signature_trend_reports": installs, "signature_trend_baseline_days": 56,
            "signature_trend_baseline_installs": 5,
            "signature_trend_expected_installs": 6.2, "signature_trend_ratio": ratio}


def _put_report(channel="nightly", trend=None, signature=SIG, prev=None, broken=False):
    """The `mindate` `put_report` really hands `inspector.get_crash`, obtained the way
    `tests/test_beta_windows.py` does: stand in for `get_crash`, which is the only thing between
    the mindate and the network, and return None ("no json_dump") so `put_report` stops there."""
    seen = {}

    def fake_get_crash(uuid, bid, chan, mindate, chgset, filelog, interesting):
        seen["mindate"] = mindate
        return None

    if broken:
        facts = mock.patch.object(update.sigtrend, "trend_facts",
                                  side_effect=RuntimeError("rollup down"))
    else:
        facts = mock.patch.object(update.sigtrend, "trend_facts", return_value=trend or {})
    with mock.patch.object(update.inspector, "get_crash", side_effect=fake_get_crash), \
         mock.patch.object(update.models.Build, "get_pushdate_before", return_value=prev), \
         facts as trend_facts:
        update.put_report("u-1", BUILD, channel, "Firefox", "buildnode", signature=signature)
    return seen["mindate"], trend_facts


class TestTheConstantIsASumOfLags(unittest.TestCase):
    def test_ten_days_with_the_shipped_config(self):
        self.assertEqual(sigtrend.rise_lookback_days(), sigtrend.WINDOW_DAYS + config.get_ndays())
        self.assertEqual(sigtrend.rise_lookback_days(), 10)

    def test_the_fixture_really_rises(self):
        """If `_rising` drifted out of `is_rising`'s acceptance every test below would pass for
        the wrong reason."""
        self.assertTrue(sigtrend.is_rising(_rising()))
        self.assertFalse(sigtrend.is_rising({}))


class TestNightly(unittest.TestCase):
    DEPLOYED = BUILD - relativedelta(days=config.get_ndays())

    def test_a_quiet_signature_keeps_the_deployed_window(self):
        mindate, _ = _put_report(trend={})
        self.assertEqual(mindate, self.DEPLOYED)

    def test_a_rising_signature_reaches_back_ten_days(self):
        mindate, _ = _put_report(trend=_rising())
        self.assertEqual(mindate, BUILD - relativedelta(days=10))
        self.assertLess(mindate, self.DEPLOYED)

    def test_the_widened_window_contains_the_july_batch_and_the_deployed_one_misses_it(self):
        self.assertGreater(self.DEPLOYED, BATCH)
        mindate, _ = _put_report(trend=_rising())
        self.assertLessEqual(mindate, BATCH)

    def test_the_rate_is_read_for_this_signature_on_this_channel(self):
        _, trend_facts = _put_report(trend=_rising())
        trend_facts.assert_called_once_with("Firefox", "nightly", SIG)

    def test_a_ratio_below_the_line_does_not_widen(self):
        mindate, _ = _put_report(trend=_rising(ratio=sigtrend.MIN_INTERESTING_RATIO - 0.1))
        self.assertEqual(mindate, self.DEPLOYED)

    def test_no_signature_is_the_deployed_behaviour(self):
        """`put_report(uuid, buildid, channel, product, chgset)` -- every caller before the
        signature was threaded through -- must not even look at the rollup."""
        mindate, trend_facts = _put_report(trend=_rising(), signature=None)
        self.assertEqual(mindate, self.DEPLOYED)
        trend_facts.assert_not_called()

    def test_a_broken_rollup_keeps_the_deployed_window(self):
        """The rollup is observability. It must not be able to stop a report being scored."""
        mindate, _ = _put_report(broken=True)
        self.assertEqual(mindate, self.DEPLOYED)


class TestBeta(unittest.TestCase):
    """Beta's deployed bound is the previous build's pushdate + 1 s -- the cycle's uplifts, with
    the merge push one second below the window (`tests/test_beta_windows.py`)."""

    def test_a_quiet_signature_keeps_the_uplift_window(self):
        prev = BUILD - timedelta(days=2)
        mindate, _ = _put_report(channel="beta", trend={}, prev=prev)
        self.assertEqual(mindate, prev + relativedelta(seconds=1))

    def test_a_rising_signature_reaches_across_the_merge(self):
        prev = BUILD - timedelta(days=2)
        mindate, _ = _put_report(channel="beta", trend=_rising(), prev=prev)
        self.assertEqual(mindate, BUILD - relativedelta(days=10))
        self.assertLess(mindate, prev)

    def test_never_narrower_than_the_deployed_bound(self):
        """A gap in the build stream already gave a wider window than ten days; the rise must
        not take it back."""
        prev = BUILD - timedelta(days=20)
        mindate, _ = _put_report(channel="beta", trend=_rising(), prev=prev)
        self.assertEqual(mindate, prev + relativedelta(seconds=1))

    def test_no_previous_build_falls_back_to_the_nightly_rule_and_still_widens(self):
        mindate, _ = _put_report(channel="beta", trend=_rising(), prev=None)
        self.assertEqual(mindate, BUILD - relativedelta(days=10))


class TestTheChainHandsOverTheSignature(unittest.TestCase):
    """`analyze_one_report` calls `put_report(*a)` with whatever `UUID.to_analyze` returns, so
    the tuple's sixth field IS the `signature` parameter -- by position, nothing names it."""

    def test_the_sixth_field_arrives_as_the_signature(self):
        row = ("u-1", BUILD, "nightly", "Firefox", "buildnode", SIG)
        with mock.patch.object(update.models.UUID, "to_analyze", return_value=row), \
             mock.patch.object(update, "put_report") as put_report, \
             mock.patch.object(update, "analyze_reports"):
            update.analyze_one_report()
        put_report.assert_called_once_with("u-1", BUILD, "nightly", "Firefox", "buildnode", SIG)
        self.assertEqual(put_report.call_args.args[5], SIG)

    def test_put_report_accepts_the_tuple_positionally(self):
        """The shape the chain relies on: six positional arguments, the last the signature."""
        seen = {}

        def fake_get_crash(uuid, bid, chan, mindate, chgset, filelog, interesting):
            seen["mindate"] = mindate
            return None

        with mock.patch.object(update.inspector, "get_crash", side_effect=fake_get_crash), \
             mock.patch.object(update.sigtrend, "trend_facts", return_value=_rising()):
            update.put_report("u-1", BUILD, "nightly", "Firefox", "buildnode", SIG)
        self.assertEqual(seen["mindate"], BUILD - relativedelta(days=10))


if __name__ == "__main__":
    unittest.main()
