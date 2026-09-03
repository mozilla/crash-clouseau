# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""The one crash-triage agent coroutine (#02).

``run_crash_triage`` assembles ``ClaudeAgentOptions`` (principal system prompt,
in-process MCP evidence servers, the five senior ``AgentDefinition``s, tiering,
options-level ``effort``), drives ``ClaudeSDKClient`` to a terminal
``ResultMessage``, and folds it into a typed ``CrashTriageResult`` — best-effort
parsing the trailing ```json handoff into a #03-validated ``Dossier`` (abstain on a
validation failure; ``MissingHandoffError`` when there is no readable block at all,
which is an infrastructure failure and not a verdict). #11 runs
``asyncio.run(run_crash_triage(...))`` inside the RQ job; the vendored
``run``/``run_async`` are NOT used (they SystemExit). Options assembly
(`build_options`) and result folding (`build_result`) are split out so they unit-
test without spawning the bundled CLI."""
from __future__ import annotations

import functools
import json
import os
import re
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

from crashclouseau import config, sigage, utils
from crashclouseau.agent import roles
from crashclouseau.logger import logger
from crashclouseau.agent.errors import MissingHandoffError
from crashclouseau.agent.result import CrashTriageResult
from crashclouseau.agent.schema import (
    Decision,
    NO_HANDOFF_REASON,
    parse_and_validate,
)
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

# THE thing that keeps the five subagents inline. Handed to the bundled CLI subprocess
# via ``ClaudeAgentOptions.env``, which the SDK MERGES over ``os.environ`` (options.env
# wins) -- see subprocess_cli.connect() -- so this one key adds nothing and clobbers
# nothing else.
#
# Why an env var and not ``AgentDefinition.background=False`` (which we also set, and
# which does NOT work -- the long WHY is in ``roles.make_role``): the CLI's Task/Agent
# launch computes
#     let q = F === "remote",
#         ee = q || (o === !0 || V.background === !0 || K || G || !A && o !== !1) && !U;
# ``V.background`` is only ever compared ``=== true``, so the agent definition is a
# one-way opt-IN to backgrounding; the trailing ``&& !U``, where ``U`` is this env var,
# is the only term in that expression that can turn it OFF. Setting it additionally
# makes the CLI omit ``run_in_background`` from the Agent (and Bash) tool schemas and
# drop the "agents run in the background by default, you will be notified" prompt
# bullets -- so it removes the model's incentive, not just the mechanism. Verified by
# live repro against the bundled CLI: 3/3 runs with it set emitted the ```json handoff,
# 4/4 comparable runs without it ended on a progress note. It also closes the MCP
# auto-background path (a >120s main-thread MCP call being parked as a task).
#
# The value MUST be one of "1"/"true"/"yes"/"on": the CLI parses it through a typed
# boolean, so "0"/"false"/"" read as OFF -- setting it to "0" does not "disable the
# workaround", it just leaves backgrounding enabled.
#
# Inline is not serial: the model still emits several Agent tool_use blocks in one
# message and the CLI runs them concurrently, which is the pre-0.2.131 behaviour this
# restores.
_CLI_ENV = {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}

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


# The revision-drift paragraph names a repo, and for a year there was only one. `system.md` keeps
# the nightly wording (it is the readable prompt, and the byte ledger measures it); a non-nightly
# crash gets these substitutions, which is the smallest change that cannot leave the two out of
# step.
_DRIFT_NIGHTLY = (
    "The searchfox tools read ~tip of mozilla-central, which is NEWER than the crash build."
)


def _drift_paragraph(repo, channel):
    """The revision-drift opener for a crash whose tools read *repo*."""
    return (
        "The searchfox tools read ~tip of {repo} — the {channel} branch, NOT trunk — which is "
        "NEWER than the crash build. Two branches matter here and they have DIVERGED: {repo} "
        "carries uplifts that are not on mozilla-central, and mozilla-central carries a whole "
        "train of changes that were never in this build. So code you find on trunk is not "
        "evidence about what shipped. Querying mozilla-central deliberately is often the right "
        "move — a {channel} regressor usually LANDED there first, so that is where its history "
        "and its original landing date live — but pass the repo explicitly and say in your note "
        "which tree you read.".format(repo=repo, channel=channel)
    )


# maxsize 8, not 1: the prompt is now per channel (nightly / beta / aurora / release), and a
# cache of one would re-read and re-substitute on every alternation.
@functools.lru_cache(maxsize=8)
def _system_prompt(channel: str | None = None) -> str:
    path = os.path.join(os.path.dirname(__file__), "prompts", "system.md")
    with open(path, "r") as handle:
        text = handle.read()
    if not channel or channel.lower() == "nightly":
        return text
    from crashclouseau.searchfox import repo_for_channel

    repo = repo_for_channel(channel).value
    if repo != "mozilla-central":
        text = text.replace(_DRIFT_NIGHTLY, _drift_paragraph(repo, channel.lower()))
        text = text.replace("## Revision drift (searchfox indexes ~tip, not the crash build)",
                            "## Revision drift (searchfox indexes ~tip of {}, not the crash "
                            "build)".format(repo))
        # The handoff EXAMPLE hands the model the repo token to echo into its citations. Left at
        # `mozilla-central` it invites a beta run to record trunk as the source of code it read
        # on the beta branch.
        text = text.replace('"repo": "mozilla-central"', '"repo": "{}"'.format(repo))
    return text


def _short_value(value, limit=300):
    """One crash fact, newline-flattened and head-truncated at ``limit`` chars.

    THE 300-CHAR CAP BITES ON EXACTLY TWO OF THE 23 FACTS ``_crash_facts`` WRAPS, so it is not
    the blunt instrument it looks like and the two renderers below must NOT be generalised to
    the rest -- on everything that was measured there is nothing there to recover. Measured on a
    2,800-report nightly control (200/day x 14d, 2026-08-07..2026-08-21, public SuperSearch):
    zero values over 300 chars for ``moz_crash_reason`` (n=1,469, longest 211), ``reason``
    (2,761, 72), ``cpu_info`` (2,676, 43), ``address`` (2,761, 18), ``adapter_driver_version``
    (2,559, 17), ``platform_pretty_version`` (2,800, 55), ``shutdown_progress`` (274, 31) and
    ``shutdown_reason`` (289, 10). It bites on ``xpcom_spin_event_loop_stack`` (1 of 334) and
    ``async_shutdown_timeout`` (141 of 143 = 98.6%), and those two get ``_render_spin_stack``
    and ``_render_async_shutdown``. (The control also measured ``ipc_shutdown_state`` (13, max
    237) and ``last_error_value`` (1,823, 27), both clean -- but NEITHER reaches ``_crash_facts``
    at all, so they are neighbours, not fields this function wraps.)

    THE NULL RESULT DOES NOT COVER ALL 23, so do not read it as one. Of the 23 fact lines: 2
    have the renderers below, 3 are PHC (unmeasurable, next paragraph), 7 are backed 1:1 by a
    clean column above (MOZ_CRASH_REASON, Crash reason, Crash type, Fault address, OS, Shutdown
    phase reached, Why shutdown started), 2 are DERIVED from a clean column, and the remaining 9
    -- Product, Version, Build ID, Process type, Report type, Assertion, Faulting instruction,
    the bit-flip line and the analysed-thread label -- were NOT MEASURED; they are short by
    construction, not by measurement. Derived is the part worth watching, because a summary can
    be longer than any column it reads: ``_cpu_summary`` renders 245 chars once the Raptor-Lake
    warning fires (measured, on a 29-char ``cpu_info``), i.e. 55 chars of headroom rather than a
    structural no-op. If that warning text grows, this cap is what will eat it.

    The three PHC lines are UNMEASURED rather than measured-clean: Socorro marks ``phc_kind`` /
    ``phc_alloc_stack`` / ``phc_free_stack`` ``view_pii``, so SuperSearch silently drops both
    the column and the filter (a ``phc_kind=!__null__`` query returns the whole nightly
    population) and ``/api/ProcessedCrash/`` omits them. Whether those lines render at all is
    an open prod question; Socorro documents the two stacks as comma-separated decimal
    addresses, which no truncation policy can make useful.

    Panel and sweeps: ``spike/CRASH_FACT_RENDERERS_PANEL.json``, rebuilt by
    ``spike/crash_fact_renderers_panel.py``.
    """
    if value is None or value == "":
        return ""
    text = str(value).replace("\n", " ").strip()
    if len(text) > limit:
        return text[:limit - 3] + "..."
    return text


# Where the collapse+cap sweep flattens: 400 is the first cap that loses no subsystem name at
# all, and 500/600/800 buy nothing more. See `_render_spin_stack`.
_SPIN_STACK_LIMIT = 400
# `async_shutdown_timeout`, after parsing. `state` is the counter-example rather than a nicety
# -- today's head-300 already keeps 79.5% of the state dicts and 79.7% of ALL states -- and 160
# is the smallest cap that clears that bar (80.0% / 80.1%); 0 drops `state` entirely, which is
# the variant the sweep KILLED (0.0%). The other three are budget choices and NOT optima: the
# sweeps in the artifact show 4+ blockers and 8 frames both score better on source files. See
# `_render_async_shutdown`.
_ASYNC_SHUTDOWN_BLOCKERS = 3
_ASYNC_SHUTDOWN_FRAMES = 6
_ASYNC_SHUTDOWN_STATE_LIMIT = 160
_ASYNC_SHUTDOWN_LIMIT = 900


