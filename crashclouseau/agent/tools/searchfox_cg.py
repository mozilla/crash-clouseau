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
    FieldLayout,
    SearchfoxClient,
    SearchfoxError,
    SearchfoxNoResult,
    SearchHit,
    SymbolRef,
)
from crashclouseau.searchfox import repo_for_channel
from crashclouseau.vendor.agent_tools.registry import ToolError, tool, tools_in


@dataclass
class SearchfoxCtx:
    """Shared per-run searchfox client (one in-process cache per run), plus the CHANNEL the
    crash is on.

    ``channel`` was missing, and it was the only one of the five MCP contexts without it
    (``PatchCtx`` / ``HistoryCtx`` / ``SourceCtx`` / ``SocorroCtx`` all take it). Every
    ``SearchfoxClient`` method takes ``repo=None``, and ``searchfox._coerce_repo(None)`` falls
    through to ``agent.searchfox.default_repo`` = ``mozilla-central`` — so for a beta crash the
    whole call-graph surface, every ``define``/``search``/``field_layout``, and every permalink
    the filed bug cites came from firefox-main tip: code that may never have been in the beta
    build, missing the uplifts that were. ``SearchfoxCitation.repo`` then honestly recorded
    ``mozilla-central`` and nothing downstream could tell it was the wrong tree.

    ``repo`` resolves ONCE per run (``searchfox.repo_for_channel``) and every tool uses it as
    its default, so a tool may still be pointed elsewhere explicitly — which is a real need:
    a beta regressor usually LANDED on central, so "look at where it came from" is a legitimate
    query. Same affordance as ``tools/history.py``'s channel argument."""

    client: SearchfoxClient
    channel: str = ""

    @property
    def repo(self) -> str:
        """The searchfox repo token this run reads by default."""
        return repo_for_channel(self.channel).value

    def repo_or(self, repo):
        """``repo`` if the caller named one, else this run's own repo."""
        return repo or self.repo


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


