# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# The untriaged sweep recovers crashes the pipeline was never given at all — a queued agent job
# lives only in Redis, and this deployment's Redis has no persistence, so a restart drops the whole
# queue leaving NO dossier, no error and no log line. Prod 2026-08-12: 86 such crashes, 16 of them
# with an on-stack score (i.e. the run would certainly have written a dossier had it started).
#
# What these tests pin is the three bounds, because this job spends money unattended: each crash is
# offered at most once (the SweepMark cursor), a cap per tick, and a grace period so the sweep can
# never re-enqueue the live queue.
#
# The candidate-selection round-trips need a disposable Postgres (JSONB paths + on_conflict), as in
# tests/test_persistence.py:
#   docker run -d --rm --name clouseau_test_pg -e POSTGRES_USER=clouseau \
#       -e POSTGRES_PASSWORD=passwd -e POSTGRES_DB=clouseau_test -p 55432:5432 postgres
#   DATABASE_URL=postgresql://clouseau:passwd@localhost:55432/clouseau_test \
#       uv run python -m unittest tests.test_sweep_untriaged
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import config, db, models  # noqa: E402
from crashclouseau.agent import orchestrator as orch  # noqa: E402


def _is_postgres():
    try:
        return db.engine.dialect.name == "postgresql"
    except Exception:
        return False


