# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# The orphan reaper must actually RECOVER the run it re-enqueues.
#
# It did not, for ten days and 28 dossiers (0 recovered): `bump_reap_attempts` refreshed
# `Dossier.updated`, which is the very field the re-enqueued job reads back to decide whether the
# orphan is still owned by a live worker — so the reaper told the retry "someone is already on it"
# a fraction of a second before scheduling it, and every retry skipped itself until the give-up cap
# marked the crash `error`. These tests pin the two halves of that contract: the counter write must
# leave `updated` alone, and a bumped orphan must stay claimable.
#
# The statement-shape checks run anywhere; the round-trips need a disposable Postgres (the schema
# uses pg ARRAY/JSONB), matching tests/test_persistence.py. A throwaway one:
#   docker run -d --name clouseau-pgtest -e POSTGRES_USER=clouseau -e POSTGRES_PASSWORD=passwd \
#       -e POSTGRES_DB=clouseau_test -p 5433:5432 postgres:16
#   DATABASE_URL=postgresql://clouseau:passwd@127.0.0.1:5433/clouseau_test \
#       REDIS_URL=redis://localhost:6379/0 uv run python -m unittest tests.test_reaper_recovery
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import db, models  # noqa: E402
from crashclouseau.agent import orchestrator as orch  # noqa: E402

_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeee026073"  # uuids.uuid is String(36)


def _is_postgres():
    try:
        return db.engine.dialect.name == "postgresql"
    except Exception:
        return False


class TestBumpLeavesLivenessAlone(unittest.TestCase):
    """Backend-agnostic: the counter write must not touch the liveness clock."""

    def _bump_statement(self, payload):
        """Run bump_reap_attempts against a stubbed session; return (sql, returned_count)."""
        seen = {}
        row = mock.Mock(payload=payload)
        chain = mock.MagicMock()
        chain.filter.return_value.first.return_value = row

        def execute(stmt, *a, **kw):
            seen["sql"] = str(stmt)
            return mock.Mock(rowcount=1)

        with mock.patch.object(models.UUID, "get_id", return_value=7), \
                mock.patch.object(db.session, "query", return_value=chain), \
                mock.patch.object(db.session, "execute", execute), \
                mock.patch.object(db.session, "expire", lambda *a: None), \
                mock.patch.object(db.session, "commit", lambda: None):
            n = models.Dossier.bump_reap_attempts(_UUID)
        return seen.get("sql", ""), n

    def test_it_writes_updated_back_to_its_own_value(self):
        """The column carries onupdate=now(), so the statement has to name `updated`
        explicitly — leaving it out of the SET clause is what re-stamps it."""
        sql, _ = self._bump_statement({})
        norm = sql.lower().replace(" = ", "=").replace(" =", "=").replace("= ", "=")
        self.assertIn("update dossiers", norm)
        self.assertIn("updated=dossiers.updated", norm)

    def test_it_never_stamps_now(self):
        """The regression, stated directly: a bump that writes now() into `updated` makes
        every re-enqueued run skip itself as a duplicate."""
        sql, _ = self._bump_statement({})
        # Assert against a statement that EXISTS. The pre-fix code stamped `updated` through the
        # ORM and never called execute() at all, so a bare not-in would have been checking the
        # empty string — the test would have passed against the very regression it names.
        self.assertIn("update dossiers", sql.lower())
        self.assertNotIn("now()", sql.lower())
        self.assertNotIn("current_timestamp", sql.lower())

    def test_it_still_increments_and_returns_the_count(self):
        _, n = self._bump_statement({"reap_attempts": 2})
        self.assertEqual(n, 3)
        _, n = self._bump_statement({})
        self.assertEqual(n, 1)

    def test_an_unknown_uuid_never_reaches_the_db(self):
        with mock.patch.object(models.UUID, "get_id", return_value=None), \
                mock.patch.object(db.session, "execute") as ex:
            self.assertEqual(models.Dossier.bump_reap_attempts(_UUID), 0)
        ex.assert_not_called()

    def test_a_missing_dossier_never_reaches_the_db(self):
        chain = mock.MagicMock()
        chain.filter.return_value.first.return_value = None
        with mock.patch.object(models.UUID, "get_id", return_value=7), \
                mock.patch.object(db.session, "query", return_value=chain), \
                mock.patch.object(db.session, "execute") as ex:
            self.assertEqual(models.Dossier.bump_reap_attempts(_UUID), 0)
        ex.assert_not_called()


