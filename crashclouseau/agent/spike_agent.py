# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""The spike investigator: one Claude Fable 5.1 run over everything we know about a REAL spike.

WHEN IT RUNS. ``spike_escalation.sweep_real_spikes`` found a (signature, build-day) the selector
analysed that is a real spike by ``spikes.judge_selection`` -- a floor, several installations,
the 3x ratio and a Poisson excess, or the same excess on the 7-day install rate -- and the
ordinary pushlog triage on that build filed nothing (abstained, was refuted, never ran). The
spike is a fact and a bug will be filed for it either way; this run is what tries to put
something useful for the developers into that bug.

WHAT IT GETS. The brief ``spike_escalation.build_spike_brief`` assembles: the spike numbers, the
signature's age and rate history, up to ``max_stacks`` distinct stacks with their report-level
facts, what the ordinary runs concluded and why, and the candidate changesets -- the on-stack
scored ones and the whole pushlog window of the spiking build (widened when the rate was already
rising). Plus the tools: searchfox, pinned source and blame, patch diffs, Bugzilla reads, and the
two crash-stats population tools (``tools/crashstats.py``) that answered the QuotaManager spike by
hand: facets split at the spike build, and other threads of a hang dump.

WHAT IT MAY NOT DO. No shell, no file system, no subagents: ``ClaudeAgentOptions.tools=[]``
switches the CLI's built-in toolset off (the second opinion only ALLOWLISTS, which is not a
registration control -- see ``agent-tool-sandbox`` in the memory notes), so the model has exactly
the scoped MCP tools and nothing that could reach the worker's credentials. The brief is built
from client-supplied crash annotations, so this matters more here than anywhere else.

WHAT COMES BACK. ``SpikeFindings``: a summary for the bug, a product::component, a culprit
candidate or none, a checked trigger path, the evidence with its sources, what was ruled out.
``run_spike_agent`` also reports how many tool calls the run made; a run that consulted nothing
has grounded nothing, and the filer publishes only the volume facts from it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    ToolUseBlock,
)
from pydantic import BaseModel, Field, field_validator

from crashclouseau import config
from crashclouseau.agent import roles, triage
from crashclouseau.agent.schema import _extract_last_json_block
from crashclouseau.agent.tools import bugzilla as bugzilla_tools
from crashclouseau.agent.tools import crashstats as crashstats_tools
from crashclouseau.agent.tools import history as history_tools
from crashclouseau.agent.tools import patch as patch_tools
from crashclouseau.agent.tools import searchfox_cg
from crashclouseau.agent.tools import socorro as socorro_tools
from crashclouseau.agent.tools import source as source_tools
from crashclouseau.agent.tools.bugzilla import BugzillaCtx
from crashclouseau.agent.tools.crashstats import CrashStatsCtx
from crashclouseau.agent.tools.history import HistoryCtx
from crashclouseau.agent.tools.patch import PatchCtx
from crashclouseau.agent.tools.searchfox_cg import SearchfoxCtx
from crashclouseau.agent.tools.socorro import SocorroCtx
from crashclouseau.agent.tools.source import SourceCtx
from crashclouseau.logger import logger
from crashclouseau.searchfox import SearchfoxClient
from crashclouseau.vendor.agent_tools.claude_sdk import build_sdk_server
from crashclouseau.vendor.hackbot_runtime.claude import Reporter

ASSESSMENTS = ("regression", "exposure", "external", "environment", "unknown")
CONFIDENCES = ("low", "medium", "high")

# The population tools' ids, in the same shape as ``roles.*_tool_ids``.
_CRASHSTATS = ["mcp__crashstats__{}".format(name) for name in ("facets", "report")]


def crashstats_tool_ids() -> list[str]:
    return list(_CRASHSTATS)


