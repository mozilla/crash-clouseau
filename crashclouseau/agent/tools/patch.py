# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Patch tool (`mcp__patch__*`).

A read-only @tool over the deterministic #14 patch-extraction (``patch_extract``),
exposed to the Claude Agent SDK loop by ``build_sdk_server("patch", PatchCtx(...),
TOOLS)``. It hands the agent a candidate changeset's parsed diff in ONE fast,
cached call — each changed file's hunks with exact +/- line numbers, content, and
the enclosing function — so patch-scout / data-flow-tracer stop shelling out
(git/hg archaeology was the run's long pole). Output is citation-ready: node +
filename + line + side + content is exactly a #03 ``diff_line`` citation.

The DB stores changeset hashes as git post-migration while ``fetch_raw_diff`` reads
the hg ``raw-rev`` endpoint, so a git hash is converted via ``inspector.git2hg``
first (falling back to the node as-is when it has no hg counterpart)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Annotated

from pydantic import Field

from crashclouseau import inspector
from crashclouseau.agent import patch_extract
from crashclouseau.vendor.agent_tools.registry import tool, tools_in

_MAX_LINES = 400  # cap the rendered diff so a large patch can't flood the context


@dataclass
class PatchCtx:
    """Per-run patch context (the crash's channel selects the raw-rev repo)."""

    channel: str = "nightly"


def _resolve_hg(node: str) -> str:
    # DB hashes are git post-migration; raw-rev wants hg. Fall back to the node
    # as-is when git2hg has no counterpart (already-hg nodes / vendored sources).
    return inspector.git2hg(node) or node


def _fmt_patch(ext, node: str) -> str:
    if ext is None or ext.is_empty():
        return "No diff available for {} (channel {}).".format(node, ext.channel if ext else "?")
    out = ["patch {} (channel {}):".format(node, ext.channel)]
    n = 0
    for f in ext.files:
        out.append("file {} ({})".format(f.filename, f.status))
        for h in f.hunks:
            where = " in {}".format(h.enclosing_function) if h.enclosing_function else ""
            out.append("  @@ -{} +{}{}".format(h.old_start, h.new_start, where))
            for ln, text in h.deleted_lines:
                out.append("    - {}: {}".format(ln, text))
                n += 1
            for ln, text in h.added_lines:
                out.append("    + {}: {}".format(ln, text))
                n += 1
            if n >= _MAX_LINES:
                out.append("  ... (diff truncated at {} lines)".format(_MAX_LINES))
                return "\n".join(out)
    return "\n".join(out)


@tool
async def diff(
    ctx: PatchCtx,
    node: Annotated[
        str,
        Field(description="Candidate changeset hash (git or hg; a git hash is auto-converted)."),
    ],
) -> str:
    """Fetch the parsed unified diff for a candidate changeset: each changed file's
    hunks with exact +/- line numbers, line content, and the enclosing function.
    Citation-ready (node + filename + line + side + content is a diff_line citation).
    Prefer this over shelling out with Bash/hg to read a patch."""
    hg_node = await asyncio.to_thread(_resolve_hg, node)
    ext = await asyncio.to_thread(patch_extract.extract, hg_node, ctx.channel)
    return _fmt_patch(ext, node)


TOOLS = tools_in(__name__)
