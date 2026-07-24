# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Scoped crash-stats / Socorro tool (`mcp__socorro__*`) for the blind second-opinion agent.

A read-only @tool over ``libmozdata.socorro.SuperSearch`` that returns ONE crash
signature's recent occurrence breakdown — the first buildid it appears in (the regression
window) and the OS / CPU / process-type / channel / moz_crash_reason / reason facets. It is
deliberately SIGNATURE-scoped (not a generic Socorro passthrough): the second-opinion agent
runs on a tight allowlist with no shell, so this is its only crash-stats access and it
cannot be turned into a pushlog / arbitrary-query tool. UA-safe — libmozdata's SuperSearch
inherits the allowlisted ``crash-clouseau`` User-Agent stamped by ``crashclouseau.net``.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated

from pydantic import Field

from libmozdata import socorro
from crashclouseau.vendor.agent_tools.registry import tool, tools_in

# Facets that carry cause-pointing signal: build_id (regression window), and the platform /
# cpu / process-type / channel / crash-reason breakdown (a lopsided facet narrows the cause).
_FACETS = [
    "build_id", "platform_pretty_version", "cpu_arch", "process_type",
    "release_channel", "moz_crash_reason", "reason",
]


@dataclass
class SocorroCtx:
    """Per-run crash-stats context: the crash's ``product`` (and optional ``channel``) so a
    query is scoped to the same product/channel the crash came from."""

    product: str = "Firefox"
    channel: str = "nightly"


def _search(params: dict) -> dict:
    got: dict = {}

    def handler(json, data):
        data["result"] = json

    socorro.SuperSearch(params=params, handler=handler, handlerdata=got).wait()
    return got.get("result") or {}


def _facet_line(facets: dict, key: str, label: str, n: int = 6) -> str | None:
    rows = facets.get(key) or []
    if not rows:
        return None
    parts = ["{} ({})".format(r.get("term"), r.get("count")) for r in rows[:n] if r.get("term")]
    return "{}: {}".format(label, ", ".join(parts)) if parts else None


@tool
async def crash_stats(
    ctx: SocorroCtx,
    signature: Annotated[str, Field(description="The exact crash signature to query.")],
    days: Annotated[int, Field(description="Look-back window in days (default 30).")] = 30,
) -> str:
    """Query crash-stats.mozilla.org (Socorro SuperSearch) for this crash SIGNATURE's recent
    occurrences and their breakdown. Use it to spot a pattern that points at the cause: the
    first buildid the signature appears in (locates the regression window), plus the OS /
    CPU / process-type / channel / moz_crash_reason / reason facets (a lopsided facet — e.g.
    one platform only — narrows the likely change). Read-only; scoped to this signature."""
    days = max(1, int(days or 30))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    params = {
        "signature": "=" + signature,
        "product": ctx.product,
        "date": ">=" + since,
        "_facets": _FACETS,
        "_results_number": 0,
        "_facets_size": 20,
    }
    if ctx.channel:
        params["release_channel"] = ctx.channel
    data = await asyncio.to_thread(_search, params)
    if not data:
        return "crash_stats: no data (lookup failed) for signature {!r}.".format(signature)
    total = data.get("total", 0)
    facets = data.get("facets") or {}
    lines = ["crash-stats for [@ {}] (product {}{}, last {}d): {} crashes".format(
        signature, ctx.product,
        ", channel " + ctx.channel if ctx.channel else "", days, total)]
    builds = sorted(str(f.get("term")) for f in facets.get("build_id", []) if f.get("term"))
    if builds:
        lines.append("first-seen buildid: {} (across {} build(s))".format(builds[0], len(builds)))
    for key, label in (("platform_pretty_version", "OS"), ("cpu_arch", "CPU"),
                       ("process_type", "process"), ("release_channel", "channel"),
                       ("moz_crash_reason", "moz_crash_reason"), ("reason", "reason")):
        line = _facet_line(facets, key, label)
        if line:
            lines.append(line)
    return "\n".join(lines)


TOOLS = tools_in(__name__)
