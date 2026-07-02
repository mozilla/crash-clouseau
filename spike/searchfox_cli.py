"""Throwaway subprocess adapter for ``searchfox-cli`` (Phase-0 spike only).

Wraps the handful of ``searchfox-cli`` operations the call-graph spike needs and
does a *best-effort* parse of the LLM-oriented markdown the tool prints to stdout.

Scope & caveats (see plans/00-phase0-callgraph-spike.md):

* **THROWAWAY quality.** The production adapter (robust permalink / symbol-id
  citation capture) is Phase-1 work (sub-plan #01).
* **The markdown layout was NOT verified against a live binary** when this was
  written (``searchfox-cli`` is not installed in this checkout). Every parser
  below is best-effort and marked ``FIXME`` -- pin them to real fixtures before
  trusting the extracted symbols.
* ``searchfox-cli`` operations are **flags, not subcommands**::

      searchfox-cli --calls-from '<SYM>' --depth 2 -R mozilla-central

  ``--calls-between`` takes a single comma-joined arg (``'A,B'``) and is
  **class/namespace-scoped** (not arbitrary function -> function), per the CLI's
  own ``--long-help``.
* searchfox indexes ~tip of github.com/mozilla-firefox/firefox, **not** the crash
  build node (revision drift -- a real Phase-0 risk).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field

log = logging.getLogger("spike.searchfox_cli")

BINARY = "searchfox-cli"
DEFAULT_REPO = "mozilla-central"
DEFAULT_TIMEOUT = 60  # call-graph queries over hot symbols can be slow
DEFAULT_DEPTH = 2  # CLI default is 1; the spike raises it (reach is shallow at 1)
DEFAULT_RETRIES = 3  # transient 5xx/timeout retries (backoff below)
_BACKOFF_BASE = 2.0  # seconds; sleeps 2, 4, 8 across attempts

# searchfox's backend intermittently returns 5xx / drops the connection. Those
# surface as rc!=0 with an HTTP-ish stderr; retrying absorbs them. A genuine
# "no such symbol" / empty result must NOT be retried (it never recovers and
# would 3x-slow every miss), so we retry ONLY when stderr looks transient.
_TRANSIENT_RE = re.compile(
    r"50[0234]|Bad Gateway|Internal Server Error|Service Unavailable|"
    r"Gateway Time-?out|Request failed|Connection|timed? ?out|"
    r"reset by peer|EOF|broken pipe",
    re.IGNORECASE,
)


def _is_transient(err: str | None) -> bool:
    return bool(err and _TRANSIENT_RE.search(err))

# Valid -R/--repo values. NOTE: there is deliberately no ``autoland`` here.
REPOS = {
    "mozilla-central",
    "mozilla-beta",
    "mozilla-release",
    "mozilla-esr115",
    "mozilla-esr128",
    "mozilla-esr140",
    "comm-central",
}

# --- best-effort markdown extractors (FIXME: pin to real fixtures) ----------
# Demangled qualified names: Foo::Bar, mozilla::dom::AudioContext::CreateGain, Foo::~Foo
_QUALIFIED_RE = re.compile(r"[A-Za-z_]\w*(?:::~?[A-Za-z_]\w*)+")
# Itanium (_Z...) and Rust v0 (_R...) mangled symbols.
_MANGLED_RE = re.compile(r"_[ZR][\w$.]+")
# searchfox permalinks (full URLs, if a command emits them).
_PERMALINK_RE = re.compile(r"https?://searchfox\.org/\S+")
# source refs attached to each call-graph hit, e.g. "xpcom/base/nsDebug.h#51".
_REF_RE = re.compile(r"[\w./+\-]+\.\w+#\d+")


def _dedupe(seq):
    """Order-preserving de-dupe."""
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _extract_symbols(md: str) -> list[str]:
    # Drop the "# calls-from:'SYM' depth:N" echo header (single-'# ' line) so the
    # *query* symbol isn't counted as a result -- that also lets the Rust
    # reduce-retry detect a genuinely empty result. "## Section" headers are kept.
    # Grabs both mangled (`_Z…`/`S_rs_…` via backticks) and demangled qualified
    # names so run_spike's membership check can match on either.
    body = "\n".join(ln for ln in md.splitlines() if not ln.startswith("# "))
    return _dedupe(_MANGLED_RE.findall(body) + _QUALIFIED_RE.findall(body))


def _extract_permalinks(md: str) -> list[str]:
    return _dedupe(_PERMALINK_RE.findall(md))


def _extract_refs(md: str) -> list[str]:
    """Source references (``path#line``) that calls-from/-to attach to each hit."""
    return _dedupe(_REF_RE.findall(md))


@dataclass
class SfResult:
    """Result of one ``searchfox-cli`` invocation."""

    ok: bool
    cmd: list[str]
    raw_markdown: str = ""
    symbols: list[str] = field(default_factory=list)
    permalinks: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict:
        """Compact form for the run trace (raw markdown omitted on purpose)."""
        return {
            "cmd": " ".join(self.cmd),
            "ok": self.ok,
            "symbols": self.symbols,
            "permalinks": self.permalinks,
            "refs": self.refs,
            "error": self.error,
        }


def available() -> bool:
    """True iff the ``searchfox-cli`` binary is on ``PATH``."""
    return shutil.which(BINARY) is not None


def _repo_args(repo: str) -> list[str]:
    if repo not in REPOS:
        log.warning("unknown repo %r; searchfox-cli may reject it", repo)
    return ["-R", repo]


def _run(
    args: list[str],
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
) -> SfResult:
    """Invoke ``searchfox-cli`` with ``args``; never raises -- degrades to ok=False.

    Transient backend failures (5xx / dropped connection / timeout) are retried
    with exponential backoff so they don't silently become empty neighborhoods
    (which would poison the recall metric with false misses). Non-transient
    non-zero exits -- notably a genuine empty result -- are returned immediately.
    """
    cmd = [BINARY, *args]
    binary = shutil.which(BINARY)
    if binary is None:
        msg = f"{BINARY} not found on PATH (install: cargo install searchfox-cli)"
        log.warning(msg)
        return SfResult(ok=False, cmd=cmd, error=msg)

    last: SfResult | None = None
    for attempt in range(retries + 1):
        if attempt:  # backoff before every retry (2s, 4s, 8s, ...)
            time.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))
        try:
            proc = subprocess.run(
                [binary, *args], capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            last = SfResult(ok=False, cmd=cmd, error=f"timeout after {timeout}s")
            log.debug("searchfox-cli timeout (attempt %d/%d)", attempt + 1, retries + 1)
            continue  # a hung backend is transient -- retry
        except OSError as e:  # pragma: no cover - defensive
            return SfResult(ok=False, cmd=cmd, error=str(e))

        if proc.returncode != 0:
            # A missing edge / empty result may surface as a non-zero exit; keep
            # stdout (some tools still print "No results") and stderr for
            # debugging, but flag not-ok so callers don't treat noise as a hit.
            err = proc.stderr.strip() or f"exit {proc.returncode}"
            res = SfResult(ok=False, cmd=cmd, raw_markdown=proc.stdout, error=err)
            if _is_transient(err) and attempt < retries:
                last = res
                log.debug(
                    "searchfox-cli transient rc=%s (attempt %d/%d): %s",
                    proc.returncode, attempt + 1, retries + 1, err,
                )
                continue  # retry -- do not let a 5xx become a false miss
            log.debug("searchfox-cli rc=%s: %s", proc.returncode, err)
            return res

        md = proc.stdout
        return SfResult(
            ok=True,
            cmd=cmd,
            raw_markdown=md,
            symbols=_extract_symbols(md),
            permalinks=_extract_permalinks(md),
            refs=_extract_refs(md),
        )

    # Exhausted retries on a transient failure.
    return last if last is not None else SfResult(ok=False, cmd=cmd, error="no result")


# --- public operations ------------------------------------------------------


_ARGS_RE = re.compile(r"\(.*$", re.DOTALL)
_TMPL_RE = re.compile(r"<[^<>]*>")


def _clean_symbol(symbol: str) -> str:
    """Strip a signature/param list and template args from a symbol.

    Socorro frame functions look like ``NS_ProcessNextEvent(nsIThread*, bool)`` or
    ``mozilla::Maybe<T>::ref``; searchfox resolves the bare qualified name, so the
    parens/templates must go or every query returns nothing.
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
    (e.g. ``Renderer::render``), NOT the full ``crate::module::Type::method`` path
    that Socorro frames carry; C++ resolves on the full name. Used as a fallback.
    """
    parts = symbol.split("::")
    return "::".join(parts[-2:]) if len(parts) > 2 else symbol


def _calls(flag: str, symbol: str, depth: int, repo: str, timeout: int) -> SfResult:
    # Socorro frame functions carry signatures/templates that searchfox won't
    # match -- strip to the bare qualified name first.
    sym = _clean_symbol(symbol)
    r = _run([flag, sym, "--depth", str(depth), *_repo_args(repo)], timeout)
    if r.ok and not r.symbols:
        # Rust: the full crate path doesn't resolve -- retry on trailing Type::method.
        reduced = _reduce_symbol(sym)
        if reduced != sym:
            r2 = _run([flag, reduced, "--depth", str(depth), *_repo_args(repo)], timeout)
            if r2.symbols:
                return r2
    return r


def calls_from(
    symbol: str,
    depth: int = DEFAULT_DEPTH,
    repo: str = DEFAULT_REPO,
    timeout: int = DEFAULT_TIMEOUT,
) -> SfResult:
    """Functions **called by** ``symbol`` (out to ``depth``)."""
    return _calls("--calls-from", symbol, depth, repo, timeout)


def calls_to(
    symbol: str,
    depth: int = DEFAULT_DEPTH,
    repo: str = DEFAULT_REPO,
    timeout: int = DEFAULT_TIMEOUT,
) -> SfResult:
    """Functions that **call** ``symbol`` (out to ``depth``)."""
    return _calls("--calls-to", symbol, depth, repo, timeout)


def calls_between(
    source: str,
    target: str,
    depth: int = DEFAULT_DEPTH,
    repo: str = DEFAULT_REPO,
    timeout: int = DEFAULT_TIMEOUT,
) -> SfResult:
    """Call paths between two symbols.

    NOTE: the CLI treats this as **class/namespace -> class/namespace**, so it may
    return nothing for plain function pairs. Prefer ``calls_from``/``calls_to`` at
    a raised ``depth`` for function-level reach.
    """
    return _run(
        ["--calls-between", f"{source},{target}", "--depth", str(depth), *_repo_args(repo)],
        timeout,
    )


def define(
    symbol: str, repo: str = DEFAULT_REPO, timeout: int = DEFAULT_TIMEOUT
) -> SfResult:
    """The full source of ``symbol``'s definition (used to canonicalise names)."""
    return _run(["--define", symbol, *_repo_args(repo)], timeout)


def search(
    text: str,
    limit: int = 50,
    repo: str = DEFAULT_REPO,
    timeout: int = DEFAULT_TIMEOUT,
) -> SfResult:
    """Free-text search (fallback for resolving a name to a symbol)."""
    return _run(["-q", text, "-l", str(limit), *_repo_args(repo)], timeout)


if __name__ == "__main__":  # smoke test: eyeball one real invocation + its parse
    logging.basicConfig(level=logging.INFO)
    if not available():
        raise SystemExit("searchfox-cli not on PATH; `cargo install searchfox-cli`")
    demo = "mozilla::dom::AudioContext::CreateGain"
    r = calls_from(demo, depth=2)
    print("cmd:       ", " ".join(r.cmd))
    print("ok/error:  ", r.ok, r.error)
    print(f"symbols({len(r.symbols)}):", r.symbols[:20])
    print(f"links({len(r.permalinks)}):", r.permalinks[:5])
    print("--- raw markdown (first 800 chars) ---")
    print(r.raw_markdown[:800])
