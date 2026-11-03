# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""Two-channel onset selection, caching, and pick history."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import json  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import config, datacollector as dc, models, onset, spikes, utils  # noqa: E402

_RULE = config.get_onset_selection()
_NOW = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
_SIG = "mozilla::dom::Selection::Repaint"


def _row(reports, installs, busiest_day):
    return {"reports": reports, "installs": installs, "busiest_day": busiest_day}


class TestTheTwoChannelRule(unittest.TestCase):
    CHANNELS = ("release", "esr", "beta")

    def test_the_loudest_first_channel_and_another_second(self):
        onsets = {"release": _row(20, 18, 1), "esr": _row(7, 7, 0), "beta": _row(4, 4, 0)}
        self.assertEqual(onset.rising_on_two(onsets, _RULE, self.CHANNELS), ("release", "esr"))

    def test_one_channel_is_not_enough(self):
        onsets = {"release": _row(20, 18, 1), "esr": _row(2, 2, 0), "beta": _row(9, 9, 4)}
        self.assertIsNone(onset.rising_on_two(onsets, _RULE, self.CHANNELS))

    def test_the_second_bar_is_lower_but_has_its_own_ratio(self):
        onsets = {"release": _row(29, 27, 3), "esr": _row(5, 4, 0)}
        self.assertEqual(onset.rising_on_two(onsets, _RULE, self.CHANNELS), ("release", "esr"))
        onsets["esr"] = _row(5, 4, 2)
        self.assertIsNone(onset.rising_on_two(onsets, _RULE, self.CHANNELS))

    def test_a_second_channel_may_not_be_a_listed_one(self):
        onsets = {"release": _row(20, 18, 1), "nightly": _row(9, 9, 0)}
        self.assertIsNone(onset.rising_on_two(onsets, _RULE, self.CHANNELS))
        self.assertIsNone(onset.rising_on_two(None, _RULE, self.CHANNELS))


class TestTheScan(unittest.TestCase):
    def _scan(self, recent, totals, onsets, product="Firefox"):
        def counts(product, channel, start, end, installs):
            return (recent if installs else totals).get(channel, {})

        fetch = mock.MagicMock(side_effect=lambda info, hours, days, asof: onsets.get(
            info["signature"]))
        with mock.patch.object(onset, "_signature_counts", side_effect=counts) as calls, \
                mock.patch.object(onset.report_bug, "fetch_channel_onsets", fetch):
            found = onset._find(product, _RULE, _RULE["channels"][product], _NOW)
        return found, fetch, calls

    def test_only_two_channel_candidates_are_looked_up(self):
        steady = "steady::Topcrash"
        alone = "release::Only"
        recent = {"release": {_SIG: (20, 18), steady: (60, 50), alone: (30, 30)},
                  "esr": {_SIG: (7, 7), steady: (20, 20)}}
        totals = {"release": {_SIG: (1, 1), steady: (1680, 900), alone: (0, 0)},
                  "esr": {steady: (560, 300)}}
        onsets = {_SIG: {"release": _row(20, 18, 1), "esr": _row(7, 7, 0)}}
        found, fetch, _ = self._scan(recent, totals, onsets)
        self.assertEqual(found, [{"signature": _SIG, "first": "release", "second": "esr",
                                  "onsets": onsets[_SIG]}])
        fetch.assert_called_once_with({"signature": _SIG, "product": "Firefox"}, 24, 28,
                                      asof=_NOW)

    def test_the_prefilter_averages_over_the_histograms_buckets(self):
        self.assertEqual(onset._day_buckets(datetime(2026, 10, 6, 15), datetime(2026, 11, 3, 15)),
                         29)
        self.assertEqual(onset._day_buckets(datetime(2026, 10, 6), datetime(2026, 11, 3)), 28)
        # At 15:00, 28 elapsed days span 29 UTC buckets; 5 reports still pass 5x.
        recent = {"release": {_SIG: (5, 5)}, "esr": {_SIG: (3, 3)}}
        totals = {"release": {_SIG: (29, 29)}}
        onsets = {_SIG: {"release": _row(5, 5, 1), "esr": _row(3, 3, 0)}}
        found, fetch, _ = self._scan(recent, totals, onsets)
        self.assertEqual([f["signature"] for f in found], [_SIG])
        fetch.assert_called_once()

    def test_the_daily_series_decides(self):
        recent = {"release": {_SIG: (20, 18)}, "esr": {_SIG: (7, 7)}}
        onsets = {_SIG: {"release": _row(20, 18, 7), "esr": _row(7, 7, 0)}}
        found, fetch, _ = self._scan(recent, {}, onsets)
        self.assertEqual(found, [])
        fetch.assert_called_once()

    def test_test_crashes_and_empty_stacks_are_not_looked_up(self):
        empty = "EMPTY: no frame data available"
        recent = {"release": {empty: (20, 18)}, "esr": {empty: (7, 7)}}
        found, fetch, _ = self._scan(recent, {}, {})
        self.assertEqual(found, [])
        fetch.assert_not_called()


