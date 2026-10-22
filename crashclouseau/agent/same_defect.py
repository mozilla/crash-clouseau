# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Compare candidate bugs; the filer applies confidence and channel policies."""
from __future__ import annotations

import math
from collections import Counter

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
from pydantic import BaseModel

from crashclouseau import config, utils
from crashclouseau.agent import roles, triage
from crashclouseau.agent.schema import _extract_last_json_block
from crashclouseau.agent.tools import patch as patch_tools
from crashclouseau.agent.tools import searchfox_cg
from crashclouseau.agent.tools import source as source_tools
from crashclouseau.agent.tools.patch import PatchCtx
from crashclouseau.agent.tools.searchfox_cg import SearchfoxCtx
from crashclouseau.agent.tools.source import SourceCtx
from crashclouseau.logger import logger
from crashclouseau.searchfox import SearchfoxClient
from crashclouseau.vendor.agent_tools.claude_sdk import build_sdk_server
from crashclouseau.vendor.hackbot_runtime.claude import Reporter


_SYSTEM = (
    "You decide whether a Firefox crash that is about to be filed as a new bug is the same "
    "defect as a bug that already exists. Candidates are bugs attributed to the same regressor "
    "(filings naming it and that bug's regressions) and bugs we filed or commented on whose "
    "stacks share frames with this crash, with duplicates replaced by their targets. When a "
    "[meta] tracker holds the crash's signature, they are the tracker's dependencies instead, "
    "each about one cause of that shared signature and often without a signature of its own. "
    "None of these establishes a shared changeset or defect. One defect can appear under "
    "several crash signatures.\n\n"
    "Same defect means that one code fix would stop both crashes: the same faulty code path or "
    "state, reached through the same part of the change when a regressor is named. Sharing the "
    "regressor, stack frames, the component, a changed file or a crash type does not establish "
    "it. When the two analyses rely on different hunks of the change, different objects or "
    "different failure modes, require evidence connecting them to one fault. An analysis can be "
    "wrong; check the stack and diff rather than comparing the prose.\n\n"
    "A wrong match can hide a defect. Name a bug only when you can "
    "state the shared fault concretely.\n\n"
    "Tools (read-only): mcp__patch__diff (the regressor's diff when one is named; check which "
    "hunks each analysis relies on), searchfox (mcp__searchfox__*: read code, walk the call "
    "graph between the frames), mcp__source__raw_file (defaults to the build revision when "
    "available, otherwise tip; check the returned revision).\n\n"
    "End your reply with EXACTLY one fenced block:\n"
    "```json\n"
    '{"same_defect_bug": <bug number or null>, "confidence": "low|medium|high", '
    '"reason": "<1-3 sentences: the shared fault, or what separates the crash from each '
    'bug>"}\n'
    "```\n"
    "same_defect_bug is one of the listed bugs, or null when none is the same defect. "
    "confidence is in your own conclusion."
)

_SYSTEM_PRODUCT_PHRASE = "whether a Firefox crash"

_MAX_FRAMES = 60
_MAX_SIBLING_FRAMES = 60
_MAX_DESCRIPTION = 4000
_MAX_COMMENT = 1000
_MAX_COMMENTS = 4000
# A frame is generic only when its corpus count exceeds both thresholds.
_GENERIC_SHARE = 0.05
_GENERIC_MIN = 5


class SameDefect(BaseModel):
    """The agent's proposed match; ``bug=None`` means no accepted candidate ID."""

    bug: int | None = None
    confidence: str = "low"
    reason: str = ""
    cost_usd: float | None = None


def _system_prompt(product: str | None = None) -> str:
    product = product or "Firefox"
    if product == "Firefox":
        return _SYSTEM
    return _SYSTEM.replace(_SYSTEM_PRODUCT_PHRASE, "whether a {} crash".format(product), 1)


def crash_from_dossier(signature: str, dossier: dict | None, frames: list | None,
                       ipc_fatal_error_msg: str | None = None) -> dict:
    """Build prompt fields from the dossier, frames and a caller-supplied Socorro IPC message."""
    d = dossier or {}
    verdict = d.get("verdict") or {}
    mechanism = verdict.get("mechanism") or {}
    brief = d.get("crash") or {}
    return {
        "signature": signature or brief.get("signature") or "",
        "title": verdict.get("title") or "",
        "mechanism": mechanism.get("statement") if isinstance(mechanism, dict) else "",
        "data_flow": (d.get("data_flow") or {}).get("summary") or "",
        "crash_reason": brief.get("moz_crash_reason") or brief.get("reason") or "",
        "ipc_fatal_error_msg": ipc_fatal_error_msg or "",
        "frames": list(frames or brief.get("frames") or []),
    }


