# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""The threads of the crash report under analysis (``mcp__crash__threads``).

Reads the seed's processed crash without network access. Shares ``crashstats.thread_text``."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

from pydantic import Field

from crashclouseau.agent.tools import crashstats
from crashclouseau.vendor.agent_tools.registry import tool, tools_in


@dataclass
class ThreadsCtx:
    raw: dict = field(default_factory=dict)


@tool
async def threads(
    ctx: ThreadsCtx,
    thread: Annotated[int, Field(
        description="Thread index to print. Default -1 selects the analysed thread: usually "
                    "the crashing thread, or the main thread for a recognized shutdown hang. "
                    "The census lists indexes; invalid indexes use the default.")] = -1,
    max_frames: Annotated[int, Field(description="Frames to print (default 40, max 60).")] = 40,
) -> str:
    """Read this report's thread census and one selected stack, up to 60 frames.
    Work, idle and missing-stack groups are capped at 80 entries each. Work labels and
    wait kinds are heuristic; inspect the stack to test them.
    Read-only, using the processed crash already in the seed."""
    return "\n".join(crashstats.thread_text(ctx.raw, thread, max_frames))


TOOLS = tools_in(__name__)