class TestSharedCache(unittest.TestCase):
    KEY = "clouseau:onset:Firefox:2026110315"

    def setUp(self):
        self.redis = mock.Mock()
        self.redis.get.return_value = None
        self.lock = self.redis.lock.return_value
        self.lock.acquire.return_value = True
        self.enterContext(mock.patch.object(onset, "_redis", return_value=self.redis))
        self.scan = self.enterContext(mock.patch.object(onset, "_find", return_value=[]))

    def _find(self):
        return onset.find("Firefox", asof=_NOW)

    def test_results_including_empty_ones_are_shared(self):
        for result in ([], [{"signature": _SIG}]):
            with self.subTest(result=result):
                self.redis.reset_mock()
                self.redis.get.return_value = None
                self.scan.return_value = result
                self.assertEqual(self._find(), result)
                self.redis.set.assert_called_with(self.KEY, json.dumps(result), ex=3 * 3600)
                self.lock.release.assert_called_once()

                self.scan.reset_mock()
                self.redis.reset_mock()
                self.redis.get.return_value = json.dumps(result).encode()
                self.assertEqual(self._find(), result)
                self.scan.assert_not_called()
                self.lock.acquire.assert_not_called()

    def test_failed_scans_are_not_cached_and_release_the_lock(self):
        self.scan.side_effect = LookupError("down")
        self.assertEqual(self._find(), [])
        self.redis.set.assert_not_called()
        self.lock.release.assert_called_once()

    def test_a_scan_running_elsewhere_skips_this_tick(self):
        self.lock.acquire.return_value = False
        self.assertEqual(self._find(), [])
        self.scan.assert_not_called()
        self.lock.release.assert_not_called()

    def test_without_redis_every_call_scans(self):
        import redis

        self.redis.get.side_effect = redis.exceptions.ConnectionError("down")
        for _ in range(2):
            self._find()
        self.assertEqual(self.scan.call_count, 2)

    def test_a_failed_cache_write_still_releases_the_lock(self):
        import redis

        self.redis.set.side_effect = redis.exceptions.ConnectionError("down")
        self.assertEqual(self._find(), [])
        self.lock.release.assert_called_once()

    def test_disabled_or_single_channel_products_do_not_scan(self):
        for rule, product in ((None, "Firefox"),
                              (dict(_RULE, channels={"Fenix": ["release"]}), "Fenix"),
                              (_RULE, "Thunderbird")):
            with self.subTest(rule=rule, product=product), mock.patch.object(
                    onset.config, "get_onset_selection", return_value=rule):
                self.assertEqual(onset.find(product, asof=_NOW), [])
        self.scan.assert_not_called()
        self.redis.get.assert_not_called()


class TestTheFacetQuery(unittest.TestCase):
    def test_parameters_and_parsing(self):
        seen = {}

        class FakeSearch:
            def __init__(self, params, handler, handlerdata):
                seen.update(params)
                handler({"facets": {"signature": [
                    {"term": "A", "count": 9, "facets": {"cardinality_install_time": {"value": 7}}},
                    {"term": "B", "count": 3}]}}, handlerdata)

            def wait(self):
                return None

        start = datetime(2026, 11, 2, 15, 0)
        with mock.patch.object(onset.socorro, "SuperSearch", FakeSearch):
            got = onset._signature_counts("Firefox", "beta", start, _NOW, True)
        self.assertEqual(got, {"A": (9, 7), "B": (3, 0)})
        self.assertEqual(seen["release_channel"], ["beta", "aurora"])
        self.assertEqual(seen["date"], [">=2026-11-02T15:00:00", "<2026-11-03T15:00:00"])
        self.assertEqual(seen["_aggs.signature"], "_cardinality.install_time")

    def test_an_answer_with_errors_is_unread(self):
        class FakeSearch:
            def __init__(self, params, handler, handlerdata):
                handler({"errors": ["shard failed"], "facets": {"signature": []}}, handlerdata)

            def wait(self):
                return None

        with mock.patch.object(onset.socorro, "SuperSearch", FakeSearch), \
                self.assertRaises(LookupError):
            onset._signature_counts("Firefox", "release", datetime(2026, 11, 2, 15), _NOW, True)


def _numbers():
    day = datetime(2026, 10, 22)
    older = datetime(2026, 10, 5)
    return {older: {"count": 1, "bids": {"20261005135250": 1}, "installs": {"20261005135250": 1}},
            day: {"count": 14, "bids": {"20261022150525": 14},
                  "installs": {"20261022150525": 12}}}


