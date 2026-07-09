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
# Deterministic #14 patch-extraction, exposed as a tool so patch-scout /
# data-flow / skeptic read a candidate's diff in one fast call instead of
# shelling out.
_PATCH = ["mcp__patch__diff"]
# hg.mozilla.org history/blame/changeset via libmozdata (our UA + retry, channel->repo),
# so roles find & inspect regressors by what recently changed a file instead of shelling
# out to curl/git-log (non-portable: the prod worker has no local checkout).
_HISTORY = [f"mcp__history__{name}" for name in ("file_history", "blame", "changeset")]
_BUILTIN_READ = ["Read", "Grep", "Glob", "Bash"]

_GROUND = (
    " You have read-only access. Quote only what a tool actually returned; never "
    "invent a symbol, edge, or line. Every claim you make must carry its citation "
    "(a searchfox permalink + the DEMANGLED/readable symbol name — both copied "
    "VERBATIM from the tool output; never hand-write a mangled `_Z...` id from "
    "memory — an exact diff line, or an exact stack frame). If you cannot ground a "
    "claim, say so rather than guess. Whenever you quote code in prose — identifiers, "
    "function/type names, expressions, `file:line`, paths — wrap it in `backticks` so "
    "it renders as code; be consistent, don't backtick some and leave the rest bare."
    " Never curl hg.mozilla.org or shell out to git/hg to read source or history "
    "(there is no local checkout in production): use the searchfox tools for source, "
    "and the `mcp__history__*` tools (file_history/blame/changeset) for history."
)