def _render_spin_stack(value):
    """``xpcom_spin_event_loop_stack``, run-length collapsed and then capped at 400.

    The field is a ``|``-separated list of nested spin-loop entries and its own prompt label
    says "innermost last", so a head truncation drops the answer. PANEL: every Firefox-nightly
    report carrying the field over 10 weeks, 2026-06-12..2026-08-21 -- n=9,251 reports, 278
    distinct values. It is short almost always (p50=58, p90=71) and over 300 chars on 134 of
    9,251 = 1.45% of reports, 44 of the 278 distinct values.

    (a) THE OBVIOUS FIX, TAIL-PRESERVING TRUNCATION (head 150 + "..." + tail 147), IS MEASURED
    WORSE AND IS NOT SHIPPED. On those 134 values it does fix the innermost entry (lost 4 -> 0)
    but it raises the count of values losing SOME distinct subsystem name from 18 to 22, because
    an over-length value here is REPETITION and not deep nesting: all 103 values that hit
    Socorro's own 10,000-char annotation cap carry a median of 3 distinct entries in ~323 (e.g.
    uuid defd3256-091a-4e73-b575-c57f80260614 is ``nsThread::Shutdown: DOM Worker`` x321), so
    cutting the middle out drops entries the head was keeping. It is not a strict improvement
    over the status quo, which is the only bar a replacement has to clear.

    (b) SHIPPED INSTEAD: collapse repeats in place (a repeat rendered ``entry (xN)``) and then
    cap. FIRST-OCCURRENCE ORDER, EXCEPT THAT THE INNERMOST ENTRY IS FORCED BACK TO THE END:
    collapsing purely by first occurrence silently re-orders the line whenever the innermost
    entry also occurred earlier, and on this field that is not cosmetic -- the label says
    "innermost last" and calls it the primary lead. 20 of the 9,251 carry a non-adjacent repeat
    and 4 end on the wrong entry without the fix (see the code comment; one of them, 89910485,
    is 163 chars and is never truncated today). The trade is explicit: a repeat that was BOTH
    outermost and innermost now shows only at the innermost end, which is the end the label
    makes load-bearing. The panel counts this as ``mis_ordered_innermost``, because
    ``innermost_lost`` is a containment test and scores 0 either way. Cap swept 300/400/500/600/800 over the same 9,251: at 300 one
    value still loses its innermost entry and two lose a subsystem, at 400 both are zero, and
    500-800 change nothing. Today's head-300 loses the innermost subsystem on 4 of the 134 and
    some distinct subsystem on 18, of which 6 are a genuinely new name rather than a ``,SHDRCV``
    variant of one already in the head -- 6 of 9,251 = 0.065%, named:
    0631fbb4/22f3462b ``AsyncShutdown Spinner for quit-application``, e3a7c479
    ``nsThread::Shutdown: GraphRunner``, 0b80e246 ``nsThread::Shutdown:
    sqldb:formhistory.sqlite``, 7ca1cf8f ``nsThread::Shutdown: ProcessHangMon``, cef25046
    ``AudioCallbackDriver::Shutdown``.

    (c) THE COUNTER-EXAMPLE IS THE PROMPT BUDGET -- the reason the cap exists. ``_user_prompt``
    on three real filed hang crashes with a 40-frame seed and 20 candidates runs 8,994-13,136
    chars; this renderer adds p50 +0, p90 +0, absolute worst +100 and mean **-3.24** bytes, so
    it is a net saving. Second counter-example, a long value collapse CANNOT shrink: uuid
    fcb7058e-210c-47a9-954a-219bc0260807, 876 chars, 22 entries all distinct. At 400 its three
    subsystems (``sqldb:Login Data`` / ``sqldb:History`` / ``sqldb:Web Data``) all survive and
    only the per-thread ``#N`` suffixes are cut, which name nothing. Third: the repeat COUNT is
    itself information -- 321 stuck DOM workers -- and ``(x321)`` keeps it in 6 bytes rather
    than 9,900.

    NOT justified by the motivating case, which does not exist: the ``INNERMOST:`` marker in the
    worklist item appears in 0 of the 9,251 real values, and the crash it was drawn from (bug
    2063892, uuid 80e01888-f10a-4a4b-9120-b2aac0260816) carries a 65-char spin stack that is
    never truncated.
    """
    if value is None or value == "":
        return ""
    text = str(value).replace("\n", " ").strip()
    entries = [part.strip() for part in text.split("|")]
    counts, order = {}, []
    for entry in entries:
        if entry in counts:
            counts[entry] += 1
        else:
            counts[entry] = 1
            order.append(entry)
    # "Innermost last" is the field's own label, so the collapse must not change WHICH entry
    # ENDS the line -- and collapsing by FIRST occurrence does exactly that whenever the
    # innermost entry also appeared earlier. 20 of the 9,251 panel reports carry a non-adjacent
    # repeat and on 4 the collapsed line then ends on the wrong entry: 89910485 (163 chars,
    # never truncated today, innermost `GraphRunner` demoted to `DOM Worker`), cef25046 (one of
    # the six rescues below, innermost `DOM Worker` demoted to `AudioCallbackDriver::Shutdown`),
    # ab99e45d and e3a7c479 (a `,SHDRCV` variant of the same subsystem). The panel's
    # `innermost_lost` counter is a CONTAINMENT test -- it asks whether the name appears
    # ANYWHERE -- so it scores 0 either way and is blind to this by construction;
    # `mis_ordered_innermost` is the counter that sees it. Every other measured figure
    # (innermost/any-subsystem/new-subsystem loss, p50/p90/max/mean bytes) is identical with
    # this move in place, so it is free.
    if entries[-1] and order[-1] != entries[-1]:
        order.remove(entries[-1])
        order.append(entries[-1])
    collapsed = "|".join(
        entry + (" (x{})".format(counts[entry]) if counts[entry] > 1 else "")
        for entry in order
    )
    return _short_value(collapsed, limit=_SPIN_STACK_LIMIT)


def _render_async_shutdown(value):
    """``async_shutdown_timeout`` parsed and re-emitted as
    ``phase=<phase>; blocker "<name>" @ <file>:<line> <- <frames> state=<compact>``.

    THIS IS THE ONE FIELD THE 300-CHAR CAP ACTUALLY DESTROYS. PANEL: 4,880 Firefox-nightly
    values over 10 weeks, 2026-06-12..2026-08-21 -- raw p50=379, p90=1,061, p99=1,770,
    max=32,766, and 4,820 of 4,880 = 98.8% are over 300. The field is present on 5.1% of all
    nightly reports and truncated on 5.0% of them. What the head keeps is the part that was
    never at risk: 4,941 of 5,141 condition NAMES (96.1%), and 5,118 of those 5,141 (99.6%) are
    verbatim in the crash SIGNATURE anyway, so that channel is lossless with or without this
    renderer. What the head DESTROYS is the only source locations the field carries -- 939 of
    9,753 blocker source files survive (9.6%), 3,889 of the 4,820 truncated reports (81%) keep
    ZERO of them, and frames survive 662 of 11,673 (5.7%). Concretely, on the live filing bug
    2062062 (REOPENED, AsyncShutdownTimeout, uuid d29ac23d-eb9c-45f0-bf2c-8129b0260809) the
    head-300 cut the frame ``storage-rust.sys.mjs:null:375``.

    (a) THE OBVIOUS FIX, A RAISED GLOBAL CAP, IS MEASURED AND NOT SHIPPED: at 1,000 it recovers
    only 75.6% of the source files for mean +215 / p90 +700 bytes, at 2,000 79.2% for mean +259
    / worst +1,700, and most of those bytes go on re-printing one blocker's ``state`` string.
    Parsing buys 97.5% for mean +90.

    (b) THE COUNTER-EXAMPLE IS ``state``, WHICH TODAY'S HEAD-300 ALREADY KEEPS, because ``state``
    sits immediately after the blocker name: 3,272 of 4,118 state DICTS (79.5%) survive today.
    The first extractor dropped it -- files 97.6%, state **0.0%** -- and so ate the
    counter-example while scoring well on the metric it was built for. Sweeping the state cap at
    outer cap 900: 100 -> 73.7%, 120 -> 73.9%, 140 -> 78.7%, all BELOW today, and 160 -> 80.0%
    is the floor that clears it. Hence 160 rather than a rounder number.

    AND ``state`` IS NOT ALWAYS A DICT, which is the second way to eat the same counter-example:
    of the 5,201 non-empty states in the panel, 4,118 are dicts, 925 are bare strings and 158 are
    lists. Scored on DICTS ONLY the numbers above say 79.5% -> 80.0%; scored on ALL 5,201 states
    an ``isinstance(state, dict)`` renderer says 79.7% -> **63.4%**, i.e. a 16-point REGRESSION
    hidden by a denominator that excluded exactly what it dropped. Rendering every state instead
    costs mean +7.6 bytes and reads 79.7% -> 80.1%, above today on both denominators; the panel
    reports both as ``state_keys_pct`` and ``all_state_keys_pct``.

    THE OUTER CAP IS 900 BECAUSE OF ``state``, NOT BECAUSE OF THE FILES: 850 already holds source
    files at the same 97.5% (9,505 vs 9,506 of 9,753), so "smallest cap that keeps the files" is
    not what picks it -- 900 is the smallest that also holds ``state`` at 80.0% (850 -> 79.6%).
    Swept 500/600/700/800/850/900/1,000/1,200/uncapped in the artifact.

    THE OTHER TWO CONSTANTS ARE BUDGET CHOICES AND THE SWEEP SAYS SO, so do not read them as
    optima: ``_ASYNC_SHUTDOWN_BLOCKERS=3`` buys NOTHING against no cap at all -- same +600 worst
    case (the outer 900 already bounds it) and 3 FEWER files (9,506 vs 9,509) -- it is there to
    bound the work on the 47-condition tail and to make the drop explicit with
    ``(+N more blockers)``. ``_ASYNC_SHUTDOWN_FRAMES=6`` is not a flattening point either: 8
    frames would take source files 97.5% -> 98.8% for mean +3.7 bytes at an identical p90 and
    worst case. 6 is the conservative end of that trade, not the measured knee.

    (c) THE OTHER COUNTER-EXAMPLE IS THE PROMPT BUDGET: p50 +19, p90 +343, absolute worst +600,
    mean +90 bytes against an 8,994-13,136-char ``_user_prompt`` -- +0.8% on the mean, +7% on
    the smallest prompt in the worst case. Run in situ at HEAD, bug 2062062's prompt came out 1
    char SHORTER (13,136 -> 13,135) while gaining the frame it had been losing, and the three
    other filed hang crashes (9,949 / 8,994 / 9,705) were byte-identical.

    ON A PARSE FAILURE IT FALLS BACK TO ``_short_value`` and behaviour is exactly today's. That
    is not defensive habit: 1 of the 4,880 values (uuid d44e2101-80b8-4a5e-8d05-608070260616,
    32,766 chars) is not JSON, because Socorro's own annotation cap severed it mid-string.
    """
    if value is None or value == "":
        return ""
    text = str(value)
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return _short_value(text)
    if not isinstance(payload, dict):
        return _short_value(text)
    conditions = [c for c in (payload.get("conditions") or []) if isinstance(c, dict)]
    parts = ["phase={}".format(payload.get("phase", ""))]
    for cond in conditions[:_ASYNC_SHUTDOWN_BLOCKERS]:
        part = 'blocker "{}"'.format(cond.get("name", ""))
        if cond.get("filename"):
            part += " @ {}:{}".format(cond["filename"], cond.get("lineNumber", ""))
        stack = cond.get("stack") or []
        if isinstance(stack, str):
            stack = [stack]
        frames = list(dict.fromkeys(str(f) for f in stack))[:_ASYNC_SHUTDOWN_FRAMES]
        if frames:
            part += " <- " + " <- ".join(frames)
        state = cond.get("state")
        # EVERY state, not just the dict ones. `state` is a dict on 4,118 of the panel's
        # conditions but a bare string on 925 and a list on 158, and an `isinstance(state, dict)`
        # test would have dropped all 1,083 -- which is the counter-example this renderer exists
        # to keep, scored on a denominator that had silently excluded the cases it loses. Real
        # example: uuid b43e9c0d-7465-4acd-bfe8-2546d0260708, whose 422-char value today's
        # head-300 truncates AFTER `"state":"1. Service has initiated shutdown"`.
        if _ASYNC_SHUTDOWN_STATE_LIMIT and state not in (None, "", [], {}):
            flat = (state if isinstance(state, str)
                    else json.dumps(state, separators=(",", ":")))
            part += " state=" + _short_value(flat, _ASYNC_SHUTDOWN_STATE_LIMIT)
        parts.append(part)
    dropped = len(conditions) - _ASYNC_SHUTDOWN_BLOCKERS
    if dropped > 0:
        parts.append("(+{} more blockers)".format(dropped))
    return _short_value("; ".join(parts), limit=_ASYNC_SHUTDOWN_LIMIT)


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


# PCI vendor id -> readable GPU vendor, so a graphics/driver crash's hardware is legible to
# the agent (e.g. an NVIDIA-only regressor is likelier for an NVIDIA crash). Unknown ids
# pass through unchanged.
_GPU_VENDORS = {
    "0x10de": "NVIDIA", "0x1002": "AMD/ATI", "0x8086": "Intel", "0x1414": "Microsoft",
    "0x5143": "Qualcomm", "0x106b": "Apple", "0x13b5": "ARM", "0x15ad": "VMware",
    "0x80ee": "VirtualBox", "0x1ab8": "Parallels",
}


def _gpu_summary(raw: dict) -> str:
    """Readable GPU line from the processed crash's adapter fields, or "" when absent."""
    vid = str(raw.get("adapter_vendor_id") or "").strip()
    if not vid:
        return ""
    parts = [_GPU_VENDORS.get(vid.lower(), vid)]
    for label, key in (("device", "adapter_device_id"), ("driver", "adapter_driver_version")):
        v = str(raw.get(key) or "").strip()
        if v:
            parts.append("{} {}".format(label, v))
    return " ".join(parts)


# rust-minidump emits one candidate per register it can correct; more than a few says the
# heuristic is casting a wide net, not that the case is stronger.
_MAX_BIT_FLIPS = 3


