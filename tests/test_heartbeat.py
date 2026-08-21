# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Run heartbeat: `Dossier.updated` must mean "last known alive", not "started", or an abandoned
# run cannot say how far it got (RQ's SIGKILL leaves no error and no cost).
# The thread + statement-shape checks run anywhere; the round-trip needs a disposable Postgres
# (the schema uses pg ARRAY/JSONB), matching tests/test_persistence.py. A throwaway one:
#   docker run -d --name clouseau-pgtest -e POSTGRES_USER=clouseau -e POSTGRES_PASSWORD=passwd \
#       -e POSTGRES_DB=clouseau_test -p 5433:5432 postgres:16
#   DATABASE_URL=postgresql://clouseau:passwd@127.0.0.1:5433/clouseau_test \
#       REDIS_URL=redis://localhost:6379/0 uv run python -m unittest tests.test_heartbeat
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import threading  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import db, models  # noqa: E402
from crashclouseau.agent import orchestrator as orch  # noqa: E402

# This module drives `run_evidence_agent` end to end, and one step of it is ONLINE:
# `_resolve_compiled_out` asks searchfox whether the mechanism's machinery is compiled into the
# build, and fetches the candidate's diff. Against this file's synthetic nodes and symbols those
# are pure cost -- a 404 with retries per run -- so the resolver is stubbed for the module. Its
# behaviour, AND the fact that `run_evidence_agent` really does call it, are covered by
# `tests/test_compiled_out_gate.py`, so stubbing it here cannot hide a wiring regression.
_NO_ONLINE_LOOKUP = None


def setUpModule():
    global _NO_ONLINE_LOOKUP
    _NO_ONLINE_LOOKUP = mock.patch.object(orch, "_resolve_compiled_out")
    _NO_ONLINE_LOOKUP.start()


def tearDownModule():
    _NO_ONLINE_LOOKUP.stop()


_UUID = "11111111-2222-3333-4444-555555602607"  # uuids.uuid is String(36)


def _is_postgres():
    try:
        return db.engine.dialect.name == "postgresql"
    except Exception:
        return False


