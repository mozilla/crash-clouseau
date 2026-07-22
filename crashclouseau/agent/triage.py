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
from crashclouseau.agent.schema import Decision, parse_and_validate
from crashclouseau.agent.tools import history as history_tools
from crashclouseau.agent.tools import patch as patch_tools
from crashclouseau.agent.tools import searchfox_cg
from crashclouseau.agent.tools import source as source_tools
from crashclouseau.agent.tools.history import HistoryCtx
from crashclouseau.agent.tools.patch import PatchCtx
from crashclouseau.agent.tools.searchfox_cg import SearchfoxCtx
from crashclouseau.agent.tools.source import SourceCtx
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


def _short_value(value, limit=300):
    if value is None or value == "":
        return ""
    text = str(value).replace("\n", " ").strip()
    if len(text) > limit:
        return text[:limit - 3] + "..."
    return text


def _first_present(*values):
    for value in values:
        if value is not None and value != "":
            return value
    return ""


def _fmt_pushdate(value):
    """A candidate's landing date as ``YYYY-MM-DD`` (a ``datetime`` or an ISO str);
    falls back to ``str`` for anything else."""
    try:
        return value.strftime("%Y-%m-%d")
    except AttributeError:
        return str(value)[:10]


def _crash_facts(crash: dict) -> list[str]:
    """Compact processed-crash facts for the LLM.

    The full Socorro payload can be large; expose the failure signals that help the
    crash-interpreter classify the crash and the data-flow role pick mechanisms.
    """
    raw = crash.get("raw_crash") or {}
    dump = raw.get("json_dump") or {}
    info = dump.get("crash_info") or {}
    lines = []

    facts = [
        ("Product", _first_present(crash.get("product"), raw.get("product"))),
        ("Version", _first_present(crash.get("version"), raw.get("version"))),
        ("Build ID", _first_present(crash.get("buildid"), raw.get("build_id"))),
        ("Crash type", _first_present(info.get("type"), raw.get("reason"))),
        ("Fault address", _first_present(info.get("address"), raw.get("address"))),
        ("Crashing thread", _first_present(
            info.get("crashing_thread"), raw.get("crashing_thread")
        )),
        ("MOZ_CRASH_REASON", _first_present(
            raw.get("moz_crash_reason"), dump.get("moz_crash_reason")
        )),
        ("Crash reason", raw.get("reason")),
        ("Assertion", info.get("assertion")),
        ("PHC kind", _first_present(raw.get("phc_kind"), info.get("phc_kind"))),
        ("PHC alloc stack", _first_present(
            raw.get("phc_alloc_stack"), info.get("phc_alloc_stack")
        )),
        ("PHC free stack", _first_present(
            raw.get("phc_free_stack"), info.get("phc_free_stack")
        )),
        ("Async shutdown timeout", raw.get("async_shutdown_timeout")),
    ]
    for label, value in facts:
        value = _short_value(value)
        if value:
            lines.append(f"{label}: {value}")
    return lines