def _bit_flip_summary(info: dict) -> str:
    """Socorro's stackwalker verdict on whether the FAULT ADDRESS is trustworthy at all, or "".

    THE FACT THAT WAS MISSING. Bug 2061961 was filed, and needinfo'd at a developer, for crash
    ff888d42-ce3e-4308-8c2f-b3f060260807 -- whose processed crash said, in this very dict,
    ``possible_bit_flips: [{address: 0x0, confidence: 0.625, source_register: rax,
    details: {is_null: true}}]``. The reported address ``0x00000001000000d0`` is one flipped bit
    from ``0xd0``, i.e. a NULL base plus a struct offset, and had the pointer really been null the
    code would have taken its ``None`` branch and not crashed at all. It was hardware. The
    agent, its five subagents and the blind second opinion (which shares this function -- see
    ``second_opinion._user_prompt``) all reasoned about a wild pointer because this dict was
    opened for ``type``/``address``/``crashing_thread`` and nothing else, so a fluent
    use-after-free story had nothing to contradict it. Two developers closed it INVALID in two
    days using precisely this field.

    Renders the DISCRIMINATING detail rather than the bare score, because the flags are what
    make the number readable: ``is_null``/``was_non_canonical`` argue for a flip, while
    ``poison_registers`` argues AGAINST one (a poison value means a use-after-free -- software)
    and ``was_low`` means the corrected value is small enough to have arisen many other ways.

    Kept SHORT on purpose: ``_short_value`` truncates every fact at 300 chars, and the flags are
    the part that must survive."""
    flips = info.get("possible_bit_flips")
    if not isinstance(flips, list) or not flips:
        return ""
    parts = []
    for flip in flips[:_MAX_BIT_FLIPS]:
        if not isinstance(flip, dict):
            continue
        details = flip.get("details") or {}
        notes = [name for name, key in (
            ("NULL", "is_null"),
            ("non-canonical", "was_non_canonical"),
            ("low, so weak", "was_low"),
            ("POISON, so likelier a UAF than a flip", "poison_registers"),
        ) if details.get(key)]
        try:
            pct = "{:.0f}%".format(float(flip.get("confidence")) * 100)
        except (TypeError, ValueError):
            pct = "?"
        parts.append("{} should have been {} (conf {}{})".format(
            flip.get("source_register") or "fault address",
            flip.get("address") or "?", pct,
            "; " + ", ".join(notes) if notes else "",
        ))
    return "; ".join(parts)


def _cpu_summary(raw: dict, sysinfo: dict) -> str:
    """The crashing machine's CPU, with a warning when the silicon itself is the likely culprit.

    THE FIELD USED TO BE UNREACHABLE. This line was ``("CPU arch", _first_present(cpu_arch, ...,
    cpu_info, ...))`` — and ``cpu_arch`` is populated on essentially every crash, so the
    ``cpu_info`` fallbacks behind it could never be taken and the agent has never once seen which
    processor a crash came from. Timothy Nikkel, on bug 2064600: "there is also several of the
    known buggy family 6 model 183 stepping 1 without a bit flip annotation. Something else you
    might want to add to your llm prompt. I always look for these two things in crash reports."
    The first of his two things was already here; the second was three fallbacks deep in a
    ``_first_present`` that always short-circuited before reaching it.

    ``amd64`` and ``family 6 model 183 stepping 1`` answer different questions, so both are
    shown rather than the first one found -- and the arch is load-bearing for more than the CPU:
    emilio refuted bug 2065969 partly because the mechanism we filed lived behind
    ``#[cfg(target_pointer_width = "64")]`` on a report whose arch was ``x86``.

    The defective-CPU warning compares `sigage.cpu_model`, not the raw string: Socorro prefixes
    `cpu_info` with the vendor on 32-bit builds, so until 2026-08-24 the agent was never told the
    silicon was suspect on exactly those reports."""
    arch = _first_present(raw.get("cpu_arch"), sysinfo.get("cpu_arch"))
    info = _first_present(raw.get("cpu_info"), sysinfo.get("cpu_info"))
    parts = [str(p) for p in (arch, info) if p]
    if not parts:
        return ""
    out = ", ".join(parts)
    if sigage.cpu_model(info) in sigage.BROKEN_CPU_MODELS:
        # Kept short deliberately: `_short_value` truncates every fact at 300 chars, and the
        # warning is the part that must survive next to a 36-char CPU string.
        out += (" — KNOWN-DEFECTIVE CPU (Intel Raptor Lake, meta bug 1975808): its documented "
                "instability corrupts computation on correct software, so a wild pointer or "
                "impossible state here may be the processor, not the code.")
    return out


def _cpu_spread_line(noise: dict, crash: dict | None = None) -> str:
    """Which PROCESSOR MODELS this signature's reports come from, as one line, or ``""``.

    BUG 2065373, and :jstutte's review of it: "could clouseau do some OS / install distribution
    checks on the socorro data?" The run already had the answer and threw it away — 58 reports,
    a single `cpu_info` row, `family 25 model 117 stepping 2` — while handing both models
    `broken_cpu_rate` 0.0, a hardware clean bill computed from the very rows that say the whole
    population is one processor.

    STATED WITH ITS BACKGROUND, AND OUTSIDE THE PARAGRAPH ABOVE. Both are load-bearing. "58 of
    58 reports are on one CPU model" as bare text is an abstain instruction, and it would be the
    wrong one: 26 of 200 sampled nightly signatures (13%) sit on exactly one model, and 8 of the
    20 that are one-model with >=20 reports carry a real Firefox bug — among them bug 2056116,
    the off-stack pref-flip archetype this repo ships, and bug 1993828. Swept as a suppressor
    over the 52 filed bugs, every threshold from 0.40 to 0.95 ate a FIXED or DUPLICATE one (see
    `orchestrator._signature_is_mostly_hardware`). So the line says SCOPE, carries the 0.32
    population median in the same breath, and sits outside the "the higher these are, the
    likelier a failing-hardware artefact" paragraph, which is a suppression instruction.

    SILENT UNDER `sigage.POPULATION_TOP_CPU_SHARE_MIN_REPORTS`, because below it the number is
    arithmetic rather than evidence: one report carries one `cpu_info` string, so the share is
    exactly 1.00 by construction, and the 32%/13% background it is stated against was measured
    only over signatures with >=5 reports. That is not a corner: 18 of the canary's 52 filings
    have exactly one report and 17 of those read 1.00. The gate's own `min_signature_reports`
    exists for the same reason -- "below it any percentage is noise, 1 of 3 being 33%".

    Costs nothing: `sigage.hardware_noise` fetched the whole `cpu_info` facet already, to count
    Raptor Lakes."""
    share = noise.get("top_cpu_share")
    seen = noise.get("cpu_reports")
    terms = noise.get("cpu_terms")
    if share is None or not seen or not terms:
        return ""
    if seen < sigage.POPULATION_TOP_CPU_SHARE_MIN_REPORTS:
        return ""
    # The BACKGROUND half of this sentence exists only for a channel whose median was actually
    # measured. 0.32 comes from 200 Firefox-NIGHTLY signatures and nobody has run that sample on
    # beta, so on beta the share is stated with no "the median signature sits at ..." beside it.
    # Quoting the nightly median to a beta run would be the `hardware-noise-denominator` mistake
    # in the direction that reads as evidence.
    median = sigage.population_top_cpu_share_median((crash or {}).get("channel"))
    background = (
        " Background: the median {} signature with at least 5 reports sits at {:.0f}%, and 13% "
        "of them (26 of 200 sampled 2026-08-21) sit at 100% — one processor model is ORDINARY, "
        "and every suppression threshold tested on this statistic suppressed a crash that was "
        "later FIXED.".format(sigage.population_label((crash or {}).get("channel")),
                              100 * median)
        if median is not None else
        " Background: how concentrated a typical signature on this channel is has NOT been "
        "measured (the 32% figure this note quotes elsewhere is Firefox-nightly's), so treat "
        "the number above as unbenchmarked — do not read it as high or as low."
    )
    # THE CLOSING CLAUSE NAMES ITS OWN SAMPLE. It used to say "in that same sample ... (9 of the
    # 26 at 100%, against 118 of the 200 overall)" unconditionally -- so on a channel with no
    # measured median it landed one sentence after "how concentrated a typical signature on this
    # channel is has NOT been measured", leaving "that same sample" pointing at a sample the
    # previous sentence had just disowned and two bare denominators ("the 26", "the 200") with
    # nothing defining them. Live in 38 of 38 beta prompts, and in the blind second opinion's.
    #
    # Kept rather than dropped on the unmeasured channels, because it is an ANTI-support caveat
    # (34.6% vs 59%: concentration is evidence AGAINST a bug, not for one), so quoting it errs
    # toward abstaining; and the correlation is a different statistic from the median this
    # function benchmarks against. Naming the population is what makes it honest on any channel.
    concentration_caveat = (
        "but concentration is not support for a bug either: in the Firefox-nightly sample of 200 "
        "signatures (2026-08-21), the 26 that sit at 100% one model carry a known Firefox bug "
        "LESS often than the rest — 9 of those 26, against 118 of the 200 overall"
    )
    return (
        "CPU-MODEL SPREAD OF THIS SIGNATURE — a fact, not a verdict, and deliberately stated "
        "apart from the paragraph above. Of the {} reports that carry a cpu_info string, "
        "{:.0f}% are on {}{}.{} Read it as SCOPE and as evidence "
        "in NEITHER direction: when a signature is confined to one CPU model, one GPU driver "
        "or one distribution, naming which is worth more than calling the population small — "
        "{}.".format(
            seen, 100 * share, noise.get("top_cpu_term") or "one model",
            ", the only model seen" if terms == 1
            else ", one of {} models seen".format(terms),
            background, concentration_caveat)
    )


def _signature_trend_lines(crash: dict) -> list[str]:
    """Has this signature's crash RATE changed, as prompt lines, or ``[]``.

    THE QUANTITY THE SELECTOR CANNOT COMPUTE, and the reason bug 2063336 was filed by a human
    instead of by us. That signature put at most 2 crashes on any one build-day, so the spike rule
    saw a single-crash from-zero fire — indistinguishable from the ~187 signatures that crash
    exactly once on a normal nightly day, and 67% of what the rule emits. It selected the signature
    on 20 run-days and the pipeline said nothing, while :aryx filed off the statistic below: "single
    digit crashes per version to 14 reports from 14 installs of Firefox 155.0a1".

    Stated as a RATE against the signature's own history with the channel's daily installation
    count as the denominator, because neither half is optional. Nightly's distinct installs per day
    fell from a median 860 in June to 462 in August, so over that ramp a signature at a constant
    per-install rate lost half its raw count — a bare "9 crashes this week against 5 in the two
    months before" would have the model reading the user base.

    NO SCORE IS SHOWN. `sigtrend.tail_score` is an ordering statistic whose tail is
    anti-conservative by three to five orders of magnitude against a shuffled null; printing it as
    a probability would hand the model a confidence that does not exist. The ratio and the two
    counts are what a human would say out loud, and they are what goes in.

    Ends by naming what the fact does and does not license, because the failure mode here is
    specific and was measured: the crash STACK does not change across one of these rises (85% of
    the anchor's post-onset reports sit on a proto-signature that already existed), so a model told
    "the rate went up 15x" can invent a new mechanism to explain a mechanism that did not change.
    The rate change is evidence that something made an EXISTING failure more likely, which is a
    different question from what introduced it.

    THE BLIND SECOND OPINION GETS THIS TOO, via the shared ``_crash_facts``, on the
    ``_hardware_noise_lines`` reasoning: it is a FACT both models are otherwise blind to, not a
    suggested direction, so withholding it would only make the second opinion less informed."""
    from crashclouseau import sigtrend

    facts = crash.get("signature_trend") or {}
    sentence = sigtrend.describe(facts)
    if not sentence or not sigtrend.is_rising(facts):
        return []
    return [
        "",
        "SIGNATURE CRASH RATE HAS RISEN:",
        "  " + sentence,
        "  This is a change in how OFTEN an existing failure happens, not evidence of a new "
        "failure: on a rise like this the crash stack typically does not change at all. Treat it "
        "as a reason to look for something that made this code path more likely to fail — a "
        "timing, size, ordering or configuration change — rather than for whatever first "
        "introduced the crash, which may be years old. The signature's first-seen build "
        "therefore does NOT rule a candidate out: a change that landed long after this crash "
        "first appeared can still be what made it frequent.",
    ]


