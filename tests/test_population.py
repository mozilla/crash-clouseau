# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Crash population (crashstack.html): reports vs install_time values, and the two flags that say
# the report count is lying. `summarize` is pure, so every threshold is tested without a network
# call; `for_crash` is tested against a stubbed SuperSearch.
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from crashclouseau import config, population

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)
# 2026-08-01..08-11 in unix seconds, a day apart.
DAY = 86400
BASE = int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp())


def facets(pairs):
    return [{"term": str(t), "count": c} for t, c in pairs]


class TestSummarize(unittest.TestCase):
    def test_healthy_population_raises_no_flag(self):
        # 10 installs a day apart, one crash each: the median signature. Nothing to warn about.
        r = population.summarize(
            facets([(BASE + i * DAY, 1) for i in range(10)]), total=10, now=NOW
        )
        self.assertEqual((r["crashes"], r["installs"]), (10, 10))
        self.assertEqual(r["per_install"], 1.0)
        self.assertAlmostEqual(r["top_share"], 0.1)
        self.assertEqual(r["median_gap_s"], DAY)
        self.assertFalse(r["single_install"] or r["concentrated"] or r["clustered"])

    def test_one_machine_pretending_to_be_a_thousand_crashes(self):
        # The GMPChild::RecvPreloadLibs shape: 1066 reports, ONE install_time.
        r = population.summarize(facets([(BASE, 1066)]), total=1066, now=NOW)
        self.assertEqual(r["installs"], 1)
        self.assertEqual(r["per_install"], 1066)
        self.assertEqual(r["top_share"], 1.0)
        self.assertTrue(r["single_install"])
        self.assertTrue(r["concentrated"])
        # One install means no gaps at all -- not a gap of zero, which would read as "clustered".
        self.assertIsNone(r["median_gap_s"])
        self.assertFalse(r["clustered"])

    def test_concentration_needs_min_crashes(self):
        # 100% of two reports is not evidence of anything: with n this small every population
        # looks concentrated, so the flag stays off.
        r = population.summarize(facets([(BASE, 2)]), total=2, now=NOW)
        self.assertEqual(r["top_share"], 1.0)
        self.assertFalse(r["concentrated"])
        self.assertFalse(r["single_install"])

    def test_clustered_installs_are_flagged(self):
        # The places::History::History shape: 25 distinct installs inside half an hour. Not 25
        # users -- one shared image, or one install second colliding.
        r = population.summarize(
            facets([(BASE + i * 20, 1) for i in range(25)]), total=25, now=NOW
        )
        self.assertEqual(r["installs"], 25)
        self.assertEqual(r["median_gap_s"], 20)
        self.assertTrue(r["clustered"])
        # Spread across MANY machines, so it is emphatically not "concentrated" -- the two flags
        # answer different questions and must not collapse into each other.
        self.assertFalse(r["concentrated"])

    def test_clustering_needs_more_than_one_gap(self):
        # Two installs 62s apart: the "median" of a single gap is that gap, which is not a
        # distribution. Left to the concentration flag instead.
        r = population.summarize(facets([(BASE, 15), (BASE + 62, 7)]), total=22, now=NOW)
        self.assertEqual(r["installs"], 2)
        self.assertFalse(r["clustered"])
        self.assertTrue(r["concentrated"])

    def test_future_install_time_is_dropped_not_trusted(self):
        # A real value from the sample sat in 2124. Kept, it would dominate any span; the point
        # of dropping it is that the reader is told (`dropped`) rather than shown a fiction.
        r = population.summarize(
            facets([(BASE, 3), (BASE + DAY, 2), (4869955304, 1), ("null", 4)]),
            total=10, now=NOW,
        )
        self.assertEqual(r["installs"], 2)
        self.assertEqual(r["dropped"], 2)
        self.assertEqual(r["median_gap_s"], DAY)

    def test_no_readable_install_at_all_is_none(self):
        self.assertIsNone(population.summarize([], total=0, now=NOW))
        self.assertIsNone(population.summarize(facets([("n/a", 5)]), total=5, now=NOW))

    def test_truncation_makes_the_install_count_a_floor(self):
        # Socorro caps the facet list. The sum falling short of `total` is the only signal that
        # happened, and the page must not present a capped population as the whole one.
        r = population.summarize(facets([(BASE + i * DAY, 1) for i in range(5)]),
                                 total=900, now=NOW)
        self.assertTrue(r["truncated"])
        self.assertEqual(r["crashes"], 900)       # what Socorro counted
        self.assertEqual(r["faceted_crashes"], 5)  # what we could actually sum
        r2 = population.summarize(facets([(BASE, 5)]), total=5, now=NOW)
        self.assertFalse(r2["truncated"])

    def test_own_install_is_located_and_ranked(self):
        f = facets([(BASE, 10), (BASE + DAY, 4), (BASE + 2 * DAY, 1)])
        r = population.summarize(f, total=15, own_install_time=BASE + DAY, now=NOW)
        self.assertEqual(r["own"]["crashes"], 4)
        self.assertEqual(r["own"]["rank"], 2)
        self.assertAlmostEqual(r["own"]["share"], 4 / 15)
        # A string install_time (which is how SuperSearch returns it) resolves the same way.
        self.assertEqual(
            population.summarize(f, total=15, own_install_time=str(BASE), now=NOW)["own"]["rank"], 1
        )
        # Unknown / absent / unparseable -> no own block, never a crash.
        for bad in (None, "", "not-a-number", BASE + 999 * DAY):
            self.assertIsNone(
                population.summarize(f, total=15, own_install_time=bad, now=NOW)["own"],
                "own should be None for {!r}".format(bad),
            )

    def test_thresholds_come_from_config(self):
        # The flags are config-driven, so a deployment can retune them without a code change.
        f = facets([(BASE, 3), (BASE + DAY, 3)])
        loose = dict(config.get_population(), concentrated_share=0.4, min_crashes=5)
        self.assertTrue(population.summarize(f, total=6, now=NOW, cfg=loose)["concentrated"])
        tight = dict(config.get_population(), concentrated_share=0.9)
        self.assertFalse(population.summarize(f, total=6, now=NOW, cfg=tight)["concentrated"])


