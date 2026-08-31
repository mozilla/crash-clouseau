# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""A channel nobody turned on must cost nothing.

    DATABASE_URL=postgresql://clouseau:passwd@localhost:55432/clouseau_test \
        REDIS_URL=redis://localhost:6379/0 \
        uv run python -m unittest tests.test_release_containment

This is not a hypothetical. `INGEST_CHANNELS` was first set at Heroku release v9, and before
that `update_all`'s empty default meant "every configured channel", so one tick ingested
release. Production still carries the residue, measured read-only on 2026-08-31:

  * `nodes`      7,267 rows for channel='release' (31.9% of the table), on the perfectly
                 contiguous id block 12936..20202 — i.e. exactly one `Changeset.add` batch, once
  * `changesets` 20,320 rows (61.3% of the table), of which 19,527 are `analyzed=true`
  * `lastdate`   a release row frozen at mindate 2026-06-07, maxdate 2026-07-06
  * `builds`     ZERO release rows, so nothing could ever read any of it

Ingestion is free, but the residue was NOT: `Changeset.to_analyze()`'s no-channel form had no
channel filter, so the serial patch chain fetched and parsed 2,628 `releases/mozilla-release`
raw-revs off hg — at the 3.45-6.51 s a fetch this repo measures, ~19-35 hours of the shared
queue — producing scores `Changeset.find` can never return, because it filters
`Node.channel == channel`.