def _watchdog_lines(crash: dict) -> list[str]:
    """What a hang / timeout crash IS, as prompt lines, or ``[]`` for a fault.

    Shared with the blind second opinion through ``_crash_facts``, like the rate block: it is a
    fact about the report, not a direction. Both models otherwise reason about it as a fault, and
    the two arguments that follow from that are inverted here. "The change touches no code on
    the stack and cannot itself hang" -- the stack is the WATCHDOG's or the waiting thread's, and
    the work being waited on is elsewhere by construction. "The signature predates the change" --
    a watchdog signature fires whenever the awaited work exceeds its budget, so it is typically
    years old and a change that adds work regresses it without introducing it. On 2026-08-15 both
    were used to refute a lead the module owner confirmed the next day (bug 2063892)."""
    raw = crash.get("raw_crash") or {}
    dump = raw.get("json_dump") or {}
    if not utils.is_watchdog_crash(
        crash.get("signature") or raw.get("signature"),
        raw.get("report_type"),
        raw.get("moz_crash_reason") or dump.get("moz_crash_reason"),
    ):
        return []
    return [
        "",
        "WATCHDOG / TIMEOUT CRASH: nothing faulted at the crashing frame; a watchdog killed the "
        "process because work elsewhere exceeded a time budget. Ask what made that work slower, "
        "not what is wrong with the frames shown.",
        "  A candidate explains this crash if it adds time to the awaited work (more I/O, more "
        "items, an extra fsync, rescan or lock, a wait on another thread), even when it touches "
        "no code on this stack.",
        "  The signature's age is NOT a refutation: a timeout signature fires whenever the budget "
        "is exceeded, so it is usually years old, and a change that adds work regresses it "
        "without introducing it.",
    ]


def _hardware_noise_lines(crash: dict) -> list[str]:
    """How much of this SIGNATURE is hardware error, as prompt lines, or ``[]``.

    THE SECOND HALF OF THE BUG-2064600 FIX, and the half that needed a new measurement. The
    ``POSSIBLE BIT FLIP`` fact above reads the ONE report being triaged. Timothy Nikkel's reply
    to that filing was about the signature: "About 50% of the crashes with this signature have
    non-zero bit flip probability. That might be something you want to include in your llm prompt
    to consider." He was right, and the report we had triaged was clean on both counts — no flip
    annotation, an ordinary Rocket Lake CPU — so nothing a per-report check could ever read would
    have revealed how much of the signature is hardware noise. (The 71% this docstring used to
    quote was 29.3% flips + 42.6% Raptor Lake over ALL products and channels across 180 days,
    n=331 — the denominator `sigage.hardware_noise` refuses, because it kills bug 2062219, FIXED.
    On the denominator that runs, Firefox nightly over 364 days, the same signature read 6
    reports at 50% and 17% on 2026-08-19 and 5 reports at 60% and 20% on 2026-08-21. A share
    without its denominator and its date is not a measurement.)

    Stated WITH the population rate, because a bare "50%" is unreadable: a model cannot know
    whether that is alarming without knowing that nightly as a whole runs at 2.5%.

    Ends on the question Nikkel says comes next — "Determining if there is a signal in the
    remaining crashes that isn't hardware error is the next step" — because that, and not
    "is this signature noisy", is the thing the agent is actually being asked to decide.

    THE BLIND SECOND OPINION GETS THESE TOO, via the shared ``_crash_facts``, and that is
    deliberate: it is the ``_archetype_lines`` reasoning in reverse. An archetype is a suggested
    DIRECTION, so priming both models with it would correlate their mistakes; this is a FACT that
    both were blind to, and the original bit-flip fix established that the right move there is to
    tell both. A second opinion that independently re-derives a use-after-free story from a
    corrupted address is not independent, it is uninformed."""
    noise = crash.get("hardware_noise") or {}
    sample = noise.get("reports")
    flip = noise.get("bit_flip_rate")
    cpu = noise.get("broken_cpu_rate")
    if not sample or (flip is None and cpu is None):
        return []
    # THE POPULATION IS THIS CRASH'S OWN CHANNEL'S. Beta reads 6.75% / 5.82% against nightly's
    # 2.55% / 4.15%, so quoting nightly's here would tell a beta run that an ordinary beta
    # signature is 2.6x the population -- immediately before the paragraph that says a high
    # share means any mechanism it constructs "will be fiction that fits". An UNMEASURED
    # population (release) drops the comparison rather than borrowing nightly's.
    channel = crash.get("channel")
    pop_flip = sigage.population_bit_flip_rate(channel)
    pop_cpu = sigage.population_broken_cpu_rate(channel)
    pop_name = sigage.population_label(channel)
    bits = []
    if flip is not None:
        bits.append("{:.0f}% carry a Socorro bit-flip annotation{}".format(
            100 * flip,
            "" if pop_flip is None else
            " ({} population: {:.0f}%)".format(pop_name, 100 * pop_flip)))
    if cpu is not None:
        bits.append("{:.0f}% come from a known-defective Intel Raptor Lake CPU (family 6 model "
                    "183 stepping 1, meta bug 1975808{})".format(
                        100 * cpu,
                        "" if pop_cpu is None else
                        "; {} population: {:.0f}%".format(pop_name, 100 * pop_cpu)))
    out = [
        "",
        "HARDWARE-ERROR SHARE OF THIS SIGNATURE, on this crash's own channel over the last "
        "year. Of its {} reports, {}. The two rarely overlap, so they add up.".format(
            sample, "; and ".join(bits)),
        "What to do with that: the higher these are, the likelier it is that this signature is "
        "a failing-hardware artefact with no software defect behind it at all, and that any "
        "mechanism you can construct for it will be fiction that fits. Say so plainly if you "
        "think that is what you are looking at. If you do still believe there is a real bug "
        "here, the burden is to show a signal in the crashes that are NOT hardware error — this "
        "report's own CPU and bit-flip fields above are the first place to check.",
    ]
    # Appended as its OWN paragraph, never folded into the two above: concentration is not a
    # hardware-error share and must not inherit that paragraph's instruction.
    spread = _cpu_spread_line(noise, crash)
    if spread:
        out.append(spread)
    return out


def _days_phrase(days):
    """``"less than a day"`` / ``"1 day"`` / ``"3207 days"``. A crash brief that says "1 days"
    reads as a template, and the point of this block is to be read."""
    if days < 1:
        return "less than a day"
    if days < 2:
        return "1 day"
    return "{:.0f} days".format(days)


def _before_this_build(days, first_seen, buildid):
    """How far back a first-seen sits from the build being triaged. Says "this crash's own build"
    only when it IS that build -- an age under a day does not mean the same build."""
    if str(first_seen) == str(buildid):
        return "which is this crash's own build"
    return "{} before the build that produced this crash".format(_days_phrase(days))


def _signature_age_lines(crash: dict) -> list[str]:
    """How old this signature already was when the build crashed, as prompt lines, or ``[]``.

    THE FACT WE FETCH EVERY RUN AND TELL NOBODY. Until this block the signature's age reached no
    prompt, no bug comment and no human — while nine of the ten `Core :: JavaScript*` filings a
    module owner rejected accused a changeset that landed 283 to 3205 days AFTER the signature was
    already crashing. In the archive the reverse holds: 12 of 12 bugs that named a regressor also
    said which build the signature started in.

    STATED FROM THE UNBOUNDED CLOCK, which is the whole point. ``signature_first_seen_windowed``
    comes from a 364-day SuperSearch that Socorro's ~178-day retention truncates further, and
    truncation only ever moves first-seen FORWARD — so it reads an ancient signature as a recent
    one, never the reverse. Measured on the first prod day after ``86f6799``: of the seven dossiers
    the windowed clock called 0 days old, five sat on signatures 1098, 1413, 1631, 2255 and 2255
    days old. Printing the windowed figure would have printed that error to a developer.

    IT GOES IN ``_crash_facts``, so the blind second opinion gets it too, and that is the
    bit-flip precedent rather than the archetype one: an age is a FACT both models are blind to,
    not a suggested direction. It is also the specific blindness measured on this population —
    the SO corroborated 11 of 11 JS filings because "is this mechanism plausible?" is a question
    a nine-year-old signature does not change the answer to, while "did this patch create this
    crash?" is a question it settles.

    AND IT IS NOT A GATE. The guidance says in terms that an old signature does not clear a
    candidate, because that is measured: on FIXED bug 2061960 the signature was 326 days stale, we
    named Jan-Niklas Jaeschke, and jjaschke pushed the fix. ``sigage.first_seen_ever`` records the
    eight FIXED/DUPLICATE filings that would be lost if this number were wired to a rung."""
    windowed_seed = crash.get("signature_first_seen_buildid")
    facts = sigage.age_facts(
        crash.get("buildid"),
        windowed_seed,
        crash.get("signature_first_seen_ever"),
        observed=crash.get("signature_first_seen_any") or windowed_seed,
    )
    if not facts:
        return []
    ever = facts.get("signature_first_seen_ever")
    age_ever = facts.get("signature_age_days_ever")
    windowed = facts.get("signature_first_seen_windowed")
    age_win = facts.get("signature_age_days_windowed")
    drift = facts.get("signature_clock_drift_days")

    said, guidance = [], _OLD_SIGNATURE_GUIDANCE
    if facts.get("signature_rename_suspected"):
        # One-directional by construction (see `sigage.age_facts`): this may only ever take
        # novelty AWAY. The name is new; the crash under it is not.
        said = ["SIGNATURE AGE: this NAME is new — Socorro first recorded it in build {} ({}) —"
                " but crash-stats holds reports of it on builds up to {} OLDER than that, which"
                " is only possible if crashes that already existed were re-signatured onto this"
                " name. The signature changed; the crash did not necessarily start.".format(
                    ever, sigage.buildid_day(ever), _days_phrase(abs(drift)))]
    elif age_ever is not None:
        said = ["SIGNATURE AGE: this signature was first seen anywhere in build {} ({}), {}."
                .format(ever, sigage.buildid_day(ever),
                        _before_this_build(age_ever, ever, crash.get("buildid")))]
        if age_win is not None and drift is not None and drift >= sigage.CLOCK_DISAGREEMENT_DAYS:
            said.append(
                "Do not be misled by crash-stats itself here: a 364-day search only reaches build"
                " {} ({}) and would call this signature {} old. Socorro's search index keeps"
                " roughly six months, so it can only ever make a signature look NEWER than it is;"
                " the older figure is the true one.".format(
                    windowed, sigage.buildid_day(windowed), _days_phrase(age_win)))
        if age_ever <= sigage.NEW_SIGNATURE_DAYS:
            guidance = _NEW_SIGNATURE_GUIDANCE
        else:
            # NEW TO THIS CHANNEL, OLD EVERYWHERE. Off nightly the two are routinely different
            # and the difference is the whole question. A regressor that landed on
            # mozilla-central during the previous cycle gives an all-time first-seen of 7-35
            # days at a 4-week cadence (7-21 at two weeks) -- old enough for the guidance above
            # to tell the model that a changeset landing after the signature existed cannot have
            # created it, while the candidates beside it all print the merge date. Both halves
            # then point away from the only changeset set that can contain the origin.
            channel_said, channel_guidance = _channel_age_lines(crash, ever, age_ever)
            if channel_said:
                said.extend(channel_said)
                guidance = channel_guidance
    elif age_win is not None:
        # No row in `SignatureFirstDate`. Measured over 14 days of prod: 7.6% of dossiers analyse
        # a crash within an hour of its signature's first report EVER, and the table's cron has
        # not minted a row by then — every post-deploy dossier whose signature was over a day old
        # got an answer (36/36) and the two that did not were 2.5 and 14 minutes behind the first
        # report ever. The other reason is rarity: all four signatures still without a row after
        # 14 days had exactly one report ever. Both readings point new, neither is proof.
        said = ["SIGNATURE AGE: not established. Socorro's all-time first-appearance table has no"
                " row for this signature, which happens when a signature is newer than the daily"
                " job that maintains that table or when it has only ever produced one report. All"
                " a 364-day crash-stats search can say is that it was already crashing in build {}"
                " ({}){}. Both of those point NEW and neither is proof, so weigh it as 'probably"
                " recent, not established' — 'we could not date it' is not 'it is new'.".format(
                    windowed, sigage.buildid_day(windowed),
                    " — this crash's own build" if str(windowed) == str(crash.get("buildid"))
                    else ", {} before this one".format(_days_phrase(age_win)))]
        guidance = _UNDATED_SIGNATURE_GUIDANCE
    else:
        return []
    return ["", *said, guidance]


# The three closers. Split because the first sentence is the one that gets read, and leading a
# one-day-old signature with "a changeset that landed long after" is noise on the case where the
# pushlog window is actually trustworthy.
_OLD_SIGNATURE_GUIDANCE = (
    "What to do with that: a changeset that landed long after the signature was already crashing"
    " cannot be what CREATED this crash, and saying it did is the error a module owner rejected"
    " ten of our filings for. It does NOT clear the changeset — a new patch can perfectly well"
    " start crashing code that has crashed under this name for years, and we have been right about"
    " exactly that (bug 2061960: the signature was 326 days old, and the developer we named pushed"
    " the fix). So the claim worth making here is what a change did to a crash that was ALREADY"
    " HAPPENING — a new caller, a newly reachable path, a higher rate — and not that it introduced"
    " it. If you cannot say which, say so."
)