class TestGiveUpReason(unittest.TestCase):
    def test_it_reports_what_we_know_not_a_guess_at_oom(self):
        """The old reason asserted "likely OOM/stall on every run" and sent the
        investigation after memory limits while the reaper was the actual cause."""
        q = mock.MagicMock()
        with mock.patch.object(orch.models.Dossier, "get_stale_running",
                               return_value=["orphan"]), \
             mock.patch.object(orch.models.Dossier, "get_stale_pending", return_value=[]), \
             mock.patch.object(orch.models.Dossier, "bump_reap_attempts", return_value=3), \
             mock.patch.object(orch.config, "get_agent_reap_max_attempts", return_value=2), \
             mock.patch.object(orch.models.Dossier, "set_status") as set_status, \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            orch.reap_stale_agent_jobs()
        reason = set_status.call_args.kwargs["error"]
        self.assertNotIn("OOM", reason)
        self.assertIn("heartbeat", reason)
        self.assertIn("retrigger", reason)


class TestRecoveryIsCountable(unittest.TestCase):
    """A recovered run has to SAY it was recovered. ``upsert`` replaces ``payload`` wholesale, so
    unless the run carries the inherited counter forward, finishing successfully ERASES it — the
    counter then survives only on runs that failed, and the reaper's recovery rate reads 0%
    forever. That is what made "0 of 28 reaped dossiers ever reached done" a tautology rather
    than a measurement, on a reaper that was in fact broken for an unrelated reason.
    """

    def _finished_payload(self, inherited):
        from contextlib import contextmanager

        from tests.test_orchestrator import _SEED, _abstain_result, _triage_returning

        seen = {}
        MDoss = mock.MagicMock()
        MDoss.get_by_uuid.return_value = None
        MDoss.skip_triage.return_value = False
        MDoss.claim_running.return_value = True
        MDoss.get_reap_attempts.return_value = inherited
        # Only the settling write matters; the non-dedup path also upserts at run START.
        MDoss.upsert.side_effect = lambda *a, **kw: (
            seen.update(kw) if kw.get("status") == "done" else None
        )

        @contextmanager
        def noop_heartbeat(uuid):
            yield

        with mock.patch.object(orch.models, "Dossier", MDoss), \
                mock.patch.object(orch.models, "Verdict", mock.MagicMock()), \
                mock.patch.object(orch.models, "commit"), \
                mock.patch.object(orch, "_proto_already_triaged", return_value=False), \
                mock.patch.object(orch, "build_seed", return_value=dict(_SEED)), \
                mock.patch.object(orch, "_seed_score", return_value=5), \
                mock.patch.object(orch, "_heartbeat", noop_heartbeat), \
                mock.patch.object(orch, "_resolve_candidate_backout"), \
                mock.patch.object(orch, "_maybe_run_second_opinion", return_value=(None, None)), \
                mock.patch("crashclouseau.agent.triage.run_crash_triage",
                           _triage_returning(_abstain_result())):
            orch.run_evidence_agent("u-1")
        return seen.get("payload") or {}

    def test_a_recovered_run_keeps_the_counter(self):
        self.assertEqual(self._finished_payload(2).get("reap_attempts"), 2)

    def test_a_clean_run_does_not_invent_one(self):
        self.assertNotIn("reap_attempts", self._finished_payload(0))