class TestWindow(unittest.TestCase):
    CFG = dict(config.get_population(), min_lookback_days=7, max_lookback_days=30)

    def test_window_anchors_on_a_build_inside_the_range(self):
        build = datetime(2026, 7, 28, 8, 53, tzinfo=timezone.utc)  # 15 days old
        start, at_build = population._window_start(build, NOW, self.CFG)
        self.assertEqual(start, build)
        self.assertTrue(at_build)

    def test_fresh_build_gets_the_minimum_window(self):
        # A build a few hours old would otherwise give a population of one report, which says
        # nothing — and the flags' minimum n would (correctly) suppress every one of them.
        start, at_build = population._window_start(
            datetime(2026, 8, 11, 8, 53, tzinfo=timezone.utc), NOW, self.CFG
        )
        self.assertEqual((NOW - start).days, 7)
        self.assertFalse(at_build)  # the page must not call this "this build's date"

    def test_old_build_is_capped_not_unbounded(self):
        # A crash on a build from last spring must not ask Socorro for a quarter of history.
        start, at_build = population._window_start(
            datetime(2026, 3, 1, tzinfo=timezone.utc), NOW, self.CFG
        )
        self.assertEqual((NOW - start).days, 30)
        self.assertFalse(at_build)

    def test_naive_build_date_is_read_as_utc(self):
        # sqlite hands back naive datetimes; comparing one to an aware `now` raises.
        start, _ = population._window_start(
            datetime(2026, 7, 28, 8, 53), NOW, self.CFG
        )
        self.assertEqual(start.tzinfo, timezone.utc)

    def test_missing_build_date_falls_back_to_the_cap(self):
        cfg = dict(self.CFG, max_lookback_days=14)
        start, at_build = population._window_start(None, NOW, cfg)
        self.assertEqual((NOW - start).days, 14)
        self.assertFalse(at_build)


