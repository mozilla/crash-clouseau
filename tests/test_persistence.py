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

from crashclouseau import db, models, utils
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

    def test_a_new_table_must_be_registered_for_long_lived_dbs(self):
        # A TRIPWIRE, not a schema check. `models.create()` calls `create_all()` only when the
        # database is FRESH (no `lastdate` table), so a model added later exists in prod only if
        # `_ADDED_TABLES` names it — otherwise every read raises UndefinedTable forever, and any
        # caller with a defensive `except Exception` (the sweep has one) turns that into a silent
        # no-op that looks like "nothing to do". Nearly shipped exactly that with `sweepmarks`.
        # If this fails you added a model: decide whether prod needs the table created, add it to
        # `_ADDED_TABLES`, then add it here.
        known = {
            "archetypes", "builds", "changesets", "crashstack", "dossiers", "feedback",
            "files", "hgauthors", "lastdate", "nodes", "reviewnote", "scores", "selection",
            "signatures", "stats", "sweepmarks", "uuids", "verdicts",
        }
        self.assertEqual(
            set(db.metadata.tables) - known, set(),
            "new table(s) — add them to models._ADDED_TABLES, then to `known` here",
        )
        # And every post-deploy table named must actually exist as a model, or _ensure_tables
        # silently skips it (`name in db.Model.metadata.tables`).
        for name in models._ADDED_TABLES:
            self.assertIn(name, db.metadata.tables,
                          "_ADDED_TABLES names {!r}, which is not a model".format(name))

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
        # A real `builds` parent, not a NULL FK: the proto-signature cluster is scoped per
        # CHANNEL (`models._cluster_dossiers`) and the channel lives on `builds`, so a uuid
        # with no build row is outside every cluster. In production `update.put_crashes`
        # skips a crash whose buildid has no row, so `UUID.buildid` is never NULL there —
        # this fixture now has the production shape.
        from crashclouseau.models import Build

        self.build = Build(
            utils.get_build_date("20260817142839"), "Firefox", "nightly", "156.0a1", None
        )
        db.session.add(self.build)
        db.session.commit()
        db.session.add(UUID(self.UUID, None, "hash", self.build.id))
        db.session.commit()

    def tearDown(self):
        # Deleting the parent cascades to dossiers/verdicts.
        db.session.query(UUID).filter(UUID.uuid == self.UUID).delete()
        db.session.commit()
        db.session.query(models.Build).filter(models.Build.id == self.build.id).delete()
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

    def test_retrigger_clears_the_previous_failure(self):
        # The tasks view renders `error` for ANY status, so a stale one makes a queued
        # retrigger look like a live failure -- during a bulk recovery, like an outage
        # that is actually a queue draining normally.
        Dossier.upsert(self.UUID, payload={}, status="running")
        Dossier.set_status(self.UUID, "error", error="reaper gave up after 2 attempts")
        self.assertEqual(Dossier.get_by_uuid(self.UUID).payload["error"],
                         "reaper gave up after 2 attempts")
        Dossier.reset_for_retrigger(self.UUID)
        d = Dossier.get_by_uuid(self.UUID)
        self.assertEqual(d.status, "pending")
        self.assertNotIn("error", d.payload)

    def test_transient_retry_keeps_its_reason_on_a_pending_row(self):
        # The mirror case, and why this is fixed in reset_for_retrigger rather than by
        # hiding errors on pending rows in the view: `_should_retry` parks a run as
        # pending WITH the reason it is being requeued, and that one is current.
        Dossier.upsert(self.UUID, payload={}, status="running")
        Dossier.set_status(self.UUID, "pending", error="API error: Overloaded (529)")
        d = Dossier.get_by_uuid(self.UUID)
        self.assertEqual(d.status, "pending")
        self.assertEqual(d.payload["error"], "API error: Overloaded (529)")

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
        db.session.add(UUID(sib, None, "hash", self.build.id))        # same proto cluster
        db.session.add(UUID(other, None, "otherhash", self.build.id))  # different cluster
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

    def _broken_payload(self, reason):
        return {"dossier": {"verdict": {"decision": "abstain", "abstain_reason": reason}}}

    def test_proto_broken_run_does_not_close_the_cluster(self):
        # A DONE dossier that BROKE (no readable handoff / failed validation) examined
        # nothing, so it must not suppress the rest of its proto cluster — the bug that kept
        # 15 crashes unanalysed for a month. Bounded by agent.proto_max_unusable: the second
        # broken run in one cluster gives up rather than re-paying forever.
        sib = "test-7777-aaaa-bbbb-ccccddddeeee"
        sib2 = "test-6666-aaaa-bbbb-ccccddddeeee"
        db.session.add(UUID(sib, None, "hash", self.build.id))  # same cluster as self.UUID
        db.session.add(UUID(sib2, None, "hash", self.build.id))
        db.session.commit()
        try:
            for reason in (
                "dossier validation failed (verdict unusable): 1 malformed field: "
                "verdict.consistency",
                "dossier validation failed: 2 malformed fields: a.b, c.d",
                "no parseable ```json block in the agent result",
            ):
                Dossier.upsert(sib, payload=self._broken_payload(reason), status="done")
                self.assertFalse(
                    UUID.proto_already_analyzed(self.UUID),
                    "a broken run must not close the cluster: {}".format(reason),
                )
            # A REAL abstain does close it: the crash was examined and nothing was pinned on
            # anything. This is the line the fix must not cross.
            Dossier.upsert(
                sib,
                payload=self._broken_payload("no candidate touches the crashing frame"),
                status="done",
            )
            self.assertTrue(UUID.proto_already_analyzed(self.UUID))
            # ...as does a verdict with no abstain_reason at all (a culprit/lead). NULL must
            # read as usable, or every cluster closed by a successful run silently reopens.
            Dossier.upsert(
                sib,
                payload={"dossier": {"verdict": {"decision": "culprit"}}},
                status="done",
            )
            self.assertTrue(UUID.proto_already_analyzed(self.UUID))

            # The cap. One broken run in the cluster -> keep going (asserted above); a
            # second -> give up, so a stack that reliably breaks the schema costs 2 runs and
            # not one per crash forever.
            Dossier.upsert(
                sib,
                payload=self._broken_payload("dossier validation failed: 1 malformed field: x.y"),
                status="done",
            )
            self.assertFalse(UUID.proto_already_analyzed(self.UUID))
            Dossier.upsert(
                sib2,
                payload=self._broken_payload("no parseable ```json block in the agent result"),
                status="done",
            )
            self.assertTrue(UUID.proto_already_analyzed(self.UUID))
            # Cap 0 = retry without a bound, and must not mean "give up on the first one".
            with mock.patch.object(
                models.config, "get_agent_proto_max_unusable", return_value=0
            ):
                self.assertFalse(UUID.proto_already_analyzed(self.UUID))
        finally:
            db.session.query(UUID).filter(UUID.uuid.in_([sib, sib2])).delete(
                synchronize_session=False
            )
            db.session.commit()


