# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Per-role subagents as SDK-native ``AgentDefinition``s (#02).

Each of Clouseau's five roles has a prompt, read-only MCP tools, and a model and
effort from ``agent.llm.roles``. Roles cannot launch subagents."""
from __future__ import annotations

from claude_agent_sdk import AgentDefinition

from crashclouseau import compiled_out, config

_SEARCHFOX = [
    f"mcp__searchfox__{name}"
    for name in ("calls_from", "calls_to", "calls_between", "define", "lookup",
                 "search", "field_layout")
]
# The call-graph trio on its own, for the two roles that WRITE the mechanism (patch-scout,
# data-flow-tracer). They had define/search/field_layout but no way to ask "who calls this";
# the tracer's own prompt says "read the path bodies" with nothing to establish the path.
# Granted 2026-09-02, after c171585's re-qualification retry had been read in prod
# (calls_to 29.1% -> 13.5% empty, calls_from 30.2% -> 5.8%). `lookup` stays withheld.
_CALLGRAPH = [f"mcp__searchfox__{name}" for name in ("calls_from", "calls_to", "calls_between")]
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
# Thread census and selected stack from the seed's processed crash.
_THREADS = ["mcp__crash__threads"]
# Roles request only their scoped MCP tools.
_BUILTIN_READ = []

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

# The compiled-out clause. 270 words -- 20 of them the three macro lists -- where the walk it
# replaces was 279 (`defe860`, `9216f51`), and three gates where that walk was one instrument.
#
# WHY IT SHRANK. Measured over 1996 prod dossiers (2026-07-06..08-05; 8901 skeptic claims, 1765
# `fail`): 124 claims reason about a build guard and 39 of them `fail`. Classified, PLATFORM 15
# (38%), DEBUG/assert 6 (15%), moz.configure-option 7 (18%), other 11 (cargo features /
# USE_MEMFD_CREATE / prefs). So the `set_define` -> `option()` -> `default=` walk this clause used
# to spell out served 2 of 39 -- and BOTH of those filings (bugs 2063782, 2063902) are now
# suppressed with no LLM at all by `_apply_compiled_out_gate`, verified end to end at a real
# pinned node where the diff ranker puts `gc::AutoMarkingLock` #1 of 8 and `hollow_symbols` fires
# on it and on none of its 7 siblings. Meanwhile 21 of the 39 name a macro in
# `compiled_out.GUARD_DENY` -- exactly the ones the CODE refuses to reason about, and the PROMPT
# named none of them. The sub-clause about the CITED LINE sitting inside an `#if` went with it:
# its own author measured it 0-of-3, and its trigger is noise (4 of 44 corpus_ship top-frame
# crash lines sit inside an `#if`, and 3 of those 4 are include guards -- GLCONTEXT_H_,
# SANDBOX_WIN_SRC_POLICY_ENGINE_PARAMS_H_ -- or MOZ_HAS_MOZGLUE).
#
# WHAT STAYED IS THE CONTEXT, NOT THE INSTRUMENT. Deleting the idea, or reverting `defe860`
# (`fail` -> `unverifiable`), or restricting `fail` to macros the walk can resolve, each turns
# ~38% of the correct noise-kills back into filed leads: the PLATFORM shape is 15 of the 39
# build-guard fails and 3 of the 8 BINDING vetoes in that month, and it is right every time
# (crash 560c0f2f-07cc-46c6-950c-1d8240260731, Windows nightly, a candidate touching 4 of 6 files
# under `widget/gtk/`). The three limits below are the deny-list the code already had, the `OS:`
# line `triage._crash_facts` already puts in this prompt, and the pref caveat that nothing had.
#
# THE THREE MACRO LISTS ARE RENDERED FROM `compiled_out` so the prompt and the gate cannot drift
# -- `tests/test_compiled_out_guard.py` pins that all 20 are present.
# The build name a prompt should use for each channel, and whether MOZ_DIAGNOSTIC_ASSERT is on.
# Read from each branch's own `moz.configure` -- see `compiled_out._CHANNEL_MACROS`.
_BUILD_NAME = {
    "nightly": "nightly",
    "beta": "beta",
    "aurora": "Developer Edition",
    "release": "release",
    "esr": "ESR",
}


def _build_name(channel):
    """The build's name in the skeptic prompt: ``nightly``, ``beta``, ``Developer Edition``,
    ``release`` -- or ``ESR 140`` for an ESR line, whose label (``esr140``) carries the major
    (`config.esr_major`) and whose build type is the family's (`config.channel_family`)."""
    ch = (channel or "nightly").lower()
    if ch in _BUILD_NAME:
        return _BUILD_NAME[ch]
    major = config.esr_major(ch)
    if major is not None:
        return "ESR {}".format(major)
    return _BUILD_NAME.get(config.channel_family(ch), ch)