class TestHeartbeatStatement(unittest.TestCase):
    """Backend-agnostic: the guard that keeps a late beat from resurrecting a settled run."""

    def test_it_updates_only_a_running_dossier(self):
        seen = {}

        class FakeResult:
            rowcount = 1

        def execute(stmt, *a, **kw):
            seen["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            return FakeResult()

        with mock.patch.object(models.UUID, "get_id", return_value=7), \
                mock.patch.object(db.session, "execute", execute), \
                mock.patch.object(db.session, "commit", lambda: None):
            self.assertTrue(models.Dossier.heartbeat(_UUID))
        sql = seen["sql"].lower()
        self.assertIn("update dossiers", sql)
        self.assertIn("set updated=", sql.replace(" =", "=").replace("= ", "="))
        # BOTH predicates, or a beat could touch a done/error row or the wrong dossier
        self.assertIn("status = 'running'", sql)
        self.assertIn("uuidid = 7", sql)

    def test_an_unknown_uuid_never_reaches_the_db(self):
        with mock.patch.object(models.UUID, "get_id", return_value=None), \
                mock.patch.object(db.session, "execute") as ex:
            self.assertFalse(models.Dossier.heartbeat(_UUID))
        ex.assert_not_called()

    def test_no_row_stamped_reports_false(self):
        class FakeResult:
            rowcount = 0

        with mock.patch.object(models.UUID, "get_id", return_value=7), \
                mock.patch.object(db.session, "execute", lambda *a, **kw: FakeResult()), \
                mock.patch.object(db.session, "commit", lambda: None):
            self.assertFalse(models.Dossier.heartbeat(_UUID))


@unittest.skipUnless(_is_postgres(), "round-trip needs a disposable Postgres backend")
class TestHeartbeatRoundTrip(unittest.TestCase):
    def setUp(self):
        db.create_all()
        # Minimal parent row; the dossier FK needs it (buildid/signatureid are nullable).
        db.session.add(models.UUID(_UUID, None, "hash", None))
        db.session.commit()

    def tearDown(self):
        # Deleting the parent cascades to dossiers.
        db.session.query(models.UUID).filter(models.UUID.uuid == _UUID).delete()
        db.session.commit()

    def test_it_stamps_a_running_dossier(self):
        models.Dossier.upsert(_UUID, payload={}, status="running")
        d = models.Dossier.get_by_uuid(_UUID)
        before = d.updated
        self.assertTrue(models.Dossier.heartbeat(_UUID))
        db.session.refresh(d)
        self.assertGreaterEqual(d.updated, before)

    def test_it_refuses_to_resurrect_a_settled_dossier(self):
        """The run may settle between two beats; a late beat must not touch it, or the reaper's
        view of that dossier would be corrupted."""
        for status in ("done", "error", "pending"):
            models.Dossier.upsert(_UUID, payload={}, status=status)
            self.assertFalse(models.Dossier.heartbeat(_UUID), status)


class TestHeartbeatThread(unittest.TestCase):
    def test_it_beats_while_the_body_runs_and_stops_after(self):
        beats = []
        started = threading.Event()

        def fake_heartbeat(uuid):
            beats.append(uuid)
            started.set()
            return True

        with mock.patch.object(orch, "_HEARTBEAT_INTERVAL_S", 0.01), \
                mock.patch.object(models.Dossier, "heartbeat", fake_heartbeat):
            with orch._heartbeat(_UUID):
                self.assertTrue(started.wait(5), "the heartbeat never beat")
            after = len(beats)
        self.assertGreater(after, 0)
        self.assertEqual(len(beats), after)   # stopped on exit

    def test_a_failing_beat_does_not_end_the_heartbeat(self):
        """A transient DB blip must not silently stop it — a heartbeat that dies while the run
        lives is what would let the reaper duplicate live work."""
        calls = []
        got_two = threading.Event()

        def flaky(uuid):
            calls.append(uuid)
            if len(calls) >= 2:
                got_two.set()
            raise RuntimeError("db blip")

        with mock.patch.object(orch, "_HEARTBEAT_INTERVAL_S", 0.01), \
                mock.patch.object(models.Dossier, "heartbeat", flaky):
            with orch._heartbeat(_UUID):
                self.assertTrue(got_two.wait(5), "the heartbeat stopped after its first failure")

    def test_the_thread_is_stopped_even_when_the_body_raises(self):
        with mock.patch.object(orch, "_HEARTBEAT_INTERVAL_S", 0.01), \
                mock.patch.object(models.Dossier, "heartbeat", lambda u: True):
            with self.assertRaises(ValueError):
                with orch._heartbeat(_UUID):
                    raise ValueError("boom")
        self.assertEqual([t for t in threading.enumerate() if t.name.startswith("hb-")], [])

    def test_the_first_beat_waits_out_the_interval(self):
        """`claim_running` already stamped `updated` at the start, so an immediate beat would be
        a wasted write on every single run."""
        beats = []
        with mock.patch.object(orch, "_HEARTBEAT_INTERVAL_S", 30), \
                mock.patch.object(models.Dossier, "heartbeat", lambda u: beats.append(u)):
            with orch._heartbeat(_UUID):
                pass
        self.assertEqual(beats, [])


class TestHeartbeatScope(unittest.TestCase):
    """WHERE the heartbeat starts and stops. It used to wrap only the agent call, leaving the
    tail — backout resolve, the ~$1 second opinion with its own API retries, the gates, the
    git-sha lookup, the writes — beating not at all. A live run that stops beating looks stale
    to the reaper, which is the one condition under which recovery can duplicate work in flight.
    """

    def _run_with_recorder(self):
        from contextlib import contextmanager

        events = []
        MDoss = mock.MagicMock()
        MDoss.get_by_uuid.return_value = None
        MDoss.skip_triage.return_value = False
        MDoss.claim_running.side_effect = lambda *a, **kw: (events.append("claim"), True)[1]
        MDoss.upsert.side_effect = lambda *a, **kw: events.append(
            "upsert:{}".format(kw.get("status"))
        )
        MVerd = mock.MagicMock()
        MVerd.set.side_effect = lambda *a, **kw: events.append("verdict")

        @contextmanager
        def recording_heartbeat(uuid):
            events.append("hb-start")
            try:
                yield
            finally:
                events.append("hb-stop")

        from tests.test_orchestrator import _SEED, _abstain_result, _triage_returning

        with mock.patch.object(orch.models, "Dossier", MDoss), \
                mock.patch.object(orch.models, "Verdict", MVerd), \
                mock.patch.object(orch.models, "commit"), \
                mock.patch.object(orch, "_proto_already_triaged", return_value=False), \
                mock.patch.object(orch, "build_seed", return_value=dict(_SEED)), \
                mock.patch.object(orch, "_seed_score", return_value=5), \
                mock.patch.object(orch, "_heartbeat", recording_heartbeat), \
                mock.patch.object(
                    orch, "_resolve_candidate_backout",
                    side_effect=lambda *a: events.append("backout")), \
                mock.patch.object(
                    orch, "_maybe_run_second_opinion",
                    side_effect=lambda *a: (events.append("second-opinion"), (None, None))[1]), \
                mock.patch("crashclouseau.agent.triage.run_crash_triage",
                           _triage_returning(_abstain_result())):
            orch.run_evidence_agent("u-1")
        return events

    def test_the_whole_run_beats_not_just_the_agent_call(self):
        events = self._run_with_recorder()
        self.assertIn("hb-start", events)
        start, stop = events.index("hb-start"), events.index("hb-stop")
        # Every step that can block for minutes, and the writes that settle the dossier,
        # must sit INSIDE the beating window.
        for step in ("backout", "second-opinion", "upsert:done", "verdict"):
            self.assertIn(step, events)
            self.assertTrue(
                start < events.index(step) < stop,
                "{} runs outside the heartbeat: {}".format(step, events),
            )

    def test_it_starts_only_after_the_claim_is_won(self):
        """A beat is guarded on status=running, so beating before we own the row would keep
        ANOTHER worker's orphan looking alive and hide it from the reaper."""
        events = self._run_with_recorder()
        self.assertLess(events.index("claim"), events.index("hb-start"), events)


if __name__ == "__main__":
    unittest.main()
