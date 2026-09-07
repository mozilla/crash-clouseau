# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""Postgres-only: the enum migration really adds a label, and a widened column really widens.

    DATABASE_URL=postgresql://... CLOUSEAU_THROWAWAY_DB=1 REDIS_URL=redis://localhost:6379/0 \\
        uv run python -m unittest tests.test_enum_migration_pg

Needs a real Postgres: sqlite renders `db.Enum` as VARCHAR and does not enforce a VARCHAR width,
so there is nothing to migrate there. The class DROPS AND RECREATES THE SCHEMA, so it refuses to
run unless `CLOUSEAU_THROWAWAY_DB=1` says the database is disposable (a `pgserver` instance from
PyPI does; `DATABASE_URL` alone is not consent).

What it proves. (1) `models._ensure_enum_values` had NEVER added a value: it re-used the connection
whose `SELECT ... pg_enum` had autobegun a transaction, SQLAlchemy raised on the AUTOCOMMIT switch,
and the `except` logged a warning -- so `lead` reached production only because that DB was created
after the value was in the enum. A DB created with an enum missing a label the config has now
gains it on `models.create()`, idempotently, and the label is usable. (2) `builds.version` was
VARCHAR(10), which no version string of the current channels exceeds and the next channel's do;
`models.create()` on a long-lived DB now widens it (`_WIDENED_COLUMNS`).
"""
import datetime
import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from sqlalchemy import inspect, text  # noqa: E402

from crashclouseau import config, db, models  # noqa: E402

_OLD_CHANNELS = ("nightly", "beta")   # an enum created before a label the config has today
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
        # A database created before a label existed: every table gone, and the channel enum
        # short of one. `create_all` keeps an existing type (checkfirst), so the tables come
        # back bound to the OLD enum and only `_ensure_enum_values` can grow it.
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

    def test_the_stale_enum_gained_the_missing_labels(self):
        self.assertTrue(self.fresh)
        missing = [c for c in config.get_channels() if c not in _OLD_CHANNELS]
        self.assertTrue(missing)
        self.assertEqual(_labels("CHANNEL_TYPE"), list(_OLD_CHANNELS) + missing)
        self.assertIn("lead", _labels("VERDICT_TYPE"))

    def test_the_migration_is_idempotent(self):
        before = _labels("CHANNEL_TYPE")
        models._ensure_enum_values()
        models._ensure_enum_values()
        self.assertEqual(_labels("CHANNEL_TYPE"), before)

    def test_the_new_label_is_usable(self):
        now = datetime.datetime.now(_UTC)
        label = [c for c in config.get_channels() if c not in _OLD_CHANNELS][-1]
        models.LastDate.update(now, now, label)
        _, maxdate = models.LastDate.get(label)
        self.assertIsNotNone(maxdate)


if __name__ == "__main__":
    unittest.main()