def _compiled_out_text(channel=None):
    """The compiled-out clause of the skeptic prompt, FOR ONE CHANNEL.

    A FUNCTION AND NOT A CONSTANT, because the constant was wrong on beta in both directions.
    Three of the five macros it told the skeptic never to conclude "off" for are OFF on beta
    (`NIGHTLY_BUILD`, `EARLY_BETA_OR_EARLIER`, `MOZ_DIAGNOSTIC_ASSERT_ENABLED`) and one of the
    three it called genuinely-off is ON there (`RELEASE_OR_BETA`). So on a beta crash the old
    text refused the correct conclusion about nightly-gated code, asserted that 9-11% of the
    crashes are MOZ_DIAGNOSTIC_ASSERT crashes when on beta they cannot be, and handed out a free
    "this `RELEASE_OR_BETA` code isn't in the build" veto that is wrong by construction.

    The three macro lists are still RENDERED from `compiled_out`, now per channel, so the prompt
    and `is_build_flag_ground` cannot drift."""
    channel = (channel or "nightly").lower()
    on = compiled_out.channel_on_deny(channel)
    off = compiled_out.channel_off(channel)
    build = _build_name(channel)
    # Only assert the MOZ_DIAGNOSTIC_ASSERT prevalence where the macro is actually ON. On beta
    # it is off (`when=moz_debug | milestone.is_nightly | moz_dev_edition`), so the nightly
    # figure would be a false fact about the build in hand.
    diag = (
        " and 9-11% of the crashes we analyse are MOZ_DIAGNOSTIC_ASSERT crashes"
        if "MOZ_DIAGNOSTIC_ASSERT_ENABLED" in on else ""
    )
    # ...and off nightly, say the thing that is now TRUE and was previously forbidden.
    nightly_gated = ""
    if "NIGHTLY_BUILD" in off:
        nightly_gated = (
            "This crash is on {build}, NOT nightly: code behind `#ifdef NIGHTLY_BUILD` or "
            "`#ifdef EARLY_BETA_OR_EARLIER` (an alias for it) is genuinely ABSENT from this "
            "build, so a symbol whose whole body sits inside one of them does nothing here -- "
            "that is `fail`, and on this channel it is the commonest shape of the trap above. "
        ).format(build=build)
    return (
        "ONE THING THAT LOOKS LIKE A HOLE AND IS NOT: code that is not in THIS build. A symbol "
        "can be real, findable and linked while doing NOTHING because its whole body sits "
        "inside `#ifdef X` — `js::gc::AutoMarkingLock` (`js/src/gc/Cell.h`, \"a no op outside "
        "concurrent marking builds\") refuted three of our filings. So ask it of every symbol "
        "the MECHANISM DEPENDS ON, not just the ones you cited, reading bodies with "
        "`mcp__source__raw_file`. Machinery absent from the build that crashed makes the "
        "candidate demonstrably UNRELATED: that is `fail`, not `unverifiable`. {nightly_gated}"
        "THREE LIMITS, because a wrong \"off\" silently kills a good lead. (1) Never conclude "
        "\"off\" for {channel_on}: they are ON in the {build} build that crashed (an opt build "
        "DEFINES `NDEBUG`){diag}. {build_type_off} are the opposite — an official {build} is an "
        "OPT build, so \"this `#ifdef DEBUG` assertion is not in the shipped binary\" is true "
        "and free; read that off the build type, never off `moz.configure`. (2) A PLATFORM macro "
        "({platform}) is answered by this report's own `OS:` line above, never by a "
        "`moz.configure` walk. (3) A path reachable only when a `StaticPrefList.yaml` pref is on "
        "is `unverifiable`, NEVER `fail`: the YAML default is not what shipped (16 prefs ship "
        "the opposite value from firefox.js; 82 more default to a nightly-on build template). "
        "And a `moz.configure` default is evidence, not proof — a mozconfig can turn a "
        "default-off switch on — so a `fail` resting only on one is re-checked in code; say in "
        "your note which ground you used. "
    ).format(
        channel_on=", ".join(sorted(on)),
        build_type_off=", ".join(sorted(off)),
        platform=", ".join(sorted(compiled_out.PLATFORM_DENY)),
        build=build,
        diag=diag,
        nightly_gated=nightly_gated,
    )