class TestSweepBounds(unittest.TestCase):
    """Backend-agnostic: the control flow around the (mocked) candidate query."""

    def _sweep(self, candidates, live=(), mark=0, **cfg_over):
        cfg = dict(config.get_agent_sweep(), **cfg_over)
        seen = {}

        def set_mark(name, pos, **kw):
            seen["mark"] = pos

        with mock.patch.object(orch.config, "get_agent_sweep", return_value=cfg), \
                mock.patch.object(orch.models.SweepMark, "get", return_value=mark), \
                mock.patch.object(orch.models.SweepMark, "set", side_effect=set_mark), \
                mock.patch.object(orch.models.UUID, "untriaged", return_value=candidates) as q, \
                mock.patch.object(orch.worker, "get_queue"), \
                mock.patch.object(orch, "_live_job_uuids", return_value=set(live)), \
                mock.patch.object(orch, "enqueue_agent") as enq:
            n = orch.sweep_untriaged_crashes()
        return n, [c.args[0] for c in enq.call_args_list], seen, q

    def test_it_enqueues_each_candidate_on_the_right_channel(self):
        n, enqueued, seen, _ = self._sweep([(10, "u-a", "nightly"), (11, "u-b", "nightly")])
        self.assertEqual(n, 2)
        self.assertEqual(enqueued, ["u-a", "u-b"])
        # NOT forced: the proto gate must still get to refuse, so the sweep can never pay for a
        # cluster something else triaged in the meantime.
        self.assertEqual(seen["mark"], 11)

    def test_enqueue_is_not_forced(self):
        with mock.patch.object(orch.models.SweepMark, "get", return_value=0), \
                mock.patch.object(orch.models.SweepMark, "set"), \
                mock.patch.object(orch.models.UUID, "untriaged",
                                  return_value=[(1, "u-a", "nightly")]), \
                mock.patch.object(orch.worker, "get_queue"), \
                mock.patch.object(orch, "_live_job_uuids", return_value=set()), \
                mock.patch.object(orch, "enqueue_agent") as enq:
            orch.sweep_untriaged_crashes()
        self.assertNotIn("force", enq.call_args.kwargs)
        self.assertNotIn(True, enq.call_args.args[1:])

    def test_the_cursor_advances_past_everything_considered(self):
        # Including what the queue filter or the gate declined: they were examined, and
        # re-examining them next tick is exactly the loop the cursor exists to prevent.
        n, enqueued, seen, _ = self._sweep(
            [(10, "u-a", "nightly"), (11, "u-b", "nightly")], live=["u-b"]
        )
        self.assertEqual((n, enqueued), (1, ["u-a"]))
        self.assertEqual(seen["mark"], 11)

    def test_a_still_queued_candidate_is_not_enqueued_twice(self):
        n, enqueued, _, _ = self._sweep([(10, "u-a", "nightly")], live=["u-a"])
        self.assertEqual((n, enqueued), (0, []))

    def test_the_cursor_is_passed_to_the_query(self):
        _, _, _, q = self._sweep([(10, "u-a", "nightly")], mark=99)
        self.assertEqual(q.call_args.args[0], 99)

    def test_the_cap_is_passed_to_the_query(self):
        _, _, _, q = self._sweep([(10, "u-a", "nightly")], max_per_run=7)
        self.assertEqual(q.call_args.args[3], 7)

    def test_an_unreadable_queue_skips_the_pass_without_advancing(self):
        # Fails SAFE, like the reaper's pending sweep: not knowing whether a job is live must not
        # become a duplicate paid run. Leaving the cursor alone means nothing is lost — the next
        # tick reconsiders the same crashes.
        marked = {}
        with mock.patch.object(orch.models.SweepMark, "get", return_value=5), \
                mock.patch.object(orch.models.SweepMark, "set",
                                  side_effect=lambda *a, **k: marked.setdefault("set", a)), \
                mock.patch.object(orch.models.UUID, "untriaged",
                                  return_value=[(10, "u-a", "nightly")]), \
                mock.patch.object(orch.worker, "get_queue"), \
                mock.patch.object(orch, "_live_job_uuids", side_effect=RuntimeError("redis down")), \
                mock.patch.object(orch, "enqueue_agent") as enq:
            self.assertEqual(orch.sweep_untriaged_crashes(), 0)
        enq.assert_not_called()
        self.assertNotIn("set", marked)

    def test_disabled_does_nothing_and_does_not_query(self):
        with mock.patch.object(orch.config, "get_agent_sweep",
                               return_value=dict(config.get_agent_sweep(), enabled=False)), \
                mock.patch.object(orch.models.UUID, "untriaged") as q:
            self.assertEqual(orch.sweep_untriaged_crashes(), 0)
        q.assert_not_called()

    def test_no_candidates_leaves_the_cursor_alone(self):
        with mock.patch.object(orch.models.SweepMark, "get", return_value=5), \
                mock.patch.object(orch.models.UUID, "untriaged", return_value=[]), \
                mock.patch.object(orch.models.SweepMark, "set") as s:
            self.assertEqual(orch.sweep_untriaged_crashes(), 0)
        s.assert_not_called()

    def test_one_failing_enqueue_does_not_lose_the_others(self):
        with mock.patch.object(orch.models.SweepMark, "get", return_value=0), \
                mock.patch.object(orch.models.SweepMark, "set"), \
                mock.patch.object(orch.models.UUID, "untriaged",
                                  return_value=[(1, "u-a", "nightly"), (2, "u-b", "nightly")]), \
                mock.patch.object(orch.worker, "get_queue"), \
                mock.patch.object(orch, "_live_job_uuids", return_value=set()), \
                mock.patch.object(orch, "enqueue_agent",
                                  side_effect=[RuntimeError("boom"), None]):
            self.assertEqual(orch.sweep_untriaged_crashes(), 1)

    def test_a_query_failure_never_escapes_to_the_clock(self):
        with mock.patch.object(orch.models.SweepMark, "get", side_effect=RuntimeError("db down")):
            self.assertEqual(orch.sweep_untriaged_crashes(), 0)

    def test_the_clock_calls_it(self):
        # The job is useless unscheduled, and nothing else would notice.
        import ast
        with open(os.path.join(os.path.dirname(__file__), "..", "bin", "schedule.py")) as f:
            src = f.read()
        self.assertIn("sweep_untriaged_crashes", src)
        tree = ast.parse(src)
        funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        jobs = [n for n in funcs if "sweep_untriaged_crashes" in ast.dump(n) and n.decorator_list]
        self.assertTrue(jobs, "no SCHEDULED job calls sweep_untriaged_crashes")


class TestSweepConfig(unittest.TestCase):
    def test_defaults_are_bounded(self):
        cfg = config.get_agent_sweep()
        self.assertTrue(cfg["enabled"])
        self.assertGreater(cfg["max_per_run"], 0)
        # The grace period must exceed any realistic queue delay, or the sweep re-enqueues the
        # live queue: three workers at ~16min drain ~11/hour, and one ingestion batch can be
        # dozens of crashes.
        self.assertGreaterEqual(cfg["min_age_s"], 3600)
        self.assertGreater(cfg["max_age_s"], cfg["min_age_s"])

    def test_env_kill_switch(self):
        with mock.patch.dict(os.environ, {"AGENT_SWEEP": "0"}):
            self.assertFalse(config.get_agent_sweep()["enabled"])
        with mock.patch.dict(os.environ, {"AGENT_SWEEP": "1"}):
            self.assertTrue(config.get_agent_sweep()["enabled"])