def _fmt_field_layout(fl: FieldLayout) -> str:
    head = f"field-layout {fl.class_name} (repo {fl.repo}"
    if fl.size is not None:
        head += f", size {fl.size}B, align {fl.align}B"
    head += "):"
    lines = [head]
    for f in fl.fields:
        lines.append(
            f"- offset {f.offset} (size {f.size}): {f.type} {f.name}".rstrip()
        )
    lines.append(
        "To cite a fault-address match, emit a citation "
        '{"kind":"struct_layout","type_name":"%s","field":"<name>",'
        '"offset":<n>,"repo":"%s"} where <n> is the field offset that equals '
        "the crash fault address." % (fl.class_name, fl.repo)
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# An empty call graph is NOT a fact about the code
# --------------------------------------------------------------------------- #
# Bug 2067349 was filed INVALID on a mechanism asserting "invalidation wired only into
# InsertChildToChildList/DisconnectChild, never into whole-parent-node destruction". The
# component owner refuted it in one sentence by naming three callers, and
# ``--calls-to 'nsINode::DisconnectChild'`` returns all three in its first two lines. But
# ``--calls-to 'DisconnectChild'`` -- the exact spelling that mechanism used -- returns an
# EMPTY graph, because ``searchfox._reduce_symbol`` only ever STRIPS qualification on retry,
# never adds it. So the old message here, "No callers found for 'DisconnectChild'.", was a
# false sentence, and it manufactured the very absence claim this tool exists to break.
#
# Measured 2026-08-28 over the 78 filings: 88 of 127 distinct backticked symbols in
# ``mechanism`` are this under-qualified single-``::`` form. Reproduces off-case, so it is not
# one bug's quirk: ``MatchClassList`` -> empty, ``mozilla::dom::ViewTransition::MatchClassList``
# -> its one real caller (that one was bug 2066182, which was FIXED, because its skeptic
# happened to qualify the name).
#
# ``NO_GRAPH_RESULT`` is exported so a reader can anchor on a GENERATED prefix instead of
# matching model prose -- the discipline that two retracted measurements here were missing.
# The same argument the repo already makes to the skeptic for ``field_layout``
# (``roles.py``: "If field_layout returns nothing, you under-qualified the name ... do NOT
# settle for `unverifiable`") applies to the whole call-graph family; it was simply never
# said here.
NO_GRAPH_RESULT = "No result"


def _no_graph_result(what: str, subject: str, relation: str) -> str:
    """The empty-graph answer, phrased as an UNANSWERED question rather than an absence."""
    return (
        "{} for {} {!r}: searchfox returned an empty graph. **This is not evidence that "
        "nothing {}.** An under-qualified name returns empty here, so retry with the "
        "FULLY-QUALIFIED symbol -- add every namespace and drop template `<...>` args "
        "(`Type::method` is usually not enough; `ns::Type::method` is). If it is still empty, "
        "the question is UNANSWERED: do not conclude an absence from it, and do not write "
        "that you searched and found none."
    ).format(NO_GRAPH_RESULT, what, subject, relation)


@tool
async def calls_from(
    ctx: SearchfoxCtx,
    symbol: Annotated[str, Field(description="Mangled or demangled symbol to expand outward.")],
    repo: Annotated[str | None, Field(description="searchfox repo token (default: THIS CRASH'S OWN repo -- mozilla-central for a nightly crash, mozilla-beta for a beta/DevEdition one). Pass mozilla-central explicitly to look at where a beta regressor originally landed. No autoland.")] = None,
    depth: Annotated[int, Field(description="Call-graph depth; 1 = direct callees.")] = 1,
) -> str:
    """List the functions CALLED BY `symbol` out to `depth`, each with its mangled symbol id + searchfox permalink for citation. Walk this outward from a crash frame toward off-stack callees."""
    try:
        graph = await asyncio.to_thread(ctx.client.calls_from, symbol, ctx.repo_or(repo), depth)
    except SearchfoxNoResult:
        return _no_graph_result("callees of", symbol, "is called by it")
    except SearchfoxError as exc:
        raise _sf_error(exc, "calls_from") from exc
    return _fmt_callgraph(graph)


@tool
async def calls_to(
    ctx: SearchfoxCtx,
    symbol: Annotated[str, Field(description="Mangled or demangled symbol whose callers to find.")],
    repo: Annotated[str | None, Field(description="searchfox repo token (default: this crash's own repo; pass mozilla-central to look at trunk even for a beta crash). No autoland.")] = None,
    depth: Annotated[int, Field(description="Call-graph depth; 1 = direct callers.")] = 1,
) -> str:
    """List the functions that CALL `symbol` out to `depth`, each with its mangled symbol id + searchfox permalink for citation. Walk this to find off-stack callers that may pass bad state in."""
    try:
        graph = await asyncio.to_thread(ctx.client.calls_to, symbol, ctx.repo_or(repo), depth)
    except SearchfoxNoResult:
        return _no_graph_result("callers of", symbol, "calls it")
    except SearchfoxError as exc:
        raise _sf_error(exc, "calls_to") from exc
    return _fmt_callgraph(graph)


@tool
async def calls_between(
    ctx: SearchfoxCtx,
    source: Annotated[str, Field(description="Source symbol/class scope.")],
    target: Annotated[str, Field(description="Target symbol/class scope.")],
    repo: Annotated[str | None, Field(description="searchfox repo token (default: this crash's own repo; pass mozilla-central to look at trunk even for a beta crash). No autoland.")] = None,
    depth: Annotated[int, Field(description="Path depth to search.")] = 2,
) -> str:
    """Direct call edges on paths between `source` and `target`. NB `--calls-between` is class/namespace-scoped, so it may return nothing for plain function pairs; prefer calls_from/calls_to at higher depth for function-level reach. Returns 'no path' rather than fabricating an edge."""
    try:
        graph = await asyncio.to_thread(
            ctx.client.calls_between, source, target, ctx.repo_or(repo), depth
        )
    except SearchfoxNoResult:
        # This one already documents its own scoping caveat on the tool description, but the
        # under-qualification failure is identical, so it gets the identical answer.
        return _no_graph_result(
            "call paths between", "{} -> {}".format(source, target), "connects them"
        )
    except SearchfoxError as exc:
        raise _sf_error(exc, "calls_between") from exc
    return _fmt_callgraph(graph)


@tool
async def define(
    ctx: SearchfoxCtx,
    symbol: Annotated[str, Field(description="Symbol whose full definition body to fetch.")],
    repo: Annotated[str | None, Field(description="searchfox repo token (default: this crash's own repo; pass mozilla-central to look at trunk even for a beta crash). No autoland.")] = None,
) -> str:
    """Fetch the full source body of `symbol`'s definition, with a commit-pinned permalink and line range for citation. Use to read a function before reasoning about its data flow."""
    try:
        definition = await asyncio.to_thread(ctx.client.define, symbol, ctx.repo_or(repo))
    except SearchfoxNoResult:
        return f"No definition found for {symbol!r}."
    except SearchfoxError as exc:
        raise _sf_error(exc, "define") from exc
    return _fmt_definition(definition)


@tool
async def lookup(
    ctx: SearchfoxCtx,
    name: Annotated[str, Field(description="Name/identifier to resolve to source locations.")],
    repo: Annotated[str | None, Field(description="searchfox repo token (default: this crash's own repo; pass mozilla-central to look at trunk even for a beta crash). No autoland.")] = None,
    limit: Annotated[int, Field(description="Max locations to return.")] = 50,
) -> str:
    """Resolve `name` to source locations (built on search). NB these carry no mangled symbol id; use a symbol id from a calls_* result when one is required."""
    try:
        refs = await asyncio.to_thread(ctx.client.lookup, name, ctx.repo_or(repo), limit)
    except SearchfoxError as exc:
        raise _sf_error(exc, "lookup") from exc
    return _fmt_refs(refs)


@tool
async def search(
    ctx: SearchfoxCtx,
    query: Annotated[str, Field(description="Text (or regex, if regex=true) to search the indexed source for.")],
    regex: Annotated[bool, Field(description="Treat query as a regular expression.")] = False,
    repo: Annotated[str | None, Field(description="searchfox repo token (default: this crash's own repo; pass mozilla-central to look at trunk even for a beta crash). No autoland.")] = None,
    limit: Annotated[int, Field(description="Max matches to return.")] = 50,
) -> str:
    """Free-text (or regex) search over the indexed source, each hit with file:line + permalink. Use to bridge call-graph holes (virtual/IPC/FFI) by finding implementors/message names/symbols."""
    try:
        hits = await asyncio.to_thread(ctx.client.search, query, regex, ctx.repo_or(repo), limit)
    except SearchfoxError as exc:
        raise _sf_error(exc, "search") from exc
    return _fmt_hits(hits)


@tool
async def field_layout(
    ctx: SearchfoxCtx,
    class_name: Annotated[str, Field(description="Bare C++ class/struct name (no template <...> args), e.g. mozilla::detail::nsTStringRepr.")],
    repo: Annotated[str | None, Field(description="searchfox repo token (default: this crash's own repo; pass mozilla-central to look at trunk even for a beta crash). No autoland.")] = None,
) -> str:
    """Byte-level memory layout (offset/size/type/name of each field) of a C++ class/struct. Use to VERIFY a null/small-address fault: a fault at address 0xN on type T is a null-deref of whichever field T places at byte offset N — turning an otherwise 'unverifiable' offset claim into a citable `struct_layout` fact. Pass the FULLY-QUALIFIED class name WITH namespaces and WITHOUT template <...> args (e.g. `mozilla::detail::nsTStringRepr`, taken from the crash signature/frames); a bare or template-suffixed name returns nothing. Layout the CONTAINING object (whose field is at the fault offset), not a template accessor."""
    try:
        fl = await asyncio.to_thread(ctx.client.field_layout, class_name, ctx.repo_or(repo))
    except SearchfoxNoResult:
        return (
            f"No field layout found for {class_name!r}. field-layout needs the "
            "FULLY-QUALIFIED class name WITH namespaces (and no template <...> args) — "
            "e.g. `mozilla::detail::nsTStringRepr`, not `nsTStringRepr`. The crash "
            "signature/frames usually contain the full qualification; copy it from "
            "there. Also layout the CONTAINING object (the struct whose field sits at "
            "the fault offset), not a template accessor from the crash frame."
        )
    except SearchfoxError as exc:
        raise _sf_error(exc, "field_layout") from exc
    return _fmt_field_layout(fl)


TOOLS = tools_in(__name__)