_ROLES: dict[str, dict] = {
    "crash-interpreter": {
        "description": "Normalize a raw processed crash into a grounded crash brief "
        "(failure class, crashing thread, decoded signals, expanded inlines).",
        "prompt": "You are the crash interpreter. Turn the provided processed-crash "
        "facts into a normalized crash brief: classify the failure "
        "(uaf/null_deref/assertion/oob/shutdownhang/other) only from the decoded "
        "signals, pick the thread that matters, and list the actionable frames (skip "
        "the universal bottom-of-stack anchors: event loop, message pump, thread "
        "entry). Prefer decoded "
        "crash facts (crash_info.type/address, MOZ_CRASH_REASON, PHC alloc/free "
        "stacks, assertion text, async-shutdown fields) over guessing from the "
        "signature alone. End with one fenced ```json block shaped like: "
        "{\"uuid\":\"...\",\"signature\":\"...\",\"failure_class\":\"uaf|null_deref|"
        "assertion|oob|shutdownhang|other\",\"faulting_address\":\"...\","
        "\"moz_crash_reason\":\"...\",\"reason\":\"...\",\"crashing_thread\":0,"
        "\"frames\":[{\"stackpos\":0,\"function\":\"...\",\"filename\":\"...\","
        "\"line\":0,\"node\":\"...\",\"inlines\":[]}]}." + _GROUND,
        "tools": [*_BUILTIN_READ],
    },
    "call-graph-explorer": {
        "description": "Navigate the searchfox call graph from crash frames to reach "
        "off-stack callers/callees, returning a cited neighborhood.",
        "prompt": "You are the call-graph explorer (navigator). Starting from the "
        "crash frames, drive the searchfox tools (calls_from/calls_to/calls_between/"
        "define/search) to build a cited neighborhood, explicitly reaching off-stack "
        "functions. Start from the first actionable non-anchor frames, expand both "
        "callers and callees to bounded depth, and use search only to bridge likely "
        "virtual/IPC/FFI/macro/template/cross-language holes. If the principal names "
        "candidate files/functions, bias exploration toward reaching them (as well as "
        "the off-stack area around the crash) so the neighborhood is useful to the "
        "patch scout. A search hit without a "
        "symbol_id is a clue or caveat, not a final call-edge citation. Explore "
        "persistently to a fixpoint; record holes rather than inventing edges. End "
        "with one fenced ```json block shaped like: {\"edges\":[{\"caller_symbol\":"
        "\"Readable::caller\",\"callee_symbol\":\"Readable::callee\",\"via\":"
        "\"calls-from|calls-to|calls-between|search-hole\",\"citations\":[{\"kind\":"
        "\"searchfox\",\"permalink\":\"https://searchfox.org/...\",\"symbol_id\":"
        "\"Readable::symbol\",\"repo\":\"mozilla-central\"}]}],\"from_stackpos\":0,"
        "\"to_symbol\":\"Readable::symbol\"}." + _GROUND,
        "tools": [*_BUILTIN_READ, *_SEARCHFOX, *_HISTORY],
    },
    "patch-scout": {
        "description": "Intersect the neighborhood with recent patches and summarize, "
        "in one cited line, what each candidate changed.",
        "prompt": "You are the patch scout. For each candidate changeset, call the "
        "`mcp__patch__diff` tool to get its parsed diff (changed files, hunks with "
        "exact line numbers + content + enclosing function). To find WHICH changeset is "
        "the likely regressor use `mcp__history__file_history` (recent changes to the "
        "crashing file/area) and `mcp__history__changeset` to inspect a candidate; "
        "`mcp__history__blame` blames the crashing line. Do NOT shell out with "
        "git/hg/curl. Match changed functions to the neighborhood and write a one-line, "
        "fully-cited semantic summary per candidate (cite the diff line and the "
        "searchfox symbol). Treat the provided seed list as a priority queue, not as "
        "proof that no off-stack candidate exists: if neighborhood files/functions "
        "point outside the seed list, report that gap as a cited lead/caveat for the "
        "principal. DOWN-RANK obviously-unrelated candidates so leads stay "
        "credible: cosmetic/comment/doc-only diffs (the tool prints a NOTE), changes "
        "to ubiquitous primitives (nsTArray/HashMap/RefPtr/nsCOMPtr/strings/allocators "
        "— a break there would crash all of Firefox, not one signature), and universal "
        "bottom-of-stack frames used as anchors. Down-rank, don't discard. Order your "
        "output so the candidate whose change best matches the crashing area comes "
        "first. End with "
        "one fenced ```json block containing a list of diff-hunk objects shaped like: "
        "[{\"node\":\"<hg node>\",\"filename\":\"...\",\"header\":\"@@ ... @@\","
        "\"lines\":[],\"citations\":[{\"kind\":\"diff_line\",\"node\":\"<hg node>\","
        "\"filename\":\"...\",\"line\":42,\"side\":\"added|deleted|context\","
        "\"content\":\"exact tool line\"}]}]." + _GROUND,
        "tools": [*_BUILTIN_READ, "mcp__patch__diff", *_HISTORY,
                  "mcp__searchfox__define", "mcp__searchfox__search"],
    },
    "data-flow-tracer": {
        "description": "Read the function bodies along a call path and decide whether "
        "a change can free/mutate/null/overrun the crashing value.",
        "prompt": "You are the data-flow tracer. For a (candidate patch, crash frame) "
        "pair, read the changed lines with `mcp__patch__diff` and the path bodies with "
        "define, then reason about whether the change can free/mutate/null/overrun the "
        "value the crash site dereferences. Also consider Firefox-specific mechanisms: "
        "refcount/lifetime changes, task dispatch ordering, IPC actor teardown, GC "
        "marking/tracing, shutdown ordering, assertion invariant changes, thread/race "
        "assumptions, Rust panic paths, and FFI boundary changes. Return a cited "
        "hypothesis or 'insufficient'. End with one fenced ```json block only when "
        "you have at least one citation for the hypothesis, shaped like: {\"summary\":"
        "\"...\",\"object_name\":\"...\",\"operation\":\"free|mutate|null_deref|uaf|"
        "oob|race|assertion|shutdown|gc|ipc|other\",\"crash_site\":{\"kind\":"
        "\"stack_frame\",\"uuid\":\"...\",\"stackpos\":0,\"filename\":\"...\","
        "\"function\":\"...\",\"line\":0,\"node\":\"...\"},\"citations\":[{\"kind\":"
        "\"searchfox\",\"permalink\":\"https://searchfox.org/...\",\"symbol_id\":"
        "\"Readable::symbol\",\"repo\":\"mozilla-central\"}]}." + _GROUND,
        "tools": [*_BUILTIN_READ, "mcp__patch__diff", *_HISTORY,
                  "mcp__searchfox__define", "mcp__searchfox__search"],
    },
    "skeptic": {
        "description": "Adversarially re-verify each assembled claim (edges, diff "
        "lines, reachability) and refute the unsupported ones.",
        "prompt": "You are the skeptic. Independently re-verify every claim in a "
        "candidate chain — re-query searchfox for each claimed edge, re-check each "
        "cited diff line with `mcp__patch__diff` — and mark each pass/fail/"
        "unverifiable. Use fail for a contradiction or missing claimed evidence; use "
        "unverifiable for searchfox holes such as virtual/IPC/FFI/macro/template "
        "edges. A claim without a fresh citation cannot pass. End with one fenced "
        "```json block holding a LIST with ONE object per claim you checked, shaped "
        "like: [{\"claim_ref\":\"edge0|mechanism|hunk0|...\","
        "\"status\":\"pass|fail|unverifiable\",\"note\":\"...\",\"citations\":[...]}]"
        "." + _GROUND,
        "tools": [*_BUILTIN_READ, *_SEARCHFOX, "mcp__patch__diff", *_HISTORY],
    },
}


def searchfox_tool_ids() -> list[str]:
    return list(_SEARCHFOX)


def patch_tool_ids() -> list[str]:
    return list(_PATCH)


def history_tool_ids() -> list[str]:
    return list(_HISTORY)


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
