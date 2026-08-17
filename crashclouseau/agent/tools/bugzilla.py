# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Scoped Bugzilla tool (`mcp__bugzilla__*`) for the blind second-opinion agent.

Read-only @tools over ``libmozdata.bugzilla.Bugzilla``: look up a bug (product::component,
summary, status, keywords, regressed_by / regressions), and find the bugs whose
crash-signature field matches a signature. Deliberately scoped to those two queries (not a
generic Bugzilla passthrough) — the second-opinion agent runs on a tight allowlist with no
shell, so this is its only Bugzilla access. UA-safe: libmozdata's Bugzilla inherits the
allowlisted ``crash-clouseau`` User-Agent stamped by ``crashclouseau.net``.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Annotated

from pydantic import Field

from libmozdata.bugzilla import Bugzilla
from crashclouseau.vendor.agent_tools.registry import tool, tools_in

_BUG_FIELDS = [
    "id", "summary", "status", "resolution", "product", "component",
    "keywords", "regressed_by", "regressions", "dupe_of",
]
_SEARCH_FIELDS = ["id", "summary", "status", "resolution", "product", "component"]
_MAX_SIG_BUGS = 15


@dataclass
class BugzillaCtx:
    """No per-run state — Bugzilla queries are self-contained."""


def _fetch(bugids=None, params=None) -> dict:
    got: dict = {}

    def handler(bug, data):
        data[bug["id"]] = bug

    if bugids is not None:
        Bugzilla(bugids=[str(b) for b in bugids], include_fields=_BUG_FIELDS,
                 bughandler=handler, bugdata=got).get_data().wait()
    else:
        Bugzilla(params, bughandler=handler, bugdata=got).get_data().wait()
    return got


@tool
async def bug(
    ctx: BugzillaCtx,
    bug_id: Annotated[int, Field(description="The Bugzilla bug number to look up.")],
) -> str:
    """Look up a Bugzilla bug: product::component, summary, status/resolution, keywords, and
    its ``regressed_by`` / ``regressions`` links. Use it to understand a candidate regressor
    bug — what it changed and what it is known to have regressed. Read-only. A
    security-restricted bug the token cannot read comes back as not accessible."""
    try:
        data = await asyncio.to_thread(_fetch, [int(bug_id)], None)
    except Exception as exc:  # pragma: no cover - network/defensive
        return "bug {}: lookup failed ({}).".format(bug_id, exc)
    b = data.get(int(bug_id))
    if not b:
        return "bug {}: not found or not accessible (may be security-restricted).".format(bug_id)
    parts = [
        "bug {} — {} :: {}".format(b.get("id"), b.get("product", "?"), b.get("component", "?")),
        "summary: {}".format(b.get("summary", "")),
        "status: {} {}".format(b.get("status", ""), b.get("resolution", "") or "").strip(),
    ]
    if b.get("keywords"):
        parts.append("keywords: {}".format(", ".join(b["keywords"])))
    if b.get("regressed_by"):
        parts.append("regressed_by: {}".format(", ".join(str(x) for x in b["regressed_by"])))
    if b.get("regressions"):
        parts.append("regressions: {}".format(", ".join(str(x) for x in b["regressions"])))
    if b.get("dupe_of"):
        parts.append("duplicate of: {}".format(b["dupe_of"]))
    return "\n".join(parts)


@tool
async def signature_bugs(
    ctx: BugzillaCtx,
    signature: Annotated[str, Field(description="The exact crash signature.")],
) -> str:
    """Find existing Bugzilla bugs whose crash-signature field matches this signature. Use it
    to see whether the crash is already reported / known — reuse prior analysis and avoid a
    duplicate — before proposing a fresh mechanism. Read-only.

    Each row names the bug's product::component, and you have to read it: every application
    built on mozilla-central (Thunderbird — ``MailNews Core``, ``Calendar``, ``Chat Core`` —
    and SeaMonkey) shares Gecko's crash signatures, so a matching bug in one of THEIR products
    is a different application's crash population with its own cause, however well the stack
    matches. It is context, not this crash's bug."""
    params = {
        "include_fields": _SEARCH_FIELDS,
        "f1": "cf_crash_signature", "o1": "substring", "v1": signature,
    }
    try:
        data = await asyncio.to_thread(_fetch, None, params)
    except Exception as exc:  # pragma: no cover - network/defensive
        return "signature_bugs: lookup failed ({}).".format(exc)
    if not data:
        return "signature_bugs: no existing bug references this signature."
    rows = []
    for b in sorted(data.values(), key=lambda x: x.get("id", 0), reverse=True)[:_MAX_SIG_BUGS]:
        state = "{} {}".format(b.get("status", ""), b.get("resolution", "") or "").strip()
        where = " {} :: {}".format(b["product"], b.get("component", "?")) \
            if b.get("product") else ""
        rows.append("bug {} [{}]{} — {}".format(
            b.get("id"), state, where, b.get("summary", "")))
    return "\n".join(rows)


TOOLS = tools_in(__name__)
