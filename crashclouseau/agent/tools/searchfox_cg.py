# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Searchfox call-graph tools (`mcp__searchfox__*`).

Read-only @tool wrappers over Clouseau's #01 ``SearchfoxClient`` (call-graph +
definition + search), exposed to the Claude Agent SDK loop by
``build_sdk_server("searchfox", SearchfoxCtx(...), TOOLS)``. The first parameter
of every handler is the ctx (stripped from the agent-facing schema); the client
is *synchronous* (it shells out to ``searchfox-cli``), so each call is offloaded
with ``asyncio.to_thread`` to avoid blocking the event loop. Results are rendered
as LLM-friendly markdown that PRESERVES the citation anchors (mangled symbol id +
permalink) every downstream claim needs. A valid-but-empty result
(``SearchfoxNoResult``) is returned as a plain "no result" string (the abstain
path), not an error; real failures become ``ToolError``.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from pydantic import Field
from typing import Annotated

from crashclouseau.searchfox import (
    CallGraph,
    Definition,
    SearchfoxClient,
    SearchfoxError,
    SearchfoxNoResult,
    SearchHit,
    SymbolRef,
)
from crashclouseau.vendor.agent_tools.registry import ToolError, tool, tools_in


@dataclass
class SearchfoxCtx:
    """Shared per-run searchfox client (one in-process cache per run)."""

    client: SearchfoxClient


def _sf_error(exc: SearchfoxError, what: str) -> ToolError:
    return ToolError(
        f"searchfox {what} failed: {exc}",
        payload={"error": "searchfox_error", "what": what, "message": str(exc)},
    )


def _fmt_callgraph(g: CallGraph) -> str:
    header = (
        f"calls-{g.direction} {g.root.pretty} "
        f"(depth {g.depth}, repo {g.repo}):"
    )
    lines = [header]
    for e in g.edges:
        c = e.callee if g.direction != "to" else e.caller
        sym = c.symbol_id or "?"
        loc = f"{c.file}:{c.line}" if c.file else ""
        link = c.permalink or ""
        lines.append(
            f"- d{e.depth} {e.caller.pretty} -> {e.callee.pretty} "
            f"[{sym}] {loc} {link}".rstrip()
        )
    return "\n".join(lines)


def _fmt_definition(d: Definition) -> str:
    head = d.symbol.pretty
    if d.permalink:
        head += f"  {d.permalink}"
    if d.start_line is not None:
        head += f"  (lines {d.start_line}-{d.end_line})"
    return f"{head}\n\n{d.source}"


def _fmt_hits(hits: list[SearchHit]) -> str:
    if not hits:
        return "No results found."
    return "\n".join(
        f"{h.file}:{h.line}: {h.text} {h.permalink or ''}".rstrip() for h in hits
    )


def _fmt_refs(refs: list[SymbolRef]) -> str:
    if not refs:
        return "No matches found."
    return "\n".join(
        f"{r.pretty} {r.file}:{r.line} {r.permalink or ''}".rstrip() for r in refs
    )


@tool
async def calls_from(
    ctx: SearchfoxCtx,
    symbol: Annotated[str, Field(description="Mangled or demangled symbol to expand outward.")],
    repo: Annotated[str | None, Field(description="searchfox repo token, e.g. mozilla-central (default: configured; no autoland).")] = None,
    depth: Annotated[int, Field(description="Call-graph depth; 1 = direct callees.")] = 1,
) -> str:
    """List the functions CALLED BY `symbol` out to `depth`, each with its mangled symbol id + searchfox permalink for citation. Walk this outward from a crash frame toward off-stack callees."""
    try:
        graph = await asyncio.to_thread(ctx.client.calls_from, symbol, repo, depth)
    except SearchfoxNoResult:
        return f"No callees found for {symbol!r}."
    except SearchfoxError as exc:
        raise _sf_error(exc, "calls_from") from exc
    return _fmt_callgraph(graph)


