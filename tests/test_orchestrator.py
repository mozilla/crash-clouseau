# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# DATABASE_URL=sqlite:// python -m unittest tests.test_orchestrator
# (REDIS_URL is set below before importing worker; run_crash_triage is mocked, so
#  no Redis connection, SDK call, or CLI is ever made.)
import os

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau.agent import orchestrator as orch  # noqa: E402
from crashclouseau.agent.result import CrashTriageResult  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    Candidate,
    Claim,
    Confidence,
    Decision,
    Dossier,
    SearchfoxCitation,
    Verdict,
)

_SF = SearchfoxCitation(
    permalink="https://searchfox.org/x#1", symbol_id="_Z1", repo="mozilla-central"
)
_SEED = {"uuid": "u-1", "signature": "S", "channel": "nightly", "stack": "#0 f a:1"}


def _strong_result(cost=0.3):
    return CrashTriageResult(
        num_turns=5,
        total_cost_usd=cost,
        result="ok",
        dossier=Dossier(
            verdict=Verdict(
                decision=Decision.strong_evidence,
                confidence=Confidence.high,
                mechanism=Claim(statement="m", citations=[_SF]),
                consistency=Claim(statement="c", citations=[_SF]),
            )
        ),
    )


def _abstain_result():
    return CrashTriageResult(
        num_turns=2,
        total_cost_usd=0.1,
        result="ok",
        dossier=Dossier(
            verdict=Verdict(decision=Decision.abstain, abstain_reason="not enough")
        ),
    )


def _lead_result(cost=0.2):
    return CrashTriageResult(
        num_turns=4,
        total_cost_usd=cost,
        result="ok",
        dossier=Dossier(
            candidate=Candidate(node="abc123def456", bug=42),  # the lead anchor
            verdict=Verdict(
                decision=Decision.lead,
                confidence=Confidence.medium,
                needinfo_draft="could you take a look at this crash?",
            )
        ),
    )


def _triage_returning(result, record_action=False):
    async def _fake(*, crash, tools_cfg=None, llm_cfg=None, recorder=None, extra=None):
        if record_action and recorder is not None:
            # In production build_result folds the recorder's actions into
            # result.actions; the orchestrator persists result.actions (not the raw
            # recorder), so model that here by reflecting the record on the result.
            act = recorder.record(
                "bugzilla.update_bug", {"bug_id": 1, "changes": {}}, reasoning="x"
            )
            result.actions = [act]
        return result

    return _fake


async def _triage_boom(*, crash, tools_cfg=None, llm_cfg=None, recorder=None, extra=None):
    raise RuntimeError("triage exploded")


async def _triage_must_not_run(**kwargs):
    raise AssertionError("run_crash_triage should not have been called")


