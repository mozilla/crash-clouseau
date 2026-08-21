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
    for name in ("calls_from", "calls_to", "calls_between", "define", "lookup",
                 "search", "field_layout")
]
# Deterministic #14 patch-extraction, exposed as a tool so patch-scout /
# data-flow / skeptic read a candidate's diff in one fast call instead of
# shelling out.
_PATCH = ["mcp__patch__diff"]
# hg.mozilla.org history/blame/changeset via libmozdata (our UA + retry, channel->repo),
# so roles find & inspect regressors by what recently changed a file instead of shelling
# out to curl/git-log (non-portable: the prod worker has no local checkout).
_HISTORY = [f"mcp__history__{name}" for name in ("file_history", "blame", "changeset")]
# Pinned source read: a source file's text AS OF the crash build rev (P1 off-stack). The
# leak-free replacement for tip-only searchfox `define`/`search` when the exact build-time
# source matters. On-stack (no pinned rev) it reads tip, same as searchfox, so it is safe
# to grant broadly.
_SOURCE = ["mcp__source__raw_file"]
# Scoped Mozilla-data tools for the blind second-opinion agent (#SO): one Bugzilla bug /
# signature lookup and one crash-stats (Socorro SuperSearch) signature query. Both are
# signature/bug-scoped by construction, so they cannot be turned into a pushlog or
# arbitrary-query tool even without a shell.
_BUGZILLA = [f"mcp__bugzilla__{name}" for name in ("bug", "signature_bugs")]
_SOCORRO = ["mcp__socorro__crash_stats"]
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
    " To read a source file AS OF the crash build revision (not tip), use "
    "`mcp__source__raw_file` — prefer it over searchfox `define` whenever the exact "
    "build-time source matters, because searchfox indexes ~tip and can show code that "
    "only exists AFTER the fix landed."
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
        "tools": [*_BUILTIN_READ, *_SEARCHFOX, *_HISTORY, *_SOURCE],
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
        "credible: cosmetic/comment/doc-only AND code-motion/refactor diffs (the tool "
        "prints a NOTE — a pure extract-method/relocation rarely introduces a crash), changes "
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
        "tools": [*_BUILTIN_READ, "mcp__patch__diff", *_HISTORY, *_SOURCE,
                  "mcp__searchfox__define", "mcp__searchfox__search",
                  "mcp__searchfox__field_layout"],
    },
    "data-flow-tracer": {
        "description": "Read the function bodies along a call path and decide whether "
        "a change can free/mutate/null/overrun the crashing value.",
        "prompt": "You are the data-flow tracer. For a (candidate patch, crash frame) "
        "pair, read the changed lines with `mcp__patch__diff` and the path bodies with "
        "define, then reason about whether the change can free/mutate/null/overrun the "
        "value the crash site dereferences. For a null_deref (or any small faulting "
        "address 0xN), CONFIRM the fault: call `mcp__searchfox__field_layout` on the "
        "FULLY-QUALIFIED containing type (copy the namespaces from the crash signature/"
        "frames and DROP any template `<...>` args — e.g. from "
        "`mozilla::detail::nsTStringRepr<T>::Length` layout "
        "`mozilla::detail::nsTStringRepr`, NOT `nsTStringRepr` or the accessor "
        "`nsTStringLengthStorage`). If byte offset N is a real field, the crash is a "
        "null-deref of THAT field; you MUST emit an actual `struct_layout` citation "
        "object (kind/type_name/field/offset) in your `citations` array — do not merely "
        "mention field_layout in prose, or the deterministic corroboration is lost. "
        "Also consider Firefox-specific mechanisms: "
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
        "tools": [*_BUILTIN_READ, "mcp__patch__diff", *_HISTORY, *_SOURCE,
                  "mcp__searchfox__define", "mcp__searchfox__search",
                  "mcp__searchfox__field_layout"],
    },
    "skeptic": {
        "description": "Trust guardrail: catch NOISE — a coincidental or innocent "
        "candidate wrongly fingered — without demanding end-to-end proof of a credible lead.",
        "prompt": "You are the skeptic — the TRUST GUARDRAIL. The goal is to get a human "
        "investigating a CREDIBLE lead, so your job is to catch NOISE (a coincidental or "
        "innocent candidate that shouldn't be sent to anyone), NOT to demand proof. "
        "Independently re-check every claim — re-query searchfox for each claimed edge, "
        "re-check each cited diff line with `mcp__patch__diff` — and mark each pass/fail/"
        "unverifiable. Use `fail` ONLY when a claim is CONTRADICTED by its own cited evidence "
        "or the candidate is demonstrably UNRELATED to the crash (i.e. noise). A plausible "
        "mechanism you simply cannot verify end-to-end is `unverifiable` (it lowers confidence "
        "but KEEPS the lead) — NOT `fail`: a credible-but-unproven clue is exactly what we "
        "want to surface. Use `unverifiable` for searchfox holes such as virtual/IPC/FFI/"
        "macro/template edges. ONE THING THAT LOOKS LIKE A HOLE AND IS NOT: code that was "
        "never compiled into this build. If a cited line or function sits inside a build-time "
        "guard (`#ifdef X` / `#if defined(X)`), find out whether X is actually defined in THIS "
        "build before accepting the mechanism — `mcp__searchfox__search` for X lands on the "
        "`set_define(\"X\", ...)` in a `moz.configure`, and `mcp__source__raw_file` reads the "
        "`option()` above it. Read its DEFAULT: `option(\"--enable-X\")` with no `default=` is "
        "OFF unless someone asked for it; `option(\"--disable-X\")` is ON. Default-off, with no "
        "override in the crash annotations, means the cited code is not in the binary that "
        "crashed — the candidate is demonstrably UNRELATED, so `fail` it and cite the "
        "`moz.configure` line you read. Same for a path reachable only when a "
        "`StaticPrefList.yaml` pref is on: read the pref's default. `unverifiable` is the wrong "
        "answer here — \"could not confirm whether this Nightly compiles X\" is the note a module "
        "owner had to correct by hand on bug 2063782. Only when you cannot find the option at "
        "all is this `unverifiable`. A claim without a fresh citation cannot pass. A fault-address↔field "
        "claim is NOT a searchfox hole: re-run `mcp__searchfox__field_layout` on the "
        "FULLY-QUALIFIED containing type (with namespaces, no template `<...>` args — "
        "e.g. `mozilla::detail::nsTStringRepr`) and mark it `pass` (with a "
        "`struct_layout` citation object) when the faulting offset is a real field, "
        "`fail` when it isn't. If field_layout returns nothing, you under-qualified the "
        "name (add the namespace / drop template args) — do NOT settle for "
        "`unverifiable`. Remember searchfox indexes ~tip, not the crash build: a small "
        "crash-line vs tip-line delta (or a symbol moved/renamed at tip) is revision "
        "drift or inlining, NOT a contradiction — never `fail` a mechanism over a line "
        "delta when the diff and field-layout confirm it; use `unverifiable` at most. "
        "End with one fenced "
        "```json block holding a LIST with ONE object per claim you checked, shaped "
        "like: [{\"claim_ref\":\"edge0|mechanism|hunk0|...\","
        "\"status\":\"pass|fail|unverifiable\",\"note\":\"...\",\"citations\":[...]}]"
        "." + _GROUND,
        "tools": [*_BUILTIN_READ, *_SEARCHFOX, "mcp__patch__diff", *_HISTORY, *_SOURCE],
    },
}