def _user_prompt(crash: dict) -> str:
    uuid = crash.get("uuid", "")
    signature = crash.get("signature", "")
    channel = crash.get("channel", "nightly")
    stack = crash.get("stack") or crash.get("stack_text") or ""
    extra = crash.get("notes", "")
    lines = [
        "Investigate this Firefox crash and identify the regressor changeset, "
        "reaching off-stack functions through the call graph where needed. "
        "Return strong-evidence only when the evidence chain is verified end to "
        "end. Otherwise prefer a cited lead when a candidate changeset, hunk, or "
        "call-path edge points a human at the right area. Abstain only when there "
        "is no cited lead worth anyone's time.",
        "",
        f"UUID: {uuid}",
        f"Signature: {signature}",
        f"Channel: {channel}",
    ]
    facts = _crash_facts(crash)
    if facts:
        lines += ["", "Crash facts:", *facts]
    if stack:
        lines += ["", "Stack:", str(stack)]
    candidates = crash.get("candidates") or []
    if candidates:
        if crash.get("is_offstack"):
            # P1 off-stack: no changeset landed on a crash-stack file, so this is the FULL
            # first-bad-build pushlog window (lightly pre-ranked by signature/desc overlap,
            # NOT by proximity). The regressor is somewhere in here. Triage by DESCRIPTION
            # first and read only the promising few diffs; reads are PINNED to the build.
            lines += [
                "",
                "Candidate changesets = the FULL pushlog window between the last-good "
                "build and this crash's build (this crash is OFF-STACK: no candidate "
                "touched a file on the stack, so there is NO proximity score — the list "
                "is only lightly pre-ranked and the regressor may be anywhere in it). "
                "Work it as a funnel: (1) scan the one-line descriptions and pick the few "
                "whose area/subsystem best matches the crash signature + stack; (2) read "
                "ONLY those with mcp__patch__diff; (3) use the searchfox call graph to "
                "connect a candidate's changed function to a crash frame. IMPORTANT: your "
                "blame/history/source reads are PINNED to the crash build revision (never "
                "tip) — read source with mcp__source__raw_file, not searchfox define, when "
                "exact build-time code matters. For strong-evidence you MUST show a "
                "verified call path from the candidate to a crash frame (a window "
                "membership alone is only a lead). Watch for an 'exposer, not cause' "
                "changeset (it exposed a pre-existing UAF/latent bug rather than "
                "introducing it) — prefer a lead + needinfo over accusing it:",
            ]
        else:
            lines += [
                "",
                "Scored candidate changesets (already ranked by proximity to the crash — "
                "read each with the mcp__patch__diff tool. Treat this seed list as a "
                "priority queue, not as a closed world: use it first, but if the "
                "call-graph neighborhood points at off-stack files/functions not covered "
                "by these seeds, say so and treat that as a cited lead rather than "
                "pretending the seed list is complete):",
            ]
        # Off-stack: the list is already capped to max_candidates in build_seed, so show
        # it all (dropping any would risk hiding the regressor). On-stack: top 20.
        limit = len(candidates) if crash.get("is_offstack") else 20
        for c in candidates[:limit]:
            parts = [str(c.get("node", ""))]
            if c.get("score") is not None:
                parts.append("score={}".format(c["score"]))
            if c.get("bug"):
                parts.append("bug={}".format(c["bug"]))
            pushdate = c.get("pushdate")
            if pushdate:
                parts.append("landed={}".format(_fmt_pushdate(pushdate)))
            if c.get("backedout"):
                parts.append("backed-out")
            if c.get("noise"):
                parts.append("(likely-noise: down-rank)")
            desc = c.get("desc")
            if desc:
                parts.append("| {}".format(desc))
            lines.append("- " + " ".join(parts))
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
            # Description only. The spawn line and summary print the role
            # (subagent_type) separately, so embedding it here duplicated it
            # ("▶ spawn navigator — navigator: desc").
            return str(inp.get("description") or inp.get("prompt") or "")[:100]
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
                logger.info("agent:   %6.1fs  %s — %s", secs, st, label)
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
    channel = crash.get("channel", "nightly")
    # P1 pinned mode: an off-stack run pins blame/source reads to the crash BUILD rev so a
    # tip read can't leak the post-build fix. Empty for on-stack runs (tools keep tip).
    pin_rev = crash.get("pin_rev", "")
    ctx = SearchfoxCtx(client=searchfox_client)
    patch_ctx = PatchCtx(channel=channel)
    history_ctx = HistoryCtx(channel=channel, build_rev=pin_rev)
    source_ctx = SourceCtx(channel=channel, build_rev=pin_rev)
    mcp_servers = {
        "searchfox": build_sdk_server("searchfox", ctx, searchfox_cg.TOOLS),
        "patch": build_sdk_server("patch", patch_ctx, patch_tools.TOOLS),
        "history": build_sdk_server("history", history_ctx, history_tools.TOOLS),
        "source": build_sdk_server("source", source_ctx, source_tools.TOOLS),
    }
    allowed = [
        *_BUILTIN_TOOLS, "Task",
        *roles.searchfox_tool_ids(), *roles.patch_tool_ids(),
        *roles.history_tool_ids(), *roles.source_tool_ids(),
    ]

    if recorder is not None:
        _, actions_server = actions_server_for(recorder, types=NEEDINFO_ACTIONS)
        mcp_servers[ACTIONS_SERVER_NAME] = actions_server
        allowed += actions_to_tool_names(NEEDINFO_ACTIONS)

    kwargs = dict(
        system_prompt=_system_prompt(),
        mcp_servers=mcp_servers,
        agents=roles.build_roles(llm_cfg),
        allowed_tools=allowed,
        model=model,
        max_turns=max_turns,
        permission_mode="bypassPermissions",
        setting_sources=[],
    )
    if effort:
        kwargs["effort"] = effort
    return ClaudeAgentOptions(**kwargs)