@unittest.skipUnless(_is_postgres(), "candidate selection needs a disposable Postgres backend")
class TestUntriagedSelection(unittest.TestCase):
    """The query itself, against a real backend."""

    SIG = "Sweep::Signature"
    # Its own buildid + version marker so the fixture can clean up by identity: deleting the build
    # cascades to the uuids and their dossiers, which is the whole teardown. Needed because a
    # failing test would otherwise leave the build behind and every later setUp would collide on
    # uix_builds — one failure became ten.
    BUILDID = datetime(2026, 8, 11, 8, 53, tzinfo=timezone.utc)
    VERSION = "9.9.9swp"  # <=10 chars (builds.version)

    def setUp(self):
        models.create()
        self._clean()
        self.sig = models.Signature.get_id(self.SIG)
        self.build = models.Build(self.BUILDID, "Firefox", "nightly", self.VERSION, None)
        db.session.add(self.build)
        db.session.commit()

    def _clean(self):
        db.session.rollback()
        # By the unique key, not by the version marker: whatever a previous run left behind
        # collides on (buildid, product, channel), so that is what has to be cleared.
        db.session.query(models.Build).filter(
            models.Build.buildid == self.BUILDID,
            models.Build.product == "Firefox",
            models.Build.channel == "nightly",
        ).delete(synchronize_session=False)
        db.session.query(models.SweepMark).delete()
        db.session.commit()

    def tearDown(self):
        self._clean()

    def _uuid(self, name, proto="protoS", age_h=48, useless=False, analyzed=True):
        u = models.UUID(name, self.sig, proto, self.build.id)
        u.useless = useless
        u.analyzed = analyzed
        db.session.add(u)
        db.session.commit()
        # `created` has a server default, so backdating is a second write.
        db.session.query(models.UUID).filter(models.UUID.uuid == name).update(
            {"created": datetime.now(timezone.utc) - timedelta(hours=age_h)},
            synchronize_session=False,
        )
        db.session.commit()
        return db.session.query(models.UUID.id).filter(models.UUID.uuid == name).scalar()

    def _find(self, after=0, min_age_s=21600, max_age_s=1209600, limit=10):
        return models.UUID.untriaged(after, min_age_s, max_age_s, limit)

    def test_a_crash_with_no_dossier_is_a_candidate(self):
        uid = self._uuid("sweep-0001-aaaa-bbbb-ccccddddeeee")
        self.assertEqual([r[0] for r in self._find()], [uid])
        self.assertEqual(self._find()[0][2], "nightly")  # the channel the re-enqueue needs

    def test_a_crash_that_already_has_a_dossier_is_not(self):
        name = "sweep-0002-aaaa-bbbb-ccccddddeeee"
        self._uuid(name)
        for status in ("pending", "running", "done", "error"):
            models.Dossier.upsert(name, payload={}, status=status)
            self.assertEqual(self._find(), [], "a {} dossier must not be swept".format(status))

    def test_a_usably_triaged_cluster_is_not_swept(self):
        # The sibling closes the cluster: this is the whole point of the dedup, and the sweep
        # must agree with the gate about it.
        sib = "sweep-0003-aaaa-bbbb-ccccddddeeee"
        self._uuid(sib)
        uid = self._uuid("sweep-0004-aaaa-bbbb-ccccddddeeee")
        models.Dossier.upsert(sib, payload={"dossier": {"verdict": {"decision": "culprit"}}},
                              status="done")
        self.assertEqual(self._find(), [])
        # ...but a BROKEN sibling does not close it, exactly as proto_already_analyzed now says.
        models.Dossier.upsert(sib, payload={"dossier": {"verdict": {
            "decision": "abstain",
            "abstain_reason": "dossier validation failed (verdict unusable): 1 malformed field: x",
        }}}, status="done")
        self.assertEqual([r[0] for r in self._find()], [uid])
        # The two must not disagree — that is what sharing _cluster_dossiers buys.
        self.assertFalse(models.UUID.proto_already_analyzed(
            "sweep-0004-aaaa-bbbb-ccccddddeeee"))

    def test_an_instance_suppressed_sibling_does_not_close_the_cluster(self):
        sib = "sweep-0005-aaaa-bbbb-ccccddddeeee"
        self._uuid(sib)
        uid = self._uuid("sweep-0006-aaaa-bbbb-ccccddddeeee")
        models.Dossier.upsert(sib, payload={"dossier": {
            "verdict": {"decision": "abstain"},
            "corroborations": {"bad_machine_suppressed": True},
        }}, status="done")
        self.assertEqual([r[0] for r in self._find()], [uid])

    def test_a_different_cluster_is_unaffected_by_our_sibling(self):
        # The correlated subquery must compare against EACH candidate's own cluster; unaliased it
        # would match any done dossier at all and the sweep would find nothing, ever.
        sib = "sweep-0007-aaaa-bbbb-ccccddddeeee"
        self._uuid(sib, proto="protoA")
        other = self._uuid("sweep-0008-aaaa-bbbb-ccccddddeeee", proto="protoB")
        models.Dossier.upsert(sib, payload={"dossier": {"verdict": {"decision": "culprit"}}},
                              status="done")
        self.assertEqual([r[0] for r in self._find()], [other])

    def test_useless_and_unanalysed_crashes_are_skipped(self):
        self._uuid("sweep-0009-aaaa-bbbb-ccccddddeeee", useless=True)
        self._uuid("sweep-0010-aaaa-bbbb-ccccddddeeee", analyzed=False)
        self.assertEqual(self._find(), [])

    def test_the_grace_period_excludes_a_fresh_crash(self):
        # A crash whose job is merely QUEUED looks exactly like a lost one.
        self._uuid("sweep-0011-aaaa-bbbb-ccccddddeeee", age_h=1)
        self.assertEqual(self._find(min_age_s=21600), [])
        self.assertEqual(len(self._find(min_age_s=60)), 1)

    def test_an_old_crash_is_out_of_scope(self):
        self._uuid("sweep-0012-aaaa-bbbb-ccccddddeeee", age_h=24 * 40)
        self.assertEqual(self._find(max_age_s=1209600), [])

    def test_the_cursor_and_the_limit_page_through_in_id_order(self):
        a = self._uuid("sweep-0013-aaaa-bbbb-ccccddddeeee")
        b = self._uuid("sweep-0014-aaaa-bbbb-ccccddddeeee")
        self.assertEqual([r[0] for r in self._find(limit=1)], [a])
        self.assertEqual([r[0] for r in self._find(after=a)], [b])
        self.assertEqual(self._find(after=b), [])

    def test_channels_restrict_the_candidates(self):
        # The cap must be spent on crashes the agent will accept: three beta candidates would
        # otherwise fill a tick, be dropped by enqueue_agent, and starve the nightly ones.
        uid = self._uuid("sweep-0016-aaaa-bbbb-ccccddddeeee")
        self.assertEqual([r[0] for r in self._find()], [uid])
        self.assertEqual(
            models.UUID.untriaged(0, 21600, 1209600, 10, channels=["beta"]), []
        )
        self.assertEqual(
            [r[0] for r in models.UUID.untriaged(0, 21600, 1209600, 10, channels=["nightly"])],
            [uid],
        )

    def test_a_protoless_crash_is_skipped(self):
        # No protohash means no cluster to reason about; UUID.add would not have written one.
        self._uuid("sweep-0015-aaaa-bbbb-ccccddddeeee", proto=None)
        self.assertEqual(self._find(), [])


@unittest.skipUnless(_is_postgres(), "SweepMark round-trip needs a disposable Postgres backend")
class TestSweepMark(unittest.TestCase):
    def setUp(self):
        models.create()
        db.session.query(models.SweepMark).delete()
        db.session.commit()

    tearDown = setUp

    def test_unset_reads_as_zero(self):
        self.assertEqual(models.SweepMark.get("nope"), 0)

    def test_set_and_get(self):
        models.SweepMark.set("m", 42)
        self.assertEqual(models.SweepMark.get("m"), 42)

    def test_it_never_moves_backwards(self):
        # Two overlapping passes: a slow one finishing after a later tick must not rewind the
        # cursor and re-offer everything in between.
        models.SweepMark.set("m", 42)
        models.SweepMark.set("m", 7)
        self.assertEqual(models.SweepMark.get("m"), 42)
        models.SweepMark.set("m", 43)
        self.assertEqual(models.SweepMark.get("m"), 43)


if __name__ == "__main__":
    unittest.main()