Plan #20 is the full write-up. These tests pin the four things that make it not happen again.
"""
import os
import unittest
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau import config, models, update                          # noqa: E402

_PG = models.db.engine.url.get_backend_name() == "postgresql"


class TestTheIngestChannelsReaderIsTheOnlyOne(unittest.TestCase):
    def test_absent_and_empty_both_mean_nothing(self):
        for value in (None, "", "   ", " \t "):
            with self.subTest(INGEST_CHANNELS=value):
                env = dict(os.environ)
                env.pop("INGEST_CHANNELS", None)
                if value is not None:
                    env["INGEST_CHANNELS"] = value
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(config.get_ingest_channels(), [])

    def test_it_never_falls_back_to_the_enum_list(self):
        """`config.get_channels()` defines the CHANNEL_TYPE enum, so it holds every channel ever
        contemplated -- the wrong list to default an ACTION to. That fallback is what ingested
        release."""
        with mock.patch.dict(os.environ, {"INGEST_CHANNELS": ""}):
            self.assertNotEqual(config.get_ingest_channels(), config.get_channels())
            self.assertIn("release", config.get_channels())
            self.assertNotIn("release", config.get_ingest_channels())

    def test_update_all_enqueues_nothing_and_says_so(self):
        with mock.patch.dict(os.environ, {"INGEST_CHANNELS": ""}), \
                mock.patch.object(update, "update_in_queue") as enq, \
                mock.patch.object(update.logger, "warning") as warn:
            update.update_all(products=["Firefox"])
        self.assertEqual(enq.call_args_list, [])
        self.assertEqual(warn.call_count, 1, "a closed default that says nothing is the same "
                                             "silent no-op with the opposite sign")


class TestThePatchChainIsScopedToTheIngestedChannels(unittest.TestCase):
    """`analyze_one_patch` is the path that spent 2,628 hg fetches on release."""

    def test_it_passes_the_ingested_channels_to_the_query(self):
        with mock.patch.dict(os.environ, {"INGEST_CHANNELS": "nightly beta"}), \
                mock.patch.object(models.Changeset, "to_analyze",
                                  return_value=(None, None, None)) as ta:
            update.analyze_one_patch()
        ta.assert_called_once_with(channels=["nightly", "beta"])

    @unittest.skipUnless(_PG, "needs Postgres: DISTINCT ON")
    def test_a_switched_off_channels_rows_are_not_offered(self):
        """The whole point, against real rows written by the PRODUCTION path.

        Driven through `Changeset.add` with pushlog-shaped dicts rather than hand-built ORM
        objects, so the rows are the ones `put_filelog` actually creates -- a fixture that
        writes a shape production never writes is how a query test passes on nothing."""
        models.create()
        pushdate = datetime.now(timezone.utc)

        def _chgset(node):
            return {"node": node, "date": pushdate, "backedout": False, "merge": False,
                    "bug": None,
                    # The shape `pushlog.collect` stores: `hgauthors.analyze_author`'s list of
                    # (email, real, nick) triples, which is what `HGAuthor.get_id` indexes into.
                    "author": [("a@b.c", "A B", "")],
                    "files": ["dom/Foo.cpp"]}

        try:
            models.Changeset.add([_chgset("aaaaaaaaaaaa")], pushdate, "nightly")
            models.Changeset.add([_chgset("rrrrrrrrrrrr")], pushdate, "release")

            # Both ingested: the chain is offered one of them.
            self.assertIsNotNone(
                models.Changeset.to_analyze(channels=["nightly", "release"])[1])
            # Only nightly ingested: the release row is invisible, however many there are.
            for _ in range(3):
                self.assertEqual(
                    models.Changeset.to_analyze(channels=["nightly"])[2], "nightly")
            # Nothing ingested: nothing is work. This is the state production was in for the
            # month it was parsing release patches.
            self.assertEqual(models.Changeset.to_analyze(channels=[]),
                             (None, None, None))
            # And the unfiltered form still sees everything, for callers that want it.
            self.assertIsNotNone(models.Changeset.to_analyze()[1])
        finally:
            for channel in ("nightly", "release"):
                models.db.session.query(models.Node).filter(
                    models.Node.channel == channel).delete()
            models.db.session.commit()


class TestThePushlogRequestIsBounded(unittest.TestCase):
    """A channel that has been dark longer than the retention window must not ask hg for the
    whole gap: production's frozen `lastdate(release)` of 2026-07-06 made the first switch-on
    tick a 56-day, 30.4 MB, 20,094-changeset request."""

    def _window(self, last_scan, calls):
        end = datetime(2026, 8, 31, tzinfo=timezone.utc)
        with mock.patch.object(models.Node, "get_max_date", return_value=None), \
                mock.patch.object(models.LastDate, "get",
                                  return_value=(None, last_scan)), \
                mock.patch.object(update, "pushlog",
                                  side_effect=lambda s, e, channel: calls.append((s, e)) or []), \
                mock.patch.object(models.Changeset, "add", return_value=(None, None)):
            update.put_filelog("release", end_date=end)
        return calls[-1][0], end

    def test_a_stale_clock_is_clamped_to_the_retention_window(self):
        calls = []
        start, end = self._window(datetime(2026, 7, 6, tzinfo=timezone.utc), calls)
        floor = end - relativedelta(days=config.get_ndays_of_data())
        self.assertEqual(start, floor)
        self.assertLessEqual((end - start).days, config.get_ndays_of_data())

    def test_a_fresh_clock_is_left_alone(self):
        calls = []
        recent = datetime(2026, 8, 30, tzinfo=timezone.utc)
        start, _ = self._window(recent, calls)
        self.assertEqual(start, recent + relativedelta(seconds=1))

    def test_the_clamp_is_logged_because_a_dark_channel_has_no_other_signal(self):
        with mock.patch.object(update.logger, "warning") as warn:
            self._window(datetime(2026, 7, 6, tzinfo=timezone.utc), [])
        self.assertEqual(warn.call_count, 1)

    def test_an_explicit_start_date_is_not_clamped(self):
        """`bin/create.py` passes its own window. Silently truncating a hand-run backfill is the
        same fails-by-doing-less shape, with no log line."""
        calls = []
        end = datetime(2026, 8, 31, tzinfo=timezone.utc)
        explicit = end - relativedelta(days=365)
        with mock.patch.object(update, "pushlog",
                               side_effect=lambda s, e, channel: calls.append((s, e)) or []), \
                mock.patch.object(models.Changeset, "add", return_value=(None, None)):
            update.put_filelog("release", start_date=explicit, end_date=end)
        self.assertEqual(calls[-1][0], explicit)


class TestRetentionIsNotCoupledToABusyChannel(unittest.TestCase):
    def test_an_empty_pushlog_window_still_prunes(self):
        """`Changeset.add` used to return a bare `LastDate.update` on an empty window, so a
        channel kept rows past the retention window until its next NON-empty tick. Release ships
        every ~7 days, so most of its ticks are empty ones."""
        date = datetime(2026, 8, 31, tzinfo=timezone.utc)
        with mock.patch.object(models.Node, "clean", return_value=None) as clean:
            models.Changeset.add([], date, "release")
        clean.assert_called_once_with(date, "release")


if __name__ == "__main__":
    unittest.main()