class TestForCrash(unittest.TestCase):
    """``for_crash`` reads the real clock, so the build date here is relative to it — pinning it
    to a literal date would make these tests pass today and fail next week, as the build aged
    past the minimum window and the expected `since` moved."""

    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.INFO = {
            "uuid": "c55f41e6-9999-4cc9-83d7-dfedb0260811",
            "signature": "stackoverflow | Foo::Bar",
            "buildid": self.now - timedelta(days=1),  # fresh -> minimum window
            "channel": "nightly",
            "product": "Firefox",
        }

    def _expected_since(self, days=None):
        d = days if days is not None else config.get_population()["min_lookback_days"]
        return (self.now - timedelta(days=d)).strftime("%Y-%m-%d")

    def _run(self, responses, build_stats=None):
        """Drive for_crash against a stubbed SuperSearch: `queries=` handlers are invoked in
        order with the canned responses."""
        def fake_search(**kwargs):
            for q, resp in zip(kwargs.get("queries") or [], responses):
                q.handler(resp, q.handlerdata)
            return mock.Mock(wait=lambda: None)

        with mock.patch.object(population.socorro, "SuperSearch",
                               side_effect=lambda **kw: mock.Mock(wait=lambda: fake_search(**kw))), \
             mock.patch.object(population, "_build_stats", return_value=build_stats):
            return population.for_crash(self.INFO)

    def test_happy_path(self):
        r = self._run([
            {"total": 15, "facets": {"install_time": facets(
                [(BASE, 10), (BASE + DAY, 4), (BASE + 2 * DAY, 1)])}},
            {"hits": [{"install_time": BASE}]},
        ], build_stats={"crashes": 3, "installs": 2})
        self.assertEqual(r["crashes"], 15)
        self.assertEqual(r["installs"], 3)
        self.assertTrue(r["concentrated"])
        self.assertEqual(r["own"]["rank"], 1)
        self.assertEqual(r["build"], {"crashes": 3, "installs": 2})
        # The build is one day old, so the window widens to the minimum and the page is told not
        # to label it "this build's date".
        self.assertEqual(r["since"], self._expected_since())
        self.assertFalse(r["since_is_build"])
        self.assertEqual(r["channel"], "nightly")

    def test_missing_own_install_still_renders_the_rest(self):
        # The uuid lookup is the expendable half: a partial answer is worth showing.
        r = self._run([
            {"total": 6, "facets": {"install_time": facets([(BASE, 3), (BASE + DAY, 3)])}},
            {"hits": []},
        ])
        self.assertIsNone(r["own"])
        self.assertEqual(r["installs"], 2)

    def test_no_facets_returns_none(self):
        self.assertIsNone(self._run([{"total": 0, "facets": {}}, {"hits": []}]))

    def test_disabled_by_config_makes_no_request(self):
        with mock.patch.object(config, "get_population",
                               return_value=dict(config.get_population(), enabled=False)), \
             mock.patch.object(population.socorro, "SuperSearch") as ss:
            self.assertIsNone(population.for_crash(self.INFO))
            ss.assert_not_called()

    def test_no_signature_makes_no_request(self):
        with mock.patch.object(population.socorro, "SuperSearch") as ss:
            self.assertIsNone(population.for_crash({"uuid": "x"}))
            self.assertIsNone(population.for_crash(None))
            ss.assert_not_called()

    def test_socorro_failure_is_swallowed(self):
        # A stats block must never take the crash page down.
        with mock.patch.object(population.socorro, "SuperSearch",
                               side_effect=RuntimeError("socorro down")):
            self.assertIsNone(population.for_crash(self.INFO))

    def test_query_scoping(self):
        # The facet query must be scoped to this signature/product/channel and anchored at the
        # build date, or the numbers on the page describe some other population.
        seen = {}

        def fake(**kwargs):
            seen["queries"] = kwargs.get("queries") or []
            for q in seen["queries"]:
                q.handler({"total": 1, "facets": {"install_time": facets([(BASE, 1)])}},
                          q.handlerdata)
            return None

        with mock.patch.object(population.socorro, "SuperSearch",
                               side_effect=lambda **kw: mock.Mock(wait=lambda: fake(**kw))), \
             mock.patch.object(population, "_build_stats", return_value=None):
            population.for_crash(self.INFO)
        p = seen["queries"][0].params
        self.assertEqual(p["signature"], "=" + self.INFO["signature"])
        self.assertEqual(p["release_channel"], "nightly")
        self.assertEqual(p["product"], "Firefox")
        self.assertEqual(p["date"], ">=" + self._expected_since())
        self.assertEqual(p["_results_number"], 0)
        self.assertEqual(seen["queries"][1].params["uuid"], self.INFO["uuid"])


class TestHumanGap(unittest.TestCase):
    def test_units(self):
        from crashclouseau import human_gap

        self.assertEqual(human_gap(None), "—")
        self.assertEqual(human_gap(20), "20s")
        self.assertEqual(human_gap(142), "2.4min")
        self.assertEqual(human_gap(28790), "8.0h")
        self.assertEqual(human_gap(86400), "1.0d")
        self.assertEqual(human_gap(424723), "4.9d")


if __name__ == "__main__":
    unittest.main()
