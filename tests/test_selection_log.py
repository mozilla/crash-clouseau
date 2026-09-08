# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# The selection log, and the config knobs that decide what the selector can even see.
# Pure-logic + wiring checks run anywhere (DATABASE_URL=sqlite://). The round-trip needs a
# DISPOSABLE Postgres backend (pg.JSONB / on_conflict are Postgres-only) and is skipped
# otherwise:
#   DATABASE_URL=postgresql://user@localhost/clouseau_test \
#     REDIS_URL=redis://localhost:6379/0 python -m unittest tests.test_selection_log
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from libmozdata import utils as lmdutils  # noqa: E402

from crashclouseau import config, datacollector as dc, db, models, update, utils  # noqa: E402


class TestConfigWiring(unittest.TestCase):
    def test_shipped_values(self):
        self.assertEqual(config.get_nightly_window_ndays(), 21)
        self.assertEqual(config.get_build_facets_limit(), 500)
        self.assertEqual(config.get_spike("mature_after_days", "Firefox", "nightly"), 5)
        self.assertEqual(config.get_spike("mature_installs", "Firefox", "nightly"), 4)

    def test_maturity_bar_applies_to_nightly_only(self):
        """The bar prices the wider NIGHTLY window; no other channel's window changed.

        The install half is inert elsewhere anyway (`max(threshold, mature_installs)` with
        beta/release at 6/50), but the FLOOR half is not: beta's spike floor is 10 while
        its install threshold is 6, so a mature build-day with 6-9 crashes would go from
        selected to `immature` — silent over-gating on a channel nobody measured."""
        self.assertEqual(dc.get_maturity_bar("Firefox", "nightly"), (5, 4))
        for channel in ("beta", "release"):
            self.assertEqual(dc.get_maturity_bar("Firefox", channel), (None, 1))
        self.assertGreater(
            config.get_spike("floor", "Firefox", "beta"),
            config.get_threshold("installs", "Firefox", "beta"),
            "if this ever stops holding, re-check whether the floor half is still unsafe",
        )

    def test_a_floor_at_or_below_the_install_threshold_cannot_bind(self):
        """RELEASE SHIPS IN EXACTLY THAT STATE (floor 50 == installs 50), so its floor is not a
        lever and must not be "tuned".

        `evaluate_days` computes `spiked` and continues before the install test, so the floor
        LOOKS like the first gate. But a day's `count` sums each buildid's report count, and
        reports >= distinct installations for a given buildid by construction, so a day failing
        `floor <= threshold` could never have passed `installs >= threshold`. Measured on
        release: floor 1/3/10/20/50 all give exactly 329 pairs over 133 run-days; 100 is the
        first value that changes anything."""
        self.assertLessEqual(
            config.get_spike("floor", "Firefox", "release"),
            config.get_threshold("installs", "Firefox", "release"),
            "release's floor has risen above its install threshold, so it now BINDS and the "
            "docstring in config.get_spike calling it inert is out of date",
        )


# 2026-07-31 build, first crashes seen on the 2026-08-07 run.
INCIDENT_BUILD_AGE_DAYS = 7


