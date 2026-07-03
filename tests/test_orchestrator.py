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


def _triage_returning(result, record_action=False):
    async def _fake(*, crash, tools_cfg=None, llm_cfg=None, recorder=None, extra=None):
        if record_action and recorder is not None:
            recorder.record(
                "bugzilla.update_bug", {"bug_id": 1, "changes": {}}, reasoning="x"
            )
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
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            orch.enqueue_agent("u-1")
        q.enqueue_call.assert_called_once()
        kwargs = q.enqueue_call.call_args.kwargs
        self.assertIs(kwargs["func"], orch.run_evidence_agent)
        self.assertEqual(kwargs["args"], ("u-1",))


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
        self.assertEqual(cands[0], {"node": "n2", "score": 9, "bug": 222, "backedout": True})
        self.assertEqual(cands[1]["score"], 5)  # n1 deduped to its max score across frames


class TestRunEvidenceAgent(unittest.TestCase):
    def _patches(self):
        MDoss = mock.MagicMock()
        MDoss.get_by_uuid.return_value = None  # not skipped
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

    def test_exception_isolation(self):
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        with pD, pV, pC, pS, pSc, \
             mock.patch("crashclouseau.agent.triage.run_crash_triage", _triage_boom):
            orch.run_evidence_agent("u-1")  # must not raise
        MDoss.set_status.assert_called_with("u-1", "error")
        MVerd.set.assert_not_called()

    def test_skip_if_existing(self):
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        MDoss.get_by_uuid.return_value = object()  # a dossier already exists
        with pD, pV, pC, pS, pSc, \
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


if __name__ == "__main__":
    unittest.main()