def crash_from_spike(signature: str, findings, frames: list | None,
                     details: dict | None = None) -> dict:
    """Build prompt fields from spike findings (model or dict) and crash data."""
    d = details or {}
    f = findings if isinstance(findings, dict) else vars(findings) if findings is not None else {}
    return {
        "signature": signature or "",
        "title": "",
        "mechanism": f.get("summary") or "",
        "data_flow": f.get("trigger_path") or "",
        "crash_reason": d.get("moz_crash_reason") or d.get("reason") or "",
        "ipc_fatal_error_msg": d.get("ipc_fatal_error_msg") or "",
        "frames": list(frames or []),
    }


def frame_key(frame: dict) -> tuple[str, str]:
    """Return ``(file, function)`` without arguments and with lambdas in the ``<T>`` form."""
    fn = (frame.get("function") or "").strip()
    start = fn.find("operator()")
    paren = fn.find("(", start + len("operator()") if start >= 0 else 0)
    if paren > 0:
        fn = fn[:paren].strip()
    return frame.get("filename") or "", utils.lambda_family(fn)


def frame_keys(frames, depth: int) -> dict:
    """Map frame keys to their first index within the first *depth* frames."""
    out = {}
    for i, f in enumerate((frames or [])[:depth]):
        if f.get("function"):
            out.setdefault(frame_key(f), i)
    return out


def rank_by_shared_frames(keys: dict, corpus: list[dict], candidates: list[dict]):
    """Rank candidates by the rarity of shared non-generic frames in *corpus*.

    Return ``(score, shared keys, candidate)`` tuples, highest score first.
    """
    n = len(corpus)
    df = Counter(k for item in corpus for k in item["keys"])
    generic = {k for k, c in df.items() if c > max(_GENERIC_SHARE * n, _GENERIC_MIN)}
    out = []
    for item in candidates:
        shared = sorted((set(keys) & set(item["keys"])) - generic, key=keys.get)
        if shared:
            out.append((sum(math.log(n / df[k]) for k in shared if df[k]), shared, item))
    out.sort(key=lambda x: -x[0])
    return out


def _frame_lines(frames: list, limit: int) -> list[str]:
    out = []
    for f in (frames or [])[:limit]:
        where = f.get("filename") or f.get("module") or ""
        if where and f.get("line"):
            where = "{}:{}".format(where, f["line"])
        out.append("  {} {}{}".format(f.get("stackpos", len(out)), f.get("function") or "?",
                                      "  ({})".format(where) if where else ""))
    return out


def _analysis_lines(item: dict) -> list[str]:
    lines = []
    if item.get("title"):
        lines.append("Analysis title: {}".format(item["title"]))
    if item.get("crash_reason"):
        lines.append("Crash reason: {}".format(item["crash_reason"]))
    if item.get("ipc_fatal_error_msg"):
        lines.append("IPC FatalError message: {}".format(item["ipc_fatal_error_msg"]))
    if item.get("mechanism"):
        lines.append("Mechanism: {}".format(item["mechanism"]))
    if item.get("data_flow"):
        lines.append("Data flow: {}".format(item["data_flow"]))
    return lines


def _comment_lines(comments) -> list[str]:
    """Keep the newest comments within the text budget; render them oldest first."""
    blocks, total = [], 0
    for c in reversed(comments or []):
        text = (c.get("text") or "").strip()
        if not text:
            continue
        if len(text) > _MAX_COMMENT:
            text = text[:_MAX_COMMENT] + " [... truncated]"
        if total + len(text) > _MAX_COMMENTS:
            break
        total += len(text)
        blocks.append("- {}: {}".format(c.get("author") or "?", text))
    return ["Later comments:", *reversed(blocks)] if blocks else []


def user_prompt(crash: dict, regressor: dict | None, siblings: list[dict]) -> str:
    """Describe the crash and candidate bugs, including their selection reasons,
    stored analyses or descriptions, and comments.
    """
    regressor = regressor or {}
    node = (regressor.get("node") or "") if regressor.get("bug") else ""
    lines = []
    if regressor.get("bug"):
        lines += ["Regressor: bug {}{}".format(regressor["bug"],
                                               ", changeset {}".format(node) if node else ""),
                  ""]
    lines += [
        "THE CRASH ABOUT TO BE FILED",
        "Signature: {}".format(crash.get("signature") or ""),
        *_analysis_lines(crash),
    ]
    frames = _frame_lines(crash.get("frames"), _MAX_FRAMES)
    if frames:
        lines += ["Stack:", *frames]
    awaited = crash.get("awaited") or {}
    frames = _frame_lines(awaited.get("frames"), _MAX_FRAMES)
    if frames:
        lines += ["The work this hang waits for, on thread `{}`:".format(
            awaited.get("name") or "?"), *frames]
    for sib in siblings:
        lines += ["", "BUG {}: {}".format(sib.get("bug"), sib.get("summary") or "")]
        if sib.get("status"):
            lines.append("Status: {}".format(sib["status"]))
        sigs = sib.get("signatures") or []
        if sigs:
            lines.append("Crash signatures: {}".format(" ".join("[@ {}]".format(s) for s in sigs)))
        if sib.get("by_regressor"):
            lines.append("Listed because: attributed to the regressor")
        if sib.get("by_tracker"):
            lines.append("Listed because: blocks [meta] bug {}, which holds the crash's "
                         "signature".format(sib["by_tracker"]))
        if sib.get("shared_frames"):
            lines.append("Listed because: our filing, whose stack shares these frames with the "
                         "crash: {}".format("; ".join(sib["shared_frames"])))
        analysis = _analysis_lines(sib)
        if analysis:
            lines += ["Our analysis of a crash on this bug:", *analysis]
            frames = _frame_lines(sib.get("frames"), _MAX_SIBLING_FRAMES)
            if frames:
                lines += ["Stack:", *frames]
        elif sib.get("description"):
            text = sib["description"]
            if len(text) > _MAX_DESCRIPTION:
                text = text[:_MAX_DESCRIPTION] + "\n[... truncated]"
            lines += ["Description (comment 0):", text]
        lines += _comment_lines(sib.get("comments"))
    diff = ("the regressor's diff{} and ".format(
        " (mcp__patch__diff {})".format(node) if node else "") if regressor.get("bug") else "")
    lines += [
        "",
        "Is the crash about to be filed the same defect as one of these bugs? Inspect {}the "
        "code the stacks go through before answering.".format(diff),
    ]
    return "\n".join(lines)