class TestTheSelectorPath(unittest.TestCase):
    RISE = {"signature": _SIG, "first": "release", "second": "esr",
            "onsets": {"release": _row(20, 18, 1), "esr": _row(7, 7, 0)}}

    def setUp(self):
        for p in (
            mock.patch.object(dc.onset, "find", return_value=[dict(self.RISE)]),
            mock.patch.object(models.OnsetPick, "count_since", return_value=0),
            mock.patch.object(models.OnsetPick, "picked_since", return_value=set()),
            mock.patch.object(models.OnsetPick, "record", return_value=True),
            mock.patch.object(models.Selection, "covered_recently", return_value=set()),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _pick(self, channel="release", declined=None, already=()):
        selection = []
        picks = dc._onset_picks("Firefox", channel, _NOW,
                                {_SIG: _numbers()} if declined is None else declined,
                                set(already), selection)
        return picks, selection

    def test_the_newest_build_with_a_report_is_picked_and_logged(self):
        picks, selection = self._pick()
        bid = "20261022150525"
        self.assertEqual(picks, {_SIG: {"bids": {bid: 14}, "protos": {bid: []},
                                        "installs": {bid: 0}}})
        (rec,) = selection
        self.assertIn(rec["outcome"], models.SELECTION_OUTCOMES)
        self.assertTrue(models.Selection._row(rec, "Firefox", "release", _NOW)["ever_selected"])
        self.assertEqual((rec["outcome"], rec["picked"], rec["count"], rec["index"]),
                         (utils.CROSS_CHANNEL_ONSET, bid, 14, 1))
        dc.onset.find.assert_called_once_with("Firefox", asof=_NOW)
        models.OnsetPick.record.assert_called_once_with(
            {"signature": _SIG, "product": "Firefox", "channel": "release", "buildid": bid,
             "onsets": self.RISE["onsets"]})

    def test_a_pick_this_week_covers_an_old_build(self):
        models.OnsetPick.picked_since.return_value = {_SIG}
        self.assertEqual(self._pick(), ({}, []))
        product, sgns, since = models.OnsetPick.picked_since.call_args.args
        self.assertEqual((product, sgns), ("Firefox", [_SIG]))
        self.assertAlmostEqual((datetime.now(timezone.utc) - since).total_seconds(),
                               7 * 86400, delta=60)
        models.OnsetPick.record.assert_not_called()

    def test_an_unrecorded_pick_is_not_taken(self):
        models.OnsetPick.record.return_value = False
        self.assertEqual(self._pick(), ({}, []))

    def test_only_the_first_channel_picks(self):
        for channel in ("esr153", "beta", "nightly"):
            with self.subTest(channel=channel):
                self.assertEqual(self._pick(channel=channel), ({}, []))
        dc.onset.find.return_value = [dict(self.RISE, first="esr")]
        self.assertIn(_SIG, self._pick(channel="esr153")[0])

    def test_what_another_path_kept_or_did_not_decline_is_left(self):
        self.assertEqual(self._pick(already={_SIG}), ({}, []))
        self.assertEqual(self._pick(declined={"Other": _numbers()}), ({}, []))
        self.assertEqual(self._pick(declined={}), ({}, []))

    def test_a_signature_selected_on_any_channel_this_week_is_left(self):
        models.Selection.covered_recently.side_effect = (
            lambda product, channel, sigs: {_SIG} if channel == "nightly" else set())
        self.assertEqual(self._pick(), ({}, []))
        channels = {c.args[1] for c in models.Selection.covered_recently.call_args_list}
        self.assertEqual(channels, set(config.get_channels()))

    def test_the_daily_budget(self):
        models.OnsetPick.count_since.return_value = _RULE["per_day"]
        self.assertEqual(self._pick(), ({}, []))
        product, channel, since = models.OnsetPick.count_since.call_args.args
        self.assertEqual((product, channel), ("Firefox", "release"))
        self.assertEqual((since.hour, since.minute, since.date()),
                         (0, 0, datetime.now(timezone.utc).date()))
        models.OnsetPick.count_since.return_value = _RULE["per_day"] - 1
        self.assertIn(_SIG, self._pick()[0])

    def test_a_failed_lookup_picks_nothing(self):
        models.Selection.covered_recently.side_effect = RuntimeError("db down")
        self.assertEqual(self._pick(), ({}, []))


class TestTheOutcome(unittest.TestCase):
    def test_it_is_not_judged_a_build_day_spike(self):
        row = {"build_day": "2026-10-22", "outcome": utils.CROSS_CHANNEL_ONSET}
        with mock.patch.object(models.Selection, "for_signature", return_value=[row]), \
                mock.patch.object(spikes, "judge_selection") as judge:
            self.assertIsNone(spikes.crash_spike(_SIG, "Firefox", "release",
                                                 "20261022150525", {}))
        judge.assert_not_called()


_LONG = "mozilla::dom::" + "Frame::" * 73 + "Run"


class TestLongSignatures(unittest.TestCase):
    def test_stored_keys_map_back_to_the_signatures_given(self):
        self.assertGreater(len(_LONG), 512)
        got = models._matching_signatures([_LONG, _SIG, "Other"], {_LONG[:512], _SIG})
        self.assertEqual(got, {_LONG, _SIG})


class TestTheConfig(unittest.TestCase):
    def test_the_default_a_partial_value_and_the_off_switch(self):
        default = config.ONSET_SELECTION_DEFAULT
        with mock.patch.object(config, "_get_global", return_value={}):
            self.assertEqual(config.get_onset_selection(), default)
        with mock.patch.object(config, "_get_global",
                               return_value={"onset_selection": {"per_day": 1}}):
            self.assertEqual(config.get_onset_selection(), dict(default, per_day=1))
        for off in (None, {}, False):
            with mock.patch.object(config, "_get_global",
                                   return_value={"onset_selection": off}):
                self.assertIsNone(config.get_onset_selection())
        self.assertEqual(_RULE, default)


def _is_postgres():
    try:
        return models.db.engine.dialect.name == "postgresql"
    except Exception:
        return False


@unittest.skipUnless(_is_postgres(), "the JSONB pick table needs a disposable Postgres")
class TestThePickTable(unittest.TestCase):
    def setUp(self):
        models.create()
        models.db.session.query(models.OnsetPick).delete()
        models.db.session.commit()

    def _record(self, **over):
        row = dict({"signature": _SIG, "product": "Firefox", "channel": "release",
                    "buildid": "20261022150525", "onsets": {"release": _row(20, 18, 1)}}, **over)
        self.assertTrue(models.OnsetPick.record(row))

    def test_picks_are_read_by_pick_time_and_product(self):
        self._record()
        self._record(signature="Other", channel="esr153")
        self._record(signature="Fenix::Only", product="Fenix")
        since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        self.assertEqual(models.OnsetPick.picked_since("Firefox", [_SIG, "Fenix::Only", "New"],
                                                       since), {_SIG})
        self.assertEqual(models.OnsetPick.count_since("Firefox", "release", since), 1)
        self.assertEqual(models.OnsetPick.count_since("Firefox", "esr153", since), 1)
        later = datetime.now(timezone.utc).replace(year=2100)
        self.assertEqual(models.OnsetPick.picked_since("Firefox", [_SIG], later), set())
        self.assertEqual(models.OnsetPick.picked_since("Firefox", [], since), set())

    def test_a_signature_longer_than_the_column_is_still_found(self):
        self._record(signature=_LONG)
        since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        self.assertEqual(models.OnsetPick.picked_since("Firefox", [_LONG, _SIG], since),
                         {_LONG})

    def test_a_long_signature_picked_before_is_not_picked_again(self):
        self._record(signature=_LONG)
        rise = {"signature": _LONG, "first": "release", "second": "esr",
                "onsets": {"release": _row(20, 18, 1), "esr": _row(7, 7, 0)}}
        with mock.patch.object(dc.onset, "find", return_value=[rise]):
            picks = dc._onset_picks("Firefox", "release", _NOW, {_LONG: _numbers()}, set(), [])
        self.assertEqual(picks, {})
        self.assertEqual(models.db.session.query(models.OnsetPick).count(), 1)

    def test_selection_coverage_maps_long_signatures_back(self):
        def clear():
            models.db.session.query(models.Selection).filter(
                models.Selection.signature == _LONG[:512]).delete()
            models.db.session.commit()

        clear()
        self.addCleanup(clear)
        rec = {"signature": _LONG, "day": datetime.now(), "count": 3, "index": 1,
               "baseline": [], "evaluable": True, "bids": {"20261022150525": 3},
               "installs": {"20261022150525": 3}, "picked": "20261022150525",
               "outcome": utils.SELECTED}
        models.Selection.record_many([rec], "Firefox", "release")
        self.assertEqual(models.Selection.covered_recently("Firefox", "release", [_LONG, _SIG]),
                         {_LONG})

    def test_prune_drops_old_picks(self):
        self._record()
        models.db.session.query(models.OnsetPick).update(
            {"picked_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})
        models.db.session.commit()
        self._record(signature="Recent")
        models.OnsetPick.prune()
        self.assertEqual([r.signature for r in models.db.session.query(models.OnsetPick)],
                         ["Recent"])