_SYSTEM = (
    "You are investigating a crash-volume SPIKE in Firefox for Mozilla's crash triage. One "
    "crash signature's volume has risen well beyond its own recent baseline -- the numbers, the "
    "stacks, the population facts and the candidate changesets are in the brief. The spike is a "
    "fact: a bug will be filed for it whatever you find. Your job is to give the developers who "
    "will read that bug the most useful FACT-BASED account of what could be wrong.\n\n"
    "What a good answer is, in order of value:\n"
    "1. A culprit candidate: a specific changeset (from the pushlog windows in the brief, or one "
    "you found through blame or file history) with a concrete mechanism connecting what it "
    "changed to this crash, and the evidence you checked for it. Say how confident you are.\n"
    "2. Failing that, a plausible path to the crash: what the code is doing when it dies, what "
    "state or input makes it die, and what changed in the crashing population (an OS or driver "
    "version, a process type, a version, an annotation value, a memory state) -- each statement "
    "backed by something you read with a tool in this run.\n"
    "3. Failing that, an honest map: what you checked and ruled out, and what the code owner "
    "should look at first.\n\n"
    "Never invent. Every claim about code, history or the crash population must come from a tool "
    "result you obtained here; label anything else as a hypothesis, or leave it out. A short "
    "answer of checked facts is worth more than a long one with guesses: a wrong culprit sends a "
    "team down the wrong path and costs every later finding its credibility. A candidate merely "
    "being in the window, or sharing a keyword with the signature, is not evidence. Being "
    "already present in the spiking build is not a refutation -- for a rate change, a hang or a "
    "timeout, that is exactly what a regressor looks like; for a crash the signature has had "
    "for years, ask what made it FREQUENT, not what introduced it. A change can also merely "
    "expose an older bug; say which you think it is.\n\n"
    "You also name the Bugzilla product and component the bug belongs to: read them off the "
    "existing bugs for this signature (mcp__bugzilla__signature_bugs) or off a candidate's bug "
    "(mcp__bugzilla__bug), or infer them from the area of the crashing files, and give a "
    "one-line reason. Use an existing Bugzilla pair exactly as Bugzilla spells it.\n\n"
    "The ordinary pipeline already analysed this build and did not find a fileable culprit; its "
    "conclusions are in the brief for you to check, not to trust -- say where you agree or "
    "disagree and why.\n\n"
    "Tools (all read-only):\n"
    "- mcp__crashstats__facets: this signature's crash population broken down by one field, "
    "optionally SPLIT at the spiking build so the reports before and in the spike can be "
    "compared -- the fastest way to find what the spiking reports have in common that the "
    "earlier ones did not (an OS, a driver, a version, a shutdown phase, an annotation, a memory "
    "state). mcp__crashstats__report: one report's annotations, its thread list and any one "
    "thread's stack -- on a hang, read the thread that owns the awaited work, not the watchdog.\n"
    "- mcp__socorro__crash_stats: the signature's age (first-seen build) and top facets.\n"
    "- mcp__searchfox__*: read the crashing code at tip, walk the call graph (calls_from / "
    "calls_to / calls_between with FULLY-QUALIFIED symbols), field_layout for a struct offset.\n"
    "- mcp__source__raw_file: source AS OF the crash build (leak-free; searchfox is tip-only). "
    "mcp__history__blame / file_history / changeset: who changed a line or a file and when. "
    "mcp__patch__diff: a candidate changeset's actual diff.\n"
    "- mcp__bugzilla__bug / signature_bugs: a bug's product::component, status, regressed_by; "
    "the bugs already filed on this signature (read what a human already found).\n\n"
    "You have a bounded number of turns; spend them on the checks that would change the answer. "
    "When the window has a plausible candidate, read its diff and connect it to a crash frame "
    "before naming it. When it does not, the population comparison and the crashing code are "
    "where the answer usually is.\n\n"
    "End your reply with EXACTLY one fenced block:\n"
    "```json\n"
    "{\n"
    '  "summary": "<2-6 sentences for the bug: what is spiking, what you think is wrong, how sure you are>",\n'
    '  "assessment": "regression|exposure|external|environment|unknown",\n'
    '  "product": "<Bugzilla product>", "component": "<Bugzilla component>",\n'
    '  "component_reason": "<one line: where the pair came from>",\n'
    '  "culprit": {"node": "<changeset hash>", "bug": <bug number or null>, "confidence": "low|medium|high", "why": "<the mechanism connecting the change to this crash>"} or null,\n'
    '  "trigger_path": "<the checked path to the crash, or empty>",\n'
    '  "evidence": [{"claim": "<one checked fact>", "source": "<what you read: a searchfox permalink, an hg node + file, a crash-stats facet line, a bug id>"}],\n'
    '  "ruled_out": ["<candidate or hypothesis>: <why it is out>"],\n'
    '  "open_questions": ["<what the code owner should check first>"]\n'
    "}\n"
    "```\n"
    "assessment: regression = a Firefox change made this crash happen or happen more; exposure = "
    "a change exposed an older defect; external = the cause is outside Firefox (an OS update, a "
    "driver, an antivirus, web content); environment = a collection or signature artefact, not a "
    "real change; unknown = you could not tell. culprit.confidence is about the CAUSAL link, not "
    "about the change existing. An empty evidence list means you found nothing checkable -- say "
    "so in the summary rather than filling it."
)


