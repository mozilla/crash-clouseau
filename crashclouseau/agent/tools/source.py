# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Pinned source-read tool (`mcp__source__*`).

A read-only @tool that returns a Firefox source file's text AS OF the crash BUILD revision
(via ``crashclouseau.hgedge`` -> hg-edge raw-file, allowlisted UA + throttle). This is the
LEAK-FREE way to read a function body during a P1 off-stack run: searchfox is tip-only, so
``mcp__searchfox__define``/``search`` show code as it exists AFTER the fix landed -- reading
it there can make a post-fix state look like the crash cause. ``rev`` defaults to the run's
pinned build rev (``ctx.build_rev``); when that is empty (on-stack runs) it reads tip, i.e.
the same view searchfox gives, so granting this tool is harmless outside pinned mode.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Annotated

from pydantic import Field

from crashclouseau import hgedge, inspector
from crashclouseau.agent.tools import pin_node
from crashclouseau.vendor.agent_tools.registry import tool, tools_in

_MAX_LINES = 400  # cap the rendered slice so a huge file can't flood the context


@dataclass
class SourceCtx:
    """Per-run source context: ``channel`` selects the hg-edge repo; ``build_rev`` is the
    crash BUILD rev that reads default to (empty -> tip, for on-stack runs)."""

    channel: str = "nightly"
    build_rev: str = ""


def _resolve(node: str) -> str:
    # DB/build nodes are git post-migration; hg-edge wants hg. Skip the tip sentinel;
    # fall back to the node as-is when git2hg has no counterpart.
    if not node or node in ("tip", "default"):
        return node or "tip"
    try:
        return inspector.git2hg(node) or node
    except Exception:  # pragma: no cover - resolver is best-effort
        return node


@tool
async def raw_file(
    ctx: SourceCtx,
    path: Annotated[str, Field(description="Repo-relative source path, e.g. dom/base/nsINode.cpp")],
    line_start: Annotated[int, Field(description="First line to return (1-based); 0 = from the top.")] = 0,
    line_end: Annotated[int, Field(description="Last line, inclusive; 0 = a default window from line_start.")] = 0,
    rev: Annotated[str, Field(description="As-of revision; default = the crash BUILD rev (pinned). Leave empty for the pinned build rev.")] = "",
) -> str:
    """Read a source file AS OF the crash build revision (pinned, leak-free). Use THIS —
    not `mcp__searchfox__define`/`search` — to read a function body when investigating an
    off-stack candidate: searchfox is tip-only and shows post-fix code. Returns the
    requested line range (capped); the header states the pinned revision so the line
    numbers are citable against the build, not tip."""
    # DETERMINISTIC pin (shared with the history tools): in a pinned run an empty rev OR an
    # explicit tip/default is redirected to the build rev, so this tool — whose whole point
    # is leak-free reads — can never be talked into reading tip. An explicit non-tip node
    # (e.g. to deliberately compare against another rev) is still honored.
    node = _resolve(pin_node(ctx.build_rev, rev))
    text = await asyncio.to_thread(hgedge.raw_file, path, node, ctx.channel)
    if text is None:
        return "raw_file: not found or fetch failed for {}@{} (channel {}).".format(
            path, node, ctx.channel
        )
    lines = text.splitlines()
    n = len(lines)
    start = max(1, int(line_start or 1))
    if start > n:
        return "raw_file: {} has {} lines; start {} is past EOF (rev {}).".format(
            path, n, start, node
        )
    end = int(line_end) if line_end and int(line_end) >= start else start + _MAX_LINES - 1
    end = min(end, n, start + _MAX_LINES - 1)
    out = ["{} lines {}-{} of {} (rev {}, channel {}):".format(
        path, start, end, n, node, ctx.channel)]
    for i in range(start, end + 1):
        out.append("{:>6}: {}".format(i, lines[i - 1]))
    return "\n".join(out)


TOOLS = tools_in(__name__)
