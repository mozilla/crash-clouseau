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
from crashclouseau import config, utils
from crashclouseau.vendor.agent_tools.registry import tool, tools_in

_BUG_FIELDS = [
    "id", "summary", "status", "resolution", "product", "component",
    "keywords", "regressed_by", "regressions", "dupe_of",
]
_SEARCH_FIELDS = [
    "id", "summary", "status", "resolution", "product", "component",
    "cf_crash_signature",
]
_MAX_SIG_BUGS = 15
_SEARCH_PAGE_SIZE = 100
_MAX_SIG_CANDIDATES = 500


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
    signature: Annotated[
        str, Field(description="The exact crash signature.", min_length=1)
    ],
) -> str:
    """Find existing Bugzilla bugs whose crash-signature field matches this signature. Use it
    to see whether the crash is already reported / known — reuse prior analysis and avoid a
    duplicate — before proposing a fresh mechanism. Read-only.

    Each row names the bug's product::component, and you have to read it: {other_applications}
    also build on mozilla-central and so share Gecko's crash signatures, which means a matching
    bug in one of THEIR products is a different application's crash population with its own
    cause, however well the stack matches. It is context, not this crash's bug."""
    signature = str(signature or "").strip()
    if not signature:
        return "signature_bugs: invalid empty crash signature."

    # BMO has only a substring operator for this field.  It is a candidate fetch, not the
    # match: one signature is often a prefix of another (especially AsyncShutdownTimeout
    # blocker lists), so hold every returned field to an exact ``[@ signature]`` entry.
    # ``limit`` also deliberately selects libmozdata's single-query path.  Without one it
    # first counts and then downloads EVERY matching bug in 100-row pages, even though this
    # tool renders only ``_MAX_SIG_BUGS`` rows; its count request also silently turns a
    # non-2xx response into an empty result.
    base_params = {
        "include_fields": _SEARCH_FIELDS,
        "f1": "cf_crash_signature", "o1": "substring", "v1": signature,
        "limit": _SEARCH_PAGE_SIZE,
        "order": "bug_id DESC",
    }
    exact: dict[int, dict] = {}
    folded_signature = signature.casefold()
    exhausted = False
    try:
        for offset in range(0, _MAX_SIG_CANDIDATES, _SEARCH_PAGE_SIZE):
            page = await asyncio.to_thread(
                _fetch, None, {**base_params, "offset": offset})
            for b in page.values():
                if any(entry.casefold() == folded_signature
                       for entry in utils.bugzilla_signature_entries(
                           b.get("cf_crash_signature"))):
                    exact[b["id"]] = b
            if len(page) < _SEARCH_PAGE_SIZE:
                exhausted = True
                break
            if len(exact) >= _MAX_SIG_BUGS:
                break
    except Exception as exc:  # pragma: no cover - network/defensive
        return "signature_bugs: lookup failed ({}).".format(exc)
    if not exact:
        if not exhausted:
            return (
                "signature_bugs: no exact match among the {} newest substring candidates; "
                "older candidates were not scanned."
            ).format(_MAX_SIG_CANDIDATES)
        return "signature_bugs: no existing bug references this signature."
    ordered = sorted(exact.values(), key=lambda x: x.get("id", 0), reverse=True)
    rows = []
    if not exhausted:
        rows.append("signature_bugs: showing up to {} newest exact matches; more may exist.".format(
            _MAX_SIG_BUGS))
    elif len(ordered) > _MAX_SIG_BUGS:
        rows.append("signature_bugs: showing the {} newest exact matches of {}.".format(
            _MAX_SIG_BUGS, len(ordered)))
    for b in ordered[:_MAX_SIG_BUGS]:
        state = "{} {}".format(b.get("status", ""), b.get("resolution", "") or "").strip()
        where = " {} :: {}".format(b["product"], b.get("component", "?")) \
            if b.get("product") else ""
        rows.append("bug {} [{}]{} — {}".format(
            b.get("id"), state, where, b.get("summary", "")))
    return "\n".join(rows)


TOOLS = tools_in(__name__)

# The other-application clause in ``signature_bugs`` is RENDERED from the map in
# ``crashclouseau.config``, not written out. It used to be a second hand-written copy of that
# map (``eval/study_corpus`` was a third, and had already drifted to the opposite answer for
# Android and GeckoView), so correcting the map left the agent reading the old list with
# nothing anywhere to surface the divergence.
#
# ``.replace`` and not ``.format``: this docstring is RST and reaches the model verbatim, so one
# future ``{...}`` anywhere in it would turn a prompt edit into a KeyError at import.
#
# Safe at module level because ``ToolDefinition`` is a plain (non-frozen) dataclass and the SDK
# adapter reads ``description`` inside ``build_sdk_server`` (vendor/agent_tools/claude_sdk.py:31),
# which runs per RUN. That is also the seam for Fenix day: ``second_opinion.build_options``
# already knows the crash's product (second_opinion.py:125), so a per-crash clause is a
# ``dataclasses.replace`` of this one tool there, not a change here.
for _defn in TOOLS:
    _defn.description = _defn.description.replace(
        "{other_applications}", config.describe_other_applications())
