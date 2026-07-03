# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""The one crash-triage agent coroutine (#02).

``run_crash_triage`` assembles ``ClaudeAgentOptions`` (principal system prompt,
in-process MCP evidence servers, the five senior ``AgentDefinition``s, tiering,
options-level ``effort``), drives ``ClaudeSDKClient`` to a terminal
``ResultMessage``, and folds it into a typed ``CrashTriageResult`` — best-effort
parsing the trailing ```json handoff into a #03-validated ``Dossier`` (abstain on
failure). #11 runs ``asyncio.run(run_crash_triage(...))`` inside the RQ job; the
vendored ``run``/``run_async`` are NOT used (they SystemExit). Options assembly
(`build_options`) and result folding (`build_result`) are split out so they unit-
test without spawning the bundled CLI."""
from __future__ import annotations

import functools
import os
import time
from collections import defaultdict

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from crashclouseau import config
from crashclouseau.agent import roles
from crashclouseau.logger import logger
from crashclouseau.agent.result import CrashTriageResult
from crashclouseau.agent.schema import parse_and_validate
from crashclouseau.agent.tools import searchfox_cg
from crashclouseau.agent.tools.searchfox_cg import SearchfoxCtx
from crashclouseau.searchfox import SearchfoxClient
from crashclouseau.vendor.agent_tools.claude_sdk import build_sdk_server
from crashclouseau.vendor.hackbot_runtime.actions import ACTIONS_SERVER_NAME
from crashclouseau.vendor.hackbot_runtime.actions.claude_sdk import (
    actions_server_for,
    actions_to_tool_names,
)
from crashclouseau.vendor.hackbot_runtime.claude import Reporter
from crashclouseau.vendor.hackbot_runtime.errors import AgentError

# The needinfo actions the agent may RECORD (nothing is executed; #12 applies).
NEEDINFO_ACTIONS = ["bugzilla.add_comment", "bugzilla.update_bug"]

_BUILTIN_TOOLS = ["Read", "Grep", "Glob", "Bash"]

# Config short names (used verbatim for AgentDefinition) -> full ids for the
# principal ClaudeAgentOptions model= level.
_MODEL_IDS = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-4-8",
    "fable": "claude-fable-5",
}


def _model_id(model: str) -> str:
    return _MODEL_IDS.get(model, model)


@functools.lru_cache(maxsize=1)
def _system_prompt() -> str:
    path = os.path.join(os.path.dirname(__file__), "prompts", "system.md")
    with open(path, "r") as handle:
        return handle.read()


def _user_prompt(crash: dict) -> str:
    uuid = crash.get("uuid", "")
    signature = crash.get("signature", "")
    channel = crash.get("channel", "nightly")
    stack = crash.get("stack") or crash.get("stack_text") or ""
    extra = crash.get("notes", "")
    lines = [
        "Investigate this Firefox crash and identify the regressor changeset, "
        "reaching off-stack functions through the call graph where needed. "
        "Abstain unless the evidence chain is verified end to end.",
        "",
        f"UUID: {uuid}",
        f"Signature: {signature}",
        f"Channel: {channel}",
    ]
    if stack:
        lines += ["", "Stack:", str(stack)]
    if extra:
        lines += ["", str(extra)]
    return "\n".join(lines)


def _crash_label(crash: dict) -> str:
    return "crash {}".format(crash.get("uuid", "?"))


class _RunTrace:
    """Logs where a run's wall-time goes: each AI subagent (Task) with its
    description + elapsed time, plus a per-tool and per-model breakdown. Emitted via
    ``logger`` so it shows in worker logs and the offline harness without changing
    the agent's behavior. Purely observational (helps decide what to trim)."""

    def __init__(self):
        self._t0 = time.monotonic()
        self._start = {}       # tool_use_id -> (name, start, issuer_subagent_or_None, label)
        self._task_type = {}   # Task tool_use_id -> subagent_type
        self.tasks = []        # [(subagent_type, label, seconds)] in completion order
        self._tool = defaultdict(lambda: [0, 0.0])   # tool name -> [count, seconds]

    def _clock(self):
        return time.monotonic() - self._t0

    # The SDK spawns senior roles via the "Agent" tool (older builds: "Task").
    _SUBAGENT_TOOLS = ("Agent", "Task")

    @staticmethod
    def _label(name, inp):
        inp = inp or {}
        if name in _RunTrace._SUBAGENT_TOOLS:
            desc = str(inp.get("description") or inp.get("prompt") or "")[:100]
            role = ""
            for k in ("subagent_type", "subagentType", "agent_type", "agent",
                      "type", "name", "role"):
                if inp.get(k):
                    role = str(inp[k])
                    break
            return "{}: {}".format(role, desc) if role else desc
        if name == "Bash":
            return str(inp.get("command", ""))[:100]
        for k in ("symbol", "caller", "callee", "file_path", "pattern", "path", "query"):
            if inp.get(k):
                return "{}={}".format(k, str(inp[k])[:80])
        return str(inp)[:80]

    def observe(self, msg):
        if isinstance(msg, AssistantMessage):
            issuer = self._task_type.get(msg.parent_tool_use_id)  # None => principal
            for b in msg.content:
                if isinstance(b, ToolUseBlock):
                    label = self._label(b.name, b.input)
                    self._start[b.id] = (b.name, time.monotonic(), issuer, label)
                    if b.name in self._SUBAGENT_TOOLS:
                        st = (b.input or {}).get("subagent_type", "?")
                        self._task_type[b.id] = st
                        logger.info("agent: [+%5.1fs] ▶ spawn %s — %s",
                                    self._clock(), st, label)
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            for b in msg.content:
                if isinstance(b, ToolResultBlock):
                    rec = self._start.pop(b.tool_use_id, None)
                    if not rec:
                        continue
                    name, start, _issuer, label = rec
                    dt = time.monotonic() - start
                    if name in self._SUBAGENT_TOOLS:
                        st = self._task_type.get(b.tool_use_id, "?")
                        self.tasks.append((st, label, dt))
                        logger.info("agent: [+%5.1fs] ✔ %s done in %.1fs",
                                    self._clock(), st, dt)
                    else:
                        self._tool[name][0] += 1
                        self._tool[name][1] += dt

    def summary(self, result_msg):
        wall = (getattr(result_msg, "duration_ms", None) or 0) / 1000.0
        api = (getattr(result_msg, "duration_api_ms", None) or 0) / 1000.0
        if not wall:
            wall = self._clock()
        turns = getattr(result_msg, "num_turns", "?")
        cost = getattr(result_msg, "total_cost_usd", 0) or 0.0
        logger.info("agent: ===== run timing =====")
        logger.info("agent: total wall=%.1fs api=%.1fs turns=%s cost=$%.4f",
                    wall, api, turns, cost)
        if self.tasks:
            logger.info("agent: AI subagent tasks (slowest first):")
            for st, label, secs in sorted(self.tasks, key=lambda t: -t[2]):
                logger.info("agent:   %6.1fs  %s", secs, label)
        if self._tool:
            logger.info("agent: tools (slowest first):")
            for name, (n, secs) in sorted(self._tool.items(), key=lambda kv: -kv[1][1]):
                logger.info("agent:   %6.1fs  %-30s x%d", secs, name, n)
        model_usage = getattr(result_msg, "model_usage", None) or {}
        for model, u in model_usage.items():
            if isinstance(u, dict):
                logger.info("agent:   model %-22s in=%s out=%s", model,
                            u.get("inputTokens", u.get("input_tokens", "?")),
                            u.get("outputTokens", u.get("output_tokens", "?")))


def build_options(
    crash: dict,
    *,
    llm_cfg: dict | None = None,
    recorder=None,
    searchfox_client=None,
) -> ClaudeAgentOptions:
    """Assemble the principal ``ClaudeAgentOptions``. Pass ``searchfox_client`` to
    avoid resolving the ``searchfox-cli`` binary (unit tests)."""
    llm_cfg = config.get_llm() if llm_cfg is None else llm_cfg
    principal = llm_cfg.get("principal", {})
    model = _model_id(principal.get("model", "opus"))
    effort = principal.get("effort")
    max_turns = principal.get("max_turns")

    if searchfox_client is None:
        searchfox_client = SearchfoxClient()
    ctx = SearchfoxCtx(client=searchfox_client)
    mcp_servers = {"searchfox": build_sdk_server("searchfox", ctx, searchfox_cg.TOOLS)}
    allowed = [*_BUILTIN_TOOLS, "Task", *roles.searchfox_tool_ids()]

    if recorder is not None:
        _, actions_server = actions_server_for(recorder, types=NEEDINFO_ACTIONS)
        mcp_servers[ACTIONS_SERVER_NAME] = actions_server
        allowed += actions_to_tool_names(NEEDINFO_ACTIONS)

    kwargs = dict(
        system_prompt=_system_prompt(),
        mcp_servers=mcp_servers,
        agents=roles.build_roles(),
        allowed_tools=allowed,
        model=model,
        max_turns=max_turns,
        permission_mode="bypassPermissions",
        setting_sources=[],
    )
    if effort:
        kwargs["effort"] = effort
    return ClaudeAgentOptions(**kwargs)


def build_result(result_msg, *, recorder=None) -> CrashTriageResult:
    """Fold a terminal ``ResultMessage`` into a typed ``CrashTriageResult``,
    best-effort parsing + #03-validating the trailing ```json handoff (abstain on
    failure). Raises ``AgentError`` on a missing/errored result."""
    if result_msg is None:
        raise AgentError("crash triage produced no result message")
    if getattr(result_msg, "is_error", False):
        detail = result_msg.result or getattr(result_msg, "subtype", "")
        raise AgentError(f"crash triage failed: {detail}")
    dossier = parse_and_validate(result_msg.result)
    return CrashTriageResult(
        num_turns=result_msg.num_turns,
        total_cost_usd=result_msg.total_cost_usd,
        result=result_msg.result or "",
        dossier=dossier,
        actions=list(recorder.actions) if recorder is not None else [],
    )


async def run_crash_triage(
    *,
    crash: dict,
    tools_cfg: dict | None = None,
    llm_cfg: dict | None = None,
    recorder=None,
    extra: dict | None = None,
) -> CrashTriageResult:
    """Drive the principal loop for one crash and return the typed result."""
    options = build_options(
        crash,
        llm_cfg=llm_cfg,
        recorder=recorder,
        searchfox_client=(extra or {}).get("searchfox_client"),
    )
    result_msg = None
    trace = _RunTrace()
    with Reporter(verbose=False, log_path=None) as reporter:
        reporter.header(_crash_label(crash))
        async with ClaudeSDKClient(options=options) as client:
            await client.query(_user_prompt(crash))
            async for msg in client.receive_response():
                reporter.message(msg)
                trace.observe(msg)
                if isinstance(msg, ResultMessage):
                    result_msg = msg
    trace.summary(result_msg)
    return build_result(result_msg, recorder=recorder)
