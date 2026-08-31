# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Typed subprocess adapter for the external ``searchfox-cli`` binary.

This is the single seam every call-graph-using role (Call-graph Explorer,
Patch Scout, Data-flow Tracer, Skeptic) goes through to reach searchfox. It
builds the tool's **flag-style** commands, runs them with timeouts / bounded
retries / an in-process cache, and parses the LLM-oriented markdown into typed
Pydantic results that carry a citation (symbol-id + a searchfox link) for every
node, edge and definition.

It owns *only* subprocess management, parsing and error taxonomy -- nothing
about LLMs, prompts, model selection or dossier assembly.

CLI surface (verified against ``searchfox-cli --help``, 2026-07-02)
------------------------------------------------------------------
``searchfox-cli`` uses **flags, not positional subcommands**::

    searchfox-cli --calls-from 'ns::Cls::Method' --depth 2 -R mozilla-central

* ``--calls-from <S>`` / ``--calls-to <S>`` / ``--calls-between <A,B>``
  ``[--depth <N>]`` -- all three flags exist. ``--calls-between`` takes a single
  comma-joined pair and is **class/namespace-scoped** (not arbitrary
  function->function), per the CLI's own help.
* ``--define <S>`` -- full definition source.
* ``-q <Q>`` free-text/regex search (``-r`` regex, ``-l <N>`` limit).
* ``-R <repo>`` -- ``mozilla-central`` (default) / ``mozilla-beta`` /
  ``mozilla-release`` / ``mozilla-esr*`` / ``comm-central``. **No autoland.**

Permalinks / citations
----------------------
The link flags behave differently per command (verified empirically):