class TestUnusableVerdictPrefixes(unittest.TestCase):
    """Needs no backend — the prefixes are literals, and that is exactly the risk."""

    def test_unusable_prefixes_match_the_agent_schema(self):
        # models.py cannot import agent.schema (pydantic / claude-agent-sdk must stay off the
        # web+ingestion import path), so the reasons it matches on are literals. Pin them to
        # the strings the agent actually writes, or a schema.py reword silently restores the
        # bug: every broken run would close its cluster again, and nothing would fail.
        from crashclouseau.agent import schema

        # Driven through the REAL parse path rather than compared against copies of the
        # format strings — a test that restates schema.py's literals pins nothing. Both
        # branches that can persist a Clouseau-side failure as `done`: no readable handoff,
        # and a handoff whose verdict did not survive validation. (The third branch,
        # "nothing salvageable", writes the same "dossier validation failed" prefix.)
        written = [
            schema.parse_and_validate("no json block in here at all"),
            schema.parse_and_validate({"verdict": "not-a-dict"}),
        ]
        for d in written:
            reason = d.verdict.abstain_reason
            self.assertTrue(
                reason.startswith(models._UNUSABLE_VERDICT_PREFIXES),
                "{!r} matches no prefix in _UNUSABLE_VERDICT_PREFIXES".format(reason),
            )
        # ...and a real analytical abstain must NOT match any of them.
        self.assertFalse(
            "no candidate touches the crashing frame".startswith(
                models._UNUSABLE_VERDICT_PREFIXES
            )
        )


if __name__ == "__main__":
    unittest.main()