_NEW_SIGNATURE_GUIDANCE = (
    "What to do with that: this is the case where the pushlog window below is genuinely"
    " trustworthy. The crash did not exist under this name before roughly this build, so whatever"
    " caused it is very likely to be in that window, and 'this changeset introduced this crash' is"
    " a claim the dates actually support. The one thing that would undo it is a renaming — an old"
    " crash re-signatured onto a new name — and where we can detect that, it is said above."
)

# How old is this signature ON ITS OWN CHANNEL, said only when that differs from "how old is it
# anywhere" by enough to matter. Nightly is mozilla-central's own channel, so the two are the
# same question there and this never fires; `_CHANNEL_NEW_DAYS` is deliberately the same
# `NEW_SIGNATURE_DAYS` boundary the all-time clock uses, so there is one definition of "new".
_CHANNEL_LABEL = {"beta": "beta", "aurora": "Developer Edition", "release": "release"}


def _channel_age_lines(crash, ever, age_ever):
    """``(lines, guidance)`` for "new to this channel, old elsewhere", else ``([], None)``."""
    channel = (crash.get("channel") or "").lower()
    label = _CHANNEL_LABEL.get(channel)
    first_channel = crash.get("signature_first_seen_channel")
    if not label or not first_channel:
        return [], None
    age_channel = sigage.signature_age_days(first_channel, crash.get("buildid"))
    if age_channel is None or age_channel > sigage.NEW_SIGNATURE_DAYS:
        return [], None
    if str(first_channel) == str(ever):
        # Same build on both clocks: the signature is simply new, and the all-time branch above
        # has already said so correctly.
        return [], None
    return (
        ["...but it is NEW ON {}: its first {} report is build {} ({}), {}. The figure above is"
         " its debut ANYWHERE, which for a change that rode this cycle's merge from"
         " mozilla-central is the nightly debut, weeks before this branch ever built it."
         .format(label.upper(), label, first_channel, sigage.buildid_day(first_channel),
                 _before_this_build(age_channel, first_channel, crash.get("buildid")))],
        _NEW_TO_CHANNEL_GUIDANCE.format(label=label),
    )


_NEW_TO_CHANNEL_GUIDANCE = (
    "What to do with that: treat the window below as TRUSTWORTHY even though the signature is not"
    " new. The crash is new to {label} users, so something reached them that had not before — a"
    " change uplifted onto this branch, or a change that came in with the merge from"
    " mozilla-central and only now ships here. 'This changeset introduced this crash on {label}'"
    " is a claim the dates support; 'this changeset introduced this crash' full stop is not, and"
    " the difference is worth stating explicitly in your mechanism. Note the landing dates below"
    " are the date the change reached {label}, which for anything that arrived with the cycle"
    " merge is the merge date and NOT when the code was written."
)

_UNDATED_SIGNATURE_GUIDANCE = (
    "What to do with that: work the pushlog window below as usual, but do not lean on the"
    " signature's age in either direction — neither 'brand new, so the window must contain it' nor"
    " 'long-standing, so no changeset created it' is supported here."
)


def _archetype_lines(crash: dict) -> list[str]:
    """Learned archetypes matching this crash (``models.Archetype``), as prompt lines, or ``[]``.

    WHERE A REVIEWER'S CORRECTION COMES BACK IN. Jens Stutte, after rejecting the changeset the
    pipeline named on bug 2062119 and finding the real origin himself: "maybe a general 'is a
    singleton involved that may not have a good/complete shutdown handling?'". That is an
    investigation rule, and the only place it can change anything is here, before the agent
    picks its first move — a rule applied after the verdict would be a critic, not a lead.

    Framed as a PRIOR TO TEST, never a conclusion, and it says so in the text. These rows are
    added from feedback without the review a patch gets, so the standing grounding rule has to
    keep doing the work: an archetype may tell the agent where to look and can never be cited as
    why it concluded something.

    THE BLIND SECOND OPINION IS NOT GIVEN THESE, and that is the opposite of the bit-flip fix:
    there both models were blind to something TRUE and telling both constrained them toward the
    same right answer, whereas an archetype is a suggested DIRECTION, and pointing the independent
    reviewer the same way correlates the two analyses' mistakes — it would agree because it was
    primed, and the SO's whole measured value (it refutes 74% of leads, specificity 1.00) is that
    it was not. ``second_opinion._user_prompt`` is a SEPARATE function from this module's and adds
    no archetype lines; ``tests/test_feedback_archetypes`` pins that it never sees the guidance.
    (This paragraph previously claimed the two shared one prompt, which was never true.)"""
    hints = crash.get("archetypes") or []
    if not hints:
        return []
    lines = [
        "",
        "KNOWN ARCHETYPES matching this crash. Each is a pattern a reviewer taught us after a "
        "previous bug, with what they said to check. Treat every one as a PRIOR TO TEST, not a "
        "finding: confirm it against real code before it changes your conclusion, and say so "
        "plainly if it does not hold here.",
    ]
    for hint in hints:
        if not isinstance(hint, dict) or not hint.get("guidance"):
            continue
        lines.append("- {}: {}".format(
            (hint.get("title") or hint.get("slug") or "archetype").strip(),
            str(hint["guidance"]).strip()))
    return lines if len(lines) > 2 else []


def _analysed_thread(raw: dict) -> str:
    """``"0 (MainThread)"`` — the index and name of the thread whose stack the agent is shown.

    Was ``crash_info.crashing_thread``, which on a hang is the watchdog and NOT the thread the
    stack comes from (see ``inspector.thread_for_analysis``). Printing the un-analysed index next
    to another thread's frames is worse than printing nothing: on bug 2064436 the same report
    would have said "Crashing thread: 45" above thread 0's stack.

    ``"12 (unnamed)"`` WHEN THE ANALYSED THREAD ITSELF HAS NO NAME, which is not an edge case: in
    47 of 821 sampled nightly crashes (5.7%) it has none while ``_thread_inventory`` below still,
    correctly, calls its list COMPLETE. A bare "12" over a list advertised as complete that has no
    thread 12 in it reads as a contradiction and invites the model to explain one; one word says
    which of the two it actually is. An index with no thread OBJECT behind it stays bare — we do
    not know that one is unnamed, only that we cannot see it."""
    threads = ((raw or {}).get("json_dump") or {}).get("threads") or []
    try:
        from crashclouseau import inspector

        idx = inspector.thread_for_analysis(raw or {})
    except Exception:                                   # pragma: no cover - defensive
        idx = ((raw or {}).get("json_dump") or {}).get("crash_info", {}).get("crashing_thread")
    if not isinstance(idx, int):
        return ""
    if not 0 <= idx < len(threads):
        return str(idx)
    thread = threads[idx] if isinstance(threads[idx], dict) else {}
    name = str(thread.get("thread_name") or "").strip()
    return "{} ({})".format(idx, name or "unnamed")


# RENDERING budget only — NOT the soundness ceiling, which is `_MAX_THREAD_FAMILIES` below. Past
# this many distinct names the block prints FAMILIES with instance counts ("FSBroker x24") instead
# of every name, because what makes a list long is instance numbering and no reader gains anything
# from 24 pids. Budget was never the constraint here: over 840 Firefox-nightly crashes the widest
# name list is 3,683 bytes (1,090 folded into 75 families) and the median is 489.
_MAX_THREAD_NAMES = 120

# THE CEILING THAT DECIDES WHETHER THE ABSENCE CLAIM SURVIVES, and it counts FAMILIES.
#
# It used to be `len(distinct names) <= 120`: an n=1 number read off one report ("the widest hang
# report sampled ran 113 threads") that counted the wrong thing. Measured over 840 Firefox-nightly
# crashes (60/day x 14 days, 2026-08-07..08-20): it fired on 24 of 840 (2.9%), ALL parent-process
# — 11.8% of parent crashes silently lost the absence claim — and 0 of 116 hang/shutdownhang
# reports were ever clipped, so it never once protected the case it was written for. What pushed
# those 24 over was INSTANCE NUMBERING, not subsystems: `FSBroker<pid>` alone contributes 582 of
# their names, plus WRWorkerLP#N / TaskCon~ller #N / StyleThread#N / DNS Resolver #N. Collapse the
# instance suffix and the widest crash in the 840 has 79 FAMILIES, p99 = 75, and zero exceed 96.
# 96 is that distribution plus ~20% headroom, so this stays a real guard against a pathological
# process instead of eating a sound claim for a cosmetic reason.
#
# WHAT IT MUST NOT EAT, the other direction: a process with 200 genuinely different subsystem
# threads must still be TRUNCATED. That is what `_MIN_FAMILY_STEM` protects — "T0".."T124"
# collapses to the stem "T", which is a parse failure and not a subsystem, and treating it as one
# family would let a 125-name list claim completeness. That guard is a NULL RESULT on the panel
# and is recorded as one: no name in the 840 has a stripped stem shorter than 3 characters, and
# sweeping `_MIN_FAMILY_STEM` over 0..5 leaves max families at 79 and over-96 at 0 either way. It
# exists for a parse-failure shape the sample does not contain, so read it as a guard, not as a
# fitted threshold.
_MAX_THREAD_FAMILIES = 96
_MIN_FAMILY_STEM = 3
_INSTANCE_SUFFIX_RE = re.compile(r"[\s#]*\d+$")


def _thread_family(name: str) -> str:
    """``"FSBroker4242"``/``"TaskCon~ller #7"``/``"StyleThread#5"`` -> the subsystem they instance.

    Used ONLY for the COMPLETE/TRUNCATED ceiling and for the collapsed rendering. The absence
    check itself (``orchestrator._absent_named_threads``) still matches against every raw name, so
    collapsing here can never make the gate miss a name the agent could have read."""
    stem = _INSTANCE_SUFFIX_RE.sub("", str(name)).strip()
    return stem if len(stem) >= _MIN_FAMILY_STEM else str(name).strip()


def _thread_name_of(thread) -> str:
    """A thread's name, stripped, or ``""`` when it has none. The ONE place a thread dict is read
    for a name, so "unnamed" means the same thing to the prompt, the ceiling and the gate."""
    if not isinstance(thread, dict):
        return ""
    return str(thread.get("thread_name") or "").strip()


def _thread_families(names) -> dict:
    """``{family: how many of the names fed in collapsed into it}``, in first-seen order.

    Feed it DISTINCT names for the ceiling (how many subsystems) and every thread's name for the
    rendering label (how many threads)."""
    families: dict = {}
    for name in names:
        family = _thread_family(name)
        families[family] = families.get(family, 0) + 1
    return families


def _thread_names(raw: dict) -> tuple[list[str], int]:
    """``(ordered distinct thread names, count of unnamed threads)`` for the crashing process.

    THE single reader of ``json_dump.threads``: ``_thread_inventory`` below and
    ``orchestrator._process_thread_names`` both come through here, so the prompt and the gate
    cannot disagree about what the agent was shown. They used to compute completeness over
    DIFFERENT inputs — the prompt over raw distinct names, the gate over SQUASHED ones — under a
    comment in the gate claiming they used "the SAME ceiling", which was false for any process
    whose names collide under squashing (``DNS Resolver #1`` and ``dnsresolver1``): the agent
    could be told the list was TRUNCATED and have its verdict clamped for an absence from it
    anyway."""
    threads = ((raw or {}).get("json_dump") or {}).get("threads") or []
    names, unnamed = [], 0
    for thread in threads:
        name = _thread_name_of(thread)
        if not name:
            unnamed += 1
        elif name not in names:
            names.append(name)
    return names, unnamed


def _inventory_complete(raw: dict) -> bool:
    """Is this thread list short enough that an ABSENCE from it is sound? See
    ``_MAX_THREAD_FAMILIES``."""
    names, _ = _thread_names(raw)
    return len(_thread_families(names)) <= _MAX_THREAD_FAMILIES


