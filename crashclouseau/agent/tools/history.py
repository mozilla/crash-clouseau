# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Firefox source-history tools (`mcp__history__*`).

Read-only @tools over hg.mozilla.org's ``json-filelog`` / ``json-annotate`` /
``json-rev``, via libmozdata (which stamps our ``crash-clouseau`` User-Agent, adds
429 retry/backoff, and maps channel -> repo). They give the agent file history, line
blame, and changeset metadata in ONE structured call, so patch-scout / call-graph-
explorer / skeptic stop reconstructing history by shelling out to ``curl
hg.mozilla.org/...`` or a local ``git log`` -- the run's turn-cost long pole, and
non-portable to the prod worker, which has no checkout (see the 2026-07-09 prod-sim).
Output is citation-friendly: an hg node + path + line.

``channel`` defaults to the crash's channel; pass ``nightly`` to query mozilla-central
(where regressors usually land first) even for a beta/release crash."""
from __future__ import annotations

import asyncio
import datetime
import re
from dataclasses import dataclass
from typing import Annotated

from pydantic import Field

from libmozdata.hgmozilla import Annotate, FileInfo, Revision

from crashclouseau import inspector
from crashclouseau.vendor.agent_tools.registry import tool, tools_in

_BUG_RE = re.compile(r"\bbug[ \t]*([0-9]+)", re.I)
_BACKOUT_RE = re.compile(r"^(?:back(?:ed|ing|s)?[ _]*out|revert(?:ing|s)?)\b", re.I)
_MAX_HISTORY = 50
_MAX_BLAME_LINES = 60
_MAX_FILES = 40


@dataclass
class HistoryCtx:
    """Per-run history context; ``channel`` selects the default hg repo."""

    channel: str = "nightly"


def _short(node) -> str:
    return (node or "")[:12]


def _bug(desc):
    m = _BUG_RE.search(desc or "")
    return int(m.group(1)) if m else None


def _first_line(desc) -> str:
    lines = (desc or "").strip().splitlines()
    return lines[0] if lines else ""


def _date(ts) -> str:
    # hg dates are ``[epoch, tzoffset]``; take the epoch.
    try:
        if isinstance(ts, (list, tuple)):
            ts = ts[0]
        return datetime.datetime.fromtimestamp(
            float(ts), datetime.timezone.utc
        ).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return "?"


def _author(user) -> str:
    # "Name <email>" -> "Name"; a bare email -> its local part.
    user = user or ""
    if "<" in user:
        return user.split("<", 1)[0].strip() or user
    return user.split("@", 1)[0] if "@" in user else user


def _resolve(node: str) -> str:
    # Candidate nodes are git post-migration; the json-* endpoints want hg. Skip the
    # tip/default sentinels; fall back to the node as-is when git2hg has no counterpart.
    if not node or node in ("tip", "default"):
        return node or "tip"
    try:
        return inspector.git2hg(node) or node
    except Exception:  # pragma: no cover - resolver is best-effort
        return node


@tool
async def file_history(
    ctx: HistoryCtx,
    path: Annotated[str, Field(description="Repo-relative source path, e.g. "
                               "xpcom/base/nsCycleCollector.cpp")],
    node: Annotated[str, Field(description="As-of revision (hg/git node or 'tip').")] = "tip",
    channel: Annotated[str, Field(description="Firefox channel -> hg repo; default = the "
                                  "crash's channel. Pass 'nightly' for mozilla-central.")] = "",
    limit: Annotated[int, Field(description="Max changesets to return (newest first).")] = 15,
) -> str:
    """Recent changesets that touched a file (newest first): hg node, date, bug, backout
    flag, author, one-line description. Use to find the regressor -- what changed in the
    crashing file/area before the crash. Prefer this over `curl hg.mozilla.org` or `git log`."""
    ch = channel or ctx.channel
    hg_node = _resolve(node)
    try:
        data = await asyncio.to_thread(FileInfo.get, path, ch, hg_node)
    except Exception as exc:  # noqa: BLE001 - a tool must not raise into the agent loop
        return "file_history: fetch failed for {}@{} ({}: {})".format(
            path, ch, type(exc).__name__, exc)
    entries = ((data or {}).get(path) or {}).get("entries") or []
    if not entries:
        return "file_history: no changesets for {} at {} (channel {}).".format(path, hg_node, ch)
    lim = max(1, min(int(limit or 15), _MAX_HISTORY))
    out = ["history of {} (channel {}, {} of {} entries):".format(
        path, ch, min(lim, len(entries)), len(entries))]
    for e in entries[:lim]:
        desc = e.get("desc") or ""
        tags = []
        bug = _bug(desc)
        if bug:
            tags.append("bug {}".format(bug))
        if _BACKOUT_RE.match(desc.strip()):
            tags.append("BACKOUT")
        tag = (" ".join(tags) + "  ") if tags else ""
        out.append("  {}  {}  {}{}  {}".format(
            _short(e.get("node")), _date(e.get("date")), tag,
            _author(e.get("user")), _first_line(desc)[:90]))
    return "\n".join(out)


@tool
async def blame(
    ctx: HistoryCtx,
    path: Annotated[str, Field(description="Repo-relative source path.")],
    line_start: Annotated[int, Field(description="First line number to blame (1-based).")],
    line_end: Annotated[int, Field(description="Last line (inclusive); default = line_start.")] = 0,
    node: Annotated[str, Field(description="As-of revision (hg/git node or 'tip').")] = "tip",
    channel: Annotated[str, Field(description="Channel -> hg repo; default = crash's channel.")] = "",
) -> str:
    """Blame a line range: for each line, the changeset that last touched it (hg node, bug,
    author, description). Use on the crashing line to find which change last modified it.
    Prefer this over `curl .../json-annotate` or local `hg/git blame`."""
    ch = channel or ctx.channel
    hg_node = _resolve(node)
    start = max(1, int(line_start))
    end = int(line_end) if line_end and int(line_end) >= start else start
    end = min(end, start + _MAX_BLAME_LINES - 1)
    try:
        data = await asyncio.to_thread(Annotate.get, path, ch, hg_node)
    except Exception as exc:  # noqa: BLE001
        return "blame: fetch failed for {}@{} ({}: {})".format(
            path, ch, type(exc).__name__, exc)
    rows = ((data or {}).get(path) or {}).get("annotate") or []
    sel = [r for r in rows if start <= int(r.get("lineno", -1)) <= end]
    if not sel:
        return "blame: no annotation for {} lines {}-{} (channel {}).".format(path, start, end, ch)
    out = ["blame {} lines {}-{} (channel {}):".format(path, start, end, ch)]
    for r in sel:
        bug = _bug(r.get("desc"))
        out.append("  L{}: {}  {}{}  | {}".format(
            r.get("lineno"), _short(r.get("node")),
            ("bug {}  ".format(bug)) if bug else "",
            _author(r.get("author")), (r.get("line") or "").rstrip("\n")[:100]))
    return "\n".join(out)


@tool
async def changeset(
    ctx: HistoryCtx,
    node: Annotated[str, Field(description="Changeset hash (hg or git; git is auto-converted).")],
    channel: Annotated[str, Field(description="Channel -> hg repo; default = crash's channel.")] = "",
) -> str:
    """Metadata for one changeset: author, date, bug, backed-out-by, and the changed files
    (with status). Use to inspect a candidate regressor. Prefer this over `curl .../json-rev`."""
    ch = channel or ctx.channel
    hg_node = _resolve(node)
    try:
        rev = await asyncio.to_thread(Revision.get_revision, ch, hg_node)
    except Exception as exc:  # noqa: BLE001
        return "changeset: fetch failed for {}@{} ({}: {})".format(
            node, ch, type(exc).__name__, exc)
    if not rev or not rev.get("node"):
        return "changeset: not found: {} (channel {}).".format(node, ch)
    desc = rev.get("desc") or ""
    out = ["changeset {} (channel {})".format(_short(rev.get("node")), ch)]
    if rev.get("git_commit"):
        out.append("  git: {}".format(_short(rev.get("git_commit"))))
    out.append("  date: {}   author: {}".format(_date(rev.get("date")), _author(rev.get("user"))))
    bug = _bug(desc)
    if bug:
        out.append("  bug: {}".format(bug))
    if rev.get("backedoutby"):
        out.append("  BACKED OUT BY: {}".format(_short(rev.get("backedoutby"))))
    out.append("  desc: {}".format(_first_line(desc)[:200]))
    files = rev.get("files") or []
    out.append("  files ({}):".format(len(files)))
    for f in files[:_MAX_FILES]:
        if isinstance(f, dict):
            out.append("    {} {}".format(f.get("status", "?"), f.get("file", "?")))
        else:
            out.append("    {}".format(f))
    if len(files) > _MAX_FILES:
        out.append("    ... (+{} more)".format(len(files) - _MAX_FILES))
    return "\n".join(out)


TOOLS = tools_in(__name__)
