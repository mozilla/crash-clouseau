# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""The rate statistic, and the four ways it could quietly lie.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        uv run python -m unittest tests.test_sigtrend

The arithmetic tests are backend-agnostic. The round-trip tests need a disposable Postgres
(``pg.insert(...).on_conflict_do_update`` has no sqlite equivalent) and SKIP without one:

    docker compose up -d db
    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/crashclouseau \
      REDIS_URL=redis://localhost:6379/0 uv run python -m unittest tests.test_sigtrend
"""
import os
import unittest
from datetime import date, timedelta
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau import db, models, report_bug, sigtrend          # noqa: E402
from crashclouseau.agent import triage                             # noqa: E402

_SIG = ("AsyncShutdownTimeout | profile-before-change | "
        "CookiePersistentStorage: cookies.sqlite closing")


def _is_postgres():
    try:
        return db.engine.dialect.name == "postgresql"
    except Exception:
        return False


class TestTheStatisticOrdersCorrectly(unittest.TestCase):
    """The one thing this module has to get right: a lone crash on a silent baseline is the
    deployed selector's whole noise floor (88.1% of its selections come from the from-zero branch,
    67% on a single crash), so a statistic meant to RANK that noise must not rate it highly."""

    def test_a_lone_crash_on_a_silent_baseline_is_unremarkable(self):
        # The from-zero trap, reached from the other side. With a flat prior this would be 0.0 --
        # infinitely surprising -- and the statistic would reproduce exactly the noise it exists
        # to order below a real rise.
        lone = sigtrend.tail_score(1, 0, 56 * 500, 7 * 500)
        self.assertGreater(lone, 0.01)
        real = sigtrend.tail_score(9, 5, 56 * 500, 7 * 500)
        self.assertLess(real, lone / 100)

    def test_the_anchor_beats_every_quieter_shape(self):
        # Bug 2063336's real numbers against the shapes it has to outrank.
        anchor = sigtrend.tail_score(9, 5, 56 * 500, 7 * 500)
        for k, base in ((1, 0), (2, 1), (3, 3), (2, 8), (4, 20)):
            self.assertLess(anchor, sigtrend.tail_score(k, base, 56 * 500, 7 * 500),
                            "anchor must outrank k={} base={}".format(k, base))

    def test_it_is_monotone_in_the_observed_count(self):
        prev = 1.0
        for k in range(1, 12):
            got = sigtrend.tail_score(k, 4, 56 * 500, 7 * 500)
            self.assertLessEqual(got, prev)
            prev = got

    def test_exposure_is_not_decoration(self):
        """THE MEASURED REASON THIS EXISTS. Nightly's distinct installs/day fell from a median 860
        (2026-06) to 462 (2026-08). The same observed count against the same baseline COUNT is a
        very different claim once the population halves, and a statistic that ignores exposure
        cannot tell the two apart."""
        same_pop = sigtrend.tail_score(6, 6, 56 * 800, 7 * 800)
        halved = sigtrend.tail_score(6, 6, 56 * 800, 7 * 400)
        self.assertLess(halved, same_pop)
        self.assertLess(halved, same_pop / 5)

    def test_no_exposure_is_no_answer(self):
        self.assertIsNone(sigtrend.tail_score(5, 1, 0, 3500))
        self.assertIsNone(sigtrend.tail_score(5, 1, 28000, 0))


class TestWordingIsGatedOnTheRatio(unittest.TestCase):
    def _facts(self, ins, expected, ratio):
        return {"signature_trend_installs": ins, "signature_trend_reports": ins,
                "signature_trend_expected_installs": expected,
                "signature_trend_ratio": ratio,
                "signature_trend_window_days": 7, "signature_trend_baseline_days": 56}

    def test_a_flat_signature_says_nothing_anywhere(self):
        flat = self._facts(9, 8.7, 1.03)
        self.assertFalse(sigtrend.is_rising(flat))
        self.assertIsNone(report_bug.build_trend_note(flat))
        self.assertEqual(triage._signature_trend_lines({"signature_trend": flat}), [])

    def test_a_rise_reaches_both_surfaces_with_the_same_numbers(self):
        rise = self._facts(9, 0.62, 14.52)
        note = report_bug.build_trend_note(rise)
        lines = triage._signature_trend_lines({"signature_trend": rise})
        self.assertIsNotNone(note)
        self.assertTrue(lines)
        # One arithmetic, two prose surfaces -- the numbers may not drift apart.
        for token in ("9 distinct installations", "0.62 expected", "14.52x"):
            self.assertIn(token, note)
            self.assertIn(token, "\n".join(lines))

    def test_a_big_ratio_on_one_install_is_still_nothing(self):
        # Two installs against a near-silent baseline is an enormous ratio and no story.
        self.assertFalse(sigtrend.is_rising(self._facts(2, 0.05, 40.0)))

    def test_the_facts_bar_and_the_wording_bar_are_separate_on_purpose(self):
        """The ordering statistic must stay available BELOW the bar for saying anything out loud.

        On bug 2063336 the facts exist from 2026-07-26 at 3 installs / 43x -- 18 days before :aryx
        filed -- while the sentence waits until 2026-08-10 at 6 installs / 17x. Collapsing the two
        into one threshold loses one of the two things: either the note speaks about 9.2% of all
        signatures (measured, and its weakest member is 2.02x on 3 installs), or the ranking goes
        blind for a fortnight on the case this module was built for."""
        self.assertLess(sigtrend.MIN_INSTALLS, sigtrend.WORDING_MIN_INSTALLS)
        below = self._facts(3, 0.07, 43.18)            # the anchor on 2026-07-26
        self.assertFalse(sigtrend.is_rising(below))     # no sentence
        # ...but the numbers, and therefore any ranking over them, are all there.
        self.assertIsNotNone(sigtrend.describe(below))
        self.assertIsNotNone(sigtrend.tail_score(3, 0, 56 * 500, 7 * 500))
        above = self._facts(6, 0.35, 17.11)            # the anchor on 2026-08-10
        self.assertTrue(sigtrend.is_rising(above))

    def test_no_surface_ever_prints_the_score(self):
        """The tail is anti-conservative by three to five orders of magnitude against a shuffled
        null. It is an ordering statistic; a bug comment is the last place to launder it into a
        confidence claim."""
        rise = self._facts(9, 0.62, 14.52)
        rise["signature_trend_score"] = 1.9e-06
        note = report_bug.build_trend_note(rise)
        lines = "\n".join(triage._signature_trend_lines({"signature_trend": rise}))
        for banned in ("1.9e-06", "p =", "p-value", "significant", "probability"):
            self.assertNotIn(banned, note)
            self.assertNotIn(banned, lines)

    def test_missing_pieces_produce_no_sentence(self):
        self.assertIsNone(sigtrend.describe({}))
        self.assertIsNone(sigtrend.describe({"signature_trend_installs": 9}))
        self.assertFalse(sigtrend.is_rising({}))
        self.assertFalse(sigtrend.is_rising(None))


class TestTheCollectorRefusesRatherThanTruncates(unittest.TestCase):
    def test_an_over_full_day_is_refused_not_stored(self):
        """Socorro orders terms facets by COUNT, so a day above the facet page silently drops its
        QUIETEST signatures -- precisely the ones this module is for. Release runs ~3,200 distinct
        signatures a day. A truncated day stored as fact would make every quiet signature look
        like it stopped crashing, i.e. it would manufacture rises."""
        rows = {"sig-%d" % i: (1, 1) for i in range(sigtrend.MAX_SIGNATURES + 5)}

        def fake(params=None, handler=None, handlerdata=None, **kw):
            handler({"errors": [], "total": 9999,
                     "facets": {"cardinality_install_time": {"value": 5000},
                                "signature": [
                                    {"term": s, "count": c,
                                     "facets": {"cardinality_install_time": {"value": i}}}
                                    for s, (c, i) in rows.items()]}},
                    handlerdata)
            return mock.Mock(wait=lambda: None)

        with mock.patch("crashclouseau.sigtrend.socorro.SuperSearch", side_effect=fake), \
                mock.patch.object(models.ChannelDaily, "upsert") as up, \
                mock.patch.object(models.SignatureDaily, "record_day") as rec:
            written = sigtrend.collect_day("Firefox", "nightly", date(2026, 8, 26))
        self.assertEqual(written, 0)
        up.assert_not_called()
        rec.assert_not_called()

    def test_release_is_skipped_without_a_single_query(self):
        with mock.patch("crashclouseau.sigtrend.socorro.SuperSearch") as ss:
            self.assertEqual(sigtrend.backfill("Firefox", "release"), 0)
        ss.assert_not_called()

    def test_beta_is_queried_with_aurora(self):
        """A beta build and its DevEdition twin share a buildid; dropping the twin loses ~36% of
        the channel's reports, i.e. a third of the denominator."""
        seen = {}

        def fake(params=None, handler=None, handlerdata=None, **kw):
            seen["channel"] = params["release_channel"]
            handler({"errors": [], "total": 0,
                     "facets": {"cardinality_install_time": {"value": 0}, "signature": []}},
                    handlerdata)
            return mock.Mock(wait=lambda: None)

        with mock.patch("crashclouseau.sigtrend.socorro.SuperSearch", side_effect=fake), \
                mock.patch.object(models.ChannelDaily, "upsert", return_value=True), \
                mock.patch.object(models.SignatureDaily, "record_day", return_value=0):
            sigtrend.collect_day("Firefox", "beta", date(2026, 8, 26))
        self.assertEqual(seen["channel"], ["beta", "aurora"])

    def test_the_query_asks_for_processed_date(self):
        """`date` is `processed_crash.date_processed`, which is what makes the series causal: a
        day's row is what was VISIBLE that day. A switch to a client-side crash date would
        reintroduce the late arrivals the whole design avoids."""
        seen = {}

        def fake(params=None, handler=None, handlerdata=None, **kw):
            seen.update(params)
            handler({"errors": [], "total": 0,
                     "facets": {"cardinality_install_time": {"value": 0}, "signature": []}},
                    handlerdata)
            return mock.Mock(wait=lambda: None)

        with mock.patch("crashclouseau.sigtrend.socorro.SuperSearch", side_effect=fake), \
                mock.patch.object(models.ChannelDaily, "upsert", return_value=True), \
                mock.patch.object(models.SignatureDaily, "record_day", return_value=0):
            sigtrend.collect_day("Firefox", "nightly", date(2026, 8, 26))
        self.assertEqual(seen["date"], [">=2026-08-26", "<2026-08-27"])
        self.assertEqual(seen["_aggs.signature"], ["_cardinality.install_time"])