def _needinfo_action(dossier) -> dict | None:
    """Bridge a strong-evidence verdict's ``needinfo_draft`` into a recordable
    ``bugzilla.add_comment`` action for the human-confirmed #12 apply step.

    The agent only *drafts* the needinfo text (the ``needinfo_draft`` field of the
    JSON dossier); it does not call the ``actions`` MCP tool, so ``recorder.actions``
    is otherwise always empty and the apply UI has nothing to execute. This
    synthesizes one apply-eligible action from what the agent already produced.
    Returns ``None`` unless the verdict is strong-evidence OR a lead (#15 phase 4) with
    a draft and a known candidate bug — a lead carries the soft, non-accusatory draft,
    so the human can send it as a needinfo to a knowledgeable person. Shape matches
    ``ActionsRecorder.record`` / ``bugzilla_apply``:
    ``{type, params:{bug_id, text, is_private}, reasoning}``."""
    if dossier is None:
        return None
    v, c = dossier.verdict, dossier.candidate
    if v is None or v.decision not in (Decision.strong_evidence, Decision.lead):
        return None
    if not v.needinfo_draft or c is None or not c.bug:
        return None
    return {
        "type": "bugzilla.add_comment",
        # Default private: the candidate bug may be security-restricted and the draft
        # can quote the crash mechanism. A human confirms (and can un-private) before
        # apply; over-privating is far safer than leaking sec details on a public post.
        "params": {"bug_id": c.bug, "text": v.needinfo_draft, "is_private": True},
        "reasoning": "auto-drafted from the verdict's needinfo_draft ({}); "
                     "human-confirmed before apply".format(v.decision.value),
    }


def _sum_tokens(result_msg):
    """(input, output, cache_read) token totals for the whole run, robust to both usage
    shapes the CLI can report on the terminal ResultMessage:

    - ``model_usage`` (from ``modelUsage``): per-model breakdown with camelCase keys
      (``inputTokens``/``outputTokens``/``cacheReadInputTokens``). Summed across models
      it includes subagents, so it's the fullest total -- preferred when present.
    - ``usage``: the aggregate dict with Anthropic snake_case keys
      (``input_tokens``/``output_tokens``/``cache_read_input_tokens``) -- the fallback.

    Missing/empty usage -> zeros. Kept lenient (accepts either key casing on either
    field) because the exact shape has varied across CLI versions."""
    mu = getattr(result_msg, "model_usage", None) or {}
    ti = to = tc = 0
    for u in mu.values():
        if not isinstance(u, dict):
            continue
        ti += int(u.get("inputTokens", u.get("input_tokens", 0)) or 0)
        to += int(u.get("outputTokens", u.get("output_tokens", 0)) or 0)
        tc += int(u.get("cacheReadInputTokens", u.get("cache_read_input_tokens", 0)) or 0)
    if ti or to or tc:
        return ti, to, tc

    # No per-model data -> fall back to the aggregate usage dict.
    u = getattr(result_msg, "usage", None)
    if isinstance(u, dict):
        return (
            int(u.get("input_tokens", u.get("inputTokens", 0)) or 0),
            int(u.get("output_tokens", u.get("outputTokens", 0)) or 0),
            int(u.get("cache_read_input_tokens", u.get("cacheReadInputTokens", 0)) or 0),
        )
    return 0, 0, 0


def build_result(result_msg, *, recorder=None) -> CrashTriageResult:
    """Fold a terminal ``ResultMessage`` into a typed ``CrashTriageResult``,
    best-effort parsing + #03-validating the trailing ```json handoff (abstain on
    failure). Raises ``AgentError`` on a missing/errored result. The recorded
    actions are the agent's own (via the ``actions`` MCP server) plus a synthesized
    needinfo (``_needinfo_action``) so a strong-evidence verdict always yields an
    apply-eligible action even though the agent only drafts the text."""
    if result_msg is None:
        raise AgentError("crash triage produced no result message")
    if getattr(result_msg, "is_error", False):
        detail = result_msg.result or getattr(result_msg, "subtype", "")
        raise AgentError(f"crash triage failed: {detail}")
    dossier = parse_and_validate(result_msg.result)
    actions = list(recorder.actions) if recorder is not None else []
    bridged = _needinfo_action(dossier)
    if bridged is not None:
        bp = bridged["params"]
        # Dedup on the full (type, bug_id, text): suppress only a true duplicate of
        # this exact needinfo, never a distinct comment the agent already recorded
        # for the same bug (that would silently drop the drafted needinfo).
        dup = any(a.get("type") == bridged["type"] and (a.get("params") or {}).get("bug_id") == bp["bug_id"] and (a.get("params") or {}).get("text") == bp["text"] for a in actions)
        if not dup:
            actions.append(bridged)
    ti, to, tc = _sum_tokens(result_msg)
    return CrashTriageResult(
        num_turns=result_msg.num_turns,
        total_cost_usd=result_msg.total_cost_usd,
        result=result_msg.result or "",
        dossier=dossier,
        actions=actions,
        input_tokens=ti,
        output_tokens=to,
        cache_read_tokens=tc,
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
