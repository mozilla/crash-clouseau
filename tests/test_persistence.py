# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Schema-definition checks run anywhere (DATABASE_URL=sqlite://). The round-trip +
# cascade checks require a DISPOSABLE Postgres backend (pg.JSONB / pg.insert
# on_conflict / ON DELETE CASCADE are Postgres-only) and are skipped otherwise:
#   DATABASE_URL=postgresql://user@localhost/clouseau_test python -m unittest tests.test_persistence
import unittest

from crashclouseau import db, models
from crashclouseau.models import (
    AGENT_STATUS_TYPE,
    DOSSIER_SCHEMA_VERSION,
    Dossier,
    UUID,
    VERDICT_TYPE,
    Verdict,
)


def _is_postgres():
    try:
        return db.engine.dialect.name == "postgresql"
    except Exception:
        return False


class TestSchemaDefinition(unittest.TestCase):
    """Runs on any backend — guards the table/column/FK/DAO surface #11 depends on."""

    def test_tables_registered(self):
        self.assertIn("dossiers", db.metadata.tables)
        self.assertIn("verdicts", db.metadata.tables)

    def test_dossier_columns(self):
        cols = {c.name for c in db.metadata.tables["dossiers"].columns}
        self.assertEqual(
            cols,
            {"id", "uuidid", "schema_version", "payload", "status", "worker_models",
             "seed_score", "input_tokens", "output_tokens", "cache_read_tokens",
             "cost_usd", "created", "updated"},
        )

    def test_verdict_columns(self):
        cols = {c.name for c in db.metadata.tables["verdicts"].columns}
        self.assertEqual(
            cols,
            {"id", "uuidid", "dossierid", "verdict", "confidence", "principal_model",
             "rationale", "evidence", "effort", "created"},
        )

    def test_fk_cascade_to_uuids(self):
        for table in ("dossiers", "verdicts"):
            fks = [
                fk for fk in db.metadata.tables[table].foreign_keys
                if fk.target_fullname == "uuids.id"
            ]
            self.assertTrue(fks, f"{table} must FK to uuids.id")
            self.assertEqual(fks[0].ondelete, "CASCADE")

    def test_unique_uuidid(self):
        col = db.metadata.tables["dossiers"].columns["uuidid"]
        self.assertTrue(col.unique)

    def test_enum_values(self):
        self.assertEqual(set(VERDICT_TYPE.enums), {"culprit", "unrelated", "abstain", "error"})
        self.assertEqual(set(AGENT_STATUS_TYPE.enums), {"pending", "running", "done", "error"})
        self.assertEqual(DOSSIER_SCHEMA_VERSION, 1)

    def test_dao_surface(self):
        for m in ("upsert", "set_status", "add_usage", "get_by_uuid", "get_pending"):
            self.assertTrue(callable(getattr(Dossier, m)))
        for m in ("set", "get_by_uuid", "get_for_build"):
            self.assertTrue(callable(getattr(Verdict, m)))


@unittest.skipUnless(_is_postgres(), "round-trip/cascade need a disposable Postgres backend")
class TestPersistenceRoundTrip(unittest.TestCase):
    """Requires DATABASE_URL to point at a THROWAWAY Postgres DB — writes+deletes rows."""

    UUID = "test-0000-1111-2222-333344445555"

    def setUp(self):
        models.db.create_all()
        # Minimal parent row (buildid/signatureid are nullable FKs).
        db.session.add(UUID(self.UUID, None, "hash", None))
        db.session.commit()

    def tearDown(self):
        # Deleting the parent cascades to dossiers/verdicts.
        db.session.query(UUID).filter(UUID.uuid == self.UUID).delete()
        db.session.commit()

    def test_upsert_idempotent_and_get(self):
        Dossier.upsert(self.UUID, payload={"schema_version": 1, "x": 1}, status="running", seed_score=7)
        d1 = Dossier.get_by_uuid(self.UUID)
        self.assertEqual(d1.payload["x"], 1)
        self.assertEqual(d1.seed_score, 7)
        Dossier.upsert(self.UUID, payload={"schema_version": 1, "x": 2}, status="done")
        rows = db.session.query(Dossier).join(UUID, Dossier.uuidid == UUID.id).filter(UUID.uuid == self.UUID).all()
        self.assertEqual(len(rows), 1)  # single row, updated in place
        self.assertEqual(rows[0].payload["x"], 2)
        self.assertEqual(rows[0].status, "done")

    def test_status_and_usage(self):
        Dossier.upsert(self.UUID, payload={}, status="pending")
        Dossier.set_status(self.UUID, "running")
        self.assertEqual(Dossier.get_by_uuid(self.UUID).status, "running")
        Dossier.add_usage(self.UUID, input_tokens=10, output_tokens=3, cost_usd=0.02)
        Dossier.add_usage(self.UUID, input_tokens=5, output_tokens=1)
        d = Dossier.get_by_uuid(self.UUID)
        self.assertEqual(d.input_tokens, 15)
        self.assertEqual(d.output_tokens, 4)

    def test_verdict_set_get(self):
        Dossier.upsert(self.UUID, payload={})
        Verdict.set(self.UUID, "culprit", confidence=90, principal_model="claude-opus-4-8",
                    rationale="r", evidence=[{"kind": "searchfox"}], effort="high")
        v = Verdict.get_by_uuid(self.UUID)
        self.assertEqual(v.verdict, "culprit")
        self.assertEqual(v.confidence, 90)
        self.assertEqual(v.principal_model, "claude-opus-4-8")

    def test_cascade_on_parent_delete(self):
        Dossier.upsert(self.UUID, payload={})
        Verdict.set(self.UUID, "abstain")
        db.session.query(UUID).filter(UUID.uuid == self.UUID).delete()
        db.session.commit()
        self.assertIsNone(Dossier.get_by_uuid(self.UUID))
        self.assertIsNone(Verdict.get_by_uuid(self.UUID))


if __name__ == "__main__":
    unittest.main()