@unittest.skipUnless(_is_postgres(), "round-trip needs a disposable Postgres backend")
class TestOrphanStaysClaimable(unittest.TestCase):
    """The end-to-end proof, and the test that would have caught the regression: after the
    reaper counts an attempt, the run it just scheduled must still be able to take the row."""

    STALE_AFTER = 2100

    def setUp(self):
        db.create_all()
        # Minimal parent row; the dossier FK needs it (buildid/signatureid are nullable).
        db.session.add(models.UUID(_UUID, None, "hash", None))
        db.session.commit()

    def tearDown(self):
        # Deleting the parent cascades to dossiers.
        db.session.query(models.UUID).filter(models.UUID.uuid == _UUID).delete()
        db.session.commit()

    def _orphan(self, age_s):
        """A dossier stuck `running` with its last beat `age_s` ago."""
        models.Dossier.upsert(_UUID, payload={}, status="running")
        old = datetime.now(timezone.utc) - timedelta(seconds=age_s)
        db.session.execute(
            db.update(models.Dossier)
            .where(models.Dossier.uuidid == models.UUID.get_id(_UUID))
            .values(updated=old)
        )
        db.session.commit()
        return old

    def test_a_bumped_orphan_is_still_stale(self):
        old = self._orphan(self.STALE_AFTER + 600)
        self.assertEqual(models.Dossier.bump_reap_attempts(_UUID), 1)
        d = models.Dossier.get_by_uuid(_UUID)
        self.assertEqual(d.payload.get("reap_attempts"), 1)
        upd = d.updated
        if upd.tzinfo is None:
            upd = upd.replace(tzinfo=timezone.utc)
        self.assertAlmostEqual(
            (upd - old).total_seconds(), 0, delta=1,
            msg="the bump refreshed `updated`, so the retry will skip itself",
        )

    def test_the_reenqueued_run_can_still_claim_it(self):
        self._orphan(self.STALE_AFTER + 600)
        models.Dossier.bump_reap_attempts(_UUID)
        # Exactly what run_evidence_agent asks, in order.
        self.assertFalse(
            models.Dossier.skip_triage(_UUID, self.STALE_AFTER),
            "the retry skipped itself: the orphan looks like a live run",
        )
        self.assertTrue(
            models.Dossier.claim_running(_UUID, self.STALE_AFTER),
            "the retry lost the claim on a dead run",
        )

    def test_get_reap_attempts_reads_what_the_bump_wrote(self):
        self._orphan(self.STALE_AFTER + 600)
        self.assertEqual(models.Dossier.get_reap_attempts(_UUID), 0)
        models.Dossier.bump_reap_attempts(_UUID)
        self.assertEqual(models.Dossier.get_reap_attempts(_UUID), 1)

    def test_the_settling_write_erases_it_unless_carried(self):
        """The mechanism, pinned: a plain upsert of a finished payload drops the counter. This
        is why run_evidence_agent reads it at the claim and puts it back."""
        self._orphan(self.STALE_AFTER + 600)
        models.Dossier.bump_reap_attempts(_UUID)
        models.Dossier.upsert(_UUID, payload={"verdict": "abstain"}, status="done")
        self.assertEqual(models.Dossier.get_reap_attempts(_UUID), 0)
        # ...and survives when the caller carries it, which is what the orchestrator does.
        models.Dossier.upsert(
            _UUID, payload={"verdict": "abstain", "reap_attempts": 1}, status="done"
        )
        self.assertEqual(models.Dossier.get_reap_attempts(_UUID), 1)

    def test_the_counter_survives_several_attempts(self):
        self._orphan(self.STALE_AFTER + 600)
        self.assertEqual(models.Dossier.bump_reap_attempts(_UUID), 1)
        self.assertEqual(models.Dossier.bump_reap_attempts(_UUID), 2)
        self.assertFalse(models.Dossier.skip_triage(_UUID, self.STALE_AFTER))

    def test_a_fresh_run_is_still_protected(self):
        """The other half: a run that IS beating must not be claimable, or the reaper
        would duplicate live work."""
        self._orphan(10)
        models.Dossier.bump_reap_attempts(_UUID)
        self.assertTrue(models.Dossier.skip_triage(_UUID, self.STALE_AFTER))
        self.assertFalse(models.Dossier.claim_running(_UUID, self.STALE_AFTER))


if __name__ == "__main__":
    unittest.main()