class TestWindowPinsTheIncident(unittest.TestCase):
    """Without these, reverting the window to its old value leaves the suite green.

    Mutation-checked: restoring `ndays + 5` in datacollector.get_builds passed 1065/1065
    before these existed, so nothing actually held the fix in place."""

    def test_window_leaves_a_full_baseline_ahead_of_a_week_old_build(self):
        window = config.get_nightly_window_ndays()
        self.assertGreaterEqual(
            window - INCIDENT_BUILD_AGE_DAYS,
            config.get_ndays(),
            "a build as old as the one in the incident must still have `ndays` of earlier "
            "build-days ahead of it in the window, or evaluate_days can never test it",
        )

    def test_window_outlives_the_maturity_bar(self):
        """A build-day old enough to be judged `mature` has to still be inside the window,
        otherwise the maturity branch is unreachable and the bar is dead config."""
        self.assertGreater(
            config.get_nightly_window_ndays() - config.get_ndays(),
            config.get_spike("mature_after_days", "Firefox", "nightly"),
        )

    def test_window_stays_inside_the_pushlog_retention(self):
        """`Node.clean` prunes changesets older than `max_ndays`; a build reachable by the
        window but outside that retention has no pushlog left, so every crash on it is
        useless. Keep window + baseline under the retention."""
        self.assertLess(
            config.get_nightly_window_ndays() + config.get_ndays(),
            config.get_ndays_of_data(),
        )

    def test_get_builds_reads_the_knob(self):
        """Pins the wiring, not just the number: the lower bound has to move with it."""
        seen = {}

        def fake(search_buildid, search_date, product):
            seen["buildid"] = search_buildid
            seen["date"] = search_date
            return []

        date = lmdutils.get_date_ymd("2026-08-07")
        with mock.patch.object(dc, "get_buildids_from_socorro", side_effect=fake):
            with mock.patch.object(config, "get_nightly_window_ndays", return_value=21):
                dc.get_builds("Firefox", "nightly", date)
                wide = seen["buildid"][0]
            with mock.patch.object(config, "get_nightly_window_ndays", return_value=8):
                dc.get_builds("Firefox", "nightly", date)
                narrow = seen["buildid"][0]
        self.assertEqual(wide, ">=20260717000000")
        self.assertEqual(narrow, ">=20260730000000")

    def test_no_buildids_still_returns_a_pair(self):
        """put_crashes unpacks two values; a bare `{}` here raised ValueError. Reachable
        on beta/release whenever the `builds` table is empty."""
        with mock.patch.object(dc, "get_builds", return_value=([], "")):
            self.assertEqual(dc.get_new_signatures("Firefox", "beta", None), ({}, []))

    def test_ndays_is_untouched(self):
        """`backward_lookup_ndays` is ALSO the baseline length, the buildhub backfill and
        the regressor pushlog window (update.put_report, orchestrator). Widening the build
        window through it would widen what the agent may blame, which is why
        `nightly_window_ndays` exists. Pin it."""
        self.assertEqual(config.get_ndays(), 3)

    def test_spike_defaults_cover_every_knob(self):
        """get_spike() indexes _SPIKE_DEFAULTS[typ]; a knob missing from it raises
        KeyError on any config that omits the block."""
        for typ in ("floor", "ratio", "mature_after_days", "mature_installs",
                    "min_build_installs"):
            self.assertIn(typ, config._SPIKE_DEFAULTS)
            self.assertIsInstance(config.get_spike(typ, "Nonesuch", "nightly"), int)


class TestOutcomeVocabulary(unittest.TestCase):
    def test_models_and_utils_agree(self):
        self.assertEqual(
            models.SELECTION_OUTCOMES,
            frozenset(
                {
                    utils.SELECTED,
                    utils.NOT_SPIKING,
                    utils.UNTESTABLE_PREFIX,
                    utils.BELOW_INSTALL_THRESHOLD,
                    utils.IMMATURE,
                    utils.DROPPED_NO_USERS,
                    utils.RISING_RATE,
                    utils.IGNORED,
                }
            ),
        )

    def test_every_outcome_fits_the_column(self):
        width = models.Selection.__table__.c.outcome.type.length
        for outcome in models.SELECTION_OUTCOMES:
            self.assertLessEqual(len(outcome), width, outcome)


_TEST_SIG = "CrashChannel::OpenContentStream"