class SpikeCulprit(BaseModel):
    node: str = ""
    bug: int | None = None
    confidence: str = "low"
    why: str = ""

    @field_validator("node", mode="before")
    @classmethod
    def _clean_node(cls, v):
        return str(v or "").strip().lower()

    @field_validator("bug", mode="before")
    @classmethod
    def _int_bug(cls, v):
        if v in (None, "", 0, "0"):
            return None
        try:
            return int(str(v).strip().lstrip("#"))
        except (TypeError, ValueError):
            return None

    @field_validator("confidence", mode="before")
    @classmethod
    def _known_confidence(cls, v):
        s = str(v or "").strip().lower()
        return s if s in CONFIDENCES else "low"


class SpikeEvidence(BaseModel):
    claim: str = ""
    source: str = ""

    @field_validator("claim", "source", mode="before")
    @classmethod
    def _text(cls, v):
        return str(v or "").strip()


class SpikeFindings(BaseModel):
    """What the investigator concluded. Lenient on purpose: one malformed optional field must
    not destroy the whole handoff -- the volume facts file the bug, and a summary is all the
    analysis strictly needs."""

    summary: str = ""
    assessment: str = "unknown"
    product: str | None = None
    component: str | None = None
    component_reason: str = ""
    culprit: SpikeCulprit | None = None
    trigger_path: str = ""
    evidence: list[SpikeEvidence] = Field(default_factory=list)
    ruled_out: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)

    @field_validator("assessment", mode="before")
    @classmethod
    def _known_assessment(cls, v):
        s = str(v or "").strip().lower()
        return s if s in ASSESSMENTS else "unknown"

    @field_validator("summary", "trigger_path", "component_reason", mode="before")
    @classmethod
    def _text(cls, v):
        return str(v or "").strip()

    @field_validator("product", "component", mode="before")
    @classmethod
    def _opt_text(cls, v):
        s = str(v or "").strip()
        return s or None

    @field_validator("culprit", mode="before")
    @classmethod
    def _culprit_or_none(cls, v):
        if not isinstance(v, dict) or not str(v.get("node") or "").strip():
            return None
        return v

    @field_validator("evidence", mode="before")
    @classmethod
    def _evidence_items(cls, v):
        out = []
        for item in v or []:
            if isinstance(item, dict):
                out.append(item)
            elif isinstance(item, str) and item.strip():
                out.append({"claim": item, "source": ""})
        return out

    @field_validator("ruled_out", "open_questions", mode="before")
    @classmethod
    def _string_list(cls, v):
        if isinstance(v, str):
            v = [v]
        return [str(x).strip() for x in (v or []) if str(x or "").strip()]


def parse_findings(text: str | None) -> SpikeFindings | None:
    """The trailing ```json block as ``SpikeFindings``, or ``None`` when there is no readable
    one. Salvages: a block that parses but fails validation is retried with the offending
    optional fields dropped, because the summary is what the bug needs."""
    obj = _extract_last_json_block(text)
    if not isinstance(obj, dict):
        return None
    try:
        return SpikeFindings.model_validate(obj)
    except Exception:
        pass
    keep = {k: obj.get(k) for k in ("summary", "assessment", "product", "component",
                                    "component_reason", "trigger_path")}
    try:
        return SpikeFindings.model_validate(keep)
    except Exception:
        return None