# The NIGHTLY rendering, baked into `_ROLES` so the prompt a reader (and every existing test)
# sees is a real prompt rather than a template. `make_role` swaps it for the channel's own
# rendering when the crash is not on nightly.
_COMPILED_OUT = _compiled_out_text("nightly")

# The presence inversion. Crash 0027161c (2026-09-06): the skeptic failed a correct lead with
# "the 512KB cap is ALREADY present in this exact crash build, so the WAL-cap fix cannot be
# blamed for causing THIS instance". `schema.is_presence_ground` stops that `fail` from binding
# in code; this tells the model why, so it stops emitting it. Kept short on purpose.
_PRESENCE = (
    "BUILD TIMING: a candidate being PRESENT in the crashing build is the precondition for it "
    "to be the cause, never a refutation -- the only build-timing `fail` is a candidate ABSENT "
    "from the build (landed after it, or backed out before it). A change described as a fix, "
    "cap, tuning or mitigation can still be the regressor: shrinking or reverting an earlier "
    "mitigation re-exposes the failure it hid. A 'more frequent' claim is checked on TIMING: "
    "the rise must sit at the version boundary and be absent from versions and rollouts "
    "WITHOUT the change; a rise days into the version, or one the previous version shows too, "
    "fails it. "
)

# What a `pass` means. Bugs 2070489 and 2070554 (2026-09-09) each carried four skeptic checks,
# all `pass` or `unverifiable`, every one of them a TRUE fact that would have been equally true
# had the candidate been innocent ("the download uses necko", "the pref was on before", "the
# other window entries are not networking"). A skeptic that passes such facts is not a guardrail.
_RELEVANCE = (
    "A `pass` means the claim bears on whether THIS candidate caused THIS crash. A true code "
    "fact that would be equally true for an innocent candidate is not a pass -- note it as "
    "irrelevant. A mechanism whose every link is 'can', 'could' or 'may', with no link "
    "OBSERVED in this report (an annotation, the blocker's state, a facet, a changed line the "
    "crashing code executes), is `unverifiable` at best, and say that none of its links is "
    "observed. "
)