class TestIgnoredSignatures(unittest.TestCase):
    """`config.ignored_signatures`: a deliberate test crash is never analysed, whatever its
    numbers. `CrashChannel::OpenContentStream` is what about:crashparent and about:crashcontent
    both produce; it spike-selected on nightly on 2026-09-08 (build 20260908042139) and had been
    analysed four times. The list has to bite at the selector, in the rate path, in the spike
    sweep (its old `selected` rows are inside the lookback) and on a queued agent run."""

    def test_the_shipped_list_names_the_about_crash_signature(self):
        self.assertIn(_TEST_SIG, config.get_ignored_signatures())
        self.assertTrue(config.is_ignored_signature(_TEST_SIG))
        self.assertFalse(config.is_ignored_signature("mozilla::places::History::History"))
        self.assertFalse(config.is_ignored_signature(None))
        self.assertFalse(config.is_ignored_signature(""))

    def _collect(self, population, builds):
        """The real `get_new_signatures` over `{buildid: {signature: (count, installs)}}`."""
        class FakeSuperSearch:
            def __init__(self, params=None, handler=None, handlerdata=None):
                bid = params["build_id"]
                facets = [
                    {"term": sgn,
                     "facets": {"cardinality_install_time": {"value": installs},
                                "build_id": [{"term": bid, "count": count}]}}
                    for sgn, (count, installs) in population.get(bid, {}).items()
                ]
                handler({"errors": None, "facets": {"signature": facets}}, handlerdata)

            def wait(self):
                pass

        rising = mock.Mock(return_value={})
        with mock.patch.object(dc.socorro, "SuperSearch", FakeSuperSearch), \
                mock.patch.object(dc, "get_proto_small"), \
                mock.patch.object(dc, "get_proto_big"), \
                mock.patch.object(dc, "_rising_picks", rising), \
                mock.patch.object(dc, "get_builds", return_value=(list(builds), ">=2026-09-01")):
            data, selection = dc.get_new_signatures("Firefox", "nightly", datetime(2026, 9, 4))
        return data, selection, rising

    def test_a_spiking_test_signature_is_dropped_and_logged_not_selected(self):
        builds = [20260901040000, 20260902040000, 20260903040000, 20260904040000]
        other = "mozilla::Real::Regression"
        population = {b: {other: (2, 2)} for b in builds[:3]}
        population[builds[0]] = dict(population[builds[0]], **{_TEST_SIG: (3, 3)})
        # Both spike on the last build-day; only one is a regression.
        population[builds[3]] = {other: (40, 30), _TEST_SIG: (40, 30)}
        data, selection, rising = self._collect(population, builds)

        self.assertIn(other, data)
        self.assertNotIn(_TEST_SIG, data)
        mine = [r for r in selection if r["signature"] == _TEST_SIG]
        self.assertEqual({r["outcome"] for r in mine}, {utils.IGNORED})
        # One row per build-day it was reported on (two here), none picked, not evaluated.
        self.assertEqual(len(mine), 2)
        self.assertTrue(all(r["picked"] is None and not r["evaluable"] for r in mine))
        self.assertEqual({r["count"] for r in mine}, {3, 40})
        # The other signature's decision is untouched.
        self.assertIn(utils.SELECTED, {r["outcome"] for r in selection
                                       if r["signature"] == other})
        # And the rate path is told to keep its hands off it.
        already = rising.call_args.args[4]
        self.assertIn(_TEST_SIG, already)
        self.assertIn(other, already)

    def test_the_row_fits_the_selection_table(self):
        bid = utils.get_build_date(20260904040000)
        numbers = {datetime(2026, 9, 4): {"count": 40, "bids": {bid: 40}, "installs": {bid: 30}}}
        rec = dict(dc.ignored_day_records(numbers)[0], signature=_TEST_SIG)
        row = models.Selection._row(rec, "Firefox", "nightly", datetime.now(timezone.utc))
        self.assertEqual((row["outcome"], row["picked"], row["number"], row["ever_selected"]),
                         (utils.IGNORED, None, 40, False))
        self.assertLessEqual(len(utils.IGNORED), models.Selection.__table__.c.outcome.type.length)

    def test_a_queued_agent_run_on_it_does_not_spend(self):
        from crashclouseau.agent import orchestrator

        with mock.patch.object(models.UUID, "get_channel", return_value="nightly"), \
                mock.patch.object(models.UUID, "get_signature", return_value=_TEST_SIG), \
                mock.patch.object(models.Dossier, "skip_triage",
                                  side_effect=AssertionError("the run went past the gate")):
            self.assertIsNone(orchestrator.run_evidence_agent("u-ignored"))

    def test_a_forced_run_still_goes_through(self):
        """A retrigger click is one explicit uuid somebody asked for; it is not gated here."""
        from crashclouseau.agent import orchestrator

        with mock.patch.object(models.UUID, "get_signature",
                               side_effect=AssertionError("a forced run must not ask")), \
                mock.patch.object(orchestrator, "build_seed", return_value=None) as seed:
            orchestrator.run_evidence_agent("u-forced", force=True)
        seed.assert_called_once_with("u-forced")       # past every gate, up to the seed

    def test_enqueue_skips_it(self):
        from crashclouseau.agent import orchestrator

        with mock.patch.object(config, "get_agent_enabled", return_value=True), \
                mock.patch.object(config, "get_agent_channels", return_value=["nightly"]), \
                mock.patch.object(models.UUID, "get_signature", return_value=_TEST_SIG), \
                mock.patch.object(orchestrator.worker, "get_queue",
                                  side_effect=AssertionError("enqueued an ignored signature")):
            orchestrator.enqueue_agent("u-ignored", channel="nightly")


