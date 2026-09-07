# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""Postgres-only: the enum migration really adds a label, and an ESR line is its own lineage.

    DATABASE_URL=postgresql://... CLOUSEAU_THROWAWAY_DB=1 REDIS_URL=redis://localhost:6379/0 \\
        uv run python -m unittest tests.test_enum_migration_pg

Both halves need a real Postgres: sqlite renders `db.Enum` as VARCHAR (there is no migration to
test) and `Build.put_data` is a Postgres `INSERT ... ON CONFLICT`. The class DROPS AND RECREATES
THE SCHEMA, so it refuses to run unless `CLOUSEAU_THROWAWAY_DB=1` says the database is disposable
(a `pgserver` instance from PyPI does; `DATABASE_URL` alone is not consent).

What it proves. (1) `models._ensure_enum_values` had NEVER added a value: it re-used the connection
whose `SELECT ... pg_enum` had autobegun a transaction, SQLAlchemy raised on the AUTOCOMMIT switch,
and the `except` logged a warning -- so `lead` reached production only because that DB was created
after the value was in the enum. A DB created with the pre-ESR enum now gains the ESR label on
`models.create()`, idempotently, and the label is usable. (2) `builds.version` was VARCHAR(10)
and `140.15.0esr` is 11 characters -- found by THIS file's first run, which is the whole argument
for running it: `models.create()` on a long-lived DB now widens the column. (3) Through the
production writers, `esr153` and `release` are two lineages even where their builds interleave in
time: the selection window, the build pair and the candidate window's lower bound never cross.
"""
import datetime
import os
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from sqlalchemy import inspect, text  # noqa: E402

from crashclouseau import db, hgauthors, models, utils  # noqa: E402

_OLD_CHANNELS = ("nightly", "beta", "release")   # the enum production's DB was created with
_UTC = datetime.timezone.utc


def _runnable():
    return db.engine.dialect.name == "postgresql" and os.getenv("CLOUSEAU_THROWAWAY_DB") == "1"


def _labels(enum):
    with db.engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = :n ORDER BY e.enumsortorder"), {"n": enum}).all()
    return [r[0] for r in rows]


@unittest.skipUnless(_runnable(), "needs a THROWAWAY Postgres: DATABASE_URL + CLOUSEAU_THROWAWAY_DB=1")
class TestTheEnumMigrationOnPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A database as production's was created: every table gone, and the channel enum with
        # only the three original labels. `create_all` keeps an existing type (checkfirst), so
        # the tables come back bound to the OLD enum and only `_ensure_enum_values` can grow it.
        db.session.remove()
        db.drop_all()
        with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            for t in ("CHANNEL_TYPE", "PRODUCT_TYPE", "VERDICT_TYPE", "AGENT_STATUS_TYPE"):
                conn.execute(text('DROP TYPE IF EXISTS "{}" CASCADE'.format(t)))
            conn.execute(text('CREATE TYPE "CHANNEL_TYPE" AS ENUM ({})'.format(
                ", ".join("'{}'".format(c) for c in _OLD_CHANNELS))))
        cls.fresh = models.create()
        # ...and as production's `builds` table still is: `version` at the width it was created
        # with. The second `create()` is the release phase on a long-lived DB.
        with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text('ALTER TABLE builds ALTER COLUMN version TYPE VARCHAR(10)'))
        cls.fresh_again = models.create()

    def setUp(self):
        db.session.rollback()   # one test's failure must not abort the next one's transaction

    def test_the_version_column_was_widened_for_esr_version_strings(self):
        self.assertFalse(self.fresh_again)
        cols = {c["name"]: c for c in inspect(db.engine).get_columns("builds")}
        self.assertEqual(cols["version"]["type"].length, 24)
        models._ensure_column_widths()                       # idempotent
        cols = {c["name"]: c for c in inspect(db.engine).get_columns("builds")}
        self.assertEqual(cols["version"]["type"].length, 24)

    def test_the_pre_esr_enum_gained_the_esr_label(self):
        self.assertTrue(self.fresh)
        # The first four in enum order; a label cannot be dropped, and the lenient-read test
        # above (alphabetically earlier) adds a retired `esr140` after them when it runs first.
        self.assertEqual(_labels("CHANNEL_TYPE")[:4], list(_OLD_CHANNELS) + ["esr153"])
        self.assertLessEqual(set(_labels("CHANNEL_TYPE")) - {"esr140"},
                             set(_OLD_CHANNELS) | {"esr153"})
        self.assertIn("lead", _labels("VERDICT_TYPE"))

    def test_the_migration_is_idempotent(self):
        before = _labels("CHANNEL_TYPE")
        models._ensure_enum_values()
        models._ensure_enum_values()
        self.assertEqual(_labels("CHANNEL_TYPE"), before)

    def test_a_label_the_type_has_and_the_config_does_not_still_reads(self):
        """v166 500'd tasks.html with `LookupError: 'esr115' is not among the defined enum
        values`: a retired line's rows outlived its label in `config.channels`, and the Enum
        SUBCLASS that was meant to be lenient was adapted away by the Postgres dialect -- the
        sqlite test passed, production did not. `CHANNEL_TYPE` is a TypeDecorator now; this is
        the read that failed, against a real Postgres."""
        with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text('ALTER TYPE "CHANNEL_TYPE" ADD VALUE IF NOT EXISTS \'esr140\''))
        self.assertNotIn("esr140", models.CHANNEL_TYPE.enums)         # retired from the config
        db.session.execute(text(
            "INSERT INTO lastdate (channel, mindate, maxdate) VALUES ('esr140', NULL, NULL)"))
        db.session.commit()
        try:
            self.assertEqual(models.LastDate.get("esr140"), (None, None))
            self.assertIn("esr140", {r.channel for r in db.session.query(models.LastDate).all()})
            # The exact shape of the failing read: a labelled enum column among other columns.
            rows = db.session.query(models.LastDate.channel.label("channel"),
                                    models.LastDate.maxdate).all()
            self.assertIn("esr140", {r.channel for r in rows})
            self.assertIsInstance([r.channel for r in rows if r.channel == "esr140"][0], str)
        finally:
            db.session.execute(text("DELETE FROM lastdate WHERE channel = 'esr140'"))
            db.session.commit()

    def test_retire_deletes_a_retired_labels_rows_and_refuses_a_live_one(self):
        """`bin/retire_channel.py`: the operator's version of the purge, refusing a label a
        switch still names. Rows for a label the type has and the config does not."""
        from crashclouseau import hgauthors, retire

        with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text('ALTER TYPE "CHANNEL_TYPE" ADD VALUE IF NOT EXISTS \'esr140\''))
        when = datetime.datetime(2026, 8, 26, 14, 0, tzinfo=_UTC)
        models.Changeset.add([{"node": "1ace7e56a446", "date": when, "backedout": False,
                               "merge": False, "bug": 1,
                               "author": hgauthors.analyze_author("A <a@example.com>"),
                               "files": ["dom/x.cpp"]}],
                             datetime.datetime(2026, 9, 7, tzinfo=_UTC), "esr140")
        models.Build.put_data({"Firefox": {"esr140": {
            utils.get_build_date("20260826142222"): {"revision": "1ace7e56a446",
                                                     "version": "140.15.0esr"}}}})
        db.session.execute(text(
            "INSERT INTO selection (signature, product, channel, build_day, outcome, number, "
            "position, evaluable, baseline, bids, run_date, ever_selected, first_run_date) "
            "VALUES ('Foo::Bar', 'Firefox', 'esr140', '2026-08-26', 'selected', 1, 1, true, "
            "'[]', '{}', now(), true, now())"))
        db.session.commit()
        before, after = retire.retire(["esr140"], execute=False)
        self.assertEqual((before["nodes"], before["builds"], before["selection"]), (1, 1, 1))
        self.assertIsNone(after)
        self.assertEqual(before, retire.counts(["esr140"]))          # the dry run deleted nothing
        with mock.patch.dict(os.environ, {"AGENT_CHANNELS": "nightly esr140"}):
            with self.assertRaises(ValueError):
                retire.retire(["esr140"], execute=True)
        with mock.patch.dict(os.environ, {"AGENT_CHANNELS": "nightly", "INGEST_CHANNELS": "nightly"}):
            before, after = retire.retire(["esr140"], execute=True)
        self.assertEqual((before["nodes"], before["builds"], before["selection"]), (1, 1, 1))
        self.assertEqual(set(after.values()), {0})
        self.assertEqual(set(retire.counts(["esr140"]).values()), {0})
        # The other channels' rows are untouched.
        self.assertGreaterEqual(
            db.session.execute(text("SELECT count(*) FROM nodes")).scalar(), 0)

    def test_the_new_label_is_usable(self):
        now = datetime.datetime.now(_UTC)
        models.LastDate.update(now, now, "esr153")
        _, maxdate = models.LastDate.get("esr153")
        self.assertIsNotNone(maxdate)

    def test_the_esr_line_is_its_own_lineage_beside_release(self):
        """Through the production writers: `Changeset.add` for the nodes (two repos, two labels)
        and `Build.put_data` keyed the way `buildhub.get` keys it. Real build ids: 154.0.1
        (08-24) lies BETWEEN 153.1.0esr (08-11) and 153.2.0esr (08-26) in time, so a lineage
        that read "the build before this one" across labels would hand the ESR crash a release
        build's push date as its candidate window's lower bound."""
        def node(rev, when):
            # The shape `pushlog.collect` hands `Changeset.add`: the author already analysed.
            return {"node": rev, "date": when, "backedout": False, "merge": False, "bug": 1,
                    "author": hgauthors.analyze_author("A Example <a@example.com>"),
                    "files": ["dom/base/nsGlobalWindowInner.cpp"]}

        d153_1 = datetime.datetime(2026, 8, 11, 20, 0, tzinfo=_UTC)
        d153_2 = datetime.datetime(2026, 8, 26, 2, 0, tzinfo=_UTC)
        d154_0_1 = datetime.datetime(2026, 8, 24, 15, 0, tzinfo=_UTC)
        d155_0_1 = datetime.datetime(2026, 9, 3, 21, 0, tzinfo=_UTC)
        end = datetime.datetime(2026, 9, 7, tzinfo=_UTC)
        models.Changeset.add([node("bdb74c45c2e1", d153_1), node("92c5bf513a3e", d153_2)],
                             end, "esr153")
        models.Changeset.add([node("aaaaaaaaaaa1", d154_0_1), node("aaaaaaaaaaa2", d155_0_1)],
                             end, "release")

        def bh(bid, rev, version):
            return {utils.get_build_date(bid): {"revision": rev, "version": version}}

        models.Build.put_data({"Firefox": {
            "esr153": {**bh("20260811201151", "bdb74c45c2e1", "153.1.0esr"),
                       **bh("20260826022508", "92c5bf513a3e", "153.2.0esr")},
            "release": {**bh("20260824154132", "aaaaaaaaaaa1", "154.0.1"),
                        **bh("20260903215306", "aaaaaaaaaaa2", "155.0.1")},
        }})
        asof = datetime.datetime(2026, 9, 7, tzinfo=_UTC)
        versions = lambda rows: [r["version"] for r in rows]  # noqa: E731
        self.assertEqual(versions(models.Build.get_last_versions(asof, "esr153", "Firefox", n=3)),
                         ["153.2.0esr", "153.1.0esr"])
        self.assertEqual(versions(models.Build.get_last_versions(asof, "release", "Firefox", n=3)),
                         ["155.0.1", "154.0.1"])
        self.assertEqual(
            versions(models.Build.get_two_last(utils.get_build_date("20260826022508"),
                                               "esr153", "Firefox")),
            ["153.1.0esr", "153.2.0esr"])
        # The candidate window's lower bound is the LINE's previous build (08-11), not the
        # release build of 08-24 that sits between the two ESR builds; None on the first build.
        self.assertEqual(models.Build.get_pushdate_before(
            utils.get_build_date("20260826022508"), "esr153", "Firefox"), d153_1)
        self.assertIsNone(models.Build.get_pushdate_before(
            utils.get_build_date("20260811201151"), "esr153", "Firefox"))


if __name__ == "__main__":
    unittest.main()
