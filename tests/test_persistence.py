# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Schema-definition checks run anywhere (DATABASE_URL=sqlite://). The round-trip +
# cascade checks require a DISPOSABLE Postgres backend (pg.JSONB / pg.insert
# on_conflict / ON DELETE CASCADE are Postgres-only) and are skipped otherwise:
#   DATABASE_URL=postgresql://user@localhost/clouseau_test python -m unittest tests.test_persistence
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

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
        self.assertEqual(set(VERDICT_TYPE.enums), {"culprit", "lead", "unrelated", "abstain", "error"})
        self.assertEqual(set(AGENT_STATUS_TYPE.enums), {"pending", "running", "done", "error"})
        self.assertEqual(DOSSIER_SCHEMA_VERSION, 1)

    def test_dao_surface(self):
        for m in ("upsert", "set_status", "merge_payload", "add_usage", "get_by_uuid",
                  "get_pending"):
            self.assertTrue(callable(getattr(Dossier, m)))
        for m in ("set", "get_by_uuid", "get_for_build"):
            self.assertTrue(callable(getattr(Verdict, m)))


class _FakeDossier:
    def __init__(self, status, updated):
        self.status = status
        self.updated = updated


class TestSkipTriage(unittest.TestCase):
    """skip_triage: skip a done/error dossier or a FRESH running run; RETRY a stale
    running orphan (dead worker, e.g. dyno restart) and pending. get_by_uuid mocked."""

    STALE = 2100  # job_timeout(1800) + buffer(300)

    def _skip(self, dossier):
        with mock.patch.object(Dossier, "get_by_uuid", return_value=dossier):
            return Dossier.skip_triage("u", self.STALE)

    def test_no_dossier_runs(self):
        self.assertFalse(self._skip(None))

    def test_done_skips(self):
        self.assertTrue(self._skip(_FakeDossier("done", datetime.now(timezone.utc))))

    def test_error_skips(self):
        self.assertTrue(self._skip(_FakeDossier("error", datetime.now(timezone.utc))))

    def test_pending_runs(self):
        self.assertFalse(self._skip(_FakeDossier("pending", datetime.now(timezone.utc))))

    def test_fresh_running_skips(self):
        fresh = datetime.now(timezone.utc) - timedelta(seconds=60)
        self.assertTrue(self._skip(_FakeDossier("running", fresh)))

    def test_stale_running_retries(self):
        stale = datetime.now(timezone.utc) - timedelta(seconds=self.STALE + 60)
        self.assertFalse(self._skip(_FakeDossier("running", stale)))

    def test_naive_updated_treated_as_utc(self):
        stale = datetime.utcnow() - timedelta(seconds=self.STALE + 60)  # naive (sqlite)
        self.assertFalse(self._skip(_FakeDossier("running", stale)))

    def test_running_without_updated_skips(self):
        self.assertTrue(self._skip(_FakeDossier("running", None)))


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

    def test_merge_payload_keeps_the_other_keys(self):
        # The whole reason merge_payload exists rather than reusing upsert, which
        # REPLACES the payload wholesale. The failure path calls set_status (writing
        # `error`) and only then records the forensics, so a wholesale write would
        # delete the error string and the reaper's job_id/reap_attempts on its way past
        # — losing the diagnosis it was added to preserve.
        Dossier.upsert(self.UUID, payload={"job_id": "j1"}, status="running")
        Dossier.set_status(self.UUID, "error", error="no readable handoff")
        Dossier.merge_payload(self.UUID, {"result": "Waiting for the agents.",
                                          "num_turns": 17})
        p = Dossier.get_by_uuid(self.UUID).payload
        self.assertEqual(p["result"], "Waiting for the agents.")
        self.assertEqual(p["num_turns"], 17)
        self.assertEqual(p["error"], "no readable handoff")   # survived
        self.assertEqual(p["job_id"], "j1")                   # survived
        # None is skipped, not stored, so a caller can pass an optional field
        # unconditionally without writing a null over a real value.
        Dossier.merge_payload(self.UUID, {"num_turns": None, "result": "second"})
        p = Dossier.get_by_uuid(self.UUID).payload
        self.assertEqual(p["num_turns"], 17)
        self.assertEqual(p["result"], "second")

    def test_merge_payload_missing_row_is_a_noop(self):
        Dossier.merge_payload("no-such-uuid", {"result": "x"})  # must not raise

    def test_bump_reap_attempts_and_reset(self):
        # The reaper give-up counter lives in the JSONB payload (no migration). It
        # increments per reap and is cleared by an operator retrigger.
        Dossier.upsert(self.UUID, payload={"job_id": "j1"}, status="running")
        self.assertEqual(Dossier.bump_reap_attempts(self.UUID), 1)
        self.assertEqual(Dossier.bump_reap_attempts(self.UUID), 2)
        self.assertEqual(Dossier.get_by_uuid(self.UUID).payload["reap_attempts"], 2)
        Dossier.reset_for_retrigger(self.UUID)
        d = Dossier.get_by_uuid(self.UUID)
        self.assertEqual(d.status, "pending")
        self.assertNotIn("reap_attempts", d.payload)   # fresh give-up budget
        self.assertNotIn("job_id", d.payload)

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

    def test_map_for_build(self):
        # reports.html index tagging: {uuid -> {verdict, confidence}} for a build.
        from crashclouseau import utils
        from crashclouseau.models import Build
        sbid = "20260204094524"
        b = Build(utils.get_build_date(sbid), "Firefox", "nightly", "138.0a1", None)
        db.session.add(b)
        db.session.commit()
        u = "test-map0-aaaa-bbbb-ccccddddeeee"
        db.session.add(UUID(u, None, "protoZ", b.id))  # UUID.buildid = Build.id
        db.session.commit()
        try:
            # No verdict yet -> uuid absent (index unchanged without the agent).
            self.assertNotIn(u, Verdict.map_for_build(sbid, "Firefox", "nightly"))
            Dossier.upsert(u, payload={})
            Verdict.set(u, "lead", confidence=50)
            m = Verdict.map_for_build(sbid, "Firefox", "nightly")
            self.assertEqual(m[u]["verdict"], "lead")
            self.assertEqual(m[u]["confidence"], 50)
            # Scoped to the exact build/product/channel.
            self.assertNotIn(u, Verdict.map_for_build(sbid, "Firefox", "beta"))
        finally:
            db.session.query(UUID).filter(UUID.uuid == u).delete()
            db.session.query(Build).filter(Build.id == b.id).delete()
            db.session.commit()

    def test_get_stale_running(self):
        # A fresh running dossier isn't stale; backdate `updated` -> orphaned; a done
        # dossier is never stale-running.
        Dossier.upsert(self.UUID, payload={}, status="running")
        self.assertNotIn(self.UUID, Dossier.get_stale_running(1800))
        uid = db.session.query(UUID.id).filter(UUID.uuid == self.UUID).scalar()
        old = datetime.now(timezone.utc) - timedelta(seconds=4000)
        db.session.query(Dossier).filter(Dossier.uuidid == uid).update(
            {"updated": old}, synchronize_session=False
        )
        db.session.commit()
        self.assertIn(self.UUID, Dossier.get_stale_running(1800))
        Dossier.set_status(self.UUID, "done")  # bumps updated + status
        self.assertNotIn(self.UUID, Dossier.get_stale_running(1800))

    def test_claim_running_atomic(self):
        # First claim wins (creates a running dossier); an immediate second claim loses
        # (fresh running); a stale running is re-claimable; done is not.
        self.assertTrue(Dossier.claim_running(self.UUID, 1800))
        self.assertEqual(Dossier.get_by_uuid(self.UUID).status, "running")
        self.assertFalse(Dossier.claim_running(self.UUID, 1800))  # fresh -> not claimable
        uid = db.session.query(UUID.id).filter(UUID.uuid == self.UUID).scalar()
        old = datetime.now(timezone.utc) - timedelta(seconds=4000)
        db.session.query(Dossier).filter(Dossier.uuidid == uid).update(
            {"updated": old}, synchronize_session=False
        )
        db.session.commit()
        self.assertTrue(Dossier.claim_running(self.UUID, 1800))   # stale -> reclaimable
        Dossier.set_status(self.UUID, "done")
        self.assertFalse(Dossier.claim_running(self.UUID, 1800))  # done -> not claimable

    def test_proto_already_analyzed_dedup(self):
        # One paid agent run per proto-signature cluster: a dossier on ANY uuid sharing
        # this uuid's (signatureid, protohash) marks the whole cluster triaged (dedup
        # across builds — a different uuid, same proto-signature). self.UUID has
        # protohash "hash"; add a same-proto sibling and an unrelated (different proto).
        sib = "test-9999-aaaa-bbbb-ccccddddeeee"
        other = "test-8888-aaaa-bbbb-ccccddddeeee"
        db.session.add(UUID(sib, None, "hash", None))        # same proto cluster
        db.session.add(UUID(other, None, "otherhash", None))  # different cluster
        db.session.commit()
        try:
            # Nothing triaged yet anywhere.
            self.assertFalse(UUID.proto_already_analyzed(self.UUID))
            self.assertFalse(UUID.proto_already_analyzed(sib))
            self.assertFalse(UUID.proto_already_analyzed(other))
            # A non-DONE sibling (running/error) must NOT suppress the cluster — else a
            # single failed/stuck run poisons every other uuid in the proto cluster.
            Dossier.upsert(sib, payload={}, status="running")
            self.assertFalse(UUID.proto_already_analyzed(self.UUID))
            Dossier.set_status(sib, "error")
            self.assertFalse(UUID.proto_already_analyzed(self.UUID))
            # Only a DONE sibling marks the whole "hash" cluster triaged (incl. the
            # not-yet-run self.UUID); the different-proto crash stays unaffected.
            Dossier.set_status(sib, "done")
            self.assertTrue(UUID.proto_already_analyzed(self.UUID))
            self.assertTrue(UUID.proto_already_analyzed(sib))
            self.assertFalse(UUID.proto_already_analyzed(other))
            # An unknown uuid never blocks a first run.
            self.assertFalse(UUID.proto_already_analyzed("nope-not-a-uuid"))
        finally:
            db.session.query(UUID).filter(UUID.uuid.in_([sib, other])).delete(
                synchronize_session=False
            )
            db.session.commit()


if __name__ == "__main__":
    unittest.main()