class TestEnqueueGating(unittest.TestCase):
    def test_disabled_is_noop(self):
        with mock.patch.object(orch.config, "get_agent_enabled", return_value=False), \
             mock.patch.object(orch.worker, "get_queue") as gq:
            orch.enqueue_agent("u-1")
        gq.assert_not_called()

    def test_enabled_enqueues(self):
        q = mock.MagicMock()
        with mock.patch.object(orch.config, "get_agent_enabled", return_value=True), \
             mock.patch.object(orch, "_proto_already_triaged", return_value=False), \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            orch.enqueue_agent("u-1")
        q.enqueue_call.assert_called_once()
        kwargs = q.enqueue_call.call_args.kwargs
        self.assertIs(kwargs["func"], orch.run_evidence_agent)
        self.assertEqual(kwargs["args"], ("u-1",))
        # RQ's enqueue_call takes `timeout`, not `job_timeout` — the wrong kwarg raised
        # TypeError and silently dropped every agent job. Lock the correct name + that
        # the value is passed (RQ's 180s default would kill a ~20-min triage).
        self.assertIn("timeout", kwargs)
        self.assertNotIn("job_timeout", kwargs)
        self.assertEqual(kwargs["timeout"], orch.config.get_agent_job_timeout())
        # And it must actually match rq.Queue.enqueue_call's real signature.
        import inspect
        import rq
        sig = inspect.signature(rq.Queue.enqueue_call)
        self.assertLessEqual(set(kwargs), set(sig.parameters))

    def test_force_bypasses_channel_and_proto(self):
        # A retrigger forces past both the channel gate and proto dedup, and tells
        # run_evidence_agent to re-run past its own guards (kwargs force=True).
        q = mock.MagicMock()
        with mock.patch.object(orch.config, "get_agent_enabled", return_value=True), \
             mock.patch.object(orch.config, "get_agent_channels", return_value=["nightly"]), \
             mock.patch.object(orch, "_proto_already_triaged", return_value=True), \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            orch.enqueue_agent("u-1", "beta", force=True)  # wrong channel + proto dup
        q.enqueue_call.assert_called_once()
        self.assertEqual(q.enqueue_call.call_args.kwargs["kwargs"], {"force": True})

    def test_non_nightly_channel_skipped(self):
        q = mock.MagicMock()
        with mock.patch.object(orch.config, "get_agent_enabled", return_value=True), \
             mock.patch.object(orch.config, "get_agent_channels", return_value=["nightly"]), \
             mock.patch.object(orch, "_proto_already_triaged", return_value=False), \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            orch.enqueue_agent("u-1", "beta")
            orch.enqueue_agent("u-1", "release")
        q.enqueue_call.assert_not_called()

    def test_nightly_channel_enqueues(self):
        q = mock.MagicMock()
        with mock.patch.object(orch.config, "get_agent_enabled", return_value=True), \
             mock.patch.object(orch.config, "get_agent_channels", return_value=["nightly"]), \
             mock.patch.object(orch, "_proto_already_triaged", return_value=False), \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            orch.enqueue_agent("u-1", "nightly")
        q.enqueue_call.assert_called_once()

    def test_proto_already_triaged_not_enqueued(self):
        q = mock.MagicMock()
        with mock.patch.object(orch.config, "get_agent_enabled", return_value=True), \
             mock.patch.object(orch.config, "get_agent_skip_if_existing", return_value=True), \
             mock.patch.object(orch, "_proto_already_triaged", return_value=True), \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            orch.enqueue_agent("u-1", "nightly")
        q.enqueue_call.assert_not_called()

    def test_proto_dedup_fails_open(self):
        # A DB error in the dedup check must NOT skip the crash (fail-open to enqueue).
        q = mock.MagicMock()
        with mock.patch.object(orch.config, "get_agent_enabled", return_value=True), \
             mock.patch.object(orch.models.UUID, "proto_already_analyzed",
                               side_effect=RuntimeError("db down")), \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            orch.enqueue_agent("u-1", "nightly")  # must not raise
        q.enqueue_call.assert_called_once()


class TestReaper(unittest.TestCase):
    def test_reap_reenqueues_stale_running(self):
        q = mock.MagicMock()
        with mock.patch.object(orch.models.Dossier, "get_stale_running",
                               return_value=["u1", "u2"]), \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            n = orch.reap_stale_agent_jobs()
        self.assertEqual(n, 2)
        self.assertEqual(q.enqueue_call.call_count, 2)
        kwargs = q.enqueue_call.call_args.kwargs
        self.assertIs(kwargs["func"], orch.run_evidence_agent)
        self.assertIn("timeout", kwargs)      # not job_timeout (RQ signature)

    def test_reap_noop_when_none(self):
        with mock.patch.object(orch.models.Dossier, "get_stale_running", return_value=[]), \
             mock.patch.object(orch.worker, "get_queue") as gq:
            self.assertEqual(orch.reap_stale_agent_jobs(), 0)
        gq.assert_not_called()

    def test_reap_never_raises(self):
        with mock.patch.object(orch.models.Dossier, "get_stale_running",
                               side_effect=RuntimeError("db down")):
            self.assertEqual(orch.reap_stale_agent_jobs(), 0)  # swallowed

    def test_reap_pushes_app_context_off_main_thread(self):
        # The clock runs the reaper on an APScheduler pool thread with no Flask app
        # context; the reaper must push one or its DB query raises. Run it on a fresh
        # thread and assert the DB call sees an app context.
        import threading
        import flask
        seen = {}

        def _fake_get_stale(stale):
            seen["ctx"] = flask.has_app_context()
            return []

        def _run():
            with mock.patch.object(orch.models.Dossier, "get_stale_running",
                                   side_effect=_fake_get_stale):
                orch.reap_stale_agent_jobs()

        t = threading.Thread(target=_run)
        t.start()
        t.join()
        self.assertTrue(seen.get("ctx"))  # reaper established an app context off-thread


