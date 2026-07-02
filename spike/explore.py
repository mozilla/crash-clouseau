"""Build a call-graph neighborhood from seed symbols (Phase-0 spike).

Two modes:

* ``mechanical_neighborhood`` -- deterministic BFS over searchfox
  ``calls-from`` / ``calls-to``. No LLM, no API key. This is the *upper bound* on
  what searchfox can reach at a given hop/depth/query budget, and the most
  decisive input to the go/no-go: **if the mechanical neighborhood can't reach the
  regressor, no cheap LLM will.**
* ``llm_neighborhood`` -- a budgeted Haiku 4.5 loop that *chooses* which symbols to
  expand (closer to the production Call-graph Explorer senior). Tests whether a
  cheap model can navigate to the regressor without brute force.

Running both and comparing (plan step 8) attributes a miss: absent from mechanical
=> a searchfox hole (PLAN §7 blind spot); present in mechanical but missed by llm
=> cheap-model navigation, not the tool.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

from .searchfox_cli import DEFAULT_DEPTH, DEFAULT_REPO

log = logging.getLogger("spike.explore")

# Haiku 4.5 pricing ($/1M tok); cached input reads at ~0.1x (5-min TTL).
_HAIKU_IN = 1.0
_HAIKU_OUT = 5.0
_MODEL_DEFAULT = "claude-haiku-4-5"
_NEIGHBORHOOD_SHOWN = 60  # cap symbols shown to the LLM to bound token cost

SYSTEM_EXPLORER = (
    "You are a Call-graph Explorer for Firefox crash triage. Given a crash brief "
    "and a growing set of already-discovered symbols, your job is to reach the "
    "function that most likely CAUSED the crash -- which is often NOT on the stack "
    "(a callee that corrupted state and returned, a caller passing bad data, a "
    "callback/vtable target). You navigate the call graph with searchfox; you do "
    "not guess. Propose the next batch of searchfox queries as a STRICT JSON array "
    "and nothing else. Each element is one of:\n"
    '  {"action":"calls-from","symbol":"<qualified or mangled symbol>","depth":2}\n'
    '  {"action":"calls-to","symbol":"<symbol>","depth":2}\n'
    '  {"action":"calls-between","source":"<Class>","target":"<Class>","depth":2}\n'
    '  {"action":"define","symbol":"<symbol>"}\n'
    "Prefer calls-from/calls-to on concrete frame functions first; widen only where "
    "the crash mechanism suggests it. Do not repeat already-expanded queries. "
    "Return 1-5 proposals. Output ONLY the JSON array."
)


def _cost(in_tok: int, out_tok: int, cached_in: int = 0) -> float:
    uncached = max(in_tok - cached_in, 0)
    return (
        uncached / 1e6 * _HAIKU_IN
        + cached_in / 1e6 * _HAIKU_IN * 0.1
        + out_tok / 1e6 * _HAIKU_OUT
    )


@dataclass
class ExploreResult:
    mode: str
    symbols: set[str] = field(default_factory=set)
    steps: list[dict] = field(default_factory=list)
    queries: int = 0
    stopped: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    est_cost_usd: float = 0.0

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "neighborhood_size": len(self.symbols),
            "queries": self.queries,
            "stopped": self.stopped,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "est_cost_usd": round(self.est_cost_usd, 4),
            "steps": self.steps,
        }


# --- mode 1: mechanical BFS (no LLM) ---------------------------------------


def mechanical_neighborhood(
    seed_symbols,
    *,
    hops: int = 2,
    depth: int = DEFAULT_DEPTH,
    repo: str = DEFAULT_REPO,
    budget_queries: int = 40,
    per_call_timeout: int = 60,
) -> ExploreResult:
    """Breadth-first calls-from/calls-to expansion from the seed symbols."""
    from . import searchfox_cli as sf

    neighborhood = {s for s in seed_symbols if s}
    frontier = set(neighborhood)
    expanded: set[str] = set()
    steps: list[dict] = []
    queries = 0
    stopped = "fixpoint"
    hop = 0

    while hop < hops and frontier and queries < budget_queries:
        next_frontier: set[str] = set()
        for sym in sorted(frontier):
            if queries >= budget_queries:
                stopped = "budget"
                break
            if sym in expanded:
                continue
            expanded.add(sym)
            for action, fn in (("calls-from", sf.calls_from), ("calls-to", sf.calls_to)):
                if queries >= budget_queries:
                    stopped = "budget"
                    break
                r = fn(sym, depth=depth, repo=repo, timeout=per_call_timeout)
                queries += 1
                added = set(r.symbols) - neighborhood
                neighborhood |= set(r.symbols)
                next_frontier |= added
                steps.append(
                    {
                        "hop": hop,
                        "action": action,
                        "symbol": sym,
                        "cmd": " ".join(r.cmd),
                        "ok": r.ok,
                        "returned": len(r.symbols),
                        "added": len(added),
                    }
                )
        frontier = next_frontier
        hop += 1

    return ExploreResult("mechanical", neighborhood, steps, queries, stopped)


# --- mode 2: cheap-LLM guided loop -----------------------------------------


_ACTIONS = {"calls-from", "calls-to", "calls-between", "define"}


@dataclass
class Proposal:
    action: str
    symbol: str | None = None
    source: str | None = None
    target: str | None = None
    depth: int = DEFAULT_DEPTH

    @classmethod
    def from_dict(cls, d) -> "Proposal | None":
        """Validate one LLM-proposed query dict; return None if malformed."""
        if not isinstance(d, dict) or d.get("action") not in _ACTIONS:
            return None
        try:
            depth = int(d.get("depth", DEFAULT_DEPTH) or DEFAULT_DEPTH)
        except (TypeError, ValueError):
            depth = DEFAULT_DEPTH
        p = cls(
            action=d["action"],
            symbol=d.get("symbol"),
            source=d.get("source"),
            target=d.get("target"),
            depth=depth,
        )
        if p.action == "calls-between":
            return p if (p.source and p.target) else None
        return p if p.symbol else None


def _pkey(p: "Proposal") -> str:
    if p.action == "calls-between":
        return f"calls-between:{p.source}->{p.target}"
    return f"{p.action}:{p.symbol}"


def _api_key() -> str | None:
    """Anthropic key from ~/.mozdata.ini ([Anthropic] api_key or token), else env.

    Mirrors how report_bug.py reads the Bugzilla token (``libmozdata.config.get``).
    Secrets belong in ``~/.mozdata.ini`` (home dir), NOT the repo's *tracked*
    ``./mozdata.ini``; libmozdata reads both and the home file wins. Both
    ``api_key`` and ``token`` (libmozdata's house convention, per its
    mozdata.ini-TEMPLATE) are accepted. ``ANTHROPIC_API_KEY`` is a final fallback."""
    try:
        from libmozdata import config

        for opt in ("api_key", "token"):
            key = config.get("Anthropic", opt, "")
            if key:
                return key
    except Exception:  # libmozdata absent / no ini file -> fall back to env
        pass
    return os.environ.get("ANTHROPIC_API_KEY") or None


def _client():
    key = _api_key()
    if not key:
        log.warning(
            "no Anthropic key ([Anthropic] api_key in mozdata.ini, or "
            "ANTHROPIC_API_KEY env); skipping the LLM explorer"
        )
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic not installed; skipping the LLM explorer")
        return None
    return anthropic.Anthropic(api_key=key)


def _extract_json_array(text: str) -> str | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    return text[start : end + 1]


def _parse_proposals(text: str) -> list["Proposal"]:
    raw = _extract_json_array(text)
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        p = Proposal.from_dict(it)
        if p is not None:
            out.append(p)
    return out


def _model_call_kwargs(model: str) -> dict:
    """Per-model request tuning for the proposer call.

    Haiku 4.5 is not in the adaptive-thinking family (no ``thinking``/``effort``);
    it runs as-is at a small ``max_tokens``. Sonnet 5 / Opus 4.8 / Fable 5 ARE
    adaptive-thinking models -- give them adaptive thinking (how you'd deploy them
    for navigation) plus a larger ``max_tokens`` so thinking tokens don't crowd
    out the JSON proposal (Sonnet 5 runs adaptive-on even when omitted; Opus 4.8
    omitted == off, so enable it explicitly). ``_propose`` extracts only ``text``
    blocks, so thinking blocks are skipped and the parse is unaffected.
    """
    m = model.lower()
    if "haiku" in m:
        return {"max_tokens": 1024}
    if "opus" in m:
        # Opus 4.8's default is deliberate/conservative -- it stops proposing
        # early (no-proposals) unless pushed. The default-Opus baseline already
        # ran at effort=high (the API default), so we hold effort at high and add
        # ONLY the persistence directive (see _system_text) to isolate that lever.
        # (xhigh was tried but made each call minutes-slow for little navigator
        # benefit -- persistence, not reasoning depth, was the binding constraint.)
        return {
            "max_tokens": 8192,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "high"},
        }
    if any(t in m for t in ("sonnet", "fable", "mythos")):
        return {"max_tokens": 8192, "thinking": {"type": "adaptive"}}
    return {"max_tokens": 1024}


# Appended to the explorer system prompt for Opus only: counters its default
# early-stop ("no-proposals") behavior in this navigator role.
_PERSIST_SUFFIX = (
    "\n\nPERSISTENCE (autonomous mode): You run without a human to consult. Keep "
    "proposing NEW searchfox queries each round until you have genuinely exhausted "
    "all plausible paths to the culprit. Do NOT return an empty array or stop early "
    "merely because you have gathered some context -- an unexpanded caller, callee, "
    "or sibling may be the off-stack regressor. Stop only when further expansion is "
    "clearly futile."
)


def _system_text(model: str) -> str:
    return SYSTEM_EXPLORER + (_PERSIST_SUFFIX if "opus" in model.lower() else "")


def _propose(client, model, brief, neighborhood, expanded):
    shown = sorted(neighborhood)[:_NEIGHBORHOOD_SHOWN]
    user = (
        f"CRASH BRIEF\n{brief}\n\n"
        f"ALREADY-DISCOVERED SYMBOLS ({len(neighborhood)}, showing {len(shown)}):\n"
        + "\n".join(f"  - {s}" for s in shown)
        + "\n\nALREADY-EXPANDED QUERIES:\n"
        + ("\n".join(f"  - {k}" for k in sorted(expanded)) or "  (none yet)")
        + "\n\nPropose the next searchfox queries (JSON array only)."
    )
    resp = client.messages.create(
        model=model,
        system=[
            {"type": "text", "text": _system_text(model), "cache_control": {"type": "ephemeral"}}
        ],
        messages=[{"role": "user", "content": user}],
        **_model_call_kwargs(model),
    )
    text = "".join(
        b.text for b in resp.content if getattr(b, "type", "") == "text"
    )
    u = resp.usage
    usage = (
        u.input_tokens,
        u.output_tokens,
        getattr(u, "cache_read_input_tokens", 0) or 0,
    )
    return _parse_proposals(text), usage


def _execute(sf, p: "Proposal", depth: int, repo: str, timeout: int):
    if p.action == "calls-from":
        return sf.calls_from(p.symbol, depth=p.depth or depth, repo=repo, timeout=timeout)
    if p.action == "calls-to":
        return sf.calls_to(p.symbol, depth=p.depth or depth, repo=repo, timeout=timeout)
    if p.action == "calls-between":
        return sf.calls_between(p.source, p.target, depth=p.depth or depth, repo=repo, timeout=timeout)
    return sf.define(p.symbol, repo=repo, timeout=timeout)


def llm_neighborhood(
    brief: str,
    seed_symbols,
    *,
    model: str = _MODEL_DEFAULT,
    budget_queries: int = 40,
    max_rounds: int = 12,
    depth: int = DEFAULT_DEPTH,
    repo: str = DEFAULT_REPO,
    per_call_timeout: int = 60,
) -> ExploreResult:
    """Haiku-guided expansion: the model proposes queries, we execute them."""
    client = _client()
    if client is None:
        return ExploreResult("llm", {s for s in seed_symbols if s}, stopped="no-llm")

    from . import searchfox_cli as sf

    neighborhood = {s for s in seed_symbols if s}
    expanded: set[str] = set()
    steps: list[dict] = []
    queries = in_tok = out_tok = cached = 0
    stopped = "fixpoint"

    for _round in range(max_rounds):
        if queries >= budget_queries:
            stopped = "budget"
            break
        try:
            proposals, usage = _propose(client, model, brief, neighborhood, expanded)
        except Exception as e:  # API error -- stop the leg, keep what we have
            log.warning("LLM proposal call failed: %s", e)
            stopped = "llm-error"
            break
        in_tok, out_tok, cached = in_tok + usage[0], out_tok + usage[1], cached + usage[2]
        fresh = [p for p in proposals if _pkey(p) not in expanded]
        if not fresh:
            stopped = "no-proposals"
            break
        for p in fresh:
            if queries >= budget_queries:
                stopped = "budget"
                break
            expanded.add(_pkey(p))
            r = _execute(sf, p, depth, repo, per_call_timeout)
            queries += 1
            added = set(r.symbols) - neighborhood
            neighborhood |= set(r.symbols)
            steps.append(
                {
                    "round": _round,
                    "proposal": _pkey(p),
                    "cmd": " ".join(r.cmd),
                    "ok": r.ok,
                    "returned": len(r.symbols),
                    "added": len(added),
                }
            )

    return ExploreResult(
        "llm",
        neighborhood,
        steps,
        queries,
        stopped,
        in_tok,
        out_tok,
        cached,
        _cost(in_tok, out_tok, cached),
    )