def _record(outcome=utils.SELECTED, count=4):
    bid = utils.get_build_date(20260731085738)
    other = utils.get_build_date(20260731014234)
    return {
        "signature": "mozilla::places::History::History",
        "day": datetime(2026, 7, 31),
        "count": count,
        "index": 14,
        "baseline": [0, 0, 0],
        "evaluable": True,
        "spiked": True,
        "bids": {bid: count, other: 0},
        "installs": {bid: 4},
        "picked": bid if outcome == utils.SELECTED else None,
        "outcome": outcome,
    }


class TestRowShape(unittest.TestCase):
    def test_row_conversion(self):
        run_date = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
        row = models.Selection._row(_record(), "Firefox", "nightly", run_date)
        self.assertEqual(row["signature"], "mozilla::places::History::History")
        self.assertEqual(row["build_day"], datetime(2026, 7, 31).date())
        self.assertEqual(row["outcome"], utils.SELECTED)
        self.assertEqual(row["number"], 4)
        self.assertEqual(row["position"], 14)
        self.assertTrue(row["evaluable"])
        self.assertEqual(row["baseline"], [0, 0, 0])
        self.assertEqual(row["picked"], "20260731085738")
        self.assertEqual(row["run_date"], run_date)

    def test_buildids_are_strings_with_counts_and_installs(self):
        """The row has to be self-describing: a reader must not have to re-derive which
        build of that day carried the crashes."""
        row = models.Selection._row(_record(), "Firefox", "nightly", datetime.now(timezone.utc))
        self.assertEqual(
            row["bids"],
            {
                "20260731085738": {"count": 4, "installs": 4},
                "20260731014234": {"count": 0, "installs": 0},
            },
        )
        for key in row["bids"]:
            self.assertIsInstance(key, str)
            self.assertEqual(len(key), 14)

    def test_declined_row_has_no_picked_build(self):
        row = models.Selection._row(
            _record(outcome=utils.UNTESTABLE_PREFIX), "Firefox", "nightly",
            datetime.now(timezone.utc),
        )
        self.assertIsNone(row["picked"])

    def test_overlong_signature_is_truncated_to_the_column(self):
        record = _record()
        record["signature"] = "x" * 900
        row = models.Selection._row(record, "Firefox", "nightly", datetime.now(timezone.utc))
        self.assertEqual(len(row["signature"]), 512)

    def test_record_many_is_a_noop_on_nothing(self):
        self.assertEqual(models.Selection.record_many([], "Firefox", "nightly"), 0)