def build_options(channel: str = "nightly", build_rev: str = "", product: str | None = None, *,
                  searchfox_client=None) -> ClaudeAgentOptions:
    """``ClaudeAgentOptions`` with scoped MCP tools only. Pass ``searchfox_client`` in tests to
    avoid resolving the ``searchfox-cli`` binary."""
    cfg = config.get_agent_same_defect()
    if searchfox_client is None:
        searchfox_client = SearchfoxClient()
    mcp_servers = {
        "searchfox": build_sdk_server(
            "searchfox", SearchfoxCtx(client=searchfox_client, channel=channel),
            searchfox_cg.TOOLS),
        "patch": build_sdk_server("patch", PatchCtx(channel=channel), patch_tools.TOOLS),
        "source": build_sdk_server("source", SourceCtx(channel=channel, build_rev=build_rev),
                                   source_tools.TOOLS),
    }
    kwargs = dict(
        system_prompt=_system_prompt(product),
        mcp_servers=mcp_servers,
        allowed_tools=[*roles.searchfox_tool_ids(), *roles.patch_tool_ids(),
                       *roles.source_tool_ids()],
        # An empty tools list becomes --tools "" in the SDK.
        tools=[],
        model=triage._model_id(cfg["model"]),
        max_turns=cfg["max_turns"],
        permission_mode="bypassPermissions",
        setting_sources=[],
        env=dict(triage._CLI_ENV),
    )
    if cfg["effort"]:
        kwargs["effort"] = cfg["effort"]
    return ClaudeAgentOptions(**kwargs)


def parse_same_defect(text: str | None, sibling_ids) -> SameDefect | None:
    """Parse the last JSON block; return ``None`` for missing JSON or an invalid bug ID.
    Unlisted IDs become no match; unknown confidence values become ``low``."""
    obj = _extract_last_json_block(text)
    if not isinstance(obj, dict):
        return None
    raw = obj.get("same_defect_bug")
    try:
        bug = int(str(raw).strip().lstrip("#")) if raw not in (None, "", "null") else None
    except (TypeError, ValueError):
        return None
    confidence = str(obj.get("confidence") or "low").lower()
    if confidence not in ("low", "medium", "high"):
        confidence = "low"
    allowed = {int(b) for b in sibling_ids}
    if bug is not None and bug not in allowed:
        logger.warning("same-defect: answer names bug %s, not one of %s", bug, sorted(allowed))
        bug = None
    return SameDefect(bug=bug, confidence=confidence, reason=str(obj.get("reason") or ""))


async def run_same_defect(crash: dict, regressor: dict | None, siblings: list[dict], *,
                          channel: str = "nightly", build_rev: str = "",
                          product: str | None = None) -> SameDefect | None:
    """Run one check; return ``None`` on query/result failure. Option setup may raise."""
    options = build_options(channel, build_rev, product)
    result_msg = None
    try:
        with Reporter(verbose=False, log_path=None) as reporter:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(user_prompt(crash, regressor, siblings))
                async for msg in client.receive_response():
                    reporter.message(msg)
                    if isinstance(msg, ResultMessage):
                        result_msg = msg
    except Exception:
        logger.warning("same-defect: run failed for %r", crash.get("signature"), exc_info=True)
        return None
    if result_msg is None or getattr(result_msg, "is_error", False):
        logger.warning("same-defect: no usable result for %r: %s", crash.get("signature"),
                       getattr(result_msg, "result", None))
        return None
    out = parse_same_defect(result_msg.result or "", [s.get("bug") for s in siblings])
    if out is None:
        logger.warning("same-defect: unparseable result for %r", crash.get("signature"))
        return None
    out.cost_usd = getattr(result_msg, "total_cost_usd", None)
    return out