# An `actionable` verdict (2026-09-17) claims no causation, so the two objections that rightly
# fail a regressor claim -- "not in the window", "the signature predates it" -- are not
# contradictions of anything it says. Without this the skeptic's only teeth on that verdict
# would bite the one claim it does not make.
_ACTIONABLE_SKEPTIC = (
    "For an `actionable` verdict (a crash to be filed on its own facts, no regressor claimed) "
    "the claim under test is the MECHANISM -- the cited line and the condition that fires it. "
    "Its candidate is that code's ORIGIN by blame, so 'not in the window', 'landed long ago' or "
    "'the signature predates it' is NOT a `fail` there; only a mechanism contradicted by its own "
    "citations is. "
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
        "entry). Use `mcp__crash__threads` to inspect another thread; on a hang the "
        "awaited work may be there. Prefer decoded "
        "crash facts (crash_info.type/address, MOZ_CRASH_REASON, PHC alloc/free "
        "stacks, assertion text, async-shutdown fields) over guessing from the "
        "signature alone. End with one fenced ```json block shaped like: "
        "{\"uuid\":\"...\",\"signature\":\"...\",\"failure_class\":\"uaf|null_deref|"
        "assertion|oob|shutdownhang|other\",\"faulting_address\":\"...\","
        "\"moz_crash_reason\":\"...\",\"reason\":\"...\",\"crashing_thread\":0,"
        "\"frames\":[{\"stackpos\":0,\"function\":\"...\",\"filename\":\"...\","
        "\"line\":0,\"node\":\"...\",\"inlines\":[]}]}." + _GROUND,
        # Pinned source reads, so decoding a frame can look at the line it names; it used to have
        # the (unreachable) built-in file tools and nothing else.
        "tools": [*_BUILTIN_READ, *_SOURCE, *_THREADS],
    },
    "call-graph-explorer": {
        "description": "Navigate the searchfox call graph from crash frames to reach "
        "off-stack callers/callees, returning a cited neighborhood.",
        "prompt": "You are the call-graph explorer (navigator). Starting from the "
        "crash frames, drive the searchfox tools (calls_from/calls_to/calls_between/"
        "define/search) to build a cited neighborhood, explicitly reaching off-stack "
        "functions. Always pass symbols FULLY QUALIFIED — every namespace, no template "
        "`<...>` args (`js::jit::Assembler::PatchDataWithValueCheck`, not "
        "`Assembler::PatchDataWithValueCheck`); copy the qualification from the crash "
        "frames. An under-qualified symbol returns an EMPTY graph rather than an error, "
        "which reads as 'no callers' when callers exist. Start from the first actionable non-anchor frames, expand both "
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
        "git/hg/curl. Where a changed function is missing from the neighborhood, `calls_to`/"
        "`calls_from` bridge it to a crash frame — pass symbols FULLY QUALIFIED (every "
        "namespace, no template `<...>` args); an EMPTY graph is an unanswered question, not "
        "proof that nothing calls it. Match changed functions to the neighborhood and write a one-line, "
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
                  "mcp__searchfox__field_layout", *_CALLGRAPH],
    },
    "data-flow-tracer": {
        "description": "Read the function bodies along a call path and decide whether "
        "a change can free/mutate/null/overrun the crashing value.",
        "prompt": "You are the data-flow tracer. For a (candidate patch, crash frame) "
        "pair, read the changed lines with `mcp__patch__diff`; when the neighborhood does not "
        "already give the path from the changed function to the crash frame, establish it with "
        "`calls_to`/`calls_from`/`calls_between` (symbols FULLY QUALIFIED — every namespace, no "
        "template `<...>` args; an EMPTY graph is unanswered, not an absence); read the path "
        "bodies with define, then reason about whether the change can free/mutate/null/overrun the "
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
        "assumptions, Rust panic paths, and FFI boundary changes; the other threads of this "
        "report are readable with `mcp__crash__threads`. Return a cited "
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
                  "mcp__searchfox__field_layout", *_CALLGRAPH, *_THREADS],
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
        "macro/template edges. " + _COMPILED_OUT + _PRESENCE + _RELEVANCE + _ACTIONABLE_SKEPTIC + "A claim "
        "without a fresh citation cannot pass. A fault-address↔field "
        "claim is NOT a searchfox hole: re-run `mcp__searchfox__field_layout` on the "
        "FULLY-QUALIFIED containing type (with namespaces, no template `<...>` args — "
        "e.g. `mozilla::detail::nsTStringRepr`) and mark it `pass` (with a "
        "`struct_layout` citation object) when the faulting offset is a real field, "
        "`fail` when it isn't. If field_layout returns nothing, you under-qualified the "
        "name (add the namespace / drop template args) — do NOT settle for "
        "`unverifiable`. The call-graph family fails the SAME way and it is worse there: "
        "an EMPTY `calls_to`/`calls_from` is not evidence that nothing calls the symbol, so "
        "it can never CONFIRM a 'wired only into X' absence claim. Re-run it fully "
        "qualified (`js::gc::BufferAllocator::allocSmall`, not `BufferAllocator::allocSmall`) "
        "before you pass or fail such a claim. Remember searchfox indexes ~tip, not the crash build: a small "
        "crash-line vs tip-line delta (or a symbol moved/renamed at tip) is revision "
        "drift or inlining, NOT a contradiction — never `fail` a mechanism over a line "
        "delta when the diff and field-layout confirm it; use `unverifiable` at most. "
        "Use `mcp__crash__threads` to check any claim that a thread's stack is missing "
        "or about another thread's state; `fail` what the dump contradicts. "
        "End with one fenced "
        "```json block holding a LIST with ONE object per claim you checked, shaped "
        "like: [{\"claim_ref\":\"edge0|mechanism|hunk0|...\","
        "\"status\":\"pass|fail|unverifiable\",\"note\":\"...\",\"citations\":[...]}]"
        "." + _GROUND,
        "tools": [*_BUILTIN_READ, *_SEARCHFOX, "mcp__patch__diff", *_HISTORY, *_SOURCE,
                  *_THREADS],
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