class TestOneRowPerPairPerBatch(unittest.TestCase):
    """Postgres rejects a multi-row upsert whose rows share the `selection_pair` key -- the
    WHOLE statement, which `record_many` then swallows. Beta hit it every tick from
    2026-09-06 23:43Z: the rate path picked `AsyncShutdownTimeout | profile-before-change-
    telemetry | CrashManager: submitting Glean crash ping(s)` on build 20260831122253, the
    same build-day the spike test had just logged as `not_spiking`. Seven errors in the 80-minute
    log window, zero beta rows written, and the pick repeating because it was never recorded."""

    RUN = datetime(2026, 9, 7, 6, 43, tzinfo=timezone.utc)

    def _rows(self, *records):
        return [models.Selection._row(r, "Firefox", "beta", self.RUN) for r in records]

    def test_the_later_record_wins_the_pair(self):
        spike = _record(outcome=utils.NOT_SPIKING)
        rising = _record(outcome=utils.RISING_RATE, count=7)
        rising["picked"] = _record()["picked"]
        rows = models.Selection._collapse_pairs(self._rows(spike, rising))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], utils.RISING_RATE)
        self.assertEqual(rows[0]["number"], 7)
        self.assertEqual(rows[0]["picked"], "20260731085738")
        self.assertTrue(rows[0]["ever_selected"])

    def test_the_sticky_fields_merge_like_the_sql_does(self):
        # Across batches `ON CONFLICT` keeps `picked` and ORs `ever_selected`; one collapsed
        # batch must leave the table in the same state as two batches would.
        selected = _record()
        downgrade = _record(outcome=utils.NOT_SPIKING, count=2)
        rows = models.Selection._collapse_pairs(self._rows(selected, downgrade))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], utils.NOT_SPIKING)
        self.assertEqual(rows[0]["number"], 2)
        self.assertEqual(rows[0]["picked"], "20260731085738")
        self.assertTrue(rows[0]["ever_selected"])

    def test_distinct_pairs_are_untouched_and_keep_their_order(self):
        a = _record()
        b = _record(outcome=utils.NOT_SPIKING)
        b["signature"] = "another::signature"
        c = _record(outcome=utils.NOT_SPIKING)
        c["day"] = datetime(2026, 8, 1)
        rows = models.Selection._collapse_pairs(self._rows(a, b, c))
        self.assertEqual([(r["signature"], r["build_day"]) for r in rows],
                         [(r["signature"], r["build_day"]) for r in self._rows(a, b, c)])

    def test_record_many_hands_the_upsert_one_row_per_pair(self):
        spike = _record(outcome=utils.NOT_SPIKING)
        rising = _record(outcome=utils.RISING_RATE)
        with mock.patch.object(models.Selection, "_upsert", return_value=1) as upsert, \
                mock.patch.object(models.db.session, "commit"):
            written = models.Selection.record_many([spike, rising], "Firefox", "beta", self.RUN)
        self.assertEqual(written, 1)
        (values,), _ = upsert.call_args
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["outcome"], utils.RISING_RATE)


class TestPutCrashesWiring(unittest.TestCase):
    """put_crashes must unpack the new tuple AND persist the declined pairs — the log is
    worthless if the writer is not actually called."""

    def test_selection_is_recorded_and_pruned(self):
        run_date = datetime(2026, 8, 7, tzinfo=timezone.utc)
        records = [_record(outcome=utils.UNTESTABLE_PREFIX)]
        with mock.patch.object(
            update.dc, "get_new_signatures", return_value=({}, records)
        ) as collect, mock.patch(
            "crashclouseau.models.Selection.record_many"
        ) as record_many, mock.patch(
            "crashclouseau.models.Selection.prune"
        ) as prune:
            update.put_crashes(run_date, "nightly", "Firefox")

        collect.assert_called_once_with("Firefox", "nightly", run_date)
        record_many.assert_called_once_with(
            records, "Firefox", "nightly", run_date=run_date
        )
        prune.assert_called_once()

    def test_a_failing_log_cannot_stop_ingestion(self):
        """record_many swallows its own errors; assert the contract by making the write
        raise at the DB layer and checking put_crashes still returns."""
        with mock.patch.object(
            update.dc, "get_new_signatures", return_value=({}, [_record()])
        ), mock.patch.object(
            db.session, "execute", side_effect=RuntimeError("boom")
        ), mock.patch.object(
            db.session, "rollback"
        ), mock.patch(
            "crashclouseau.models.Selection.prune"
        ):
            update.put_crashes(datetime(2026, 8, 7, tzinfo=timezone.utc), "nightly", "Firefox")


