# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Per-role subagents as SDK-native ``AgentDefinition``s (#02).

Clouseau authors its five senior roles (hackbot ships only one generic
``investigator``). Each gets a role prompt, a curated read-only tool allowlist
(``mcp__searchfox__*`` + built-ins), and a per-role model tier from the
``agent.llm.roles`` config block. Children get NO ``Task`` tool (no recursion).
``effort`` is options-level (the principal session, set in ``triage.py``), never
a per-``AgentDefinition`` field, so the per-role ``effort`` in config is advisory
and not applied here."""
from __future__ import annotations

from claude_agent_sdk import AgentDefinition

from crashclouseau import config

_SEARCHFOX = [
    f"mcp__searchfox__{name}"
    for name in ("calls_from", "calls_to", "calls_between", "define", "lookup", "search")
]
_BUILTIN_READ = ["Read", "Grep", "Glob", "Bash"]

_GROUND = (
    " You have read-only access. Quote only what a tool actually returned; never "
    "invent a symbol, edge, or line. Every claim you make must carry its citation "
    "(a searchfox permalink + mangled symbol id, an exact diff line, or an exact "
    "stack frame). If you cannot ground a claim, say so rather than guess."
)

_ROLES: dict[str, dict] = {
    "crash-interpreter": {
        "description": "Normalize a raw processed crash into a grounded crash brief "
        "(failure class, crashing thread, decoded signals, expanded inlines).",
        "prompt": "You are the crash interpreter. Turn the provided processed-crash "
        "facts into a normalized crash brief: classify the failure "
        "(uaf/null_deref/assertion/oob/shutdownhang/other) only from the decoded "
        "signals, pick the thread that matters, and list the frames." + _GROUND,
        "tools": [*_BUILTIN_READ],
    },
    "call-graph-explorer": {
        "description": "Navigate the searchfox call graph from crash frames to reach "
        "off-stack callers/callees, returning a cited neighborhood.",
        "prompt": "You are the call-graph explorer (navigator). Starting from the "
        "crash frames, drive the searchfox tools (calls_from/calls_to/calls_between/"
        "define/search) to build a cited neighborhood, explicitly reaching off-stack "
        "functions. Recover virtual/IPC/FFI edges via search. Explore persistently to "
        "a fixpoint; record holes rather than inventing edges." + _GROUND,
        "tools": [*_BUILTIN_READ, *_SEARCHFOX],
    },
    "patch-scout": {
        "description": "Intersect the neighborhood with recent patches and summarize, "
        "in one cited line, what each candidate changed.",
        "prompt": "You are the patch scout. Given the neighborhood functions and the "
        "candidate diffs, match changed functions to the neighborhood and write a "
        "one-line, fully-cited semantic summary per candidate (cite the diff line and "
        "the searchfox symbol)." + _GROUND,
        "tools": [*_BUILTIN_READ, "mcp__searchfox__define", "mcp__searchfox__search"],
    },
    "data-flow-tracer": {
        "description": "Read the function bodies along a call path and decide whether "
        "a change can free/mutate/null/overrun the crashing value.",
        "prompt": "You are the data-flow tracer. For a (candidate patch, crash frame) "
        "pair, read the path bodies with define and reason about whether the change "
        "can free/mutate/null/overrun the value the crash site dereferences. Return a "
        "cited hypothesis or 'insufficient'." + _GROUND,
        "tools": [*_BUILTIN_READ, "mcp__searchfox__define", "mcp__searchfox__search"],
    },
    "skeptic": {
        "description": "Adversarially re-verify each assembled claim (edges, diff "
        "lines, reachability) and refute the unsupported ones.",
        "prompt": "You are the skeptic. Independently re-verify every claim in a "
        "candidate chain — re-query searchfox for each claimed edge, re-check each "
        "cited diff line — and mark each pass/fail/unverifiable. A claim without a "
        "fresh citation cannot pass." + _GROUND,
        "tools": [*_BUILTIN_READ, *_SEARCHFOX],
    },
}


def searchfox_tool_ids() -> list[str]:
    return list(_SEARCHFOX)


def role_names() -> list[str]:
    return list(_ROLES)


def make_role(name: str) -> AgentDefinition:
    spec = _ROLES[name]
    model = config.get_llm_role(name).get("model", "inherit")
    return AgentDefinition(
        description=spec["description"],
        prompt=spec["prompt"],
        tools=list(spec["tools"]),
        model=model,
    )


def build_roles() -> dict[str, AgentDefinition]:
    return {name: make_role(name) for name in _ROLES}