@dataclass
class SpikeRun:
    """What one investigator run produced, with its price."""

    findings: SpikeFindings | None = None
    result: str = ""
    num_turns: int | None = None
    total_cost_usd: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    tool_calls: int = 0
    tools_used: dict = field(default_factory=dict)
    is_error: bool = False
    error: str | None = None
    model: str | None = None
    effort: str | None = None

    @property
    def grounded(self) -> bool:
        """Did the model consult anything? A run with no tool call has read nothing, and its
        prose is a guess however fluent; the filer publishes only the facts from such a run."""
        return self.tool_calls > 0

    def usage(self) -> dict[str, Any]:
        return {
            "num_turns": self.num_turns, "total_cost_usd": self.total_cost_usd,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens, "tool_calls": self.tool_calls,
            "tools_used": dict(self.tools_used), "model": self.model, "effort": self.effort,
        }


def build_options(brief: dict, *, searchfox_client=None) -> ClaudeAgentOptions:
    """The investigator's ``ClaudeAgentOptions``: Claude Fable 5.1 at the configured effort, the
    scoped MCP tools only, and the CLI's built-in toolset OFF. Pass ``searchfox_client`` in tests
    to avoid resolving the ``searchfox-cli`` binary."""
    cfg = config.get_agent_spike_escalation()
    channel = brief.get("channel", "nightly")
    product = brief.get("product") or "Firefox"
    pin_rev = brief.get("pin_rev", "") or ""
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
        "crashstats": build_sdk_server(
            "crashstats", CrashStatsCtx(product=product, channel=channel),
            crashstats_tools.TOOLS),
    }
    allowed = [
        *roles.searchfox_tool_ids(), *roles.patch_tool_ids(),
        *roles.history_tool_ids(), *roles.source_tool_ids(),
        *roles.bugzilla_tool_ids(), *roles.socorro_tool_ids(), *crashstats_tool_ids(),
    ]
    kwargs = dict(
        system_prompt=_SYSTEM,
        mcp_servers=mcp_servers,
        allowed_tools=allowed,
        # `tools=[]` is the REGISTRATION control the allowlist is not: the transport emits
        # `--tools ""`, so Bash / Read / Write / WebFetch / Agent are not offered at all. The MCP
        # servers above are unaffected (`--tools` filters the built-in set only).
        tools=[],
        model=triage._model_id(cfg["model"]),
        max_turns=cfg["max_turns"],
        permission_mode="bypassPermissions",
        setting_sources=[],
        # Same inline-subagent pin as every other run (``triage._CLI_ENV``); no Agent tool is
        # registered here, so it is belt and braces.
        env=dict(triage._CLI_ENV),
    )
    if cfg.get("effort"):
        kwargs["effort"] = cfg["effort"]
    if cfg.get("fallback_model"):
        kwargs["fallback_model"] = triage._model_id(cfg["fallback_model"])
    if cfg.get("max_cost_usd"):
        kwargs["max_budget_usd"] = float(cfg["max_cost_usd"])
    return ClaudeAgentOptions(**kwargs)


def _candidate_line(c: dict) -> str:
    parts = [str(c.get("node", ""))]
    if c.get("score") is not None:
        parts.append("score={}".format(c["score"]))
    if c.get("bug"):
        parts.append("bug={}".format(c["bug"]))
    pushdate = c.get("pushdate")
    if pushdate:
        parts.append(
            "arrived-with-the-cycle-merge={} (not its landing date)".format(
                triage._fmt_pushdate(pushdate))
            if c.get("via_merge") else "landed={}".format(triage._fmt_pushdate(pushdate)))
    if c.get("backedout"):
        parts.append("is-itself-a-backout-commit")
    if c.get("noise"):
        parts.append("(scored only through an anchor/ubiquitous frame)")
    if c.get("prior_sig"):
        parts.append("[prior-sig: a fixed sibling of this signature was regressed by this bug]")
    if c.get("pref_flip"):
        parts.append("[feature-flip]")
    if c.get("desc"):
        parts.append("| {}".format(c["desc"]))
    return "- " + " ".join(parts)