_PG = db.engine.dialect.name == "postgresql"


@unittest.skipUnless(_PG, "needs a DISPOSABLE Postgres backend")
class TestRoundTrip(unittest.TestCase):
    """Exercises the DDL and the upsert — in particular the `selection_pair` constraint
    name that `record_many`'s ON CONFLICT refers to by name."""

    SIGNATURE = "tests::selection_log::synthetic"

    def setUp(self):
        models.Selection.__table__.create(bind=db.engine, checkfirst=True)
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        db.session.query(models.Selection).filter(
            models.Selection.signature == self.SIGNATURE
        ).delete(synchronize_session=False)
        db.session.commit()

    def _record(self, **over):
        record = _record(**over)
        record["signature"] = self.SIGNATURE
        return record

    def test_the_feed_and_the_summary_honour_the_channel(self):
        """`/api/selection` VALIDATED `?channel=` AND THEN IGNORED IT.

        Verified against prod v139: `?channel=nightly`, `?channel=beta` and
        `?channel=release` returned three byte-identical bodies -- same sha256 over `rows`,
        274,827 bytes each, 500 rows of 450 nightly + 50 beta -- while `?channel=bogus`
        correctly 400'd. Validate-then-ignore is worse than having no parameter: a 400 on a bad
        value tells the caller the good value was honoured.

        `html.selection()` read neither arg, so the page and the API disagreed about the same
        rows even in the signature branch, which the API alone filtered."""
        run = datetime(2026, 8, 7, tzinfo=timezone.utc)
        models.Selection.record_many([self._record()], "Firefox", "nightly", run)
        models.Selection.record_many([self._record()], "Firefox", "beta", run)

        # `days` is explicit: `_record`'s build_day is fixed at 2026-07-31, so the default
        # 14-day window would exclude the fixture and every assertion below would pass on an
        # empty list -- the shape this whole test exists to catch.
        days = 3650
        for channel in ("nightly", "beta"):
            with self.subTest(channel=channel):
                rows = models.Selection.recent(days=days, channel=channel)
                mine = [r for r in rows if r["signature"] == self.SIGNATURE]
                self.assertEqual([r["channel"] for r in mine], [channel])

        # A channel with no rows answers empty rather than answering another channel's. That
        # is the correct answer for release under INGEST_CHANNELS="nightly beta".
        rows = models.Selection.recent(days=days, channel="release")
        self.assertEqual([r for r in rows if r["signature"] == self.SIGNATURE], [])

        # The summary must filter the same way, or it describes a different population than
        # the rows printed beside it.
        both = models.Selection.summary(days=days)
        one = models.Selection.summary(days=days, channel="beta")
        self.assertGreaterEqual(sum(both.values()), sum(one.values()))
        self.assertEqual(
            models.Selection.summary(days=days, channel="release").get(utils.SELECTED, 0), 0)

        # ...and no filter still means every channel, for the unfiltered page.
        unfiltered = [r for r in models.Selection.recent(days=days)
                      if r["signature"] == self.SIGNATURE]
        self.assertEqual(sorted(r["channel"] for r in unfiltered), ["beta", "nightly"])

    def test_insert_then_upsert_keeps_one_row(self):
        run = datetime(2026, 8, 7, tzinfo=timezone.utc)
        self.assertEqual(
            models.Selection.record_many([self._record()], "Firefox", "nightly", run), 1
        )
        rows = models.Selection.for_signature(self.SIGNATURE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], utils.SELECTED)

        later = self._record(outcome=utils.UNTESTABLE_PREFIX, count=9)
        models.Selection.record_many([later], "Firefox", "nightly", run)
        rows = models.Selection.for_signature(self.SIGNATURE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], utils.UNTESTABLE_PREFIX)
        self.assertEqual(rows[0]["number"], 9)

    def test_two_records_for_one_pair_in_one_batch_write_one_row(self):
        # The statement Postgres used to reject outright ("ON CONFLICT DO UPDATE command
        # cannot affect row a second time"), taking the whole batch with it.
        run = datetime(2026, 9, 7, 6, 43, tzinfo=timezone.utc)
        spike = self._record(outcome=utils.NOT_SPIKING)
        rising = self._record(outcome=utils.RISING_RATE, count=7)
        self.assertEqual(
            models.Selection.record_many([spike, rising], "Firefox", "beta", run), 1
        )
        rows = models.Selection.for_signature(self.SIGNATURE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], utils.RISING_RATE)
        self.assertEqual(rows[0]["number"], 7)

    def test_a_downgrade_never_erases_that_we_analysed_it(self):
        """A pair's verdict legitimately changes as its build ages past `mature_after`
        (selected -> immature on identical inputs), and the upsert would otherwise
        overwrite the one fact the table exists to record."""
        first = datetime(2026, 8, 3, tzinfo=timezone.utc)
        later = datetime(2026, 8, 11, tzinfo=timezone.utc)
        models.Selection.record_many([self._record()], "Firefox", "nightly", first)
        models.Selection.record_many(
            [self._record(outcome=utils.IMMATURE)], "Firefox", "nightly", later
        )
        rows = models.Selection.for_signature(self.SIGNATURE)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["outcome"], utils.IMMATURE)     # latest verdict
        self.assertTrue(row["ever_selected"])                 # ...but we did analyse it
        self.assertEqual(row["picked"], "20260731085738")     # ...on this build
        self.assertEqual(row["first_run_date"], first.isoformat())
        self.assertEqual(row["run_date"], later.isoformat())

    def test_a_promotion_sets_ever_selected(self):
        run = datetime(2026, 8, 7, tzinfo=timezone.utc)
        models.Selection.record_many(
            [self._record(outcome=utils.UNTESTABLE_PREFIX)], "Firefox", "nightly", run
        )
        self.assertFalse(models.Selection.for_signature(self.SIGNATURE)[0]["ever_selected"])
        models.Selection.record_many([self._record()], "Firefox", "nightly", run)
        row = models.Selection.for_signature(self.SIGNATURE)[0]
        self.assertTrue(row["ever_selected"])
        self.assertEqual(row["outcome"], utils.SELECTED)

    def test_prune_keeps_the_window_and_drops_the_rest(self):
        run = datetime.now(timezone.utc)
        keep = self._record()
        keep["day"] = datetime(run.year, run.month, run.day)
        old = self._record()
        old["day"] = datetime(2020, 1, 1)
        models.Selection.record_many([keep, old], "Firefox", "nightly", run)
        # two distinct build_days -> two rows
        self.assertEqual(len(models.Selection.for_signature(self.SIGNATURE)), 2)
        models.Selection.prune(days=60)
        rows = models.Selection.for_signature(self.SIGNATURE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["build_day"], keep["day"].date().isoformat())

    def test_summary_counts_by_outcome(self):
        run = datetime(2026, 8, 7, tzinfo=timezone.utc)
        models.Selection.record_many([self._record()], "Firefox", "nightly", run)
        self.assertGreaterEqual(models.Selection.summary(days=36500).get(utils.SELECTED, 0), 1)


if __name__ == "__main__":
    unittest.main()