@unittest.skipUnless(_is_postgres(), "the rollup round-trip needs a disposable Postgres backend")
class TestTrendFactsAgainstARealRollup(unittest.TestCase):
    """The four ways `trend_facts` could lie, each with a row set that would make it."""

    ASOF = date(2026, 8, 12)

    def setUp(self):
        db.create_all()
        self._clear()

    def tearDown(self):
        self._clear()

    def _clear(self):
        db.session.query(models.SignatureDaily).delete()
        db.session.query(models.ChannelDaily).delete()
        db.session.commit()

    def _exposure(self, days, installs=500, start=None):
        start = start or (self.ASOF - timedelta(days=days - 1))
        for i in range(days):
            models.ChannelDaily.upsert("Firefox", "nightly", start + timedelta(days=i),
                                       installs * 2, installs, commit=False)
        db.session.commit()

    def _sig(self, day, installs, reports=None):
        models.SignatureDaily.record_day(
            "Firefox", "nightly", day, {_SIG: (reports or installs, installs)})

    def test_the_anchor_reproduces_aryx_sentence(self):
        # 63 days of exposure; 5 installs scattered through the baseline, 9 in the last week --
        # bug 2063336's real shape.
        self._exposure(63)
        for offset in (48, 40, 33, 20, 11):
            self._sig(self.ASOF - timedelta(days=offset), 1)
        for offset in range(0, 7):
            self._sig(self.ASOF - timedelta(days=offset), 2 if offset == 0 else 1)
        facts = sigtrend.trend_facts("Firefox", "nightly", _SIG, asof=self.ASOF)
        self.assertEqual(facts["signature_trend_installs"], 8)
        self.assertEqual(facts["signature_trend_baseline_days"], 56)
        self.assertEqual(facts["signature_trend_baseline_installs"], 5)
        self.assertGreater(facts["signature_trend_ratio"], 10)
        self.assertTrue(sigtrend.is_rising(facts))
        self.assertIn("distinct installations", sigtrend.describe(facts))

    def test_a_thin_baseline_answers_NOTHING(self):
        """A fresh deploy, or a rollup that has been down. A confident ratio against three days of
        history is the one output that must never happen -- so the keys are ABSENT, never zeroed,
        exactly as `sigage` omits an age it could not establish."""
        self._exposure(10)
        for offset in range(0, 7):
            self._sig(self.ASOF - timedelta(days=offset), 3)
        self.assertEqual(sigtrend.trend_facts("Firefox", "nightly", _SIG, asof=self.ASOF), {})

    def test_below_the_install_floor_answers_nothing(self):
        self._exposure(63)
        self._sig(self.ASOF, 2)
        self.assertEqual(sigtrend.trend_facts("Firefox", "nightly", _SIG, asof=self.ASOF), {})

    def test_a_gap_in_the_WINDOW_refuses_rather_than_overstating_coverage(self):
        """The rate would still be right, but the sentence would claim seven days it never saw."""
        self._exposure(63)
        # Only 2 of the last 7 days collected.
        db.session.query(models.ChannelDaily).filter(
            models.ChannelDaily.day >= self.ASOF - timedelta(days=4),
            models.ChannelDaily.day <= self.ASOF).delete()
        db.session.commit()
        for offset in range(0, 7):
            self._sig(self.ASOF - timedelta(days=offset), 4)
        self.assertEqual(sigtrend.trend_facts("Firefox", "nightly", _SIG, asof=self.ASOF), {})

    def test_a_partial_window_says_how_many_days_it_actually_saw(self):
        self._exposure(63)
        db.session.query(models.ChannelDaily).filter(
            models.ChannelDaily.day == self.ASOF - timedelta(days=3)).delete()
        db.session.commit()
        for offset in (50, 42, 30, 21, 12):
            self._sig(self.ASOF - timedelta(days=offset), 1)
        for offset in range(0, 7):
            self._sig(self.ASOF - timedelta(days=offset), 2)
        facts = sigtrend.trend_facts("Firefox", "nightly", _SIG, asof=self.ASOF)
        self.assertEqual(facts["signature_trend_window_days"], 6)
        self.assertIn("last 6 days", sigtrend.describe(facts))

    def test_a_signature_with_NO_baseline_gets_no_ratio_and_no_sentence(self):
        """A signature that did not exist before the window is a NOVELTY, not a rate change, and
        it already has an instrument (`sigage`). Emitting a ratio here would mean dividing by zero
        and calling the answer infinite — which is the from-zero branch of `utils.is_spike`, i.e.
        88% of the noise this module exists to rank below a real rise."""
        self._exposure(63)
        for offset in range(0, 7):
            self._sig(self.ASOF - timedelta(days=offset), 3)
        facts = sigtrend.trend_facts("Firefox", "nightly", _SIG, asof=self.ASOF)
        self.assertEqual(facts["signature_trend_baseline_installs"], 0)
        self.assertNotIn("signature_trend_ratio", facts)
        self.assertFalse(sigtrend.is_rising(facts))
        self.assertIsNone(sigtrend.describe(facts))

    def test_a_collection_GAP_is_not_a_quiet_period(self):
        """The subtle one. If uncollected days counted as zeros, a week of downtime would look
        like a week of silence and every signature would 'rise' when collection resumed. Days
        with no ChannelDaily row must leave BOTH sides of the ratio."""
        # Baseline present but with a 20-day hole; the signature crashed steadily throughout.
        self._exposure(63)
        hole_start = self.ASOF - timedelta(days=40)
        db.session.query(models.ChannelDaily).filter(
            models.ChannelDaily.day >= hole_start,
            models.ChannelDaily.day <= hole_start + timedelta(days=19)).delete()
        db.session.commit()
        for offset in range(0, 63):
            self._sig(self.ASOF - timedelta(days=offset), 1)
        facts = sigtrend.trend_facts("Firefox", "nightly", _SIG, asof=self.ASOF)
        # 36 baseline days survive the hole, above MIN_BASELINE_COVERAGE, and the RATE is the
        # rate over the days actually seen -- so a steady signature still reads as steady.
        self.assertEqual(facts["signature_trend_baseline_days"], 36)
        self.assertLess(facts["signature_trend_ratio"], 1.5)
        self.assertFalse(sigtrend.is_rising(facts))

    def test_a_falling_exposure_does_not_manufacture_a_rise(self):
        """Nightly lost ~45% of its installs between June and August 2026. A signature whose
        per-install rate is CONSTANT across that ramp must not read as rising -- and on raw counts
        it would read as falling, which is the same bug with the other sign."""
        start = self.ASOF - timedelta(days=62)
        for i in range(63):
            day = start + timedelta(days=i)
            installs = 900 if i < 40 else 450
            models.ChannelDaily.upsert("Firefox", "nightly", day, installs * 2, installs,
                                       commit=False)
            # constant RATE: 1 install per 90 channel installs
            models.SignatureDaily.record_day("Firefox", "nightly", day,
                                             {_SIG: (installs // 90, installs // 90)})
        db.session.commit()
        facts = sigtrend.trend_facts("Firefox", "nightly", _SIG, asof=self.ASOF)
        self.assertAlmostEqual(facts["signature_trend_ratio"], 1.0, delta=0.15)
        self.assertFalse(sigtrend.is_rising(facts))

    def test_backfill_only_fetches_the_gaps_and_always_refetches_today(self):
        self._exposure(63)
        asked = []

        def fake(product, channel, day):
            asked.append(day)
            return 0

        with mock.patch.object(sigtrend, "collect_day", side_effect=fake):
            sigtrend.backfill("Firefox", "nightly", asof=self.ASOF, days=62)
        # Every day is already known except today, which is partial by construction and refetched.
        self.assertEqual(asked, [self.ASOF])


if __name__ == "__main__":
    unittest.main()