def _classic_run_lines(run: dict) -> list[str]:
    verdict = run.get("verdict") or "?"
    head = "- run on {}: {}".format(run.get("uuid", "?"), verdict)
    if run.get("confidence"):
        head += " ({})".format(run["confidence"])
    if run.get("status") and run["status"] != "done":
        head += " [status {}]".format(run["status"])
    out = [head]
    cand = run.get("candidate") or {}
    if cand.get("node"):
        out.append("    candidate named: {}{}".format(
            cand["node"], " (bug {})".format(cand["bug"]) if cand.get("bug") else ""))
    if run.get("abstain_kind") or run.get("abstain_reason"):
        out.append("    abstained: {}{}".format(
            run.get("abstain_kind") or "", " -- {}".format(run["abstain_reason"])
            if run.get("abstain_reason") else ""))
    if run.get("mechanism"):
        out.append("    mechanism it proposed: {}".format(run["mechanism"]))
    so = run.get("second_opinion") or {}
    if so:
        verdict_so = so.get("corroborates")
        word = ("corroborated" if verdict_so is True else
                "REFUTED" if verdict_so is False else "was unsure about")
        out.append("    an independent blind second opinion {} it{}".format(
            word, ": {}".format(so["refutation"]) if so.get("refutation") else ""))
    if run.get("declined"):
        out.append("    the filer declined it: {}".format(run["declined"]))
    return out


def _user_prompt(brief: dict) -> str:
    """The brief as the investigator reads it. Everything a section states is a number or a
    fact the pipeline measured; the ask is at the end so it is what the model has last."""
    channel = brief.get("channel", "nightly")
    lines = [
        "SPIKE UNDER INVESTIGATION",
        "Signature: {}".format(brief.get("signature", "")),
        "Product / channel / version: {} {} {}".format(
            brief.get("product") or "Firefox", channel, brief.get("version") or ""),
        "Spiking build: {} (build-day {})".format(brief.get("buildid") or "?",
                                                  brief.get("build_day") or "?"),
        "Representative crash report: {}".format(brief.get("uuid") or "?"),
    ]
    if brief.get("spike_sentence"):
        lines += ["", "WHAT FIRED: " + brief["spike_sentence"]]
        spike = brief.get("spike") or {}
        if spike.get("kind") == "build_day":
            lines.append(
                "  (the selector's bar is {}x the loudest of the preceding build-days; the "
                "Poisson excess of this day against that baseline is z={}, bar {}; distinct "
                "installations {} against a floor of {})".format(
                    config.get_spike("ratio", brief.get("product") or "Firefox", channel),
                    spike.get("z"), spike.get("z_min"), spike.get("installs"),
                    spike.get("min_installs")))
    if brief.get("trend_sentence"):
        lines.append("Rate over the last week: " + brief["trend_sentence"])
    if brief.get("is_hang"):
        lines.append(
            "This is a HANG / TIMEOUT signature: the stack is what the awaited thread was doing "
            "when the watchdog fired, and a regressor of its RATE adds work, I/O or blocking to "
            "the awaited path -- it is usually years old and was not 'introduced' by anything.")
    facts = brief.get("facts") or []
    if facts:
        lines += ["", "Crash facts (the representative report, then the signature):", *facts]
    stacks = brief.get("stacks") or []
    for i, st in enumerate(stacks):
        if i == 0:
            title = "STACK 1 -- the representative report {} ({}):".format(
                st.get("uuid", "?"), st.get("share") or "one proto-signature cluster")
        else:
            title = "STACK {} -- a DIFFERENT proto-signature cluster of the same spike, report {} ({}):".format(
                i + 1, st.get("uuid", "?"), st.get("share") or "")
        lines += ["", title]
        if i > 0 and st.get("facts"):
            lines += st["facts"]
        if st.get("stack"):
            lines.append(st["stack"])
    classic = brief.get("classic_runs") or []
    lines += [""]
    if classic:
        lines.append(
            "WHAT THE ORDINARY PIPELINE CONCLUDED on this build ({} run{}; it reads the scored "
            "candidates and the pushlog window with a multi-agent triage, then a blind second "
            "opinion; nothing it found was fileable):".format(
                len(classic), "" if len(classic) == 1 else "s"))
        for run in classic:
            lines += _classic_run_lines(run)
    else:
        lines.append(
            "THE ORDINARY PIPELINE PRODUCED NO CONCLUSION on this build (it never ran, or it "
            "could not build a seed), so you are the first analysis.")
    cands = brief.get("candidates") or {}
    onstack = cands.get("onstack") or []
    window = cands.get("window") or []
    if onstack:
        lines += ["", "CANDIDATE CHANGESETS THAT TOUCHED A FILE ON THE STACK (line-proximity "
                      "scored; read a diff with mcp__patch__diff):"]
        lines += [_candidate_line(c) for c in onstack[:20]]
    if window:
        lines += ["", "THE PUSHLOG WINDOW OF THE SPIKING BUILD -- {} -- {} changeset{} "
                      "(lightly ranked by relevance to the signature, NOT by proximity; the "
                      "regressor of an off-stack spike can be anywhere in it, and a rate "
                      "regressor may predate it):".format(
                          cands.get("window_extent") or "previous build to this one",
                          len(window), "" if len(window) == 1 else "s")]
        lines += [_candidate_line(c) for c in window]
    elif not onstack:
        lines += ["", "No candidate changesets could be enumerated for this build (the pushlog "
                      "window was unavailable); use mcp__history__* and blame on the crashing "
                      "files to find what changed."]
    if brief.get("notes"):
        lines += ["", str(brief["notes"])]
    lines += [
        "",
        "YOUR TASK: explain, from facts you check with the tools, what could be wrong -- a "
        "culprit changeset with its mechanism if the evidence supports one, otherwise the most "
        "plausible checked path to the crash and what changed in the population -- and name the "
        "Bugzilla product::component for the bug. State what you ruled out. Then emit the JSON "
        "block.",
    ]
    return "\n".join(lines)


