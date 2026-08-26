# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Scoped crash-stats / Socorro tool (`mcp__socorro__*`) for the blind second-opinion agent.

A read-only @tool over ``libmozdata.socorro.SuperSearch`` that returns ONE crash
signature's occurrence breakdown — the buildid it was FIRST seen in (how old the crash is)
and the OS / CPU / process-type / channel / moz_crash_reason / reason facets. It is
deliberately SIGNATURE-scoped (not a generic Socorro passthrough): the second-opinion agent
runs on a tight allowlist with no shell, so this is its only crash-stats access and it
cannot be turned into a pushlog / arbitrary-query tool. UA-safe — libmozdata's SuperSearch
inherits the allowlisted ``crash-clouseau`` User-Agent stamped by ``crashclouseau.net``.

The signature's AGE is load-bearing — it is the argument behind every verified
high-confidence refutation — so it comes from ``sigage.first_seen_buildid``, the one
authoritative implementation, and NOT from this tool's own facet query. Deriving it here
from a ``build_id`` facet is how it was wrong until 2026-07-29: that facet is ordered by
COUNT, so ``_facets_size`` drops the OLDEST build, and the 30-day counts window understates
the age again on top. Both errors point the same way — a signature that looks NEWER than it
is makes a late changeset look like a plausible origin — which is the exact mistake this
agent exists to catch.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated

from pydantic import Field

from libmozdata import socorro
from crashclouseau import sigage, utils
from crashclouseau.vendor.agent_tools.registry import tool, tools_in

# Facets that carry cause-pointing signal: the platform / cpu / process-type / channel /
# crash-reason breakdown (a lopsided facet narrows the cause). Deliberately NOT ``build_id``:
# that facet is ordered by COUNT, so ``_facets_size`` silently drops the OLDEST build — the one
# value we would want from it — and first-seen comes from ``sigage`` instead.
_FACETS = [
    "platform_pretty_version", "cpu_arch", "process_type",
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


async def _first_seen_line(ctx: SocorroCtx, signature: str) -> str:
    """The signature's age, as ONE line of agent-facing text.

    Asks ``sigage.signature_history`` — the same implementation the deterministic
    stale-signature gate uses, so the agent and the gate cannot disagree about how old a
    signature is. Its window truncation can only make first-seen look NEWER, so the answer is
    a LOWER bound on the age and the text says so: the model must not read it as "the signature
    is exactly this old".

    When the answer came from the OTHER channels, the channel's own first-seen is named too.
    The model needs the difference: "new on nightly, a year old on release" is the shape of a
    crash that a recent changeset merely exposed, and it is the shape reviewers keep having to
    point out by hand (bug 2063902: "crash stats shows this is an existing crash signature")."""
    hist = await asyncio.to_thread(
        sigage.signature_history, signature, ctx.product, ctx.channel)
    first_seen = hist["first_seen"]
    if not first_seen:
        return ("first-seen buildid: unknown (lookup found nothing / failed) — do NOT infer "
                "the signature's age from the crash counts, which cover a short window")
    seen_dt = sigage.to_datetime(first_seen)
    age = "" if seen_dt is None else " (that build is {}d old)".format(
        (datetime.now(timezone.utc) - seen_dt).days)
    scope = ""
    if hist["first_seen_channel"] and hist["first_seen_channel"] != first_seen:
        scope = ("; ACROSS ALL CHANNELS — on {} alone the oldest is only {} ({} of this "
                 "signature's {} reports are on other channels), so the crash goes back to {} "
                 "and a changeset that landed after that can at most have EXPOSED it"
                 .format(ctx.channel, hist["first_seen_channel"],
                         hist["total_other_channels"],
                         (hist["total"] or 0) + (hist["total_other_channels"] or 0),
                         first_seen))
    return ("first-seen buildid: {}{} — the OLDEST build this signature crashes in{}; crash "
            "reports were searched over the last {}d, so the signature can only be this old "
            "or older".format(first_seen, age, scope, sigage.MAX_WINDOW_DAYS))


@tool
async def crash_stats(
    ctx: SocorroCtx,
    signature: Annotated[str, Field(description="The exact crash signature to query.")],
    days: Annotated[int, Field(
        description="Look-back window in days for the crash COUNTS and facets (default 30, "
                    "max 364). The first-seen buildid is always searched over the full "
                    "year, whatever this is.")] = 30,
) -> str:
    """Query crash-stats.mozilla.org (Socorro SuperSearch) for this crash SIGNATURE's
    occurrences and their breakdown. Use it to spot a pattern that points at the cause: the
    buildid the signature was FIRST seen in (how old the crash is — a signature that was
    already crashing before a changeset landed cannot have been introduced by it), plus the
    OS / CPU / process-type / channel / moz_crash_reason / reason facets (a lopsided facet —
    e.g. one platform only — narrows the likely change). Read-only; scoped to this
    signature."""
    # Socorro rejects a range beyond 365 days with a 400 and `days` comes from the model, so
    # clamp BOTH ends (`MAX_WINDOW_DAYS` is that hard limit less the implicit "to now").
    days = max(1, min(int(days or 30), sigage.MAX_WINDOW_DAYS))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    # Resolved BEFORE the facet query, and reported even when that query fails: this is the
    # load-bearing number, and a bare "no data" leaves the agent leaning on the candidate alone.
    first_seen = await _first_seen_line(ctx, signature)
    params = {
        "signature": "=" + signature,
        "product": ctx.product,
        "date": ">=" + since,
        "_facets": _FACETS,
        "_results_number": 0,
        "_facets_size": 20,
    }
    if ctx.channel:
        # `get_search_channel`: on beta this is the only crash-stats instrument the blind
        # second opinion has, and the raw label under-counts it by 36-41% (DevEdition is
        # filed as `aurora`) -- both in the printed total and in the `release_channel`
        # facet the model reads to judge which channels are affected.
        params["release_channel"] = utils.get_search_channel(ctx.channel)
    try:
        data = await asyncio.to_thread(_search, params)
    except Exception as exc:  # noqa: BLE001 - a tool must not raise into the agent loop
        return "crash_stats: facet lookup failed for signature {!r} ({}: {}).\n{}".format(
            signature, type(exc).__name__, exc, first_seen)
    if not data:
        return "crash_stats: no facet data (lookup failed) for signature {!r}.\n{}".format(
            signature, first_seen)
    total = data.get("total", 0)
    facets = data.get("facets") or {}
    lines = ["crash-stats for [@ {}] (product {}{}, last {}d): {} crashes".format(
        signature, ctx.product,
        ", channel " + ctx.channel if ctx.channel else "", days, total), first_seen]
    for key, label in (("platform_pretty_version", "OS"), ("cpu_arch", "CPU"),
                       ("process_type", "process"), ("release_channel", "channel"),
                       ("moz_crash_reason", "moz_crash_reason"), ("reason", "reason")):
        line = _facet_line(facets, key, label)
        if line:
            lines.append(line)
    return "\n".join(lines)


TOOLS = tools_in(__name__)