* ``--calls-from`` / ``--calls-to``: ``--link`` / ``--permalink`` have **no
  effect** -- the output only ever carries ``path#line`` + the mangled symbol
  id. We therefore synthesise a searchfox *source* URL
  (``https://searchfox.org/<tree>/source/<path>#<line>``) for each node. That is
  a tip source link, not a commit-pinned permalink (searchfox indexes ~tip; see
  below), which is consistent with ``queried_tip=True``.
* ``--define --permalink``: emits a bare **commit-hash** URL
  (``.../rev/<40-hex>/<path>#<start>-<end>``) but *replaces* the source body, so
  :meth:`SearchfoxClient.define` runs the command twice -- plain for the body,
  ``--permalink`` for the commit-pinned citation.
* ``-q``: emits ``path:line: text``; ``--link`` would replace the text with a
  URL, so we parse the plain form and synthesise the source URL ourselves.

Revision drift (PLAN section 7)
-------------------------------
searchfox indexes **~tip** of ``github.com/mozilla-firefox/firefox`` (post
hg->git migration) and ``searchfox-cli`` exposes **no per-revision flag**. This
adapter cannot pin a crash build node: it records ``queried_tip=True`` and an
advisory ``rev_label`` (never forwarded to the binary) on every result so
callers can do their own drift bookkeeping and route uncertain edges to the
abstain path.

Empty vs error
--------------
Every ``searchfox-cli`` invocation exits ``0`` -- even on a genuine miss (empty
call graph, "No direct calls found", "Total matches: 0", "No potential
definitions found"). Empty detection is therefore **parse-based, never
rc-based**. A valid-but-empty result raises :class:`SearchfoxNoResult` for the
single-object commands (``calls_*``, ``define``) and returns ``[]`` for the
list commands (``search``, ``lookup``) so callers can abstain rather than
fabricate an edge.
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import time
from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel

try:  # config is optional at import time; defaults below cover its absence
    from . import config
except Exception:  # pragma: no cover - defensive (allows standalone import)
    config = None

log = logging.getLogger("crashclouseau.searchfox")


# --- defaults (config/global.json "agent.searchfox" overrides these) --------

_DEFAULTS = {
    "bin": "searchfox-cli",
    "default_repo": "mozilla-central",
    "max_depth": 4,
    "timeout_secs": 60,
    "retries": 2,
    "retry_backoff_secs": 1.5,
    "cache_enabled": True,
}


def _settings():
    """Merge ``agent.searchfox`` config over the built-in defaults.

    Explicit ``null`` values in config are ignored so a present-but-null key
    falls back to the built-in default rather than clobbering it.
    """
    cfg = dict(_DEFAULTS)
    if config is not None:
        try:
            extra = config.get_searchfox() or {}
            cfg.update({k: v for k, v in extra.items() if v is not None})
        except Exception:  # pragma: no cover - missing/broken config file
            log.debug("could not read agent.searchfox config; using defaults")
    return cfg


# --- repositories -----------------------------------------------------------


class Repo(str, Enum):
    """The exact ``-R``/``--repo`` tokens ``searchfox-cli`` accepts.

    There is deliberately **no ``autoland``** member -- searchfox has no such
    tree. The ESR members are the variants indexed as of step 1 (2026-07-02).
    """

    CENTRAL = "mozilla-central"
    BETA = "mozilla-beta"
    RELEASE = "mozilla-release"
    ESR115 = "mozilla-esr115"
    ESR128 = "mozilla-esr128"
    ESR140 = "mozilla-esr140"
    COMM = "comm-central"

    @property
    def tree(self) -> str:
        """searchfox.org URL tree name for this repo.

        Verified mapping (search ``--link`` probe, 2026-07-02): ``mozilla-central
        -> firefox-main``; every other ``mozilla-<x>`` -> ``firefox-<x>``;
        ``comm-central`` is unchanged.
        """
        if self.value == "mozilla-central":
            return "firefox-main"
        if self.value.startswith("mozilla-"):
            return "firefox-" + self.value[len("mozilla-"):]
        return self.value


# The ONE channel -> searchfox repo map. `html._searchfox_tree` renders its tree names from
# this via `Repo.tree`, and `agent/tools/searchfox_cg.SearchfoxCtx` resolves its per-run default
# through `repo_for_channel`, so the UI's links and the agent's reads cannot disagree about
# which tree a crash belongs to. An unknown channel falls back to central rather than raising:
# a wrong-but-indexed tree degrades an answer, an exception loses the whole run.
_CHANNEL_REPO = {
    "nightly": Repo.CENTRAL,
    "beta": Repo.BETA,
    # Socorro files Developer Edition under `aurora`; it is built from mozilla-beta.
    "aurora": Repo.BETA,
    "release": Repo.RELEASE,
}


def repo_for_channel(channel) -> Repo:
    """The searchfox repo a crash on ``channel`` should be read in.

    WHY THIS EXISTS: every ``SearchfoxClient`` method takes ``repo=None`` and ``_coerce_repo``
    then falls through to ``agent.searchfox.default_repo`` = ``mozilla-central``. For a beta
    crash that means every ``calls_from`` / ``calls_to`` / ``define`` / ``search`` /
    ``field_layout`` read, and every permalink cited in the filed bug, came from firefox-main
    tip -- code that may never have existed in the beta build, while the build's own uplifts are
    absent from it. Two deterministic gates read the same tree (``_resolve_struct_layout``,
    which is fail-closed, and ``compiled_out``), so the cost is not only a wrong citation.

    ``firefox-beta`` is indexed at its own branch tip (measured 2026-08-11: ``cd001e124b15`` /
    154.0b9 while ``firefox-main`` was at 155.0a1), which for a BETA crash is the correct tree
    -- unlike the Fenix-nightly case in plan #16, where the same tree was a cycle behind."""
    return _CHANNEL_REPO.get((channel or "").lower(), Repo.CENTRAL)


def _coerce_repo(repo) -> Repo:
    if repo is None:
        repo = _settings().get("default_repo", "mozilla-central")
    if isinstance(repo, Repo):
        return repo
    try:
        return Repo(repo)
    except ValueError:
        raise SearchfoxInvocationError(
            "unknown repo {!r}; valid: {}".format(
                repo, ", ".join(r.value for r in Repo)
            ),
            cmd=[],
            stderr="",
        )


# --- result models ----------------------------------------------------------


class SymbolRef(BaseModel):
    """A single symbol occurrence with everything needed to cite it."""

    symbol_id: Optional[str] = None  # mangled id (_Z.../ _R...); None for search
    pretty: str  # demangled/display name, e.g. mozilla::dom::GainNode::Create
    file: Optional[str] = None
    line: Optional[int] = None
    permalink: Optional[str] = None
    repo: str
    rev: Optional[str] = None  # advisory label only; never forwarded to the CLI


class CallEdge(BaseModel):
    """A directed call relationship ``caller -> callee``."""

    caller: SymbolRef
    callee: SymbolRef
    depth: int
    permalink: Optional[str] = None  # citation for the discovered end of the edge


class CallGraph(BaseModel):
    """The result of a ``--calls-from`` / ``--calls-to`` / ``--calls-between``."""

    root: SymbolRef
    direction: Literal["from", "to", "between"]
    depth: int
    edges: List[CallEdge] = []
    repo: str
    queried_tip: bool = True  # always True: searchfox-cli has no per-rev flag
    rev_label: Optional[str] = None
    raw_markdown: str = ""


class Definition(BaseModel):
    """The full source body of a symbol's definition."""

    symbol: SymbolRef
    source: str  # the function body, line-number prefixes stripped
    permalink: Optional[str] = None  # commit-pinned (from --define --permalink)
    start_line: Optional[int] = None
    end_line: Optional[int] = None


class SearchHit(BaseModel):
    """A single ``-q`` search match."""

    symbol: Optional[SymbolRef] = None  # searchfox search emits no mangled id
    file: str
    line: int
    text: str
    permalink: Optional[str] = None


class FieldEntry(BaseModel):
    """One field of a C++ class/struct layout: its byte ``offset``, ``size``,
    C++ ``type`` and member ``name`` (from ``--field-layout``)."""

    offset: int
    size: Optional[int] = None
    type: str = ""
    name: str = ""


class FieldLayout(BaseModel):
    """The memory layout of a C++ class/struct (``searchfox-cli --field-layout``).

    This is the deterministic evidence that turns a null/small-address fault into a
    verifiable claim: e.g. a fault at ``0x8`` on an ``nsTStringRepr`` corroborates a
    null-deref of ``mLength`` (offset 8). ``field_at(n)`` returns the member sitting
    at byte offset ``n`` (or ``None``). There is no per-symbol permalink from the CLI,
    so the class name + field + offset ARE the citation.
    """

    class_name: str
    size: Optional[int] = None
    align: Optional[int] = None
    fields: List[FieldEntry] = []
    repo: str
    queried_tip: bool = True
    rev_label: Optional[str] = None
    raw_markdown: str = ""

    def field_at(self, offset: int) -> Optional[FieldEntry]:
        """The field exactly at byte ``offset`` (searchfox reports each field's
        start offset), or ``None``. Prefers an exact start-offset match; falls back
        to the field whose [offset, offset+size) range contains ``offset``."""
        for f in self.fields:
            if f.offset == offset:
                return f
        for f in self.fields:
            if f.size and f.offset <= offset < f.offset + f.size:
                return f
        return None


# --- error taxonomy ---------------------------------------------------------


class SearchfoxError(Exception):
    """Base class for every failure this adapter raises."""


class SearchfoxNotFound(SearchfoxError):
    """The ``searchfox-cli`` binary could not be resolved."""


class SearchfoxTimeout(SearchfoxError):
    """The invocation timed out (after the configured retries)."""


class SearchfoxInvocationError(SearchfoxError):
    """The binary exited non-zero. Carries the command and stderr."""

    def __init__(self, message, cmd=None, stderr=None, returncode=None):
        super().__init__(message)
        self.cmd = cmd or []
        self.stderr = stderr or ""
        self.returncode = returncode


class SearchfoxParseError(SearchfoxError):
    """The output could not be parsed. Carries the raw markdown."""

    def __init__(self, message, raw=""):
        super().__init__(message)
        self.raw = raw


class SearchfoxNoResult(SearchfoxError):
    """A valid run that produced an empty result (the abstain path).

    Distinct from an error: the query ran fine, there simply is no such edge /
    definition. Callers should abstain rather than fabricate one.
    """


# --- transient-failure detection (ported from the spike) --------------------

# searchfox's backend intermittently returns 5xx / drops the connection; those
# surface as rc!=0 with an HTTP-ish stderr and must be retried or they become
# false misses. A genuine empty result exits 0 and is never retried.
_TRANSIENT_RE = re.compile(
    r"50[0234]|Bad Gateway|Internal Server Error|Service Unavailable|"
    r"Gateway Time-?out|Request failed|Connection|timed? ?out|"
    r"reset by peer|EOF|broken pipe",
    re.IGNORECASE,
)


def _is_transient(err: Optional[str]) -> bool:
    return bool(err and _TRANSIENT_RE.search(err))


# --- symbol normalisation (ported from the spike) ---------------------------

_ARGS_RE = re.compile(r"\(.*$", re.DOTALL)
_TMPL_RE = re.compile(r"<[^<>]*>")


def _clean_symbol(symbol: str) -> str:
    """Strip a signature/param list and template args from a symbol.

    Socorro frame functions look like ``NS_ProcessNextEvent(nsIThread*, bool)``
    or ``mozilla::Maybe<T>::ref``; searchfox resolves the bare qualified name,
    so the parens/templates must go or every query returns nothing.
    """
    s = _ARGS_RE.sub("", symbol)
    prev = None
    while prev != s:
        prev = s
        s = _TMPL_RE.sub("", s)
    return s.strip()


def _reduce_symbol(symbol: str) -> str:
    """The trailing ``Type::method`` of a symbol.

    searchfox resolves Rust call-graph queries on the trailing ``Type::method``
    (e.g. ``Renderer::render``), not the full ``crate::module::Type::method``
    path that Socorro frames carry; C++ resolves on the full name. Used as a
    fallback when the full name returns nothing.
    """
    parts = symbol.split("::")
    return "::".join(parts[-2:]) if len(parts) > 2 else symbol


_PLAIN_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
# `--id` prints `path:line: <the source line>`; `--function-at` answers `in _ZN...`.
_ID_HIT_RE = re.compile(r"^(?P<path>[^\s:]+):(?P<line>\d+):\s*(?P<code>.*)$")
_FUNCTION_AT_MANGLED_RE = re.compile(r"^in\s+(?P<mangled>_Z\S+)\s*$")


def _demangle_nested(mangled: str) -> Optional[str]:
    """The nested-name prefix of an Itanium-mangled id, as ``a::b::c``.

    THE MANGLED ID IS THE ONLY PLACE SEARCHFOX HANDS BACK THE ENCLOSING NAMESPACE.
    ``--id refillFreeListAndAllocate`` prints the definition line exactly as the source
    writes it -- ``void ArenaLists::refillFreeListAndAllocate(`` -- which is the same
    under-qualified spelling that returned an empty graph in the first place. Only
    ``--function-at js/src/gc/Allocator.cpp:368`` -> ``_ZN2js2gc10ArenaLists25refill...``
    carries the ``js::gc``.

    Reads just the leading length-prefixed component run and stops at the first thing that
    is not ``<len><chars>`` (the closing ``E``, a substitution, a template ``I``...), so a
    parameter list can never leak into the result.
    """
    if not mangled.startswith("_ZN"):
        return None
    i, parts = 3, []
    while i < len(mangled):
        j = i
        while j < len(mangled) and mangled[j].isdigit():
            j += 1
        if j == i:
            break
        n = int(mangled[i:j])
        i = j
        if n <= 0 or i + n > len(mangled):
            return None
        end = i + n
        parts.append(mangled[i:end])
        i = end
    return "::".join(parts) if len(parts) >= 2 else None


# --- markdown parsers (pure functions, unit-testable without the binary) -----

_CG_HEADER_RE = re.compile(
    r"^#\s*calls-(?P<direction>from|to):'(?P<sym>.*?)'\s+depth:(?P<depth>\d+)",
    re.MULTILINE,
)
_CB_HEADER_RE = re.compile(
    r"^#\s*calls-between-source:'(?P<src>.*?)'\s+"
    r"calls-between-target:'(?P<dst>.*?)'\s+depth:(?P<depth>\d+)",
    re.MULTILINE,
)

# ``- <pretty> (`<mangled>`, <path>#<line> [(decl: <path>#<line>)])``
_BULLET_RE = re.compile(
    r"^-\s+(?P<pretty>.+?)\s+\(`(?P<mangled>[^`]+)`,\s+"
    r"(?P<file>[^\s#()]+)#(?P<line>\d+)"
    r"(?:\s+\(decl:\s+(?P<declfile>[^\s#()]+)#(?P<declline>\d+)\))?\)\s*$"
)

# An overloaded node is emitted as a header line plus one indented sub-line per
# overload (each a distinct mangled id sharing the same pretty name):
#   - <pretty> (N overloads)
#     - `<mangled>`, <path>#<line> (decl: <path>#<line>)
_OVERLOAD_HDR_RE = re.compile(r"^-\s+(?P<pretty>.+?)\s+\(\d+\s+overloads?\)\s*$")
_OVERLOAD_SUB_RE = re.compile(
    r"^\s+-\s+`(?P<mangled>[^`]+)`,\s+(?P<file>[^\s#()]+)#(?P<line>\d+)"
    r"(?:\s+\(decl:\s+[^\s#()]+#\d+\))?\s*$"
)

# calls-between 3-line block:
#   - **<caller>** (<path>#<line>) calls **<callee>** (<path>#<line>)
#     - From: `<caller mangled>`
#     - To: `<callee mangled>`
_CB_EDGE_RE = re.compile(
    r"^-\s+\*\*(?P<caller>.+?)\*\*\s+\((?P<cfile>[^\s#()]+)#(?P<cline>\d+)\)\s+"
    r"calls\s+\*\*(?P<callee>.+?)\*\*\s+\((?P<efile>[^\s#()]+)#(?P<eline>\d+)\)"
    r"\s*\n\s*-\s*From:\s*`(?P<cmangled>[^`]+)`"
    r"\s*\n\s*-\s*To:\s*`(?P<emangled>[^`]+)`",
    re.MULTILINE,
)

# ``path:line: text`` search hits.
_SEARCH_HIT_RE = re.compile(r"^(?P<file>[^\s:]+):(?P<line>\d+):\s?(?P<text>.*)$")
_TOTAL_MATCHES_RE = re.compile(r"^Total matches:\s*(?P<n>\d+)\s*$", re.MULTILINE)

# ``--define`` body lines: ``>>>  119: code`` (def start) / ``     120: code``.
_DEFINE_LINE_RE = re.compile(r"^\s*(?:>>>)?\s*(?P<line>\d+):\s?(?P<code>.*)$")

# commit-pinned permalink from ``--define --permalink``.
_PERMALINK_RE = re.compile(
    r"https://searchfox\.org/(?P<tree>[^/]+)/rev/(?P<rev>[0-9a-fA-F]+)/"
    r"(?P<path>[^#\s]+)#(?P<start>\d+)(?:-(?P<end>\d+))?"
)


def _source_url(repo: Repo, path: str, line) -> str:
    """Synthesise a searchfox *source* (tip) URL for ``path#line``."""
    frag = "#{}".format(line) if line is not None else ""
    return "https://searchfox.org/{}/source/{}{}".format(repo.tree, path, frag)


def _extract_permalink(text: str):
    """Parse a commit-pinned ``--permalink`` URL into its parts (or ``None``)."""
    m = _PERMALINK_RE.search(text)
    if not m:
        return None
    return {
        "url": m.group(0),
        "tree": m.group("tree"),
        "rev": m.group("rev"),
        "path": m.group("path"),
        "start": int(m.group("start")),
        "end": int(m.group("end")) if m.group("end") else int(m.group("start")),
    }


def _parse_symbol_header(md: str):
    """Return ``(direction, symbol, depth)`` for a calls-from/to echo header."""
    m = _CG_HEADER_RE.search(md)
    if not m:
        return None
    return m.group("direction"), m.group("sym"), int(m.group("depth"))


def _iter_call_nodes(md):
    """Yield ``(pretty, mangled, file, line)`` for every node in calls-from/to.

    Handles both the inline single-definition bullet and the multi-line
    ``(N overloads)`` block (header + one indented sub-line per overload, each a
    distinct mangled id sharing the header's pretty name).
    """
    lines = md.splitlines()
    i, n = 0, len(lines)
    while i < n:
        m = _BULLET_RE.match(lines[i])
        if m:
            yield (
                m.group("pretty").strip(),
                m.group("mangled"),
                m.group("file"),
                int(m.group("line")),
            )
            i += 1
            continue
        h = _OVERLOAD_HDR_RE.match(lines[i])
        if h:
            pretty = h.group("pretty").strip()
            i += 1
            while i < n:
                s = _OVERLOAD_SUB_RE.match(lines[i])
                if not s:
                    break
                yield (pretty, s.group("mangled"), s.group("file"), int(s.group("line")))
                i += 1
            continue
        i += 1


def _parse_call_graph(md, repo: Repo, rev_label=None) -> CallGraph:
    """Parse ``--calls-from`` / ``--calls-to`` markdown into a CallGraph.

    The output is a flat list of nodes grouped by class -- it does **not** carry
    a per-node BFS level, so every edge is stamped with the *query* depth as an
    upper bound (documented limitation). The queried root is echoed in its own
    output and excluded from the edges. Overloaded nodes (``(N overloads)``)
    expand to one edge per overload (distinct mangled ids, shared pretty name).
    """
    header = _parse_symbol_header(md)
    if header is None:
        raise SearchfoxParseError(
            "no calls-from/to header in searchfox-cli output", raw=md
        )
    direction, root_sym, depth = header

    root = SymbolRef(pretty=root_sym, repo=repo.value, rev=rev_label)
    edges: List[CallEdge] = []
    for pretty, mangled, file, line in _iter_call_nodes(md):
        node = SymbolRef(
            symbol_id=mangled,
            pretty=pretty,
            file=file,
            line=line,
            permalink=_source_url(repo, file, line),
            repo=repo.value,
            rev=rev_label,
        )
        if pretty == root_sym:
            # The query symbol is echoed in its own output; enrich the root with
            # its citation but don't emit a self-edge.
            root = node
            continue
        if direction == "from":
            caller, callee = root, node
        else:  # "to": the listed functions call the root
            caller, callee = node, root
        edges.append(
            CallEdge(
                caller=caller, callee=callee, depth=depth, permalink=node.permalink
            )
        )

    return CallGraph(
        root=root,
        direction=direction,
        depth=depth,
        edges=edges,
        repo=repo.value,
        queried_tip=True,
        rev_label=rev_label,
        raw_markdown=md,
    )


def _parse_calls_between(md, repo: Repo, rev_label=None) -> CallGraph:
    """Parse ``--calls-between`` markdown into a CallGraph of direct edges."""
    header = _CB_HEADER_RE.search(md)
    if header is None:
        raise SearchfoxParseError(
            "no calls-between header in searchfox-cli output", raw=md
        )
    src = header.group("src")
    depth = int(header.group("depth"))

    edges: List[CallEdge] = []
    for m in _CB_EDGE_RE.finditer(md):
        cfile, cline = m.group("cfile"), int(m.group("cline"))
        efile, eline = m.group("efile"), int(m.group("eline"))
        caller = SymbolRef(
            symbol_id=m.group("cmangled"),
            pretty=m.group("caller").strip(),
            file=cfile,
            line=cline,
            permalink=_source_url(repo, cfile, cline),
            repo=repo.value,
            rev=rev_label,
        )
        callee = SymbolRef(
            symbol_id=m.group("emangled"),
            pretty=m.group("callee").strip(),
            file=efile,
            line=eline,
            permalink=_source_url(repo, efile, eline),
            repo=repo.value,
            rev=rev_label,
        )
        # Each listed pair is a *direct* call in the path.
        edges.append(
            CallEdge(caller=caller, callee=callee, depth=1, permalink=callee.permalink)
        )

    root = SymbolRef(pretty=src, repo=repo.value, rev=rev_label)
    return CallGraph(
        root=root,
        direction="between",
        depth=depth,
        edges=edges,
        repo=repo.value,
        queried_tip=True,
        rev_label=rev_label,
        raw_markdown=md,
    )


def _parse_definition(md, repo: Repo, rev_label=None, permalink_md=None) -> Definition:
    """Parse ``--define`` body markdown (plus an optional ``--permalink`` blob).

    ``md`` is the plain (numbered) body; ``permalink_md`` is the output of the
    ``--define --permalink`` invocation, used for the commit-pinned citation.

    An overloaded symbol yields several ``>>>``-marked blocks; since ``define()``
    strips the parameter list it cannot disambiguate them, so this returns the
    **first** overload only (a single coherent body) rather than concatenating
    distinct functions. Callers needing a specific overload should use the
    ``symbol_id`` from a ``calls_*`` result.
    """
    # Split on the ``>>>`` def-start markers so overloaded definitions don't get
    # glued into one Frankenstein body; keep only the first block.
    blocks: List[List] = []
    current = None
    for raw in md.splitlines():
        m = _DEFINE_LINE_RE.match(raw)
        if not m:
            continue
        if raw.lstrip().startswith(">>>") or current is None:
            current = []
            blocks.append(current)
        current.append((int(m.group("line")), m.group("code")))

    if not blocks or not blocks[0]:
        raise SearchfoxParseError(
            "no numbered source lines in --define output", raw=md
        )

    first = blocks[0]
    start_line = first[0][0]
    end_line = first[-1][0]
    source = "\n".join(code for _, code in first)

    permalink = None
    file = None
    rev = rev_label
    if permalink_md:
        # --define --permalink may emit several ranges (e.g. decl + def); prefer
        # the one whose start matches the body, else the first.
        chosen = None
        for raw in permalink_md.splitlines():
            info = _extract_permalink(raw)
            if info is None:
                continue
            if chosen is None:
                chosen = info
            if info["start"] == start_line:
                chosen = info
                break
        if chosen is not None:
            permalink = chosen["url"]
            file = chosen["path"]
            rev = chosen["rev"]

    symbol = SymbolRef(
        pretty="",  # filled in by the caller (it knows the queried name)
        file=file,
        line=start_line,
        permalink=permalink,
        repo=repo.value,
        rev=rev,
    )
    return Definition(
        symbol=symbol,
        source=source,
        permalink=permalink,
        start_line=start_line,
        end_line=end_line,
    )


def _parse_search(md, repo: Repo, rev_label=None) -> List[SearchHit]:
    """Parse ``-q`` search output into ``SearchHit``s (may be empty)."""
    has_total = _TOTAL_MATCHES_RE.search(md) is not None
    hits: List[SearchHit] = []
    for raw in md.splitlines():
        if raw.startswith("Total matches:"):
            continue
        m = _SEARCH_HIT_RE.match(raw)
        if not m:
            continue
        file = m.group("file")
        line = int(m.group("line"))
        hits.append(
            SearchHit(
                file=file,
                line=line,
                text=m.group("text"),
                permalink=_source_url(repo, file, line),
            )
        )

    if not hits and not has_total:
        raise SearchfoxParseError("unrecognised -q search output", raw=md)
    return hits


# ``--field-layout`` renders an ANSI-coloured box-drawing table; strip the colour
# codes, read ``Size: N bytes, Alignment: M bytes``, then the ``│``-delimited rows.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_FL_SIZE_RE = re.compile(
    r"Size:\s*(?P<size>\d+)\s*bytes?,\s*Alignment:\s*(?P<align>\d+)\s*bytes?",
    re.IGNORECASE,
)
_FL_NAME_RE = re.compile(r"^Field Layout:\s*(?P<name>.+?)\s*$", re.MULTILINE)


def _parse_field_layout(md, repo: Repo, symbol, rev_label=None) -> FieldLayout:
    """Parse ``--field-layout`` markdown into a FieldLayout.

    The CLI emits ``No field layout information found.`` (exit 0) for templates,
    unknown types and non-class symbols -> :class:`SearchfoxNoResult` (abstain).
    """
    text = _ANSI_RE.sub("", md)
    if "No field layout information found" in text or "Field Layout" not in text:
        raise SearchfoxNoResult(
            "no field layout for {!r}".format(symbol)
        )
    nm = _FL_NAME_RE.search(text)
    class_name = nm.group("name") if nm else _clean_symbol(symbol)
    sz = _FL_SIZE_RE.search(text)
    size = int(sz.group("size")) if sz else None
    align = int(sz.group("align")) if sz else None

    fields: List[FieldEntry] = []
    for raw in text.splitlines():
        if "│" not in raw:  # the box-drawing column separator │
            continue
        cells = [c.strip() for c in raw.split("│")]
        if len(cells) < 3:
            continue
        cells = cells[1:-1]  # drop the outer border empties
        if len(cells) != 4:
            continue
        off, fsize, ftype, fname = cells
        if not off.isdigit():  # skips the header row (offset|size|type|name)
            continue
        fields.append(
            FieldEntry(
                offset=int(off),
                size=int(fsize) if fsize.isdigit() else None,
                type=ftype,
                name=fname,
            )
        )
    if not fields:
        raise SearchfoxParseError(
            "no field rows in --field-layout output", raw=md
        )
    return FieldLayout(
        class_name=class_name,
        size=size,
        align=align,
        fields=fields,
        repo=repo.value,
        queried_tip=True,
        rev_label=rev_label,
        raw_markdown=md,
    )


# --- client -----------------------------------------------------------------


class SearchfoxClient:
    """Thin, typed wrapper around the ``searchfox-cli`` binary."""

    def __init__(
        self,
        bin=None,
        default_repo=None,
        timeout=None,
        retries=None,
        backoff=None,
        cache=True,
    ):
        cfg = _settings()
        bin_name = bin or os.getenv("SEARCHFOX_CLI") or cfg.get("bin") or "searchfox-cli"
        resolved = shutil.which(bin_name)
        if resolved is None:
            raise SearchfoxNotFound(
                "{!r} not found (install: cargo install searchfox-cli; or set "
                "$SEARCHFOX_CLI / agent.searchfox.bin)".format(bin_name)
            )
        self.bin = resolved
        self.default_repo = _coerce_repo(default_repo or cfg.get("default_repo"))
        self.max_depth = int(cfg.get("max_depth", _DEFAULTS["max_depth"]))
        self.timeout = int(timeout if timeout is not None else cfg.get("timeout_secs"))
        self.retries = int(retries if retries is not None else cfg.get("retries"))
        self.backoff = float(
            backoff if backoff is not None else cfg.get("retry_backoff_secs")
        )
        self.cache_enabled = bool(
            cache and cfg.get("cache_enabled", True)
        )
        self._cache = {}

    # -- internals ----------------------------------------------------------

    def _run(self, args: List[str], repo: Repo) -> str:
        """Build flag-form argv, invoke the binary, return stdout markdown.

        Retries ``TimeoutExpired`` and transient non-zero exits with
        exponential backoff; raises the typed error taxonomy otherwise. Caches
        successful stdout on ``(repo, args)`` for the life of the client.
        """
        full = list(args) + ["-R", repo.value]
        key = (repo.value, tuple(full))
        if self.cache_enabled and key in self._cache:
            return self._cache[key]

        cmd = [self.bin] + full
        last_err = None
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(self.backoff * (2 ** (attempt - 1)))
            started = time.monotonic()
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=self.timeout
                )
            except subprocess.TimeoutExpired:
                last_err = "timeout after {}s".format(self.timeout)
                log.warning(
                    "searchfox-cli timeout (attempt %d/%d): %s",
                    attempt + 1,
                    self.retries + 1,
                    " ".join(full),
                )
                continue
            except FileNotFoundError as e:  # binary vanished after resolution
                raise SearchfoxNotFound(str(e))
            except OSError as e:  # pragma: no cover - defensive
                raise SearchfoxInvocationError(str(e), cmd=cmd)

            elapsed = time.monotonic() - started
            if proc.returncode != 0:
                err = proc.stderr.strip() or "exit {}".format(proc.returncode)
                if _is_transient(err) and attempt < self.retries:
                    last_err = err
                    log.warning(
                        "searchfox-cli transient rc=%s (attempt %d/%d): %s",
                        proc.returncode,
                        attempt + 1,
                        self.retries + 1,
                        err,
                    )
                    continue
                raise SearchfoxInvocationError(
                    "searchfox-cli exited {}".format(proc.returncode),
                    cmd=cmd,
                    stderr=err,
                    returncode=proc.returncode,
                )

            log.debug("searchfox-cli ok in %.2fs: %s", elapsed, " ".join(full))
            md = proc.stdout
            if self.cache_enabled:
                self._cache[key] = md
            return md

        # Exhausted retries on a transient failure / timeout.
        if last_err and "timeout" in last_err:
            raise SearchfoxTimeout(
                "searchfox-cli timed out after {} attempts".format(self.retries + 1)
            )
        raise SearchfoxInvocationError(
            "searchfox-cli failed after {} attempts".format(self.retries + 1),
            cmd=cmd,
            stderr=last_err or "",
        )

    def _clamp_depth(self, depth: int) -> int:
        if depth < 1:
            return 1
        if depth > self.max_depth:
            log.debug("clamping depth %d to max_depth %d", depth, self.max_depth)
            return self.max_depth
        return depth

    def _resolve_repo(self, repo) -> Repo:
        """Per-call repo, honouring the client's constructor default when None."""
        if repo is None:
            return self.default_repo
        return _coerce_repo(repo)

    #: `--id` can name the same function at many sites; read at most this many.
    _MAX_QUALIFY_SITES = 6

    def _qualify_candidates(self, sym, repo) -> List[str]:
        """Strictly MORE qualified spellings of ``sym``, via ``--id`` then ``--function-at``.

        The retry below this one only ever STRIPS qualification, which is right for a Rust
        crate path and a no-op for the 2-part C++ names that are most of what we ask about:
        ``len(parts) > 2`` is False, so ``_reduce_symbol`` returns the symbol unchanged and no
        second invocation happens at all. The failure it cannot touch is the opposite one --
        ``ArenaLists::refillFreeListAndAllocate`` returns an empty graph where
        ``js::gc::ArenaLists::refillFreeListAndAllocate`` returns two callers.

        THE SAFETY PROPERTY IS THE SUFFIX TEST, and it is the whole reason this is not
        guesswork. ``--id`` matches on the trailing identifier alone, so it happily offers
        ``webrtc::videocapturemodule::DeviceInfoV4l2::HandleEvent`` for
        ``EventTargetChainItem::HandleEvent``. Answering "who calls X" with the callers of a
        different X is worse than answering nothing, so a candidate is accepted only when it
        ENDS WITH ``::`` + the requested spelling -- i.e. it adds namespaces and changes
        nothing else. Measured over the 123 distinct under-qualified symbols that came back
        empty in prod (``payload['tool_calls']``, 6,573 calls): 40 recovered (32.5%), and the
        suffix test rejected 60 wrong-function candidates.
        """
        last = sym.rsplit("::", 1)[-1]
        if not _PLAIN_IDENT_RE.fullmatch(last):
            return []
        try:
            id_md = self._run(["--id", last], repo)
        except SearchfoxError:  # best-effort: a failed retry must not mask the empty graph
            return []
        # `--id` returns declarations, definitions AND call sites. A definition line spells
        # the class (`void ArenaLists::refill...(`), so it is the one most likely to sit
        # inside the namespace we are missing; try those first and only then fall back to
        # the rest, or a hot method burns the whole probe budget on its own callers.
        defs, others, seen = [], [], set()
        qualified_re = re.compile(r"::" + re.escape(last) + r"\s*\(")
        call_re = re.compile(r"\b" + re.escape(last) + r"\s*\(")
        for line in id_md.splitlines():
            m = _ID_HIT_RE.match(line)
            if not m or not call_re.search(m.group("code")):
                continue
            site = "{}:{}".format(m.group("path"), m.group("line"))
            if site in seen:
                continue
            seen.add(site)
            (defs if qualified_re.search(m.group("code")) else others).append(site)
        sites = defs + others
        out = []
        for site in sites[: self._MAX_QUALIFY_SITES]:
            try:
                at_md = self._run(["--function-at", site], repo)
            except SearchfoxError:
                continue
            for line in at_md.splitlines():
                m = _FUNCTION_AT_MANGLED_RE.match(line.strip())
                if not m:
                    continue
                full = _demangle_nested(m.group("mangled"))
                if full and full.endswith("::" + sym) and full not in out:
                    out.append(full)
                break
            if out:
                # Measured on the 40 prod symbols this recovers: the first suffix-safe
                # candidate was the winning spelling in 40 of 40, so probing further sites
                # only spends round-trips.
                break
        return out

    def _calls(self, flag, direction, symbol, repo, depth, rev_label) -> CallGraph:
        repo = self._resolve_repo(repo)
        depth = self._clamp_depth(depth)
        sym = _clean_symbol(symbol)
        md = self._run([flag, sym, "--depth", str(depth)], repo)
        graph = _parse_call_graph(md, repo, rev_label)
        if not graph.edges:
            # Rust: the full crate path doesn't resolve -- retry on the trailing
            # Type::method before giving up (ported from the spike).
            reduced = _reduce_symbol(sym)
            if reduced != sym:
                md2 = self._run([flag, reduced, "--depth", str(depth)], repo)
                graph2 = _parse_call_graph(md2, repo, rev_label)
                if graph2.edges:
                    return graph2
        if not graph.edges:
            # C++: the mirror failure, and the common one -- a missing NAMESPACE. Costs
            # nothing on the happy path because we are already empty here.
            for full in self._qualify_candidates(sym, repo):
                md3 = self._run([flag, full, "--depth", str(depth)], repo)
                graph3 = _parse_call_graph(md3, repo, rev_label)
                if graph3.edges:
                    log.debug("re-qualified %r -> %r", sym, full)
                    return graph3
        if not graph.edges:
            raise SearchfoxNoResult(
                "no calls-{} edges for {!r}".format(direction, symbol)
            )
        return graph

    # -- public API ---------------------------------------------------------

    def calls_from(self, symbol, repo=None, depth=1, rev_label=None) -> CallGraph:
        """Functions **called by** ``symbol`` (out to ``depth``)."""
        return self._calls("--calls-from", "from", symbol, repo, depth, rev_label)

    def calls_to(self, symbol, repo=None, depth=1, rev_label=None) -> CallGraph:
        """Functions that **call** ``symbol`` (out to ``depth``)."""
        return self._calls("--calls-to", "to", symbol, repo, depth, rev_label)

    def calls_between(self, src, dst, repo=None, depth=2, rev_label=None) -> CallGraph:
        """Direct call edges on paths between ``src`` and ``dst``.

        Note: ``--calls-between`` is **class/namespace-scoped** per the CLI, so
        it may return nothing for plain function pairs -- prefer
        :meth:`calls_from` / :meth:`calls_to` at a raised depth for
        function-level reach. Raises :class:`SearchfoxNoResult` when no path
        exists (never fabricates an edge).
        """
        repo = self._resolve_repo(repo)
        depth = self._clamp_depth(depth)
        a = _clean_symbol(src)
        b = _clean_symbol(dst)
        md = self._run(
            ["--calls-between", "{},{}".format(a, b), "--depth", str(depth)], repo
        )
        graph = _parse_calls_between(md, repo, rev_label)
        if not graph.edges:
            raise SearchfoxNoResult(
                "no path between {!r} and {!r}".format(src, dst)
            )
        return graph

    def define(self, symbol, repo=None, rev_label=None) -> Definition:
        """The full source body of ``symbol``'s definition (with a permalink)."""
        repo = self._resolve_repo(repo)
        sym = _clean_symbol(symbol)
        md = self._run(["--define", sym], repo)
        if not md.strip():
            raise SearchfoxNoResult("no definition found for {!r}".format(symbol))
        try:
            plink_md = self._run(["--define", sym, "--permalink"], repo)
        except SearchfoxError:  # citation is best-effort; body is what matters
            plink_md = None
        definition = _parse_definition(md, repo, rev_label, plink_md)
        definition.symbol.pretty = sym
        return definition

    def lookup(self, name_or_symbol, repo=None, limit=50, rev_label=None):
        """Resolve a name to location refs (built on ``-q``).

        searchfox search emits no mangled ids, so the returned ``SymbolRef``s
        carry ``symbol_id=None`` -- use the ``symbol_id`` on ``calls_*`` results
        when a mangled id is required. Returns ``[]`` when nothing matches.
        """
        hits = self.search(name_or_symbol, repo=repo, limit=limit, rev_label=rev_label)
        refs = []
        for h in hits:
            refs.append(
                SymbolRef(
                    pretty=name_or_symbol,
                    file=h.file,
                    line=h.line,
                    permalink=h.permalink,
                    repo=self._resolve_repo(repo).value,
                    rev=rev_label,
                )
            )
        return refs

    def search(
        self, query, regex=False, repo=None, limit=50, rev_label=None
    ) -> List[SearchHit]:
        """Free-text (or ``regex=True``) search. Returns ``[]`` when empty."""
        repo = self._resolve_repo(repo)
        args = ["-q", query, "-l", str(limit)]
        if regex:
            args.append("-r")
        md = self._run(args, repo)
        return _parse_search(md, repo, rev_label)

    def field_layout(self, class_name, repo=None, rev_label=None) -> FieldLayout:
        """The byte-level memory layout of a C++ class/struct.

        Use to corroborate a null/small-address fault: a fault at offset ``N`` on
        type ``T`` is a null-deref of whichever field ``T`` places at offset ``N``.
        ``--field-layout`` needs the **bare** class name (template args make it
        return nothing), so the symbol is cleaned first. Raises
        :class:`SearchfoxNoResult` for templates/unknown/non-class symbols.
        """
        repo = self._resolve_repo(repo)
        sym = _clean_symbol(class_name)
        md = self._run(["--field-layout", sym], repo)
        return _parse_field_layout(md, repo, sym, rev_label)

    def clear_cache(self):
        """Drop the in-process result cache."""
        self._cache.clear()


# --- standalone CLI (Phase-0 spike + manual debugging) ----------------------


def _dump(obj):
    if isinstance(obj, list):
        return json.dumps([o.model_dump() for o in obj], indent=2)
    return obj.model_dump_json(indent=2)


def _build_argparser():
    # Common options live on a parent parser so they can appear *after* the verb
    # (e.g. ``calls-from <sym> --repo mozilla-central --depth 2``).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-R", "--repo", default=None, help="searchfox repo (default: config)"
    )
    common.add_argument("--depth", type=int, default=1)
    common.add_argument("--limit", type=int, default=50)
    common.add_argument("--regex", action="store_true", help="regex search (-q -r)")

    p = argparse.ArgumentParser(
        prog="python -m crashclouseau.searchfox",
        description="Typed wrapper around searchfox-cli (prints JSON).",
    )
    sub = p.add_subparsers(dest="verb", required=True)
    for verb in ("calls-from", "calls-to", "define", "lookup", "search", "field-layout"):
        sp = sub.add_parser(verb, parents=[common])
        sp.add_argument("symbol")
    cb = sub.add_parser("calls-between", parents=[common])
    cb.add_argument("source")
    cb.add_argument("target")
    return p


def main(argv=None):
    logging.basicConfig(level=logging.INFO)
    args = _build_argparser().parse_args(argv)
    try:
        client = SearchfoxClient()
        if args.verb == "calls-from":
            res = client.calls_from(args.symbol, repo=args.repo, depth=args.depth)
        elif args.verb == "calls-to":
            res = client.calls_to(args.symbol, repo=args.repo, depth=args.depth)
        elif args.verb == "calls-between":
            res = client.calls_between(
                args.source, args.target, repo=args.repo, depth=args.depth
            )
        elif args.verb == "define":
            res = client.define(args.symbol, repo=args.repo)
        elif args.verb == "field-layout":
            res = client.field_layout(args.symbol, repo=args.repo)
        elif args.verb == "lookup":
            res = client.lookup(args.symbol, repo=args.repo, limit=args.limit)
        else:  # search
            res = client.search(
                args.symbol, regex=args.regex, repo=args.repo, limit=args.limit
            )
    except SearchfoxNoResult as e:
        print(json.dumps({"no_result": str(e)}, indent=2))
        return 0
    except SearchfoxError as e:
        print(json.dumps({"error": str(e)}, indent=2))
        return 1
    print(_dump(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