async def run_spike_agent(brief: dict, *, searchfox_client=None) -> SpikeRun:
    """Drive the investigator to its terminal result. Never raises for a model-side failure:
    the returned ``SpikeRun`` says what happened (``is_error`` / ``error``), and the caller files
    the volume facts either way."""
    cfg = config.get_agent_spike_escalation()
    run = SpikeRun(model=triage._model_id(cfg["model"]), effort=cfg.get("effort"))
    options = build_options(brief, searchfox_client=searchfox_client)
    prompt = _user_prompt(brief)
    logger.info(
        "spike: investigator prompt bytes system=%d user=%d for %s on %s",
        len(_SYSTEM), len(prompt), brief.get("signature", "?"), brief.get("buildid", "?"))
    result_msg = None
    try:
        with Reporter(verbose=False, log_path=None) as reporter:
            reporter.header("spike {} {}".format(brief.get("signature", "?"),
                                                 brief.get("buildid", "?")))
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for msg in client.receive_response():
                    reporter.message(msg)
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, ToolUseBlock):
                                run.tool_calls += 1
                                run.tools_used[block.name] = run.tools_used.get(block.name, 0) + 1
                    if isinstance(msg, ResultMessage):
                        result_msg = msg
    except Exception as exc:
        logger.error("spike: investigator run failed for %s", brief.get("signature", "?"),
                     exc_info=True)
        run.is_error = True
        run.error = "{}: {}".format(type(exc).__name__, exc)
        return run
    if result_msg is None:
        run.is_error = True
        run.error = "no terminal ResultMessage"
        return run
    run.result = result_msg.result or ""
    run.num_turns = getattr(result_msg, "num_turns", None)
    run.total_cost_usd = getattr(result_msg, "total_cost_usd", None)
    run.input_tokens, run.output_tokens, run.cache_read_tokens = triage._sum_tokens(result_msg)
    if getattr(result_msg, "is_error", False):
        run.is_error = True
        run.error = "errored run ({}): {}".format(
            getattr(result_msg, "subtype", "") or "error", (run.result or "")[:500])
        logger.warning("spike: investigator errored for %s: %s", brief.get("signature", "?"),
                       run.error)
        return run
    run.findings = parse_findings(run.result)
    if run.findings is None:
        logger.warning("spike: investigator emitted no readable handoff for %s after %s turns; "
                       "final text ended: %r", brief.get("signature", "?"), run.num_turns,
                       run.result[-1500:])
    return run