def _thread_inventory(raw: dict) -> list[str]:
    """Every named thread alive in the crashing process, as prompt lines, or ``[]``.

    BUG 2064436, and the clearest "the answer was in the payload" case yet. The pipeline filed a
    shutdown hang and the agent explained it through "the `MediaTrackGrph` thread owned by
    `ThreadedDriver`". Andreas Pehrson closed it INVALID in three hours: "I see no proof in the
    profile that this parent process is using or has used a MediaTrackGraph. No MediaTrackGrph
    thread, no GraphRunner thread. ... The AudioIPC server threads seem to be doing something, but
    there are no AudioIPC client threads like there would be if we were doing audio in this
    process."

    Every clause of that is a lookup in the minidump's own thread list, which the pipeline
    already fetched and had never once shown to a model: 46 threads on that crash, including
    `AudioIPC Server RPC`/`Server Callback`/`DeviceCollection RPC` with no client counterpart —
    his exact observation — and no graph thread of any kind. A FACT block rather than an
    ``_archetype_lines`` hint precisely because it is not a direction to consider but a
    checkable list, and so it must also reach the blind second opinion, which is the calibrated
    instrument for refuting a lead (specificity 1.00) and was equally blind here.

    THE ABSENCE IS THE LOAD-BEARING HALF, so it is only claimed when the list is complete. A
    truncated list licenses no conclusion at all and says so — the failure this block exists to
    stop is a confident statement about a runtime entity nobody looked for, and an agent
    reasoning from a silently clipped list would make it again with our encouragement.

    TWO CONDITIONS ON THAT LICENCE, both measured on 840 Firefox-nightly crashes (60/day x 14
    days, 2026-08-07..08-20) and both stated in the prompt text rather than only here, because
    this block also reaches the blind second opinion (``second_opinion._user_prompt`` ->
    ``_crash_facts``) and the second opinion is the specificity-1.00 instrument we use to SUPPRESS
    a lead. An absolute in here is an absolute in the refuter.

    * PROCESS TYPE IS THE DENOMINATOR, the same lesson as the bug-2064600 hardware block. This
      list speaks about ONE process, and 113 of the 159 thread families seen >=10x (71%) are >=95%
      confined to a single process type. p(thread present | process type), measured: MediaTrackGrph
      parent 0.00 / content 0.05; GraphRunner 0.00 / 0.07; MediaDecoderStateMachine 0.00 / 0.21;
      AudioIPC Client RPC 0.01 / 0.27; and the other way round, AudioIPC Server RPC 0.51 / 0.00,
      IPDL Background 0.83 / 0.00. So on a PARENT crash the absence of a media-graph thread is the
      BASE RATE, not evidence — which is exactly why Andreas Pehrson did not stop at "no
      MediaTrackGrph thread" and added the second argument: "There may be a MediaTrackGraph in a
      content process but then the shutdown blocker would live there too." The gate has always
      kept that caveat (a clamp, never an abstain — ``orchestrator._apply_absent_thread_gate``);
      this text used to drop it and say "REFUTED ... look elsewhere" flat, so it now asks for the
      cross-process step instead of granting the conclusion, and names the process type in the
      header so the reader has the denominator in front of them.
    * "COMPLETE" MEANS COMPLETE FOR NAMED THREADS. 347 of the 810 inventories in the sample
      (42.8%; 44.1% of the 786 the old ceiling called complete) hold unnamed threads — median
      14.5% of the process, p90 57.5%, max 83.5% — so the rule says the count out loud instead of
      letting "COMPLETE" imply every thread is accounted for. It still licenses the inference,
      because an unnamed thread is not a hiding place for a Gecko subsystem: of 5,982 unnamed
      threads, 13 (0.22%) have any xul.dll/libxul/``mozilla::``/nsThread frame in their top 12,
      and every one is DLL-init (``_cairo_mutex_initialize`` under LdrpCallTlsInitializers), an
      AV injection (aswJsFlt.dll), a ``_C_specific_handler`` unwind or ``AnimateSkeletonUI`` —
      not one pool or subsystem thread. Control, same detector and window: 29,747 of 32,840 NAMED
      threads (90.6%) are detected. NOT ``unnamed == 0``, which is REFUTED: it drops COMPLETE from
      786 to 439 of 810 (-44% of the rule's reach) to buy that 0.2%, and it would withdraw the
      absence claim on crash ec1ff67a-a835-4740-be14-572e50260818 (bug 2064436) — 46 threads, 14
      unnamed, zero Gecko frames among the 14 — the one crash in the corpus whose absence claim
      was CORRECT.

    Names come from the OS, so they are subject to its limits: Linux caps a pthread name at 15
    bytes and Socorro elides the middle ("Shutdow~minator" for "Shutdown Hang Terminator"), which
    is why the text asks for a substring rather than an exact match."""
    threads = ((raw or {}).get("json_dump") or {}).get("threads") or []
    if not threads:
        return []
    names, unnamed = _thread_names(raw)
    if not names:
        return []
    families = _thread_families(names)
    complete = len(families) <= _MAX_THREAD_FAMILIES
    # The ceiling counts distinct NAMES per family; the rendered label counts THREADS, because
    # that is what the header promises and the two differ in 24 of the 24 sampled crashes that
    # fold: `Renderer` is one name on 7 threads and `firefo:traceq` one name on 18, both of which
    # would otherwise print as a bare entry claiming a single thread.
    per_thread = _thread_families(_thread_name_of(t) for t in threads if _thread_name_of(t))
    if not complete:
        rendered = list(families)[:_MAX_THREAD_FAMILIES]
    elif len(names) <= _MAX_THREAD_NAMES:
        rendered = None
    else:
        rendered = list(families)
    if rendered is None:
        shown, is_folded = names, False
    else:
        shown = ["{} x{}".format(f, per_thread[f]) if per_thread[f] > 1 else f for f in rendered]
        is_folded = any(per_thread[f] > 1 for f in rendered)
    process = str((raw or {}).get("process_type") or "").strip()
    header = (
        "THREADS IN THIS PROCESS ({}{} threads, {} distinct names{}{}). Names are truncated by "
        "the OS on Linux (15 bytes, middle elided as \"Shutdow~minator\"), so match on a "
        "substring, not exactly.{}".format(
            "{} process, ".format(process) if process else "",
            len(threads), len(names),
            " in {} families".format(len(families)) if len(families) != len(names) else "",
            ", {} unnamed".format(unnamed) if unnamed else "",
            " Numbered instances are folded into their family: \"FSBroker x24\" means 24 "
            "threads of that family, individually numbered." if is_folded else "")
    )
    if complete:
        parts = [
            "This list is COMPLETE for the NAMED threads of THIS process. Use it as a hard check "
            "on any mechanism you are considering: before you name any thread, pool or runtime "
            "object, find it here; if it is absent, that IS your finding.",
            "BUT THE LICENCE IS ABOUT THIS PROCESS ONLY. 71% of thread families are at least "
            "95% confined to a single process type, so a content-process thread is absent from "
            "essentially every parent crash and vice versa — that absence is the BASE RATE, not "
            "evidence. If the "
            "subsystem you need normally runs in a different process type from this one, its "
            "absence here refutes nothing by itself: you must also show why the effect could not "
            "cross the process boundary (its shutdown blocker would have hung THAT process, not "
            "this one). With that second step the mechanism is REFUTED, not merely unproven — "
            "say so and look elsewhere; without it, do not claim either way.",
        ]
        if unnamed:
            parts.append(
                "{} of these {} threads carry no name; that does not weaken the check. Unnamed "
                "threads are OS/driver pool threads: over 840 nightly crashes only 13 of 5,982 "
                "of them (0.2%) held any Gecko frame and none was a pool or subsystem thread, so "
                "an unnamed thread is not a hiding place for a Gecko subsystem.".format(
                    unnamed, len(threads)))
        parts.append(
            "The converse is weaker: a thread being present means the subsystem exists, not that "
            "it is involved. Asymmetries are evidence too — a server-side thread with no "
            "client-side counterpart means this process was serving that subsystem, not using it.")
        rule = " ".join(parts)
    else:
        rule = (
            "This list is TRUNCATED at {} of {} thread families, so it CANNOT be used to argue "
            "that a thread is absent — a name you do not see here may simply have been cut. Use "
            "it only to confirm a thread that IS listed.".format(
                _MAX_THREAD_FAMILIES, len(families))
        )
    return ["", header, rule, ", ".join(shown)]


def _crash_facts(crash: dict) -> list[str]:
    """Compact processed-crash facts for the LLM.

    The full Socorro payload can be large; expose the failure signals that help the
    crash-interpreter classify the crash and the data-flow role pick mechanisms, plus the
    environment facets (OS / CPU / process type / GPU) that let the agent disambiguate
    candidates off-stack — e.g. a Windows-only or GPU-driver regressor for a Windows/GPU
    crash (the RULES_REPORT bug-2014723 data gap).

    Shared verbatim with the blind second opinion (``second_opinion._user_prompt``), which is
    why the bug-2064600 hardware block lives here rather than in the triage prompt beside
    ``_archetype_lines``: an archetype is a suggested direction and must not prime the
    independent reviewer, whereas a fact both models are blind to has to reach both of them.
    """
    raw = crash.get("raw_crash") or {}
    dump = raw.get("json_dump") or {}
    info = dump.get("crash_info") or {}
    sysinfo = dump.get("system_info") or {}
    lines = []

    facts = [
        ("Product", _first_present(crash.get("product"), raw.get("product"))),
        ("Version", _first_present(crash.get("version"), raw.get("version"))),
        ("Build ID", _first_present(crash.get("buildid"), raw.get("build_id"))),
        ("OS", _first_present(
            raw.get("os_pretty_version"),
            " ".join(v for v in (raw.get("os_name"), raw.get("os_version")) if v).strip(),
            sysinfo.get("os"),
        )),
        ("CPU", _cpu_summary(raw, sysinfo)),
        ("Process type", raw.get("process_type")),
        ("GPU", _gpu_summary(raw)),
        ("Crash type", _first_present(info.get("type"), raw.get("reason"))),
        ("Fault address", _first_present(info.get("address"), raw.get("address"))),
        # The instruction that faulted, so "which pointer was this" is a fact rather than an
        # inference: "mov rax, qword [rax + 0xd0]" says the base was a pointer and 0xd0 a
        # field offset, which is what makes the bit-flip line below checkable.
        ("Faulting instruction", info.get("instruction")),
        ("POSSIBLE BIT FLIP (the fault address may be hardware corruption, not a real pointer)",
         _bit_flip_summary(info)),
        # "hang" means nothing faulted: a watchdog killed the process because the main thread
        # stopped making progress. Never reached a prompt before bug 2064436, so an agent handed
        # a hang's stack had no way to know it was not looking at a fault.
        ("Report type", raw.get("report_type")),
        ("Analysed thread (the stack below is THIS thread)", _analysed_thread(raw)),
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
        ("Async shutdown timeout", raw.get("async_shutdown_timeout"),
         _render_async_shutdown),
        # THE three shutdown-hang fields, none of which had ever reached a prompt. On bug
        # 2064436's crash the spin-loop stack read `default: nsThreadPool::ShutdownWithTimeout
        # BgIOThreadPool` — Socorro naming the exact pool the main thread was blocked on, while
        # the agent, blind to it, invented a MediaTrackGraph. Present on 6 of 9 sampled
        # diverging hangs, each naming a different subsystem (nsHttpConnectionMgr::Shutdown,
        # QuotaManager::Observer::Observe, ParentImpl::ShutdownBackgroundThread, ...), so this is
        # the highest-value line in the block for a hang and it is nearly free.
        ("Shutdown phase reached", raw.get("shutdown_progress")),
        ("Why shutdown started", raw.get("shutdown_reason")),
        ("BLOCKED SPIN-EVENT-LOOP STACK (what the main thread is waiting for, innermost last "
         "— this NAMES the stuck subsystem; treat it as the primary lead for a shutdown hang)",
         raw.get("xpcom_spin_event_loop_stack"), _render_spin_stack),
    ]
    # A 3-tuple names a per-field renderer that runs INSTEAD of `_short_value`, keyed on the
    # FIELD rather than on the string's shape. Of the 23 facts below the 300-char cap is a
    # measured no-op on 10, unmeasurable on the 3 PHC lines, unmeasured on the rest and a
    # destructor on exactly two, so only those two have one. Both therefore also
    # reach the blind second opinion through the shared `_crash_facts`, which is right here:
    # they restore FACTS Socorro already sent us (the stuck subsystem, the blocker's source
    # file), never guidance.
    for fact in facts:
        label, value = fact[0], fact[1]
        render = fact[2] if len(fact) > 2 else _short_value
        value = render(value)
        if value:
            lines.append(f"{label}: {value}")
    # Before the signature-level block below, because it is still a fact about THIS report.
    lines += _thread_inventory(raw)
    lines += _watchdog_lines(crash)
    # Signature-level, and therefore last: everything above describes THIS report, and the point
    # of the block below is that the report can look clean while the signature does not.
    lines += _signature_age_lines(crash)
    lines += _hardware_noise_lines(crash)
    # Last of the signature-level block: how old the signature is says whether the crash is new,
    # and this says whether it got WORSE -- the two answers are independent and a rise on an old
    # signature is exactly the case the age lines alone read as uninteresting.
    lines += _signature_trend_lines(crash)
    return lines


