# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# DATABASE_URL=sqlite:// python -m unittest tests.test_agent
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from crashclouseau.agent import triage
from crashclouseau.agent.result import CrashTriageResult
from crashclouseau.agent.schema import Decision
from crashclouseau.agent.triage import build_options, build_result, run_crash_triage
from crashclouseau.vendor.hackbot_runtime.actions import ACTIONS_SERVER_NAME
from crashclouseau.vendor.hackbot_runtime.actions.recorder import ActionsRecorder
from crashclouseau.vendor.hackbot_runtime.errors import AgentError

_CRASH = {"uuid": "u-1", "signature": "Foo::Bar", "channel": "nightly"}

_SF = {
    "kind": "searchfox",
    "permalink": "https://searchfox.org/mozilla-central/source/foo.cpp#1",
    "symbol_id": "_ZN3Foo3BarEv",
    "repo": "mozilla-central",
}
_STRONG_DOSSIER = {
    "verdict": {
        "decision": "strong-evidence",
        "confidence": "high",
        "mechanism": {"statement": "UAF", "citations": [_SF]},
        "consistency": {"statement": "matches poison", "citations": [_SF]},
    }
}
_DOSSIER_JSON = json.dumps(_STRONG_DOSSIER)


def _result_msg(result, *, is_error=False, num_turns=3, cost=0.25, subtype="success"):
    return SimpleNamespace(
        result=result,
        is_error=is_error,
        num_turns=num_turns,
        total_cost_usd=cost,
        subtype=subtype,
    )


class TestBuildOptions(unittest.TestCase):
    def _opts(self, **kw):
        return build_options(_CRASH, searchfox_client=object(), **kw)

    def test_core_fields(self):
        o = self._opts()
        self.assertIn("Task", o.allowed_tools)
        for name in ("calls_from", "calls_to", "calls_between", "define", "lookup", "search"):
            self.assertIn(f"mcp__searchfox__{name}", o.allowed_tools)
        self.assertEqual(o.permission_mode, "bypassPermissions")
        self.assertEqual(o.setting_sources, [])
        self.assertIn("searchfox", o.mcp_servers)

    def test_principal_tiering(self):
        o = self._opts()
        self.assertEqual(o.model, "claude-opus-4-8")  # short "opus" -> full id
        self.assertEqual(o.max_turns, 40)
        self.assertEqual(getattr(o, "effort", None), "high")  # options-level

    def test_roles_registered_with_tiers(self):
        o = self._opts()
        self.assertEqual(
            set(o.agents),
            {"crash-interpreter", "call-graph-explorer", "patch-scout",
             "data-flow-tracer", "skeptic"},
        )
        # navigator is Sonnet (Phase-0 finding); seniors Haiku
        self.assertEqual(o.agents["call-graph-explorer"].model, "sonnet")
        self.assertEqual(o.agents["crash-interpreter"].model, "haiku")
        # subagents never get the Task tool (no recursion)
        self.assertNotIn("Task", o.agents["call-graph-explorer"].tools)

    def test_recorder_adds_actions_server(self):
        rec = ActionsRecorder(uploader=None)
        o = self._opts(recorder=rec)
        self.assertIn(ACTIONS_SERVER_NAME, o.mcp_servers)
        self.assertIn("mcp__actions__bugzilla_update_bug", o.allowed_tools)
        self.assertIn("mcp__actions__bugzilla_add_comment", o.allowed_tools)

    def test_no_actions_server_without_recorder(self):
        o = self._opts()
        self.assertNotIn(ACTIONS_SERVER_NAME, o.mcp_servers)

    def test_patch_server_wired(self):
        o = self._opts()
        self.assertIn("patch", o.mcp_servers)
        self.assertIn("mcp__patch__diff", o.allowed_tools)
        # patch-scout + data-flow-tracer get the deterministic patch tool; skeptic doesn't
        self.assertIn("mcp__patch__diff", o.agents["patch-scout"].tools)
        self.assertIn("mcp__patch__diff", o.agents["data-flow-tracer"].tools)
        self.assertNotIn("mcp__patch__diff", o.agents["skeptic"].tools)

    def test_user_prompt_lists_candidates(self):
        crash = dict(_CRASH, candidates=[
            {"node": "abc123def456", "score": 9, "bug": 111, "backedout": False},
            {"node": "ffff00001111", "score": 3, "bug": None, "backedout": True},
        ])
        p = triage._user_prompt(crash)
        self.assertIn("abc123def456", p)
        self.assertIn("score=9", p)
        self.assertIn("bug=111", p)
        self.assertIn("mcp__patch__diff", p)   # steer to the tool, not shelling
        self.assertIn("backed-out", p)

    def test_user_prompt_no_candidate_block_when_absent(self):
        self.assertNotIn("candidate changesets", triage._user_prompt(_CRASH))


class TestBuildResult(unittest.TestCase):
    def test_valid_handoff(self):
        msg = _result_msg("here\n```json\n" + _DOSSIER_JSON + "\n```", num_turns=4, cost=0.5)
        r = build_result(msg)
        self.assertIsInstance(r, CrashTriageResult)
        self.assertEqual(r.decision, Decision.strong_evidence)
        self.assertTrue(r.actionable)
        self.assertEqual(r.num_turns, 4)
        self.assertEqual(r.total_cost_usd, 0.5)

    def test_missing_block_abstains(self):
        r = build_result(_result_msg("no json here at all"))
        self.assertEqual(r.decision, Decision.abstain)
        self.assertTrue(r.dossier.verdict.abstain_reason)

    def test_none_raises(self):
        with self.assertRaises(AgentError):
            build_result(None)

    def test_is_error_raises(self):
        with self.assertRaises(AgentError):
            build_result(_result_msg("boom", is_error=True))

    def test_actions_captured(self):
        rec = ActionsRecorder(uploader=None)
        rec.record("bugzilla.update_bug", {"bug_id": 1, "changes": {}}, reasoning="x")
        r = build_result(_result_msg("no json"), recorder=rec)
        self.assertEqual(len(r.actions), 1)
        self.assertEqual(r.actions[0]["type"], "bugzilla.update_bug")


class _FakeSDKClient:
    def __init__(self, options=None):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt):
        self._prompt = prompt

    async def receive_response(self):
        yield _result_msg("done\n```json\n" + _DOSSIER_JSON + "\n```")


class _DummyReporter:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def header(self, *a):
        pass

    def message(self, *a):
        pass


class TestRunCrashTriage(unittest.TestCase):
    def test_drive_loop_builds_result(self):
        # Fake ResultMessage type so isinstance(msg, ResultMessage) holds.
        fake_type = type(_result_msg(""))
        with mock.patch.object(triage, "ClaudeSDKClient", _FakeSDKClient), \
             mock.patch.object(triage, "Reporter", _DummyReporter), \
             mock.patch.object(triage, "ResultMessage", fake_type), \
             mock.patch.object(triage, "build_options", return_value=object()):
            r = asyncio.run(run_crash_triage(crash=_CRASH))
        self.assertIsInstance(r, CrashTriageResult)
        self.assertEqual(r.decision, Decision.strong_evidence)
        self.assertEqual(r.num_turns, 3)


if __name__ == "__main__":
    unittest.main()
