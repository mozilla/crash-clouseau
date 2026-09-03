# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Blind second-opinion pass (#SO).

For a REPORTED lead that clears the report threshold, ask ONE fresh Opus-4.8 (effort=max)
agent — with NO context from the first pipeline — for an INDEPENDENT read of the crash:

* VERIFIER mode (we have a candidate regressor): given only the crash + the candidate's
  changeset/bug, does that changeset PLAUSIBLY cause this crash? Prompted neutrally — it may
  well be unrelated, and saying so is a valid (valuable) answer.
* GENERATOR mode (no candidate): given only the crash, what mechanism explains it?

Independence is the whole point: agreement between two blind, differently-reasoned analyses
is the plausibility signal. The agent is tool-equipped (searchfox / hg / patch-diff /
Bugzilla / crash-stats) but runs on a TIGHT allowlist — the scoped MCP tools ONLY, no
``Bash``/``Read``/``Grep``/``Glob`` and no subagents — so it cannot shell out to hg
``json-pushes`` and redo the (expensive, first-pipeline) pushlog-window analysis. The strong
model + effort=max is deliberate and safe here: this is a rare, single-shot, no-context call,
not the multi-agent pipeline the blanket effort=max OOM/no-gain finding was about.
"""
from __future__ import annotations

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage

from crashclouseau import config
from crashclouseau.agent import roles, triage
from crashclouseau.agent.schema import SecondOpinion, _extract_last_json_block
from crashclouseau.agent.tools import bugzilla as bugzilla_tools
from crashclouseau.agent.tools import history as history_tools
from crashclouseau.agent.tools import patch as patch_tools
from crashclouseau.agent.tools import searchfox_cg
from crashclouseau.agent.tools import socorro as socorro_tools
from crashclouseau.agent.tools import source as source_tools
from crashclouseau.agent.tools.bugzilla import BugzillaCtx
from crashclouseau.agent.tools.history import HistoryCtx
from crashclouseau.agent.tools.patch import PatchCtx
from crashclouseau.agent.tools.searchfox_cg import SearchfoxCtx
from crashclouseau.agent.tools.socorro import SocorroCtx
from crashclouseau.agent.tools.source import SourceCtx
from crashclouseau.logger import logger
from crashclouseau.searchfox import SearchfoxClient
from crashclouseau.vendor.agent_tools.claude_sdk import build_sdk_server
from crashclouseau.vendor.hackbot_runtime.claude import Reporter


_SYSTEM = (
    "You are an INDEPENDENT second reviewer of a Firefox crash. Another analysis already ran; "
    "you are deliberately given NONE of its reasoning so your read is unbiased. Investigate "
    "with the tools, then answer honestly and NEUTRALLY — if the evidence is weak or the "
    "candidate looks unrelated, say so plainly; a confident 'I don't see how this explains "
    "the crash' is a valuable answer, not a failure.\n\n"
    "Do NOT try to enumerate the regression window / pushlog — you cannot and should not; "
    "focus on the MECHANISM: what, at the level of the crashing code, produces this exact "
    "fault, and (if a changeset is given) whether that change can plausibly cause it.\n\n"
    "Tools (read-only):\n"
    "- searchfox (mcp__searchfox__*): read the crashing code + symbols, walk the call graph "
    "(calls_from/calls_to/calls_between) from the crash frames, field_layout to check a "
    "struct offset against the fault address.\n"
    "- hg (mcp__source__raw_file reads source AS OF the crash build — leak-free, searchfox is "
    "tip-only; mcp__history__blame/file_history/changeset for who/when/why a line changed; "
    "mcp__patch__diff to inspect a candidate changeset's actual diff).\n"
    "- Bugzilla (mcp__bugzilla__bug reads a bug's product::component/status/regressed_by/"
    "regressions; mcp__bugzilla__signature_bugs finds existing bugs for the crash signature "
    "so you reuse prior analysis).\n"
    "- crash-stats (mcp__socorro__crash_stats): this signature's occurrence breakdown — the "
    "buildid it was FIRST seen in (searched over a year of crash reports, so it may predate "
    "this build by months) and the OS/CPU/process-type/channel/moz_crash_reason facets; a "
    "lopsided facet narrows the likely change. An old first-seen build is NOT by itself a "
    "refutation: a change can make an existing crash FREQUENT without introducing it, and a "
    "hang or timeout signature fires whenever the awaited work exceeds its budget. For a crash "
    "whose rate has risen, or a shutdownhang / AsyncShutdownTimeout / watchdog crash, judge "
    "whether the change adds work, I/O or blocking to the path being waited on -- even when it "
    "touches no code on the stack -- and say so rather than arguing from the signature's age.\n\n"
    "End your reply with EXACTLY one fenced block:\n"
    "```json\n"
    '{"corroborates": true|false|null, "confidence": "low|medium|high", '
    '"mechanism": "<1-3 sentence independent mechanism for the crash>", '
    '"refutation": "<if you concluded a given changeset can NOT explain the crash, the '
    'concrete reason; otherwise empty>"}\n'
    "```\n"
    "corroborates: with a candidate changeset — true if it plausibly causes this crash, false "
    "if it clearly cannot, null if genuinely unsure; with no candidate — null. confidence is "
    "in YOUR OWN conclusion."
)


def _user_prompt(crash: dict, candidate: dict | None) -> str:
    signature = crash.get("signature", "")
    channel = crash.get("channel", "nightly")
    stack = crash.get("stack") or crash.get("stack_text") or ""
    lines = [
        "Signature: {}".format(signature),
        "Channel: {}".format(channel),
    ]
    facts = triage._crash_facts(crash)
    if facts:
        lines += ["", "Crash facts:", *facts]
    if stack:
        lines += ["", "Stack:", str(stack)]
    if candidate and candidate.get("node"):
        bug = candidate.get("bug")
        lines += [
            "",
            "A candidate regressor changeset has been proposed: {}{}. Inspect its diff "
            "(mcp__patch__diff) and its bug (mcp__bugzilla__bug), read the crashing code, and "
            "judge NEUTRALLY whether this change can plausibly cause THIS crash. It may be "
            "unrelated — if so, say why.".format(
                candidate["node"], " (bug {})".format(bug) if bug else ""),
        ]
    else:
        lines += [
            "",
            "No candidate changeset is available. From the crash alone, work out the most "
            "plausible MECHANISM that produces this exact fault — what in the crashing code "
            "goes wrong — so a human can judge whether it is worth investigating.",
        ]
    return "\n".join(lines)


def build_options(crash: dict, candidate: dict | None = None, *,
                  searchfox_client=None) -> ClaudeAgentOptions:
    """Assemble the blind second-opinion ``ClaudeAgentOptions`` with a TIGHT allowlist
    (scoped MCP tools only — no shell, no subagents). Pass ``searchfox_client`` in tests to
    avoid resolving the ``searchfox-cli`` binary."""
    cfg = config.get_agent_second_opinion()
    channel = crash.get("channel", "nightly")
    pin_rev = crash.get("pin_rev", "")
    product = crash.get("product") or "Firefox"
    if searchfox_client is None:
        searchfox_client = SearchfoxClient()
    mcp_servers = {
        "searchfox": build_sdk_server(
            "searchfox", SearchfoxCtx(client=searchfox_client, channel=channel),
            searchfox_cg.TOOLS),
        "patch": build_sdk_server("patch", PatchCtx(channel=channel), patch_tools.TOOLS),
        "history": build_sdk_server("history", HistoryCtx(channel=channel, build_rev=pin_rev),
                                    history_tools.TOOLS),
        "source": build_sdk_server("source", SourceCtx(channel=channel, build_rev=pin_rev),
                                   source_tools.TOOLS),
        "bugzilla": build_sdk_server("bugzilla", BugzillaCtx(), bugzilla_tools.TOOLS),
        "socorro": build_sdk_server("socorro", SocorroCtx(product=product, channel=channel),
                                    socorro_tools.TOOLS),
    }
    # TIGHT allowlist — scoped MCP tools ONLY. Deliberately NO builtin Read/Grep/Glob/Bash and
    # NO Task: with no shell the agent cannot GET hg json-pushes to redo the pushlog window.
    allowed = [
        *roles.searchfox_tool_ids(), *roles.patch_tool_ids(),
        *roles.history_tool_ids(), *roles.source_tool_ids(),
        *roles.bugzilla_tool_ids(), *roles.socorro_tool_ids(),
    ]
    kwargs = dict(
        system_prompt=_SYSTEM,
        mcp_servers=mcp_servers,
        allowed_tools=allowed,
        model=triage._model_id(cfg["model"]),
        max_turns=cfg["max_turns"],
        permission_mode="bypassPermissions",
        setting_sources=[],
        # Same inline-subagent pin as the principal (see ``triage._CLI_ENV``). The SO's
        # allowlist has no Task today, but ``allowed_tools`` is not a REGISTRATION
        # control -- neither options object sets ``tools``, so the CLI's whole default
        # toolset (Agent included) stays registered, and ``permission_mode`` is
        # bypassPermissions. If the SO ever launches an agent, backgrounding would turn
        # its final message into a progress note and ``parse_second_opinion`` would
        # quietly return None -- i.e. a silently un-reviewed lead. One key buys it out.
        env=dict(triage._CLI_ENV),
    )
    if cfg["effort"]:
        kwargs["effort"] = cfg["effort"]
    return ClaudeAgentOptions(**kwargs)


def parse_second_opinion(text: str | None, candidate: dict | None) -> SecondOpinion | None:
    """Parse the agent's trailing ```json block into a ``SecondOpinion`` (mode set from
    whether a candidate was given). ``None`` on a missing/unparseable block."""
    obj = _extract_last_json_block(text)
    if not obj:
        return None
    try:
        so = SecondOpinion.model_validate(obj)
    except Exception:
        return None
    so.mode = "verify" if (candidate and candidate.get("node")) else "mechanism"
    return so


async def run_second_opinion(crash: dict, candidate: dict | None = None) -> SecondOpinion | None:
    """Drive the blind agent to a terminal result and return its parsed ``SecondOpinion``
    (``None`` on an errored/empty/unparseable run). Best-effort — a failure here must never
    break the primary verdict; the caller keeps the original lead."""
    options = build_options(crash, candidate)
    result_msg = None
    try:
        with Reporter(verbose=False, log_path=None) as reporter:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(_user_prompt(crash, candidate))
                async for msg in client.receive_response():
                    reporter.message(msg)
                    if isinstance(msg, ResultMessage):
                        result_msg = msg
    except Exception:
        logger.warning("second-opinion: run failed for %s", crash.get("uuid"), exc_info=True)
        return None
    # Every ``return None`` below is a FAILURE, not a skip — the caller already decided this
    # lead deserves a second opinion. They were silent, which made a prod-only break
    # (SDK/tool/allowlist trouble, a model that never emits the JSON block) indistinguishable
    # from the pass simply not being eligible. Log each one distinctly.
    if result_msg is None:
        logger.warning(
            "second-opinion: no terminal ResultMessage for %s", crash.get("uuid")
        )
        return None
    if getattr(result_msg, "is_error", False):
        logger.warning(
            "second-opinion: errored run for %s: %s",
            crash.get("uuid"), getattr(result_msg, "result", None),
        )
        return None
    so = parse_second_opinion(result_msg.result or "", candidate)
    if so is None:
        logger.warning(
            "second-opinion: unparseable result for %s (no valid trailing json block)",
            crash.get("uuid"),
        )
    else:
        # Record THIS pass's own cost (from the terminal ResultMessage) so a validation run
        # can price the second opinion separately from the primary triage. Set here, not in
        # parse_second_opinion, because the cost is not part of the agent's JSON contract.
        so.cost_usd = getattr(result_msg, "total_cost_usd", None)
    return so
