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
after the value was in the enum. A DB created with the pre-ESR enum now gains the three ESR labels
on `models.create()`, idempotently, and the label is usable. (2) `builds.version` was VARCHAR(10)
and `140.15.0esr` is 11 characters -- found by THIS file's first run, which is the whole argument
for running it: `models.create()` on a long-lived DB now widens the column. (3) Through the
production writers, `esr140` / `esr153` / `esr115` are three lineages: the selection window, the
build pair and the candidate window's lower bound never cross from one line to another.
"""
import datetime
import os
import unittest

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

    def test_the_pre_esr_enum_gained_the_esr_labels(self):
        self.assertTrue(self.fresh)
        self.assertEqual(_labels("CHANNEL_TYPE"),
                         list(_OLD_CHANNELS) + ["esr115", "esr140", "esr153"])
        self.assertIn("lead", _labels("VERDICT_TYPE"))

    def test_the_migration_is_idempotent(self):
        before = _labels("CHANNEL_TYPE")
        models._ensure_enum_values()
        models._ensure_enum_values()
        self.assertEqual(_labels("CHANNEL_TYPE"), before)

    def test_the_new_label_is_usable(self):
        now = datetime.datetime.now(_UTC)
        models.LastDate.update(now, now, "esr153")
        _, maxdate = models.LastDate.get("esr153")
        self.assertIsNotNone(maxdate)

    def test_each_esr_line_is_its_own_lineage(self):
        """Through the production writers: `Changeset.add` for the nodes (three repos, three
        labels) and `Build.put_data` keyed the way `buildhub.get` now keys it. The builds are
        the real 2026-08-11 / 08-26 ESR builds, which is the point: the three lines ship the
        same morning, so a lineage that read "the build before this one" across labels would
        pair 140.15.0esr with 115.40.0esr."""
        def node(rev, when):
            # The shape `pushlog.collect` hands `Changeset.add`: the author already analysed.
            return {"node": rev, "date": when, "backedout": False, "merge": False, "bug": 1,
                    "author": hgauthors.analyze_author("A Example <a@example.com>"),
                    "files": ["dom/base/nsGlobalWindowInner.cpp"]}

        d140_14 = datetime.datetime(2026, 8, 11, 19, 0, tzinfo=_UTC)
        d140_15 = datetime.datetime(2026, 8, 26, 14, 0, tzinfo=_UTC)
        d153_1 = datetime.datetime(2026, 8, 11, 20, 0, tzinfo=_UTC)
        d153_2 = datetime.datetime(2026, 8, 26, 2, 0, tzinfo=_UTC)
        d115_40 = datetime.datetime(2026, 8, 26, 3, 0, tzinfo=_UTC)
        end = datetime.datetime(2026, 9, 7, tzinfo=_UTC)
        models.Changeset.add([node("ee9f2b2aedc3", d140_14), node("1ace7e56a446", d140_15)],
                             end, "esr140")
        models.Changeset.add([node("bdb74c45c2e1", d153_1), node("92c5bf513a3e", d153_2)],
                             end, "esr153")
        models.Changeset.add([node("713124d101dc", d115_40)], end, "esr115")

        def bh(bid, rev, version):
            return {utils.get_build_date(bid): {"revision": rev, "version": version}}

        models.Build.put_data({"Firefox": {
            "esr140": {**bh("20260811190631", "ee9f2b2aedc3", "140.14.0esr"),
                       **bh("20260826142222", "1ace7e56a446", "140.15.0esr")},
            "esr153": {**bh("20260811201151", "bdb74c45c2e1", "153.1.0esr"),
                       **bh("20260826022508", "92c5bf513a3e", "153.2.0esr")},
            "esr115": bh("20260826035020", "713124d101dc", "115.40.0esr"),
        }})
        asof = datetime.datetime(2026, 9, 7, tzinfo=_UTC)
        versions = lambda rows: [r["version"] for r in rows]  # noqa: E731
        self.assertEqual(versions(models.Build.get_last_versions(asof, "esr140", "Firefox", n=3)),
                         ["140.15.0esr", "140.14.0esr"])
        self.assertEqual(versions(models.Build.get_last_versions(asof, "esr153", "Firefox", n=3)),
                         ["153.2.0esr", "153.1.0esr"])
        self.assertEqual(versions(models.Build.get_last_versions(asof, "esr115", "Firefox", n=3)),
                         ["115.40.0esr"])
        self.assertEqual(
            versions(models.Build.get_two_last(utils.get_build_date("20260826142222"),
                                               "esr140", "Firefox")),
            ["140.14.0esr", "140.15.0esr"])
        # The candidate window's lower bound is the line's own previous build -- not the
        # 153.2.0esr or 115.40.0esr build of the same morning, and None on a line's first build.
        self.assertEqual(models.Build.get_pushdate_before(
            utils.get_build_date("20260826142222"), "esr140", "Firefox"), d140_14)
        self.assertEqual(models.Build.get_pushdate_before(
            utils.get_build_date("20260826022508"), "esr153", "Firefox"), d153_1)
        self.assertIsNone(models.Build.get_pushdate_before(
            utils.get_build_date("20260826035020"), "esr115", "Firefox"))


if __name__ == "__main__":
    unittest.main()