@tool
async def calls_to(
    ctx: SearchfoxCtx,
    symbol: Annotated[str, Field(description="Mangled or demangled symbol whose callers to find.")],
    repo: Annotated[str | None, Field(description="searchfox repo token (default: configured; no autoland).")] = None,
    depth: Annotated[int, Field(description="Call-graph depth; 1 = direct callers.")] = 1,
) -> str:
    """List the functions that CALL `symbol` out to `depth`, each with its mangled symbol id + searchfox permalink for citation. Walk this to find off-stack callers that may pass bad state in."""
    try:
        graph = await asyncio.to_thread(ctx.client.calls_to, symbol, repo, depth)
    except SearchfoxNoResult:
        return f"No callers found for {symbol!r}."
    except SearchfoxError as exc:
        raise _sf_error(exc, "calls_to") from exc
    return _fmt_callgraph(graph)


@tool
async def calls_between(
    ctx: SearchfoxCtx,
    source: Annotated[str, Field(description="Source symbol/class scope.")],
    target: Annotated[str, Field(description="Target symbol/class scope.")],
    repo: Annotated[str | None, Field(description="searchfox repo token (default: configured; no autoland).")] = None,
    depth: Annotated[int, Field(description="Path depth to search.")] = 2,
) -> str:
    """Direct call edges on paths between `source` and `target`. NB `--calls-between` is class/namespace-scoped, so it may return nothing for plain function pairs; prefer calls_from/calls_to at higher depth for function-level reach. Returns 'no path' rather than fabricating an edge."""
    try:
        graph = await asyncio.to_thread(
            ctx.client.calls_between, source, target, repo, depth
        )
    except SearchfoxNoResult:
        return f"No path found between {source!r} and {target!r}."
    except SearchfoxError as exc:
        raise _sf_error(exc, "calls_between") from exc
    return _fmt_callgraph(graph)


@tool
async def define(
    ctx: SearchfoxCtx,
    symbol: Annotated[str, Field(description="Symbol whose full definition body to fetch.")],
    repo: Annotated[str | None, Field(description="searchfox repo token (default: configured; no autoland).")] = None,
) -> str:
    """Fetch the full source body of `symbol`'s definition, with a commit-pinned permalink and line range for citation. Use to read a function before reasoning about its data flow."""
    try:
        definition = await asyncio.to_thread(ctx.client.define, symbol, repo)
    except SearchfoxNoResult:
        return f"No definition found for {symbol!r}."
    except SearchfoxError as exc:
        raise _sf_error(exc, "define") from exc
    return _fmt_definition(definition)


@tool
async def lookup(
    ctx: SearchfoxCtx,
    name: Annotated[str, Field(description="Name/identifier to resolve to source locations.")],
    repo: Annotated[str | None, Field(description="searchfox repo token (default: configured; no autoland).")] = None,
    limit: Annotated[int, Field(description="Max locations to return.")] = 50,
) -> str:
    """Resolve `name` to source locations (built on search). NB these carry no mangled symbol id; use a symbol id from a calls_* result when one is required."""
    try:
        refs = await asyncio.to_thread(ctx.client.lookup, name, repo, limit)
    except SearchfoxError as exc:
        raise _sf_error(exc, "lookup") from exc
    return _fmt_refs(refs)


@tool
async def search(
    ctx: SearchfoxCtx,
    query: Annotated[str, Field(description="Text (or regex, if regex=true) to search the indexed source for.")],
    regex: Annotated[bool, Field(description="Treat query as a regular expression.")] = False,
    repo: Annotated[str | None, Field(description="searchfox repo token (default: configured; no autoland).")] = None,
    limit: Annotated[int, Field(description="Max matches to return.")] = 50,
) -> str:
    """Free-text (or regex) search over the indexed source, each hit with file:line + permalink. Use to bridge call-graph holes (virtual/IPC/FFI) by finding implementors/message names/symbols."""
    try:
        hits = await asyncio.to_thread(ctx.client.search, query, regex, repo, limit)
    except SearchfoxError as exc:
        raise _sf_error(exc, "search") from exc
    return _fmt_hits(hits)


TOOLS = tools_in(__name__)