def searchfox_tool_ids() -> list[str]:
    return list(_SEARCHFOX)


def patch_tool_ids() -> list[str]:
    return list(_PATCH)


def history_tool_ids() -> list[str]:
    return list(_HISTORY)


def source_tool_ids() -> list[str]:
    return list(_SOURCE)


def bugzilla_tool_ids() -> list[str]:
    return list(_BUGZILLA)


def socorro_tool_ids() -> list[str]:
    return list(_SOCORRO)


def role_names() -> list[str]:
    return list(_ROLES)


def make_role(name: str, llm_cfg: dict | None = None) -> AgentDefinition:
    spec = _ROLES[name]
    # Prefer the (possibly swept) llm_cfg passed by build_options so a sweep's per-role
    # model/effort actually reaches the subagent; fall back to the base config.
    rcfg = ((llm_cfg or {}).get("roles") or {}).get(name)
    if rcfg is None:
        rcfg = config.get_llm_role(name)
    kwargs = dict(
        description=spec["description"],
        prompt=spec["prompt"],
        tools=list(spec["tools"]),
        model=rcfg.get("model", "inherit"),
        # DOCUMENTATION ONLY -- this line does NOT keep the subagent inline. Keep it
        # anyway (it is the honest statement of intent, and the one path that DOES read
        # the field wants exactly this value), but do not mistake it for the control:
        # the control is ``CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`` in
        # ``triage._CLI_ENV``. Deleting that env var and trusting this line reinstates
        # the outage described below -- it already happened once, in eac7285.
        #
        # WHY IT IS INERT, so a future reader can re-verify against a new CLI build.
        # Two independent reasons, either alone fatal, both checked against the bundled
        # CLI 2.1.223 shipped inside claude-agent-sdk 0.2.131:
        #
        #   1. The value never survives the trip. The SDK sends the AgentDefinition over
        #      the ``initialize`` control request, and the CLI rebuilds it with a TRUTHY
        #      conditional spread -- ``...n.background&&{background:n.background},`` --
        #      so ``false`` contributes NOTHING and the resolved definition has no
        #      ``background`` key at all. (Sibling fields use ``!==void 0``; this one
        #      does not.) By the time the Task tool looks, the field is ``undefined``.
        #   2. Even a surviving ``false`` would be ignored. The Task/Agent launch decides
        #      backgrounding with
        #          let q = F === "remote",
        #              ee = q || (o === !0 || V.background === !0 || K || G || !A && o !== !1) && !U;
        #      where ``o`` is the model's ``run_in_background`` tool input, ``V`` the
        #      resolved agent definition, ``U`` the env kill switch, and K/G/A the
        #      coordinator / fork-subagent / in-process-teammate modes (all false for us).
        #      ``V.background`` is only ever tested ``=== true``: the definition can force
        #      backgrounding ON, never off. There is no ``background === !1`` anywhere in
        #      the 290MB binary. With K/G/A false the expression collapses to
        #      ``o !== false`` -- i.e. background is ON unless the MODEL explicitly asks
        #      for a synchronous run, which the CLI's own tool description and system
        #      prompt actively discourage ("Agents run in the background by default...").
        #
        # What that costs when it fires: the principal launches its subagents, is told
        # not to poll, and correctly ends its turn to wait for a completion notification.
        # The SDK reports that as a clean terminal ResultMessage (is_error False!) whose
        # text is a progress note -- "Still waiting on the call-graph-explorer and
        # patch-scout background agents" -- with no ```json handoff. 84 runs and ~$68 in
        # the three days after claude-agent-sdk 0.2.110 -> 0.2.131 (5f23df4) landed that
        # way, every one persisted as a plausible-looking "insufficient evidence" abstain.
        # ``build_result`` now raises ``MissingHandoffError`` on that shape so the next
        # such regression is a visible error rate instead of a quiet verdict.
        #
        # This whole module assumes an inline fan-out: ``build_result`` folds exactly one
        # terminal ResultMessage, and ``run_crash_triage`` stops reading at the first one.
        # Backgrounding is a real feature -- the parent gets woken for a continuation turn
        # -- but adopting it means consuming turns until the task ledger drains, which is a
        # different program from the one here.
        background=False,
    )
    if rcfg.get("effort"):
        kwargs["effort"] = rcfg["effort"]
    return AgentDefinition(**kwargs)


def build_roles(llm_cfg: dict | None = None) -> dict[str, AgentDefinition]:
    return {name: make_role(name, llm_cfg) for name in _ROLES}