def threads_tool_ids() -> list[str]:
    return list(_THREADS)


def role_names() -> list[str]:
    return list(_ROLES)


# The one sentence a Java/Kotlin crash appends to the four roles that read line numbers or lean
# on C++-only tools. The crash-interpreter lists frames with lines, the patch scout blames "the
# crashing line", the tracer and the skeptic call `field_layout` (a C++ struct question) and the
# skeptic reads a crash-line vs tip-line delta as drift -- every one of those is an assumption an
# R8-remapped stack breaks (2026-09-15 example: the reported lines named a KDoc comment and a
# licence header). The call-graph explorer gets nothing: it already asks for fully-qualified
# symbols and `searchfox._clean_symbol` converts the dotted spelling for it.
_JAVA_ROLE_NOTE = (
    " THIS IS A JAVA/KOTLIN STACK: its line numbers are R8-remapped and UNRELIABLE, so match a "
    "change to a frame by FILE and METHOD, never by line -- do not cite, blame or compare a "
    "frame's line; `field_layout` does not apply to JVM code; for `define` and the `calls_*` "
    "tools pass the `::`-joined fully-qualified name "
    "(`mozilla::components::lib::dataprotect::Keystore::generateKey`)."
)
_JAVA_ROLES = ("crash-interpreter", "patch-scout", "data-flow-tracer", "skeptic")


def make_role(name: str, llm_cfg: dict | None = None, channel: str | None = None,
              java: bool = False) -> AgentDefinition:
    """One subagent definition. ``channel`` re-renders the skeptic's compiled-out clause for a
    non-nightly crash (see ``_compiled_out_text``); every other role is channel-independent, and
    ``channel=None`` reproduces the nightly prompt byte-for-byte. ``java`` appends
    ``_JAVA_ROLE_NOTE`` to the four roles that would otherwise read an R8-remapped line as a
    source line; ``False`` (every native crash) changes nothing."""
    spec = _ROLES[name]
    # Prefer the (possibly swept) llm_cfg passed by build_options so a sweep's per-role
    # model/effort actually reaches the subagent; fall back to the base config.
    rcfg = ((llm_cfg or {}).get("roles") or {}).get(name)
    if rcfg is None:
        rcfg = config.get_llm_role(name)
    prompt = spec["prompt"]
    if channel and channel.lower() != "nightly" and _COMPILED_OUT in prompt:
        # A targeted swap rather than a template, so `_ROLES[...]["prompt"]` stays a real,
        # readable nightly prompt (which is also what the guard tests read).
        prompt = prompt.replace(_COMPILED_OUT, _compiled_out_text(channel))
    if java and name in _JAVA_ROLES:
        # Appended, like the compiled-out swap is targeted: the base prompt stays the readable
        # native one the guard tests read.
        prompt = prompt + _JAVA_ROLE_NOTE
    kwargs = dict(
        description=spec["description"],
        prompt=prompt,
        tools=list(spec["tools"]),
        model=rcfg.get("model", "inherit"),
        # CLI 2.1.223 ignored `background=False` for inline fan-out. Keep the
        # session-level switch in `triage._CLI_ENV`; recheck it on SDK upgrades.
        background=False,
    )
    if rcfg.get("effort"):
        kwargs["effort"] = rcfg["effort"]
    return AgentDefinition(**kwargs)


def build_roles(llm_cfg: dict | None = None,
                channel: str | None = None,
                java: bool = False) -> dict[str, AgentDefinition]:
    return {name: make_role(name, llm_cfg, channel, java=java) for name in _ROLES}
