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

from crashclouseau import config, sigage
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
    shown rather than the first one found."""
    arch = _first_present(raw.get("cpu_arch"), sysinfo.get("cpu_arch"))
    info = _first_present(raw.get("cpu_info"), sysinfo.get("cpu_info"))
    parts = [str(p) for p in (arch, info) if p]
    if not parts:
        return ""
    out = ", ".join(parts)
    if str(info or "").strip() in sigage.BROKEN_CPUS:
        # Kept short deliberately: `_short_value` truncates every fact at 300 chars, and the
        # warning is the part that must survive next to a 36-char CPU string.
        out += (" — KNOWN-DEFECTIVE CPU (Intel Raptor Lake, meta bug 1975808): its documented "
                "instability corrupts computation on correct software, so a wild pointer or "
                "impossible state here may be the processor, not the code.")
    return out


def _cpu_spread_line(noise: dict) -> str:
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
    return (
        "CPU-MODEL SPREAD OF THIS SIGNATURE — a fact, not a verdict, and deliberately stated "
        "apart from the paragraph above. Of the {} reports that carry a cpu_info string, "
        "{:.0f}% are on {}{}. Background: the median Firefox-nightly signature with at least 5 "
        "reports sits at {:.0f}%, and 13% of them (26 of 200 sampled 2026-08-21) sit at 100% — "
        "one processor model is ORDINARY, and every suppression threshold tested on this "
        "statistic suppressed a crash that was later FIXED. Read it as SCOPE and as evidence "
        "in NEITHER direction: when a signature is confined to one CPU model, one GPU driver "
        "or one distribution, naming which is worth more than calling the population small — "
        "but concentration is not support for a bug either, since in that same sample the most "
        "concentrated signatures carry a known Firefox bug LESS often than the rest (9 of the "
        "26 at 100%, against 118 of the 200 overall).".format(
            seen, 100 * share, noise.get("top_cpu_term") or "one model",
            ", the only model seen" if terms == 1
            else ", one of {} models seen".format(terms),
            100 * sigage.POPULATION_TOP_CPU_SHARE_MEDIAN)
    )


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
    bits = []
    if flip is not None:
        bits.append("{:.0f}% carry a Socorro bit-flip annotation (crash population: "
                    "{:.0f}%)".format(100 * flip, 100 * sigage.POPULATION_BIT_FLIP_RATE))
    if cpu is not None:
        bits.append("{:.0f}% come from a known-defective Intel Raptor Lake CPU (family 6 model "
                    "183 stepping 1, meta bug 1975808; crash population: {:.0f}%)".format(
                        100 * cpu, 100 * sigage.POPULATION_BROKEN_CPU_RATE))
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
    spread = _cpu_spread_line(noise)
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
        ("Async shutdown timeout", raw.get("async_shutdown_timeout")),
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
         raw.get("xpcom_spin_event_loop_stack")),
    ]
    for label, value in facts:
        value = _short_value(value)
        if value:
            lines.append(f"{label}: {value}")
    # Before the signature-level block below, because it is still a fact about THIS report.
    lines += _thread_inventory(raw)
    # Signature-level, and therefore last: everything above describes THIS report, and the point
    # of the block below is that the report can look clean while the signature does not.
    lines += _signature_age_lines(crash)
    lines += _hardware_noise_lines(crash)
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


def build_result(result_msg, *, recorder=None) -> CrashTriageResult:
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