class TestBuildSeed(unittest.TestCase):
    def test_none_for_unknown_uuid(self):
        with mock.patch.object(orch.models.CrashStack, "get_by_uuid", return_value=({}, {})):
            self.assertIsNone(orch.build_seed("u-x"))

    def test_none_when_no_changesets(self):
        res = {"frames": [{"stackpos": 0, "function": "f", "filename": "a.cpp",
                           "line": 1, "changesets": {}}]}
        with mock.patch.object(orch.models.CrashStack, "get_by_uuid", return_value=(res, {})):
            self.assertIsNone(orch.build_seed("u-x"))

    def test_seed_built(self):
        res = {"frames": [{"stackpos": 0, "function": "Foo::Bar", "filename": "a.cpp",
                           "line": 42, "changesets": {"abc": {"score": 3}}}]}
        with mock.patch.object(orch.models.CrashStack, "get_by_uuid", return_value=(res, {})), \
             mock.patch.object(orch.models.UUID, "get_info",
                               return_value={"signature": "Foo::Bar", "channel": "nightly",
                                             "product": "Firefox", "buildid": "x", "version": "1"}), \
             mock.patch("crashclouseau.inspector.get_crash_data", return_value={}):
            seed = orch.build_seed("u-1")
        self.assertEqual(seed["uuid"], "u-1")
        self.assertEqual(seed["signature"], "Foo::Bar")
        self.assertIn("Foo::Bar", seed["stack"])

    def test_seed_candidates_ranked_and_deduped(self):
        res = {"frames": [
            {"stackpos": 0, "function": "F", "filename": "a.cpp", "line": 1,
             "changesets": {"n1": {"score": 3, "bugid": 111, "backedout": False},
                            "n2": {"score": 9, "bugid": 222, "backedout": True}}},
            {"stackpos": 1, "function": "G", "filename": "b.cpp", "line": 2,
             "changesets": {"n1": {"score": 5, "bugid": 111, "backedout": False}}},
        ]}
        with mock.patch.object(orch.models.CrashStack, "get_by_uuid", return_value=(res, {})), \
             mock.patch.object(orch.models.UUID, "get_info",
                               return_value={"signature": "F", "channel": "nightly",
                                             "product": "Firefox", "buildid": "x", "version": "1"}), \
             mock.patch("crashclouseau.inspector.get_crash_data", return_value={}):
            seed = orch.build_seed("u-1")
        cands = seed["candidates"]
        self.assertEqual([c["node"] for c in cands], ["n2", "n1"])  # by score desc
        self.assertEqual(
            cands[0],
            {"node": "n2", "score": 9, "bug": 222, "backedout": True, "noise": False},
        )
        self.assertEqual(cands[1]["score"], 5)  # n1 deduped to its max score across frames

    def test_seed_downranks_anchor_frame_only_candidate(self):
        # A candidate supported ONLY by a universal anchor frame is down-ranked below a
        # lower-raw-score candidate on a real frame (#15 phase 3), never dropped.
        res = {"frames": [
            {"stackpos": 0, "function": "MessageLoop::Run", "filename": "ipc/x.cpp",
             "line": 1, "changesets": {"anchor": {"score": 100, "bugid": 1, "backedout": False}}},
            {"stackpos": 1, "function": "RealCode::doThing", "filename": "dom/y.cpp",
             "line": 2, "changesets": {"real": {"score": 5, "bugid": 2, "backedout": False}}},
        ]}
        with mock.patch.object(orch.models.CrashStack, "get_by_uuid", return_value=(res, {})), \
             mock.patch.object(orch.models.UUID, "get_info",
                               return_value={"channel": "nightly"}), \
             mock.patch.object(orch.models.Node, "authors_for", return_value={}), \
             mock.patch("crashclouseau.inspector.get_crash_data", return_value={}):
            seed = orch.build_seed("u-1")
        cands = {c["node"]: c for c in seed["candidates"]}
        self.assertTrue(cands["anchor"]["noise"])
        self.assertFalse(cands["real"]["noise"])
        # 100*0.1=10 > 5, so anchor still ranks first here — but it's tagged noise so
        # the agent/prompt down-ranks it; the raw score is preserved for fidelity.
        self.assertEqual(cands["anchor"]["score"], 100)
        self.assertEqual([c["node"] for c in seed["candidates"]], ["anchor", "real"])

    def test_candidate_on_real_and_anchor_frame_not_noise(self):
        # A node supported by BOTH an anchor frame and a real code frame is NOT tagged
        # noise (regression: the per-node all-noise fix), and keeps its max raw score.
        res = {"frames": [
            {"stackpos": 0, "function": "MessageLoop::Run", "filename": "ipc/x.cpp",
             "line": 1, "changesets": {"both": {"score": 100, "bugid": 1, "backedout": False}}},
            {"stackpos": 1, "function": "RealCode::doThing", "filename": "dom/y.cpp",
             "line": 2, "changesets": {"both": {"score": 5, "bugid": 1, "backedout": False}}},
        ]}
        with mock.patch.object(orch.models.CrashStack, "get_by_uuid", return_value=(res, {})), \
             mock.patch.object(orch.models.UUID, "get_info", return_value={"channel": "nightly"}), \
             mock.patch.object(orch.models.Node, "authors_for", return_value={}), \
             mock.patch("crashclouseau.inspector.get_crash_data", return_value={}):
            seed = orch.build_seed("u-1")
        both = {c["node"]: c for c in seed["candidates"]}["both"]
        self.assertFalse(both["noise"])
        self.assertEqual(both["score"], 100)

    def test_ubiquitous_symbol_frame_is_noise(self):
        # A frame whose FUNCTION is a ubiquitous primitive (not just its path) is noise.
        res = {"frames": [
            {"stackpos": 0, "function": "mozilla::HashMap<int>::lookup", "filename": "dom/z.cpp",
             "line": 1, "changesets": {"n": {"score": 9, "bugid": 1, "backedout": False}}},
        ]}
        with mock.patch.object(orch.models.CrashStack, "get_by_uuid", return_value=(res, {})), \
             mock.patch.object(orch.models.UUID, "get_info", return_value={"channel": "nightly"}), \
             mock.patch.object(orch.models.Node, "authors_for", return_value={}), \
             mock.patch("crashclouseau.inspector.get_crash_data", return_value={}):
            seed = orch.build_seed("u-1")
        self.assertTrue(seed["candidates"][0]["noise"])

    def test_seed_attaches_area_experts(self):
        res = {"frames": [
            {"stackpos": 0, "function": "F", "filename": "dom/a.cpp", "line": 1,
             "changesets": {"n1": {"score": 9, "bugid": 111, "backedout": False}}},
        ]}
        authors = {"n1": {"email": "dev@m.org", "real": "Dev", "nick": "d",
                          "bug": 111, "backedout": False}}
        with mock.patch.object(orch.models.CrashStack, "get_by_uuid", return_value=(res, {})), \
             mock.patch.object(orch.models.UUID, "get_info",
                               return_value={"channel": "nightly"}), \
             mock.patch.object(orch.models.Node, "authors_for", return_value=authors), \
             mock.patch("crashclouseau.inspector.get_crash_data", return_value={}):
            seed = orch.build_seed("u-1")
        self.assertEqual(len(seed["experts"]), 1)
        self.assertEqual(seed["experts"][0]["email"], "dev@m.org")
        self.assertIn("n1", seed["experts"][0]["reason"])