def _user_prompt(crash: dict) -> str:
    uuid = crash.get("uuid", "")
    signature = crash.get("signature", "")
    channel = crash.get("channel", "nightly")
    stack = crash.get("stack") or crash.get("stack_text") or ""
    extra = crash.get("notes", "")
    lines = [
        "Investigate this Firefox crash to get the RIGHT PERSON INVESTIGATING it — your job "
        "is to surface the changeset/area most worth a human's time, not to prove a culprit. "
        "Reach off-stack functions through the call graph where needed. Report a cited lead "
        "whenever you have a CREDIBLE, SPECIFIC reason (a mechanism hypothesis, a domain / "
        "what-it-enables link, or a corroborating signal) and score how worth-investigating "
        "it is; use strong-evidence only for a chain verified end to end. ABSTAIN when the "
        "best you have is noise (mere window-membership or a bare keyword match) — a confident "
        "'nothing credible here' beats sending someone after noise and losing their trust in "
        "every future finding.",
        "",
        f"UUID: {uuid}",
        f"Signature: {signature}",
        f"Channel: {channel}",
    ]
    facts = _crash_facts(crash)
    if facts:
        lines += ["", "Crash facts:", *facts]
    lines += _archetype_lines(crash)
    if stack:
        lines += ["", "Stack:", str(stack)]
    candidates = crash.get("candidates") or []
    if candidates:
        if crash.get("is_offstack"):
            # P1 off-stack: no changeset landed on a crash-stack file, so this is the FULL
            # first-bad-build pushlog window (lightly pre-ranked by signature/desc overlap,
            # NOT by proximity). The regressor is somewhere in here. Triage by DESCRIPTION
            # first and read only the promising few diffs; reads are PINNED to the build.
            # WHAT THE WINDOW ACTUALLY IS, not what it usually is. `_offstack_window` widens
            # the lower bound to 24h before the build when the signature's rate is rising, and
            # a prompt that says "between the last-good build and this crash's build" would then
            # be describing a window the run did not use — telling the model that a candidate
            # which landed two builds ago cannot be in front of it, when it is.
            win = crash.get("candidate_window") or {}
            if win.get("widened") and win.get("hours"):
                extent = (
                    "the FULL pushlog window covering the {:.0f} hours before this crash's "
                    "build — WIDER than the usual last-good-build bound, because this "
                    "signature's crash rate is already rising and the landing that started a "
                    "rise routinely predates the last good build".format(win["hours"])
                )
            else:
                extent = ("the FULL pushlog window between the last-good build and this "
                          "crash's build")
            lines += [
                "",
                "Candidate changesets = " + extent + " (this crash is OFF-STACK: no candidate "
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
            lines += [
                "",
                "LINKED-CAUSE SEARCH: the regressor touched NO file on the stack (expected "
                "off-stack), so link it to the crash by what it ENABLES or its DOMAIN, not by "
                "file overlap. The classic case is a FEATURE/PREF FLIP that turns ON the "
                "crashing subsystem's code path (tagged 'feature-flip' below, e.g. 'Enable X "
                "by default'): does a flip enable the exact feature/library named in the "
                "signature or MOZ_CRASH reason? Also consider a change that sits in the "
                "crashing component, shares its reviewers, or mentions its keywords. Bug "
                "2056116 was found exactly this way — 'Enable Rust storage by default' caused "
                "a Rust/sqlite `sync15` panic that touched none of its files. A flip is a "
                "prior to VERIFY (confirm the enabled path reaches the crash), not an "
                "automatic verdict.",
            ]
            # Prior-signature (P4) hint: a prior FIXED bug with THIS crash's signature was
            # regressed by bug(s) X — a strong, stack-independent prior. Point the agent at
            # window candidates matching those bugs.
            hints = crash.get("prior_hints") or []
            if hints:
                named = "; ".join(
                    "bug {} (named by prior bug {})".format(h.get("regressor_bug"), h.get("prior_bug"))
                    for h in hints[:5]
                )
                lines += [
                    "",
                    "PRIOR-SIGNATURE PRIOR: earlier FIXED crash bug(s) with this SAME "
                    "signature were regressed by {}. Treat this as a strong prior: if any "
                    "window candidate below belongs to one of these bugs (tagged "
                    "'prior-sig'), investigate it FIRST — a repeat regression in the same "
                    "signature is common. It is a prior to verify, not an automatic "
                    "verdict.".format(named),
                ]
        else:
            lines += [
                "",
                "Scored candidate changesets (already ranked by proximity to the crash — "
                "read each with the mcp__patch__diff tool. Treat this seed list as a "
                "priority queue, not as a closed world: use it first, but if the "
                "call-graph neighborhood points at off-stack files/functions not covered "
                "by these seeds, say so and treat that as a cited lead rather than "
                "pretending the seed list is complete). PROXIMITY IS NOT CAUSATION: about 1 "
                "in 6 on-stack line hits is an 'exposer' — a correct changeset that only "
                "made a pre-existing latent bug reachable, whose fix lands outside its own "
                "diff. A poison-looking fault address (a run of one byte, e.g. "
                "0xe5e5e5e5e5e5e5e5 or 0x4b4b4b4b4b4b4b4b) or a candidate that merely "
                "perturbs timing/allocation/ordering is the classic shape: prefer a lead + "
                "soft needinfo over accusing it, and say which of the two you think it is:",
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
                # `landed=` ONLY WHEN IT IS A LANDING DATE. A candidate that arrived with a
                # release branch's cycle merge carries the MERGE's push date -- one date for a
                # whole cycle of work, measured 6.9 to 34.8 days after the code was actually
                # written -- so labelling it `landed=` tells the model something false, and in
                # the direction that makes an old changeset look like a recent one. Naming it as
                # an arrival is free; resolving the true landing date for every candidate in a
                # merge window is up to 150 hg round-trips, which is why this is a label and not
                # a lookup (the ONE chosen candidate does get the lookup, in
                # `orchestrator._apply_signature_age_gate`).
                parts.append(
                    "arrived-with-the-cycle-merge={} (NOT its landing date; it was written "
                    "earlier, on trunk)".format(_fmt_pushdate(pushdate))
                    if c.get("via_merge") else
                    "landed={}".format(_fmt_pushdate(pushdate)))
            if c.get("backedout"):
                # This seed flag is `pushlog.is_backed_out(desc)` = the changeset IS ITSELF a
                # backout commit. It does NOT mean it was backed out — labelling it "backed-out"
                # invited exactly that misreading (the model then reported a WAS-backed-out fact
                # under the same name). Whether a candidate WAS backed out is resolved
                # deterministically by `orchestrator._resolve_candidate_backout`, not here.
                parts.append("is-itself-a-backout-commit")
            if c.get("noise"):
                parts.append("(likely-noise: down-rank)")
            if c.get("prior_sig"):
                parts.append("[prior-sig: a prior sibling of this signature was regressed by this bug]")
            if c.get("pref_flip"):
                parts.append("[feature-flip: enables a feature/pref by default — a classic "
                             "off-stack cause; check the enabled area vs the crash]")
            desc = c.get("desc")
            if desc:
                parts.append("| {}".format(desc))
            lines.append("- " + " ".join(parts))
    # After the candidate list, because it is about what to do when that list does not
    # answer the question the rate above just raised.
    lines += _unexplained_rise_lines(crash)
    if extra:
        lines += ["", str(extra)]
    return "\n".join(lines)


def _unexplained_rise_lines(crash: dict) -> list[str]:
    """What to do when the RATE moved and the candidate set cannot explain it, or ``[]``.

    TASK GUIDANCE, so it lives here and NOT in ``_crash_facts``: that function is shared
    verbatim with the blind second opinion, where a fact both models are missing has to reach
    both of them but a suggested direction must not prime the reviewer
    (``_archetype_lines``' reasoning). The RATE ITSELF does go to both, via
    ``_signature_trend_lines``; only the instruction below is triage-only.

    THE FAILURE MODE THIS EXISTS TO STOP, measured on crash 84794f8d. The rate had risen, the
    window contained nothing that explained it, and the agent reached for the closest thing
    anyway — bug 2063678's own changeset, which was the FIX for the same signature and was
    already in the build. The skeptic correctly failed the window claim, `schema` rule (1b)
    read a failed claim on a lead as "noise", and a $1.39 run with a source-verified call path
    published nothing. The prompt was part of that: it frames the deliverable as "a plausible
    related changeset, or at least the right area" and tells the agent to reserve `abstain`
    for when nothing is worth anyone's time, which leaves no way to say "here is what is
    happening and no changeset explains it".

    SCOPED TO A MEASURABLE RISE AND A WEAK CANDIDATE SET, because the population that looks
    like this and SHOULD stay silent is far bigger than the one that should not. Over 1,646
    model-authored abstains in 30 days of prod, recomputing the trend as-of each run from the
    rollup: 80.7% sit on a signature whose rate is not measurable at all (``sigdaily`` is
    filled from a COUNT-ordered facet page, so it holds only the loud signatures), 17.1% are
    measurably NOT rising, and of the ~2% that are rising, 19 of 35 are third-party driver
    code or memory exhaustion — correct abstains, every one. The target class is ~16 runs a
    month before deduplication. This block is worth its bytes for what it stops the agent
    DOING, not for how often it fires.

    Which is also why the last paragraph is the longest. Turning those correct silences into
    manufactured Firefox-side observations would be a far worse trade than the one bad abstain
    this fixes."""
    from crashclouseau import sigtrend

    if not sigtrend.is_rising(crash.get("signature_trend") or {}):
        return []
    candidates = crash.get("candidates") or []
    # A weak set: the undifferentiated pushlog window (off-stack), or nothing that scored onto
    # a crash frame. With a real proximity score the ordinary hunt is the right one.
    if not crash.get("is_offstack") and any(c.get("score") for c in candidates):
        return []
    return [
        "",
        "NO CHANGESET IS REQUIRED FOR THIS ONE. The rate above has moved and the candidate "
        "list you were given cannot be scored against the crash, so it is entirely possible "
        "that nothing in it explains the rise. If that is what you find, SAY SO — "
        "\"this signature's rate rose Nx and nothing in the window I searched accounts for "
        "it\" is a finding a triager can act on, and it is the honest one. Report it with "
        "what you did check, so the next person does not repeat it.",
        "  DO NOT REACH for the nearest plausible changeset to avoid an empty answer. A "
        "candidate you cannot defend gets refuted by your own skeptic pass and the entire run "
        "is then discarded — that is a real case, not a caution: on one crash the agent named "
        "the changeset that FIXED the same signature, already present in the build, and a "
        "verified call path was thrown away with it. An unnamed cause costs a reader nothing; "
        "a wrong one costs them the whole analysis.",
        "  WHAT IS WORTH WRITING when you have no changeset: what is failing and where "
        "(component, subsystem, the crashing call path), what the rate did, what you searched "
        "and ruled out, and what a person who owns that code should look at first.",
        "  BUT AN EMPTY ANSWER IS STILL THE RIGHT ONE when the crash is not ours to fix. If "
        "the fault is inside a third-party or closed-source module (a graphics driver, a CDM, "
        "an OS library), or the stack is unsymbolicated vendor code, or this is memory "
        "exhaustion rather than a defect, then abstaining IS the finding — say which and "
        "stop. Do not manufacture a Firefox-side observation to fill the space; most crashes "
        "that look like this genuinely are somebody else's, and a rate change does not make "
        "them ours.",
    ]


def _crash_label(crash: dict) -> str:
    return "crash {}".format(crash.get("uuid", "?"))


class _RunTrace:
    """Logs where a run's wall-time goes: each AI subagent (Task) with its
    description + elapsed time, plus a per-tool and per-model breakdown. Emitted via
    ``logger`` so it shows in worker logs and the offline harness without changing
    the agent's behavior. Purely observational (helps decide what to trim).

    ...AND, since 2026-08-28, the one part of it that is PERSISTED: which searchfox symbol
    the run actually asked about, and whether the answer came back empty
    (``provenance()`` -> ``Dossier.payload['tool_calls']``).

    WHY THAT IS NOT OPTIONAL. This class already computed the symbol
    (``_label`` -> ``"symbol=nsINode::DisconnectChild"``) and dropped it on the floor: the
    per-tool aggregate keeps a count and a duration, ``summary`` logs one
    ``mcp__searchfox__calls_to xN`` line, and the app has no log drain, so a
    ``heroku logs -n 1500`` window is about two hours. The result was that "did the agent
    enumerate the callers of the symbol its own mechanism names?" -- the question that
    decides bug 2067349's class of error -- was unanswerable for every dossier ever written.

    This is the repo's dominant failure mode, not a hypothetical: measured over the 78 filings
    to 2026-08-28, ``archetypes`` non-empty fired 0/78 (the key is present on 57 rows and every
    value is ``[]``), and so did ``compiled_out_suppressed``, ``skeptic_build_flag_unbound`` and
    ``absent_named_threads``. A gate nobody can count is a gate nobody can tell has stopped
    working, so any change to the call-graph tools or to the skeptic's enumeration duty has to
    land with a counter or it lands unfalsifiable.

    Deliberately NOT a behaviour change: nothing reads ``tool_calls``, no verdict moves, and
    the field is additive JSONB so older dossiers read as absent rather than empty."""

    # Only the searchfox family gets a per-CALL record. The empty-vs-non-empty distinction is
    # what these are for (``tools/searchfox_cg.NO_GRAPH_RESULT``), the argument is a symbol
    # rather than a whole prompt, and the volume is tens per run rather than hundreds -- which
    # is what keeps this a few hundred bytes of payload instead of a few tens of KB. Everything
    # else still lands in ``totals``.
    _PROVENANCE_TOOL = "__searchfox__"
    # Bound the payload. Exceeding it is COUNTED and logged, never silently truncated: a
    # capped list that reads as complete is how a coverage number becomes a lie.
    _PROVENANCE_MAX = 150

    def __init__(self):
        self._t0 = time.monotonic()
        self._start = {}       # tool_use_id -> (name, start, issuer_subagent_or_None, label)
        self._task_type = {}   # Task tool_use_id -> subagent_type
        self.tasks = []        # [(subagent_type, label, seconds)] in completion order
        self._tool = defaultdict(lambda: [0, 0.0])   # tool name -> [count, seconds]
        self._calls = []       # [{tool, arg, empty, secs, by}] for the searchfox family
        self._calls_dropped = 0

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
        # `source`/`target` are `calls_between`'s argument names; without them its label fell
        # through to a repr of the whole input dict, which is unreadable in a log and useless
        # as provenance.
        for k in ("symbol", "caller", "callee", "source", "target",
                  "file_path", "pattern", "path", "query"):
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
                        if self._PROVENANCE_TOOL in name:
                            self._record_call(name, label, dt, _issuer, b)

    @staticmethod
    def _result_text(block) -> str:
        """The tool result as text, from any of the shapes the SDK uses for it.

        Best-effort and never raises: this is instrumentation, and a shape we have not seen
        must cost a provenance record, never a $2 run."""
        content = getattr(block, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            out = []
            for part in content:
                if isinstance(part, str):
                    out.append(part)
                elif isinstance(part, dict):
                    out.append(str(part.get("text") or ""))
                else:
                    out.append(str(getattr(part, "text", "") or ""))
            return "\n".join(out)
        return "" if content is None else str(content)

    def _record_call(self, name, label, dt, issuer, block):
        if len(self._calls) >= self._PROVENANCE_MAX:
            self._calls_dropped += 1
            return
        try:
            text = self._result_text(block)
        except Exception:                                   # pragma: no cover - defensive
            text = ""
        self._calls.append({
            "tool": name.rsplit("__", 1)[-1],
            "arg": label,
            # Anchored on the GENERATED prefix the tool emits, not on prose. An empty graph is
            # the answer this whole field exists to make countable.
            "empty": text.startswith(searchfox_cg.NO_GRAPH_RESULT),
            "secs": round(dt, 2),
            # Which role asked. `None` is the principal; the mechanism is written by
            # patch-scout / data-flow-tracer, so "who enumerated" is the interesting half.
            "by": issuer,
        })

    def provenance(self) -> dict:
        """``payload['tool_calls']``: per-call searchfox records + a per-tool total.

        Shaped so the two questions that motivated it are one query each: "was this symbol
        enumerated?" (`calls`) and "how often does the tool answer nothing?"
        (`empty`)."""
        if self._calls_dropped:
            logger.warning("agent: provenance capped at %d searchfox calls; %d not recorded",
                           self._PROVENANCE_MAX, self._calls_dropped)
        out = {
            "calls": list(self._calls),
            "totals": {n: {"n": c, "secs": round(secs, 1)}
                       for n, (c, secs) in sorted(self._tool.items())},
        }
        if self._calls_dropped:
            out["dropped"] = self._calls_dropped
        return out

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
                logger.info("agent:   model %-22s in=%s out=%s cache_write=%s", model,
                            u.get("inputTokens", u.get("input_tokens", "?")),
                            u.get("outputTokens", u.get("output_tokens", "?")),
                            # The one usage field that tracks PROMPT GROWTH. `input_tokens`
                            # does not: a stable prompt is served from cache, so growth shows
                            # up as a cache WRITE. Prod's persisted columns missed v109's
                            # +2,552 bytes entirely (Mann-Whitney p = 0.22) for this reason.
                            u.get("cacheCreationInputTokens",
                                  u.get("cache_creation_input_tokens", "?")))


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
    ctx = SearchfoxCtx(client=searchfox_client, channel=channel)
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
        system_prompt=_system_prompt(channel),
        mcp_servers=mcp_servers,
        agents=roles.build_roles(llm_cfg, channel=channel),
        allowed_tools=allowed,
        model=model,
        max_turns=max_turns,
        permission_mode="bypassPermissions",
        setting_sources=[],
        # Keeps the subagent fan-out inline; see _CLI_ENV. Copied, not shared, so a
        # caller mutating options.env can't reach back into the module constant.
        env=dict(_CLI_ENV),
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


def build_result(result_msg, *, recorder=None, tool_calls=None) -> CrashTriageResult:
    """Fold a terminal ``ResultMessage`` into a typed ``CrashTriageResult``,
    best-effort parsing + #03-validating the trailing ```json handoff. Raises
    ``AgentError`` on a missing/errored result, and ``MissingHandoffError`` when the
    run produced no readable handoff at all; a handoff that parses but fails schema
    validation still SALVAGES to an abstain. The recorded actions are the agent's own
    (via the ``actions`` MCP server) plus a synthesized needinfo
    (``_needinfo_action``) so a strong-evidence verdict always yields an
    apply-eligible action even though the agent only drafts the text."""
    if result_msg is None:
        raise AgentError("crash triage produced no result message")
    if getattr(result_msg, "is_error", False):
        detail = result_msg.result or getattr(result_msg, "subtype", "")
        raise AgentError(f"crash triage failed: {detail}")
    dossier = parse_and_validate(result_msg.result)
    verdict = dossier.verdict if dossier is not None else None
    if verdict is not None and verdict.abstain_reason == NO_HANDOFF_REASON:
        # NO handoff is an infrastructure failure, not a verdict, and it must not be
        # persisted as one. The system prompt demands the fenced block on every path
        # (abstain included, with its reason INSIDE the JSON), so its absence means the
        # run never reached a conclusion -- the model was cut off, truncated, or, as in
        # the 0.2.131 backgrounding regression, left a "waiting for the background
        # agents" progress note as its final message. Every one of those landed as a
        # status=done, confidence-25 abstain that reads exactly like a considered
        # "insufficient evidence": no error row, no reaper pickup, no alert. It took a
        # human reading one crashstack page to notice a 66% failure rate.
        #
        # Checked here rather than in ``parse_and_validate`` because that function must
        # keep its "never raises" contract for the eval runner. Auditing prod's 30-day
        # pre-regression baseline found this fires ~0.3-0.5x/day and NONE of those were
        # a legitimate decline: 6 of 9 emitted a fence whose JSON had a syntax error,
        # the other 3 ended mid-prose announcing a lead they never serialized. So the
        # false-positive risk is not "we lose a considered abstain", it is zero.
        #
        # The raw text and the usage ride on the exception so the error row keeps the
        # forensics and the spend -- these runs cost full price (~$0.81 each).
        ti, to, tc = _sum_tokens(result_msg)
        logger.error(
            "agent: no ```json handoff after %s turns; final text was: %r",
            getattr(result_msg, "num_turns", "?"), (result_msg.result or "")[:2000],
        )
        raise MissingHandoffError(
            "crash triage ended after {} turns with no readable ```json handoff "
            "-- the run never reached a verdict".format(
                getattr(result_msg, "num_turns", "?")
            ),
            raw_result=result_msg.result or "",
            cost_usd=getattr(result_msg, "total_cost_usd", None),
            num_turns=getattr(result_msg, "num_turns", None),
            input_tokens=ti, output_tokens=to, cache_read_tokens=tc,
        )
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
        tool_calls=tool_calls or {},
        input_tokens=ti,
        output_tokens=to,
        cache_read_tokens=tc,
    )


def _log_prompt_budget(system: str, user: str, crash: dict) -> None:
    """One line per run saying how big the prompt we just built is.

    WHY THIS EXISTS. `v109` (2026-08-21) grew the principal's first user message by a median
    +1,914 bytes (+20.3%, positive on 198 of 198 post-deploy runs) and `system.md` by +638, and
    prod candidate-naming stepped 41.7% -> 24.2% at that exact release. Nothing could see it. There
    was no assertion anywhere on the size of the system prompt, the crash facts, or the user
    prompt; no log line carried a prompt length; and the token columns we DO persist are
    empirically blind to it -- median `input_tokens` across the deploy moved 5,528/5,766 to
    6,199/5,883/6,402, Mann-Whitney p = 0.22, because a grown *cached* prefix lands in
    `cacheCreationInputTokens`, which `_sum_tokens` drops.

    So the growth was found weeks later by reconstructing 500 prompts offline. This makes the
    number free and contemporaneous. It is a log line and not a column on purpose:
    `models.create()` only runs `create_all()` on a FRESH database, `_ADDED_TABLES` handles tables
    and not columns, and `_ensure_enum_values` can never ALTER -- so a new column would need its
    own migration path that does not exist yet, and this does not need one.

    See also `tests/test_prompt_budget.py`, which pins the same three numbers so a future prose
    add has to change a reviewed constant instead of nothing at all."""
    facts = "\n".join(_crash_facts(crash))
    logger.info(
        "agent: prompt bytes system=%d user=%d (crash facts=%d) total=%d for %s",
        len(system), len(user), len(facts), len(system) + len(user),
        crash.get("uuid", "?"),
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
    user = _user_prompt(crash)
    # `getattr`, because a test can hand back a stub options object and a budget log line must
    # never be the thing that fails a run.
    system = getattr(options, "system_prompt", "")
    system = system if isinstance(system, str) else ""
    _log_prompt_budget(system, user, crash)
    result_msg = None
    trace = _RunTrace()
    with Reporter(verbose=False, log_path=None) as reporter:
        reporter.header(_crash_label(crash))
        async with ClaudeSDKClient(options=options) as client:
            await client.query(user)
            async for msg in client.receive_response():
                reporter.message(msg)
                trace.observe(msg)
                if isinstance(msg, ResultMessage):
                    result_msg = msg
    trace.summary(result_msg)
    return build_result(result_msg, recorder=recorder, tool_calls=trace.provenance())
