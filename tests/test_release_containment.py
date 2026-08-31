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


class TestADeclinedFilingLeavesARecord(unittest.TestCase):
    """A HOLD THAT LEAVES NO TRACE CANNOT SAY WHAT IT WOULD HAVE FILED.

    `autofile_bug` has ~25 `return {"filed": False, "skipped": ...}` sites and none of them
    wrote anything: its only two DB writes are `record_filed_bug` (success) and
    `record_filing_error` (BMO rejection), so a decline reached `logger.info` and stopped there
    -- on an app with `heroku drains` EMPTY and a ~1h40m log window.

    Measured cost, read-only on prod 2026-08-31: 34 NIGHTLY runs reached the filing rung and
    left no record of why they were declined (27 post-arming, status=done, 2026-08-05..08-26).
    Beta's cost is zero -- 0 of its 38 dossiers reached the rung at all (37 abstain + 1 lead at
    confidence 25 against `min_confidence` 70) -- so the instrument that plan #18 Phase 4 rests
    on has not been needed YET, and plan #20 Phase 4 would need it from day one.
    """

    def test_the_decline_is_persisted_under_its_own_key(self):
        from crashclouseau.agent import orchestrator as orch
        recorded = {}
        with mock.patch.object(models.CrashStack, "get_by_uuid",
                               return_value=([], {"channel": "beta",
                                                  "signature": "Foo::Bar"})), \
                mock.patch("crashclouseau.bugzilla_apply.autofile_bug",
                           return_value={"filed": False,
                                         "skipped": "channel 'beta' filing is held"}), \
                mock.patch.object(models.Dossier, "record_filing_decline",
                                  side_effect=lambda u, i, **kw: recorded.update(
                                      uuid=u, info=i) or True):
            orch._autofile("u-1", {"dossier": {}},
                           {"verdict": "lead", "confidence": 70})
        self.assertEqual(recorded["uuid"], "u-1")
        self.assertEqual(recorded["info"]["skipped"], "channel 'beta' filing is held")
        self.assertEqual(recorded["info"]["channel"], "beta")
        self.assertEqual(recorded["info"]["verdict"], "lead")
        self.assertEqual(recorded["info"]["confidence"], 70)

    def test_a_successful_filing_records_no_decline(self):
        from crashclouseau.agent import orchestrator as orch
        with mock.patch.object(models.CrashStack, "get_by_uuid",
                               return_value=([], {"channel": "nightly",
                                                  "signature": "Foo::Bar"})), \
                mock.patch("crashclouseau.bugzilla_apply.autofile_bug",
                           return_value={"filed": True, "bug": 123, "mode": "new_bug"}), \
                mock.patch.object(models.Dossier, "record_filing_decline") as rec:
            orch._autofile("u-1", {"dossier": {}},
                           {"verdict": "lead", "confidence": 70})
        rec.assert_not_called()

    def test_the_global_off_switch_is_not_a_decline(self):
        """`autofile disabled` is every run on every channel when `AUTOFILE_BUGS=0`. Recording
        it would write a row per run and drown the signal the key exists to carry."""
        from crashclouseau.agent import orchestrator as orch
        with mock.patch.object(models.CrashStack, "get_by_uuid",
                               return_value=([], {"channel": "nightly",
                                                  "signature": "Foo::Bar"})), \
                mock.patch("crashclouseau.bugzilla_apply.autofile_bug",
                           return_value={"filed": False, "skipped": "autofile disabled"}), \
                mock.patch.object(models.Dossier, "record_filing_decline") as rec:
            orch._autofile("u-1", {"dossier": {}},
                           {"verdict": "lead", "confidence": 70})
        rec.assert_not_called()

    def test_it_is_not_under_filed_bug_and_is_not_sticky(self):
        """THE TRAP, and the reason this is a new key rather than a field on `filed_bug`.

        `already_filed` returns any truthy `filed_bug` with NO `filed` test, and `filed_bug` is
        in `_STICKY_PAYLOAD_KEYS` -- so a decline recorded there would survive every retrigger
        and read as "already filed" forever, permanently closing a crash whose bug was never
        created. Three more readers would mis-render it: `list_tasks` plucks `filed_bug.bug`
        unconditionally, `html._task_view` counts any truthy `filed_bug` as filed, and
        `retrigger_agent` would warn "ALREADY went to bugzilla".

        And it must NOT be sticky, mirroring `filing_error`: stickiness is only correct for a
        fact about the outside world, and a decline is undone by re-running."""
        self.assertNotIn("filing_declined", models.Dossier._STICKY_PAYLOAD_KEYS)
        self.assertIn("filed_bug", models.Dossier._STICKY_PAYLOAD_KEYS)
        import inspect
        src = inspect.getsource(models.Dossier.record_filing_decline)
        self.assertIn('payload["filing_declined"]', src)
        self.assertNotIn('payload["filed_bug"]', src)

    @unittest.skipUnless(_PG, "needs Postgres: jsonb payload round-trip")
    def test_it_round_trips_and_does_not_read_as_filed(self):
        """The real reason this needs a DB: `already_filed` must stay False.

        `channel` lives on `builds`, not on `uuids`, so the fixture needs a real `builds`
        parent -- the same shape `tests/test_persistence` uses, and the shape production has
        (`update.put_crashes` skips a crash whose buildid has no row)."""
        from crashclouseau import utils as cutils
        models.db.create_all()
        uuid = "decline-round-trip-0001"
        build = models.Build(
            cutils.get_build_date("20260826090609"), "Firefox", "beta", "155.0b3", None)
        models.db.session.add(build)
        models.db.session.commit()
        models.db.session.add(models.UUID(uuid, None, "protoDecline", build.id))
        models.db.session.commit()
        try:
            models.Dossier.upsert(uuid, payload={"dossier": {}}, status="done")
            self.assertTrue(models.Dossier.record_filing_decline(
                uuid, {"skipped": "channel 'beta' filing is held"}))
            row = models.Dossier.get_by_uuid(uuid)
            self.assertEqual(row.payload["filing_declined"]["skipped"],
                             "channel 'beta' filing is held")
            # The whole point: a decline is not a filing.
            self.assertFalse(models.Dossier.already_filed(uuid))
            self.assertNotIn("filed_bug", row.payload)
        finally:
            models.db.session.rollback()
            models.db.session.query(models.UUID).filter(
                models.UUID.uuid == uuid).delete()
            models.db.session.query(models.Build).filter(
                models.Build.id == build.id).delete()
            models.db.session.commit()