class TestRunEvidenceAgent(unittest.TestCase):
    def setUp(self):
        # Default: proto-signature not seen (so runs proceed). The dedicated dedup test
        # overrides this locally. Avoids these tests hitting the real DB-less query.
        p = mock.patch.object(orch, "_proto_already_triaged", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def _patches(self):
        MDoss = mock.MagicMock()
        MDoss.get_by_uuid.return_value = None
        MDoss.skip_triage.return_value = False  # not skipped (no dossier / fresh run)
        MDoss.claim_running.return_value = True  # this worker wins the atomic claim
        MVerd = mock.MagicMock()
        return (
            mock.patch.object(orch.models, "Dossier", MDoss),
            mock.patch.object(orch.models, "Verdict", MVerd),
            mock.patch.object(orch.models, "commit"),
            mock.patch.object(orch, "build_seed", return_value=dict(_SEED)),
            mock.patch.object(orch, "_seed_score", return_value=5),
            MDoss,
            MVerd,
        )

    def _done_upsert(self, MDoss):
        for c in MDoss.upsert.call_args_list:
            if c.kwargs.get("status") == "done":
                return c
        return None

    def test_happy_strong_persists_culprit(self):
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        with pD, pV, pC, pS, pSc, \
             mock.patch("crashclouseau.agent.triage.run_crash_triage",
                        _triage_returning(_strong_result())):
            orch.run_evidence_agent("u-1")
        self.assertIsNotNone(self._done_upsert(MDoss))
        MVerd.set.assert_called_once()
        self.assertEqual(MVerd.set.call_args.kwargs["verdict"], "culprit")
        self.assertEqual(MVerd.set.call_args.kwargs["confidence"], 85)

    def test_abstain_persists_abstain(self):
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        with pD, pV, pC, pS, pSc, \
             mock.patch("crashclouseau.agent.triage.run_crash_triage",
                        _triage_returning(_abstain_result())):
            orch.run_evidence_agent("u-1")
        self.assertEqual(MVerd.set.call_args.kwargs["verdict"], "abstain")

    def test_lead_persists_lead(self):
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        with pD, pV, pC, pS, pSc, \
             mock.patch("crashclouseau.agent.triage.run_crash_triage",
                        _triage_returning(_lead_result())):
            orch.run_evidence_agent("u-1")
        self.assertEqual(MVerd.set.call_args.kwargs["verdict"], "lead")
        self.assertEqual(MVerd.set.call_args.kwargs["confidence"], 50)

    def test_exception_isolation(self):
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        with pD, pV, pC, pS, pSc, \
             mock.patch("crashclouseau.agent.triage.run_crash_triage", _triage_boom):
            orch.run_evidence_agent("u-1")  # must not raise
        MDoss.set_status.assert_called_with("u-1", "error")
        MVerd.set.assert_not_called()

    def test_skip_if_existing(self):
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        MDoss.skip_triage.return_value = True  # already done / a fresh run in progress
        with pD, pV, pC, pS, pSc, \
             mock.patch("crashclouseau.agent.triage.run_crash_triage", _triage_must_not_run):
            orch.run_evidence_agent("u-1")  # must not raise (triage not called)
        MDoss.upsert.assert_not_called()

    def test_lost_atomic_claim_skips(self):
        # skip_triage passed (looked stale/absent) but another worker won the atomic
        # claim first -> this worker must NOT run (no double-pay).
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        MDoss.skip_triage.return_value = False
        MDoss.claim_running.return_value = False  # lost the race
        with pD, pV, pC, pS, pSc, \
             mock.patch.object(orch, "_proto_already_triaged", return_value=False), \
             mock.patch("crashclouseau.agent.triage.run_crash_triage", _triage_must_not_run):
            orch.run_evidence_agent("u-1")  # must not raise (triage not called)
        MVerd.set.assert_not_called()
        self.assertIsNone(self._done_upsert(MDoss))  # no "done" persisted

    def test_skip_if_proto_already_triaged(self):
        # No dossier for THIS uuid, but a proto-sibling was already triaged -> skip
        # (one paid run per proto-signature cluster, across builds).
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        MDoss.skip_triage.return_value = False  # this uuid not itself done/running
        with pD, pV, pC, pS, pSc, \
             mock.patch.object(orch, "_proto_already_triaged", return_value=True), \
             mock.patch("crashclouseau.agent.triage.run_crash_triage", _triage_must_not_run):
            orch.run_evidence_agent("u-1")  # must not raise (triage not called)
        MDoss.upsert.assert_not_called()

    def test_build_seed_none_skips(self):
        pD, pV, pC, _, pSc, MDoss, MVerd = self._patches()
        with pD, pV, pC, pSc, \
             mock.patch.object(orch, "build_seed", return_value=None), \
             mock.patch("crashclouseau.agent.triage.run_crash_triage", _triage_must_not_run):
            orch.run_evidence_agent("u-1")
        MDoss.upsert.assert_not_called()

    def test_over_budget_flagged_but_persists(self):
        # real cap is 2.0 (config); a $5 run is over budget.
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        with pD, pV, pC, pS, pSc, \
             mock.patch("crashclouseau.agent.triage.run_crash_triage",
                        _triage_returning(_strong_result(cost=5.0))):
            orch.run_evidence_agent("u-1")
        done = self._done_upsert(MDoss)
        self.assertIsNotNone(done)
        self.assertTrue(done.kwargs["payload"].get("over_budget"))

    def test_recorded_actions_persisted(self):
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        with pD, pV, pC, pS, pSc, \
             mock.patch("crashclouseau.agent.triage.run_crash_triage",
                        _triage_returning(_strong_result(), record_action=True)):
            orch.run_evidence_agent("u-1")
        done = self._done_upsert(MDoss)
        self.assertEqual(len(done.kwargs["payload"]["actions"]), 1)
        self.assertEqual(done.kwargs["payload"]["actions"][0]["type"], "bugzilla.update_bug")

    def test_tokens_persisted(self):
        # The aggregate token usage on the result is written to the done dossier
        # (previously never passed -> the tasks view always showed 0/0/0).
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        res = _strong_result()
        res.input_tokens, res.output_tokens, res.cache_read_tokens = 1234, 56, 7890
        with pD, pV, pC, pS, pSc, \
             mock.patch("crashclouseau.agent.triage.run_crash_triage", _triage_returning(res)):
            orch.run_evidence_agent("u-1")
        done = self._done_upsert(MDoss)
        self.assertEqual(done.kwargs["input_tokens"], 1234)
        self.assertEqual(done.kwargs["output_tokens"], 56)
        self.assertEqual(done.kwargs["cache_read_tokens"], 7890)

    def test_force_reruns_via_claim(self):
        # A retrigger (force=True) bypasses the skip_triage/proto EARLY-OUT but STILL goes
        # through the atomic claim (the concurrency guard); it does not unconditionally
        # upsert running. retrigger_agent resets the dossier to pending so the claim wins.
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        MDoss.skip_triage.return_value = True  # would normally skip
        MDoss.claim_running.return_value = True  # claimable (reset to pending)
        with pD, pV, pC, pS, pSc, \
             mock.patch.object(orch, "_proto_already_triaged", return_value=True), \
             mock.patch("crashclouseau.agent.triage.run_crash_triage",
                        _triage_returning(_strong_result())):
            orch.run_evidence_agent("u-1", force=True)
        MDoss.claim_running.assert_called_once()  # went through the guard
        self.assertIsNotNone(self._done_upsert(MDoss))
        MVerd.set.assert_called_once()

    def test_force_loser_of_claim_does_not_double_pay(self):
        # Two concurrent retriggers of one uuid: the job that loses claim_running must NOT
        # run a triage or persist -- this is what prevents the double-pay.
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        MDoss.claim_running.return_value = False  # lost the atomic claim
        with pD, pV, pC, pS, pSc, \
             mock.patch.object(orch, "_proto_already_triaged", return_value=True), \
             mock.patch("crashclouseau.agent.triage.run_crash_triage", _triage_must_not_run):
            orch.run_evidence_agent("u-1", force=True)  # must not run / not raise
        self.assertIsNone(self._done_upsert(MDoss))
        MVerd.set.assert_not_called()


class TestRetrigger(unittest.TestCase):
    def test_cancel_running_job_sends_stop(self):
        d = mock.MagicMock(status="running", payload={"job_id": "job-1"})
        with mock.patch.object(orch.models.Dossier, "get_by_uuid", return_value=d), \
             mock.patch("rq.command.send_stop_job_command") as stop:
            self.assertTrue(orch.cancel_running_job("u-1"))
        stop.assert_called_once()
        self.assertEqual(stop.call_args.args[1], "job-1")  # (connection, job_id)

    def test_cancel_noop_when_not_running(self):
        d = mock.MagicMock(status="done", payload={"job_id": "job-1"})
        with mock.patch.object(orch.models.Dossier, "get_by_uuid", return_value=d), \
             mock.patch("rq.command.send_stop_job_command") as stop:
            self.assertFalse(orch.cancel_running_job("u-1"))
        stop.assert_not_called()

    def test_cancel_noop_without_job_id(self):
        d = mock.MagicMock(status="running", payload={})
        with mock.patch.object(orch.models.Dossier, "get_by_uuid", return_value=d), \
             mock.patch("rq.command.send_stop_job_command") as stop:
            self.assertFalse(orch.cancel_running_job("u-1"))
        stop.assert_not_called()

    def test_retrigger_cancels_resets_then_force_enqueues(self):
        with mock.patch.object(orch, "cancel_running_job", return_value=True) as cxl, \
             mock.patch.object(orch.models.Dossier, "reset_for_retrigger") as rst, \
             mock.patch.object(orch, "enqueue_agent") as enq:
            out = orch.retrigger_agent("u-1")
        cxl.assert_called_once_with("u-1")
        rst.assert_called_once_with("u-1")  # reset so claim_running can re-take it
        enq.assert_called_once()
        self.assertTrue(enq.call_args.kwargs.get("force"))
        self.assertEqual(out, {"uuid": "u-1", "cancelled": True})


if __name__ == "__main__":
    unittest.main()
