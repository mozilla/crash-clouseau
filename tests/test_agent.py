# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# DATABASE_URL=sqlite:// python -m unittest tests.test_agent
import asyncio
import json
import mmap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import claude_agent_sdk

from crashclouseau.agent import triage
from crashclouseau.agent.errors import MissingHandoffError
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
# A REAL abstain: the model spoke and declined, inside the mandated fence. Distinct from
# "no fence at all", which build_result now treats as a failed run.
_ABSTAIN_JSON = json.dumps(
    {"verdict": {"decision": "abstain",
                 "abstain_reason": "nothing credible in the window"}}
)


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
        self.assertEqual(o.model, "claude-sonnet-5")  # config default; short "sonnet" -> full id
        self.assertEqual(o.max_turns, 60)  # raised 40->60: prod cases need more turns (curl-based history)
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

    def test_subagents_pinned_inline_by_cli_env(self):
        """THE assertion that keeps the fan-out inline. The CLI backgrounds a subagent
        unless the MODEL passes ``run_in_background: false`` -- the launch predicate only
        ever tests ``V.background === true``, so the AgentDefinition can force
        backgrounding on but never off. This env var is the only global off-switch (the
        ``&& !U`` term). A backgrounded fan-out makes the principal end its turn on a
        progress note, which used to reach ``parse_and_validate`` as the final handoff:
        one silent abstain per run, no error anywhere. See ``triage._CLI_ENV``.

        Asserted as the literal ``"1"``, not truthiness: the CLI parses the value through
        a typed boolean that accepts only 1/true/yes/on, so ``"0"`` (or any other string)
        silently leaves backgrounding ENABLED."""
        self.assertEqual(
            self._opts().env.get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"), "1")

    def test_cli_env_does_not_clobber_the_process_env(self):
        # options.env is MERGED over os.environ by the SDK (options.env wins), so this
        # must stay a one-key dict -- anything else would drop ANTHROPIC_API_KEY / PATH
        # from the subprocess. Also: a copy per call, so a caller mutating options.env
        # cannot poison the module constant for every later run.
        o1, o2 = self._opts(), self._opts()
        self.assertEqual(set(o1.env), {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"})
        o1.env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "0"
        self.assertEqual(o2.env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"], "1")
        self.assertEqual(triage._CLI_ENV["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"], "1")

    def test_roles_declare_background_false(self):
        """Kept as the statement of intent, NOT as the control: the CLI drops
        ``background: false`` at parse time (a truthy conditional spread) and would
        ignore it anyway. If it ever starts honouring the field, ``False`` is the value
        we want. See the long comment in ``roles.make_role``."""
        for name, agent in self._opts().agents.items():
            self.assertIs(agent.background, False, name)

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
        # patch-scout, data-flow-tracer, and skeptic get the deterministic patch tool.
        self.assertIn("mcp__patch__diff", o.agents["patch-scout"].tools)
        self.assertIn("mcp__patch__diff", o.agents["data-flow-tracer"].tools)
        self.assertIn("mcp__patch__diff", o.agents["skeptic"].tools)

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
        # The seed flag means the changeset IS ITSELF a backout, not that it WAS backed out;
        # the old "backed-out" label invited exactly that misreading.
        self.assertIn("is-itself-a-backout-commit", p)
        self.assertNotIn("score=3 backed-out", p)
        self.assertIn("worth a human's time", p)   # triage-worthiness mission framing
        self.assertIn("not as a closed world", p)

    def test_user_prompt_includes_compact_crash_facts(self):
        crash = dict(
            _CRASH,
            product="Firefox",
            buildid="20260708000000",
            version="129.0a1",
            raw_crash={
                "reason": "EXCEPTION_ACCESS_VIOLATION_READ",
                "moz_crash_reason": "MOZ_DIAGNOSTIC_ASSERT(mThing)",
                "json_dump": {
                    "crash_info": {
                        "type": "EXCEPTION_ACCESS_VIOLATION_READ",
                        "address": "0x0",
                        "crashing_thread": 0,
                        "assertion": "mThing",
                        "phc_alloc_stack": False,
                    }
                },
            },
        )
        p = triage._user_prompt(crash)
        self.assertIn("Crash facts:", p)
        self.assertIn("Product: Firefox", p)
        self.assertIn("Build ID: 20260708000000", p)
        self.assertIn("Crash type: EXCEPTION_ACCESS_VIOLATION_READ", p)
        self.assertIn("Fault address: 0x0", p)
        self.assertIn("Crashing thread: 0", p)
        self.assertIn("MOZ_CRASH_REASON: MOZ_DIAGNOSTIC_ASSERT(mThing)", p)
        self.assertIn("PHC alloc stack: False", p)

    def test_user_prompt_no_candidate_block_when_absent(self):
        self.assertNotIn("candidate changesets", triage._user_prompt(_CRASH))

    def test_crash_facts_include_os_cpu_process_gpu(self):
        crash = dict(_CRASH, raw_crash={
            "os_name": "Windows NT", "os_version": "10.0.19045",
            "os_pretty_version": "Windows 10", "cpu_arch": "amd64", "process_type": "gpu",
            "adapter_vendor_id": "0x10de", "adapter_device_id": "0x2504",
            "adapter_driver_version": "31.0.15.3141",
            "json_dump": {"crash_info": {"type": "EXCEPTION_ACCESS_VIOLATION_READ"}},
        })
        facts = triage._crash_facts(crash)
        blob = "\n".join(facts)
        self.assertIn("OS: Windows 10", blob)
        self.assertIn("CPU arch: amd64", blob)
        self.assertIn("Process type: gpu", blob)
        self.assertIn("GPU: NVIDIA device 0x2504 driver 31.0.15.3141", blob)  # vendor id mapped

    def test_crash_facts_env_omitted_when_absent(self):
        # a processed crash without env fields simply omits those facts (no crash)
        facts = triage._crash_facts({"raw_crash": {"json_dump": {"crash_info": {"type": "x"}}}})
        labels = [f.split(":")[0] for f in facts]
        self.assertNotIn("OS", labels)
        self.assertNotIn("GPU", labels)
        self.assertEqual(triage._gpu_summary({}), "")
        self.assertEqual(triage._gpu_summary({"adapter_vendor_id": "0xABCD"}), "0xABCD")  # unknown passes through


class TestBuildResult(unittest.TestCase):
    def test_valid_handoff(self):
        msg = _result_msg("here\n```json\n" + _DOSSIER_JSON + "\n```", num_turns=4, cost=0.5)
        r = build_result(msg)
        self.assertIsInstance(r, CrashTriageResult)
        self.assertEqual(r.decision, Decision.strong_evidence)
        self.assertTrue(r.actionable)
        self.assertEqual(r.num_turns, 4)
        self.assertEqual(r.total_cost_usd, 0.5)

    def test_missing_block_raises_not_abstains(self):
        """No handoff is an INFRASTRUCTURE failure, not a verdict: the system prompt
        requires the fenced block on every path (abstain is a value inside it), so its
        absence means the run never reached a conclusion. It used to persist as a
        status=done abstain that read like a considered "insufficient evidence" — which
        is how a 66% failure rate stayed invisible for three days."""
        msg = _result_msg("Waiting for the three background agents to complete.",
                          num_turns=17, cost=0.81)
        with self.assertRaises(MissingHandoffError) as ctx:
            build_result(msg)
        exc = ctx.exception
        # The forensics + the SPEND ride on the exception: `set_status` writes neither,
        # and the run was paid for in full.
        self.assertIn("background agents", exc.raw_result)
        self.assertEqual(exc.cost_usd, 0.81)
        self.assertEqual(exc.num_turns, 17)

    def test_unparseable_json_block_also_raises(self):
        # A fence whose JSON is malformed is the SAME failure — the verdict is gone
        # either way, and prod's pre-regression baseline was mostly this shape.
        with self.assertRaises(MissingHandoffError):
            build_result(_result_msg("x\n```json\n{\"verdict\": {,}\n```"))

    def test_parsed_block_that_fails_validation_still_abstains(self):
        # The guard must NOT swallow the salvage path: a block that parses but fails
        # #03 validation is the model speaking, so it stays a (salvaged) abstain.
        bad = {"verdict": {"decision": "strong-evidence", "confidence": "high"}}
        r = build_result(_result_msg("x\n```json\n" + json.dumps(bad) + "\n```"))
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
        r = build_result(_result_msg("x\n```json\n" + _ABSTAIN_JSON + "\n```"), recorder=rec)
        self.assertEqual(len(r.actions), 1)
        self.assertEqual(r.actions[0]["type"], "bugzilla.update_bug")

    _BRIDGE_DOSSIER = {
        "candidate": {"node": "abc123", "bug": 456},
        "verdict": {
            "decision": "strong-evidence", "confidence": "high",
            "needinfo_draft": "Did bug 456 regress this?",
            "mechanism": {"statement": "UAF", "citations": [_SF]},
            "consistency": {"statement": "matches poison", "citations": [_SF]},
        },
    }

    def _bridge_msg(self):
        return _result_msg("x\n```json\n" + json.dumps(self._BRIDGE_DOSSIER) + "\n```")

    def test_needinfo_bridged_to_action(self):
        # strong-evidence + needinfo_draft + candidate.bug -> one apply-eligible
        # add_comment action even though the agent only drafted the text.
        r = build_result(self._bridge_msg())
        self.assertEqual(r.decision, Decision.strong_evidence)
        self.assertEqual(len(r.actions), 1)
        a = r.actions[0]
        self.assertEqual(a["type"], "bugzilla.add_comment")
        self.assertEqual(a["params"]["bug_id"], 456)
        self.assertEqual(a["params"]["text"], "Did bug 456 regress this?")
        self.assertTrue(a["params"]["is_private"])   # default private (may be sec bug)

    def test_no_bridge_without_needinfo_or_bug(self):
        # strong-evidence but no needinfo_draft / candidate -> nothing to apply.
        r = build_result(_result_msg("x\n```json\n" + _DOSSIER_JSON + "\n```"))
        self.assertEqual(r.decision, Decision.strong_evidence)
        self.assertEqual(r.actions, [])

    def test_no_bridge_on_abstain(self):
        r = build_result(_result_msg("x\n```json\n" + _ABSTAIN_JSON + "\n```"))
        self.assertEqual(r.decision, Decision.abstain)
        self.assertEqual(r.actions, [])

    def test_bridge_not_duplicated(self):
        # an identical add_comment already recorded for the bug suppresses the bridge.
        rec = ActionsRecorder(uploader=None)
        rec.record("bugzilla.add_comment",
                   {"bug_id": 456, "text": "Did bug 456 regress this?"}, reasoning="x")
        r = build_result(self._bridge_msg(), recorder=rec)
        self.assertEqual(len(r.actions), 1)

    def test_lead_needinfo_bridged(self):
        # #15 phase 4: a lead with a candidate + soft draft also becomes an
        # apply-eligible add_comment (leads are now apply-eligible, human-gated).
        dossier = {
            "candidate": {"node": "leadnode", "bug": 789},
            "verdict": {"decision": "lead", "confidence": "medium",
                        "needinfo_draft": "soft: could you take a look?"},
        }
        r = build_result(_result_msg("x\n```json\n" + json.dumps(dossier) + "\n```"))
        self.assertEqual(r.decision, Decision.lead)
        self.assertEqual(len(r.actions), 1)
        self.assertEqual(r.actions[0]["type"], "bugzilla.add_comment")
        self.assertEqual(r.actions[0]["params"]["bug_id"], 789)
        self.assertEqual(r.actions[0]["params"]["text"], "soft: could you take a look?")

    def test_bridge_kept_when_recorded_text_differs(self):
        # a DISTINCT recorded comment for the same bug must not drop the drafted
        # needinfo — both stay apply-eligible.
        rec = ActionsRecorder(uploader=None)
        rec.record("bugzilla.add_comment", {"bug_id": 456, "text": "unrelated"}, reasoning="x")
        r = build_result(self._bridge_msg(), recorder=rec)
        self.assertEqual(len(r.actions), 2)


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


class TestSumTokens(unittest.TestCase):
    def test_model_usage_camelcase_summed_across_models(self):
        rm = SimpleNamespace(model_usage={
            "opus": {"inputTokens": 100, "outputTokens": 20, "cacheReadInputTokens": 500},
            "sonnet": {"inputTokens": 50, "outputTokens": 10, "cacheReadInputTokens": 200},
        }, usage=None)
        self.assertEqual(triage._sum_tokens(rm), (150, 30, 700))

    def test_model_usage_snake_case_accepted(self):
        rm = SimpleNamespace(model_usage={
            "opus": {"input_tokens": 7, "output_tokens": 3, "cache_read_input_tokens": 9},
        }, usage=None)
        self.assertEqual(triage._sum_tokens(rm), (7, 3, 9))

    def test_falls_back_to_aggregate_usage_when_no_model_usage(self):
        # model_usage absent/empty -> use the aggregate usage dict (snake_case).
        rm = SimpleNamespace(model_usage=None, usage={
            "input_tokens": 42, "output_tokens": 8, "cache_read_input_tokens": 99,
        })
        self.assertEqual(triage._sum_tokens(rm), (42, 8, 99))

    def test_empty_both_is_zero(self):
        self.assertEqual(triage._sum_tokens(SimpleNamespace(model_usage=None, usage=None)),
                         (0, 0, 0))

    def test_non_dict_entries_ignored(self):
        rm = SimpleNamespace(model_usage={"x": None, "y": "bad",
                                          "z": {"inputTokens": 5}}, usage=None)
        self.assertEqual(triage._sum_tokens(rm), (5, 0, 0))


def _bundled_cli():
    """The CLI binary the SDK will actually spawn, or None (mirrors
    ``SubprocessCLITransport._find_bundled_cli``)."""
    path = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    return path if path.is_file() else None


class TestBundledCLICanary(unittest.TestCase):
    """Fail HERE, offline, when an SDK bump removes the switch the inline fan-out
    depends on — rather than in prod, silently, at ~$25/day of abstains.

    ``CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`` is an internal env var of the bundled CLI,
    not public SDK API, so nothing else notices if it disappears: the CLI ignores an
    unknown variable, the subagents background again, and the principal starts leaving
    progress notes. That is exactly how 0.2.110 -> 0.2.131 got through — the lock bump
    diffed ``ClaudeAgentOptions``' fields across every release and never looked at what
    the CLI does with them.

    KNOW WHAT THIS DOES NOT CATCH. It is a presence check on a string, and the variable
    is read at ~22 sites of which only one — the ``&& !U`` in the Agent launch predicate
    — is load-bearing for the fan-out; the rest are Bash/PowerShell/Monitor/TUI. An SDK
    bump that keeps the variable for the Bash schema while restructuring the Agent launch
    would leave this green and prod broken. Asserting the predicate itself is not an
    option: every identifier in it is minified and renames between builds. So on any SDK
    bump, re-derive it by hand —
        grep -a -o -b -E '.{80}V?\\.background===.{160}' <bundled>/claude
    — and check the term is still ``&& !U``. The runtime backstop for the case this
    misses is ``MissingHandoffError``: a backgrounded fan-out now errors loudly instead
    of persisting as a plausible abstain."""

    def test_switch_still_exists_in_the_bundled_binary(self):
        cli = _bundled_cli()
        if cli is None:  # not installed (e.g. a --no-default-groups checkout)
            self.skipTest("no bundled CLI to scan")
        with open(cli, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            self.assertNotEqual(
                mm.find(b"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"), -1,
                "the bundled CLI no longer mentions "
                "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS: triage._CLI_ENV is now inert and "
                "the subagent fan-out is backgrounding again. Re-derive the launch "
                "predicate (grep -a for 'V.background===') before shipping this SDK bump.",
            )


if __name__ == "__main__":
    unittest.main()
