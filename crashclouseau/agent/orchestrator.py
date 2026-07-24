# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Orchestration worker & seed seam (#11).

The RQ job that builds a crash seed, runs the hackbot triage agent for one UUID
via ``asyncio.run(run_crash_triage(...))`` (#02), and persists the resulting
dossier + verdict + recorded actions via the #04 DAO. It owns only the *outside*
of the agent call: enqueue gating, seed building, cost-cap enforcement (reactive,
from ``CrashTriageResult.total_cost_usd``), idempotency, and failure isolation so
LLM/SDK/searchfox flakiness can never block ingestion.

The ``run_crash_triage`` import is LAZY (inside ``run_evidence_agent``) so the
enqueue path / web dyno never pull ``claude-agent-sdk`` or spawn the bundled CLI.
"""
from __future__ import annotations

import asyncio
import re
from datetime import timedelta

from rq import Retry

from crashclouseau import app, config, db, models, worker
from crashclouseau.agent.experts import area_experts
from crashclouseau.agent.schema import (
    AreaExpert,
    CONFIDENCE_SCORE,
    Confidence,
    Decision,
    FailureClass,
    SearchfoxCitation,
    StructLayoutCitation,
    Verdict,
)
from crashclouseau.logger import logger
from crashclouseau.vendor.hackbot_runtime.actions.recorder import ActionsRecorder

# Config short names -> full model ids stored in the dossier/verdict rows.
_MODEL_IDS = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-4-8",
    "fable": "claude-fable-5",
}
_DEFAULT_COST_CAP = 2.0
# A "running" dossier older than job_timeout + this buffer is a dead orphan (RQ kills a
# run at job_timeout; the buffer avoids racing a run that's legitimately near the cap).
_STALE_BUFFER_S = 300

# Substrings (in the exception type or message) that mark a TRANSIENT failure worth an
# automatic retry rather than a terminal error. A ~20-min triage fires hundreds of
# Anthropic + searchfox + hg calls, and a single un-retried network/API/stream blip
# aborts the whole run; retrying lets that self-heal instead of dropping the crash.
# Deliberately narrow: a code bug (KeyError/ValueError/…) matches nothing and fails fast.
_TRANSIENT_MARKERS = (
    "overloaded", "rate limit", "ratelimit", "too many requests", "429", "529",
    "timeout", "timed out", "connection", "econnreset", "temporarily",
    "unavailable", "server error", "internal server", "bad gateway",
    "gateway timeout", "stream", "process ended", "processerror",
    "apiconnection", "apitimeout", "apistatus",
)


def _should_retry(exc) -> bool:
    blob = "{}: {}".format(type(exc).__name__, exc).lower()
    return any(m in blob for m in _TRANSIENT_MARKERS)


def _current_job():
    """The RQ job running this call, or None (e.g. under the eval runner / a probe)."""
    try:
        from rq import get_current_job

        return get_current_job()
    except Exception:  # pragma: no cover - defensive
        return None


def _full_model(model):
    return _MODEL_IDS.get(model, model)


def _seed_score(uuid):
    row = (
        db.session.query(models.UUID.max_score)
        .filter(models.UUID.uuid == uuid)
        .first()
    )
    return row[0] if row else None


def _inlines_by_stackpos(raw_crash):
    """Map stackpos -> [inlined function names] from the processed crash's crashing
    thread. The crash's real leaf functions are inlined and otherwise never reach the
    agent. Best-effort across the Socorro shapes (``json_dump.crashing_thread`` or
    ``json_dump.threads[crashing_thread]``); returns {} on any mismatch (never raises)."""
    out: dict = {}
    try:
        dump = (raw_crash or {}).get("json_dump") or {}
        thread = dump.get("crashing_thread")
        if not isinstance(thread, dict):
            idx = dump.get("crashing_thread")
            threads = dump.get("threads")
            if isinstance(idx, int) and isinstance(threads, list) and idx < len(threads):
                thread = threads[idx]
        if not isinstance(thread, dict):
            return {}
        for i, fr in enumerate(thread.get("frames") or []):
            if not isinstance(fr, dict):
                continue
            pos = fr.get("frame", i)
            names = [
                il.get("function")
                for il in (fr.get("inlines") or [])
                if isinstance(il, dict) and il.get("function")
            ]
            if names:
                out[pos] = names
    except Exception:  # pragma: no cover - defensive; inlines are a nicety
        return {}
    return out


# Generic abort/panic MACHINERY symbols that top the stack of every assertion/panic/OOM
# crash and bury the real crashing frame. Substring match on the frame function; curated to
# runtime-only names (no bare "abort") so a real crash site can't match. Used ONLY to tidy
# the agent-facing stack TEXT (see _stack_text) — never the stored frames.
_PROLOGUE_PATTERNS = (
    "rust_begin_unwind", "rust_panic", "__rust_start_panic", "core::panicking",
    "std::panicking", "panicking::panic", "panic_fmt", "panic_bounds_check",
    "unwrap_failed", "expect_failed", "_Unwind_RaiseException", "_Unwind_Resume",
    "MOZ_Crash", "RustMozCrash", "AnnotateMozCrashReason", "mozalloc_abort",
    "mozalloc_handle_oom", "NS_ABORT_OOM", "NS_DebugBreak", "__assert_fail", "__assert_rtn",
)


def _stack_text(frames):
    """Render the crash stack for the AGENT's prompt. PRESENTATION ONLY — it does NOT touch
    the stored ``frames`` that feed candidate scoring and the stackpos-keyed dedup hash, so
    nothing mechanical changes. It strips the LEADING run of generic abort/panic machinery
    frames (rust_begin_unwind / panic_fmt / MOZ_Crash / ... — the same for every assertion/
    panic crash) so the REAL crashing frame sits on top of what the model reads, keeping the
    original stackpos numbers (a leading ``#3`` still tells the agent how deep it was). Never
    strips so far that the stack goes empty."""
    def line(f):
        base = "#{} {}  {}:{}".format(
            f.get("stackpos"), f.get("function"), f.get("filename"), f.get("line")
        )
        fi = f.get("inlines")
        if fi:
            base += "  [inlined: {}]".format(", ".join(fi))
        return base

    lead = 0
    for f in frames:
        fn = f.get("function") or ""
        if any(p in fn for p in _PROLOGUE_PATTERNS):
            lead += 1
        else:
            break
    shown = frames[lead:] if 0 < lead < len(frames) else frames
    text = "\n".join(line(f) for f in shown)
    if shown is not frames:
        text = ("(#0-#{}: generic abort/panic machinery elided; the real crashing frame is "
                "below)\n{}".format(lead - 1, text))
    return text


# Word + camelCase sub-word tokenizer for cheap signature<->description overlap ranking
# of off-stack window candidates (no line-proximity score exists off-stack).
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+")
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|[0-9]+")
_STOP_TOKENS = frozenset(
    {"bug", "fix", "the", "and", "for", "with", "from", "this", "that", "not",
     "add", "use", "get", "set", "new", "void", "int", "bool", "make", "remove"}
)


def _tokens(text):
    """Lowercased word + camelCase sub-word tokens (len>=3, minus a tiny stoplist), so a
    crash signature (camelCase symbols) and a commit description (prose) share a
    comparable vocabulary — e.g. ``nsTStringRepr::Length`` -> {string, repr, length}."""
    out: set = set()
    for w in _WORD_RE.findall(text or ""):
        for part in _CAMEL_RE.findall(w):
            p = part.lower()
            if len(p) >= 3 and p not in _STOP_TOKENS:
                out.add(p)
    return out


# A changeset that ENABLES a feature/pref by default is a classic OFF-STACK regressor: it
# turns ON a code path that then crashes while touching no stack file (bug 2056116: "Enable
# Rust storage by default" -> a Rust/sqlite sync panic, found by mccr8 scanning the pushlog).
# Detected from the description OR a touched pref/feature-manifest file. Tag + rank/prompt
# hint only; the agent confirms the enabled area actually relates to the crash.
_PREF_FLIP_DESC_RE = re.compile(
    r"\benabl\w+\b.{0,40}\bby default\b"
    r"|\bturn(?:ing)?\s+(?:\w+\s+)?on\b.{0,40}\bby default\b"
    r"|\b(?:on|enabled)\s+by\s+default\b"
    r"|\bflip\b.{0,20}\bpref"
    r"|\bset\b.{0,20}\bpref\w*\b.{0,20}\b(?:on|true|enabled)\b"
    r"|\bship\b.{0,30}\bby default\b"
    r"|\benable\b.{0,30}\b(?:feature|pref)\b",
    re.I,
)
_PREF_FILE_HINTS = (
    "modules/libpref/init/", "staticpreflist", "/all.js", "prefs.js", "firefox.js",
    "browser/app/profile/", "featuremanifest", "/nimbus/",
)


def _looks_pref_flip(desc, files):
    """True when a changeset looks like a feature/pref FLIP — enabling something by default —
    a classic OFF-STACK regressor. Keys on the full description OR a touched pref/
    feature-manifest file. Tag + hint only; the agent confirms the enabled area matches the
    crash (see the off-stack prompt / system.md rule and bug 2056116)."""
    if desc and _PREF_FLIP_DESC_RE.search(desc):
        return True
    return any(any(h in str(f).lower() for h in _PREF_FILE_HINTS) for f in (files or []))


def _offstack_candidates(uuid_info, offstack_cfg, prior_bugs=frozenset()):
    """P1: enumerate the crash's first-bad-build pushlog window as agent candidates when
    NOTHING scored onto a stack frame (an off-stack regressor). Returns build_seed's
    normal candidate shape (``{node, score, bug, backedout, pushdate, noise, desc}``) with
    ``score=None`` (there is no line-proximity signal off-stack) and ``noise=False``,
    lightly pre-ranked and capped to ``max_candidates``. Ranking key, best first:
    prior-signature hit (``bug`` in ``prior_bugs`` — a prior FIXED sibling named it as a
    regressor, the strongest off-stack prior), then non-backout, then signature<->desc
    token overlap, then recency. A prior-sig candidate is tagged ``prior_sig=True``. Merges
    are dropped. Best-effort: returns [] on any failure (the caller then abstains) — never
    raises.

    The window is bounded by the DB (``Build.get_two_last``) so it is migration-proof and
    needs no dead Buildhub lookup; ``pushlog_for_revs`` is one ``json-pushes`` GET through
    ``net.get`` (allowlisted UA). ``file_filter=lambda f: True`` DROPS the
    interesting-extensions filter so a non-source-file regressor is still enumerated."""
    from crashclouseau import pushlog

    channel = uuid_info.get("channel")
    product = uuid_info.get("product")
    buildid = uuid_info.get("buildid")
    build_node = uuid_info.get("node")
    try:
        two = models.Build.get_two_last(buildid, channel, product)
        if len(two) == 2:
            startrev = two[0]["revision"]
            # Prefer the crash's own build node as tochange (it IS the first-bad build);
            # fall back to get_two_last's newest entry if the uuid carries no node.
            endrev = build_node or two[1]["revision"]
            window = pushlog.pushlog_for_revs(
                startrev, endrev, channel=channel, file_filter=lambda f: True
            )
        else:
            # Predecessor build not ingested (fresh/partial DB): degrade to a date window
            # mirroring update.put_report's nightly lookback so we still seed.
            start = buildid - timedelta(days=config.get_ndays())
            window = pushlog.pushlog(
                start, buildid, channel=channel, file_filter=lambda f: True
            )
    except Exception as exc:
        logger.warning(
            "agent: off-stack window enumeration failed for %s: %s",
            uuid_info.get("uuid"), exc,
        )
        return []

    sig = _tokens(uuid_info.get("signature", ""))
    nonmerge = [c for c in window if not c.get("merge")]
    # Tag feature/pref flips once (from full desc + files) for ranking + the candidate dict.
    for c in nonmerge:
        c["_pref_flip"] = _looks_pref_flip(c.get("desc"), c.get("files"))

    def _prior_hit(c):
        b = c.get("bug")
        return isinstance(b, int) and b > 0 and b in prior_bugs

    def _key(c):
        overlap = len(sig & _tokens(c.get("desc", ""))) if sig else 0
        d = c.get("date")
        recency = d.timestamp() if hasattr(d, "timestamp") else 0.0
        # prior-signature hit first, then non-backout, then feature/pref flip (a classic
        # off-stack cause), then sig-token overlap, then recency.
        return (1 if _prior_hit(c) else 0, 0 if c.get("backedout") else 1,
                1 if c.get("_pref_flip") else 0, overlap, recency)

    ranked = sorted(nonmerge, key=_key, reverse=True)
    out = []
    for c in ranked[: offstack_cfg["max_candidates"]]:
        bug = c.get("bug")
        desc = (c.get("desc") or "").strip()
        out.append(
            {
                "node": c.get("node"),
                "score": None,
                "bug": bug if isinstance(bug, int) and bug > 0 else None,
                "backedout": bool(c.get("backedout")),
                "pushdate": c.get("date"),
                "noise": False,
                "desc": desc.splitlines()[0][:200] if desc else "",
                "prior_sig": _prior_hit(c),
                "pref_flip": bool(c.get("_pref_flip")),
            }
        )
    logger.info(
        "agent: off-stack window for %s -> %d candidates (cap %d)",
        uuid_info.get("uuid"), len(out), offstack_cfg["max_candidates"],
    )
    return out


def build_seed(uuid):
    """Assemble the ``crash=`` payload for ``run_crash_triage`` from the scored
    stack + processed crash. Returns None (logged) when there is nothing to reason
    about: an unknown UUID, no frames, or — for an OFF-STACK crash (no changeset scored
    onto any frame) — only when the P1 off-stack path is disabled (the default). When
    off-stack seeding is enabled, an off-stack crash instead seeds the FULL first-bad-build
    pushlog window (``_offstack_candidates``) and runs pinned (see ``get_agent_offstack``)."""
    res, uuid_info = models.CrashStack.get_by_uuid(uuid)
    frames = res.get("frames") if res else None
    if not frames:
        logger.warning("agent: no crash stack for %s; skipping", uuid)
        return None
    offstack_cfg = config.get_agent_offstack()
    is_offstack = not any(f.get("changesets") for f in frames)
    if is_offstack and not offstack_cfg["enabled"]:
        logger.warning("agent: no scored changesets for %s; skipping", uuid)
        return None

    try:
        info = models.UUID.get_info(uuid)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("agent: UUID.get_info failed for %s: %s", uuid, exc)
        info = {}

    raw_crash = None
    try:
        from crashclouseau import inspector

        raw_crash = inspector.get_crash_data(uuid)
    except Exception as exc:
        logger.warning("agent: could not fetch processed crash for %s: %s", uuid, exc)

    frames = frames[: config.get_agent_max_seed_frames()]
    # Per-frame inlined functions from the processed crash (the crash's real leaf
    # functions — e.g. `nsTStringRepr::Length`/`HashString` — are inlined and otherwise
    # invisible to the agent). Best-effort: no-op when the shape differs.
    inlines = _inlines_by_stackpos(raw_crash)
    if inlines:
        for f in frames:
            fi = inlines.get(f.get("stackpos"))
            if fi:
                f["inlines"] = fi

    stack_text = _stack_text(frames)
    prior_hints: list = []
    prior_bugs: set = set()
    if is_offstack:
        # Prior-signature (P4) corroboration: a prior FIXED sibling of this crash's
        # signature that already names a regressor is a strong off-stack prior. Fetch it
        # once here so it can (a) rank/flag window candidates and (b) later corroborate the
        # verdict — without a second network call. Best-effort; off via config.
        if offstack_cfg.get("prior_signature", True):
            try:
                from crashclouseau import priorsig

                sig = info.get("signature") or uuid_info.get("signature")
                prior_hints = priorsig.prior_regressor_hints(
                    [sig] if sig else [], exclude_bug=None
                )
                prior_bugs = {h["regressor_bug"] for h in prior_hints}
            except Exception as exc:  # pragma: no cover - defensive; never break a seed
                logger.warning("agent: prior-signature lookup failed for %s: %s", uuid, exc)
        # P1: no changeset scored onto a stack frame — seed the full first-bad-build
        # pushlog window (already ranked/capped, score=None) instead of skipping.
        candidates = _offstack_candidates(uuid_info, offstack_cfg, prior_bugs)
        if not candidates:
            # Window enumeration failed (no bounds / hg error): nothing to reason about,
            # so abstain rather than run the agent on an empty candidate set.
            logger.warning("agent: off-stack window empty for %s; skipping", uuid)
            return None
        # FP guard: restrict the prior-signature CORROBORATION to prior-named regressor bugs
        # that ALSO landed in THIS crash's window. Window-membership is a model-INDEPENDENT
        # 2nd axis (it is deterministic from the build, not something we hinted the model
        # into), and it drops the dangling/after-the-build pointers a raw signature-sibling
        # list can contain. The FULL hint set already ranked the window (recall); only the
        # corroboration/prompt set is tightened here.
        window_bugs = {c.get("bug") for c in candidates if c.get("bug")}
        prior_bugs = {b for b in prior_bugs if b in window_bugs}
        prior_hints = [h for h in prior_hints if h["regressor_bug"] in prior_bugs]
    else:
        # The scored candidate changesets are already in the DB (frame.changesets); hand
        # them to the agent (ranked) so patch-scout reads their diffs via mcp__patch__diff
        # instead of hunting for candidates with searchfox/Bash.
        # Down-rank (never drop) candidates whose only support is "noise": a universal
        # bottom-of-stack anchor frame or a ubiquitous-primitive file (#15 phase 3). A
        # candidate that also appears on a real frame keeps its real ranking via the max.
        filters = config.get_agent_filters()

        def _frame_is_noise(fr):
            fn, fname = fr.get("function") or "", fr.get("filename") or ""
            return any(p in fn for p in filters["anchor_frame_patterns"]) or any(s in fn for s in filters["ubiquitous_symbols"]) or any(p in fname for p in filters["ubiquitous_paths"])

        # Per node: max raw score (display), max penalized score (ranking), and noise =
        # ALL supporting frames are noise. A candidate that ALSO sits on a real code frame
        # keeps its real ranking and is NOT tagged noise (so it still yields an expert).
        cand: dict = {}
        for f in frames:
            fnoise = _frame_is_noise(f)
            factor = filters["penalty"] if fnoise else 1.0
            for node, cs in (f.get("changesets") or {}).items():
                score = cs.get("score") or 0
                eff = score * factor
                prev = cand.get(node)
                if prev is None:
                    cand[node] = {
                        "node": node,
                        "score": score,
                        "_eff": eff,
                        "bug": cs.get("bugid"),
                        "backedout": cs.get("backedout"),
                        # Landing date (was previously dropped): lets the agent reason
                        # about recency/regression-window proximity, and feeds future
                        # first-seen corroboration. Per-node, so no max() on the merge.
                        "pushdate": cs.get("pushdate"),
                        "_all_noise": fnoise,
                    }
                else:
                    prev["score"] = max(prev["score"], score)
                    prev["_eff"] = max(prev["_eff"], eff)
                    prev["_all_noise"] = prev["_all_noise"] and fnoise
        candidates = sorted(cand.values(), key=lambda c: -c["_eff"])
        for c in candidates:
            c["noise"] = c.pop("_all_noise")
            c.pop("_eff", None)

    # Area-experts (#15 phase 2): the authors of the top non-noise candidates — a
    # knowledgeable person to ask, computed from local data (migration-proof). Attached
    # to the dossier by run_evidence_agent regardless of the verdict.
    #
    # ON-STACK ONLY: on-stack candidates are changesets that SCORED onto the crash frames,
    # so their authors genuinely worked near the crash. OFF-STACK candidates are the
    # undifferentiated first-bad-build pushlog window, ranked mostly by recency when nothing
    # matches the signature — their authors merely "landed a patch near this build", NOT
    # people who worked in the crashing area, so surfacing them under "recently worked in
    # this area" is misleading noise. Suppress off-stack until a real crashing-file owner
    # lookup replaces it.
    channel = info.get("channel") or uuid_info.get("channel") or "nightly"
    experts = []
    if not is_offstack:
        try:
            authors = models.Node.authors_for([c["node"] for c in candidates[:10]], channel)
            experts = area_experts(candidates, authors, max_experts=3)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("agent: area-experts failed for %s: %s", uuid, exc)

    # Pin blame/history/source reads to the crash BUILD rev (never tip). ON-STACK: always —
    # reading the crashing line as-of the build is strictly more correct (tip can attribute
    # it to the post-build fix or a later refactor). OFF-STACK: gated by OFFSTACK_PINNED.
    # We pin ONLY when the (git, post-migration) build node actually resolves to an hg rev
    # the hg endpoints accept; otherwise pin_rev stays "" and the tools read tip — a WORKING
    # read — rather than 404-ing an unresolvable git hash. A transient git2hg/lando miss thus
    # degrades that run to tip instead of breaking the agent's evidence tools.
    pin_rev = ""
    build_node = uuid_info.get("node", "")
    if build_node and (not is_offstack or offstack_cfg["pinned"]):
        try:
            from crashclouseau import inspector

            if inspector.git2hg(build_node):
                pin_rev = build_node
        except Exception:  # pragma: no cover - defensive; degrade to tip
            pin_rev = ""

    return {
        "uuid": uuid,
        "signature": info.get("signature", ""),
        "channel": channel,
        "product": info.get("product") or uuid_info.get("product", ""),
        "buildid": info.get("buildid"),
        "version": info.get("version"),
        "frames": frames,
        "stack": stack_text,
        "candidates": candidates,
        "experts": experts,
        "raw_crash": raw_crash,
        # P1 off-stack markers, consumed downstream by triage (prompt framing + pinned
        # tool ctx) and run_evidence_agent (SF-3 / exposer gates, observe-only, cost cap).
        "is_offstack": is_offstack,
        "build_node": build_node,
        # See the pin_rev computation above: the build rev pinned tools default to (or "" =>
        # tools read tip, both when pinning is off AND when the build node won't resolve).
        "pin_rev": pin_rev,
        # Prior-signature (P4) hints: regressor bugs a prior FIXED sibling of this signature
        # named. Surfaced to the agent (prompt) and used by the corroboration gate.
        "prior_regressor_bugs": sorted(prior_bugs),
        "prior_hints": prior_hints,
    }


def _gather_evidence(dossier):
    cites = []
    if dossier is None:
        return cites

    def dump(items):
        return [c.model_dump(mode="json") for c in items]

    if dossier.call_path:
        for edge in dossier.call_path.edges:
            cites += dump(edge.citations)
    for hunk in dossier.hunks:
        cites += dump(hunk.citations)
    if dossier.data_flow:
        cites += dump(dossier.data_flow.citations)
    if dossier.verdict:
        for claim in (dossier.verdict.mechanism, dossier.verdict.consistency):
            if claim:
                cites += dump(claim.citations)
    return cites


def _fault_address(raw_crash):
    """Parse the numeric faulting address from the processed crash, or None."""
    raw = raw_crash or {}
    dump = raw.get("json_dump") or {}
    info = dump.get("crash_info") or {}
    addr = info.get("address")
    if addr in (None, ""):
        addr = raw.get("address")
    if addr in (None, ""):
        return None
    s = str(addr).strip()
    try:
        return int(s, 16) if s.lower().startswith("0x") else int(s)
    except (TypeError, ValueError):
        return None


def _iter_dossier_citations(dossier):
    """Yield every TYPED Citation attached to the dossier (call-path edges, hunks,
    data-flow, verdict mechanism/consistency). Skeptic citations are untyped (dicts)
    and skipped — the corroboration flag keys on the typed ones."""
    if dossier is None:
        return
    if dossier.call_path:
        for edge in dossier.call_path.edges:
            yield from edge.citations
    for hunk in dossier.hunks:
        yield from hunk.citations
    if dossier.data_flow:
        yield from dossier.data_flow.citations
    v = dossier.verdict
    if v is not None:
        for claim in (v.mechanism, v.consistency):
            if claim:
                yield from claim.citations


# Only a small, NON-ZERO faulting address pinpoints a specific struct field beyond the
# base pointer (0x8 = the field 8 bytes in). 0x0 is the generic null pointer (ambiguous)
# and a large address is not a struct-field null-deref, so both are excluded. One page cap.
_MAX_FIELD_FAULT = 0x1000


def _corroborations(dossier, seed):
    """Deterministic, non-LLM corroboration flags for a dossier. Currently the
    fault-address<->struct-field-offset match: a NON-ZERO small faulting address N that
    equals the byte offset of a field cited via a ``struct_layout`` citation is a
    machine-verifiable null-deref of THAT field (the ab3238a5 0x8==mLength signal).
    Never raises."""
    flags: dict = {}
    try:
        fault = _fault_address((seed or {}).get("raw_crash"))
        if fault is not None and 0 < fault <= _MAX_FIELD_FAULT:
            for cit in _iter_dossier_citations(dossier):
                if isinstance(cit, StructLayoutCitation) and cit.offset == fault:
                    flags["fault_address_offset_match"] = True
                    flags["fault_offset"] = fault
                    flags["fault_field"] = cit.field
                    flags["fault_type"] = cit.type_name
                    break
    except Exception:  # pragma: no cover - defensive; never break a run
        logger.warning("agent: corroboration computation failed", exc_info=True)
    # Prior-signature (P4) match: the verdict's candidate is the SAME bug a prior FIXED
    # sibling of this crash's signature was regressed by. ``seed['prior_regressor_bugs']`` is
    # already restricted (in build_seed) to bugs that ALSO landed in this crash's window, so
    # the corroboration is genuinely two INDEPENDENT axes — history (a prior sibling names
    # the bug) + a machine fact (the bug landed in this regression window) — not just "the
    # model agreed with a hint we gave it". FOCUS GUARD: fire ONLY when there is a SINGLE
    # such in-window prior; a hot/ambiguous signature that yields several in-window priors is
    # not corroborative (it ranked candidates for recall, but must not inflate confidence).
    # Never raises.
    try:
        pbugs = set((seed or {}).get("prior_regressor_bugs") or [])
        cand = dossier.candidate if dossier is not None else None
        if len(pbugs) == 1 and cand is not None and cand.bug in pbugs:
            flags["prior_signature_match"] = True
            flags["prior_regressor_bug"] = cand.bug
    except Exception:  # pragma: no cover - defensive
        logger.warning("agent: prior-signature corroboration failed", exc_info=True)
    return flags


def _apply_corroboration_gate(dossier, seed):
    """Attach deterministic corroboration flags to the dossier and, when a STRONG one
    stands, raise a ``lead`` below ``probable`` up to ``probable`` (0.70/70%). Since the
    worth-investigating pivot the model may ALSO self-assert up to ``probable`` on a lead,
    so this is no longer the only path there — but it is a DETERMINISTIC one (a signal the
    model cannot fabricate). A strong corroboration is a fault-address<->struct-field-offset
    match OR a prior-signature match (the candidate is a bug a prior FIXED sibling of this
    signature was regressed by). Strong-evidence/abstain are untouched; a lead already at
    ``probable`` is unchanged. Returns the flags. Mutates ``dossier`` in place; never raises."""
    if dossier is None:
        return {}
    flags = _corroborations(dossier, seed)
    # MERGE, don't replace: the off-stack SF-3 / exposer gates run BEFORE this one and have
    # already stashed flags on the dossier (call_path_verified / exposer_*). Overwriting
    # dossier.corroborations here would silently drop them from the persisted payload/UI.
    dossier.corroborations = {**(dossier.corroborations or {}), **flags}
    v = dossier.verdict
    is_bare_lead = v is not None and v.decision == Decision.lead and v.confidence != Confidence.probable
    if is_bare_lead and flags.get("fault_address_offset_match"):
        dossier.verdict = v.model_copy(update={"confidence": Confidence.probable})
        logger.info(
            "agent: corroboration gate raised lead -> probable (fault 0x%x == %s.%s)",
            flags.get("fault_offset", 0), flags.get("fault_type", "?"),
            flags.get("fault_field", "?"),
        )
    elif is_bare_lead and flags.get("prior_signature_match"):
        dossier.verdict = v.model_copy(update={"confidence": Confidence.probable})
        logger.info(
            "agent: corroboration gate raised lead -> probable (prior-signature: "
            "candidate bug %s named by a prior FIXED sibling of this signature)",
            flags.get("prior_regressor_bug", "?"),
        )
    return flags


def _has_verified_callpath(dossier) -> bool:
    """True when the dossier carries a searchfox-grounded call path: at least one
    ``call_path`` edge with a ``SearchfoxCitation``. This is the structural proof that a
    candidate's code was shown to REACH a crash frame through the call graph, as opposed
    to merely landing in the pushlog window. We key on the PRESENCE of a searchfox-cited
    edge rather than string-matching candidate<->frame symbols: ``changed_functions`` is
    not populated and frame/symbol spellings differ, so a match test would false-negative
    and demote real culprits."""
    cp = dossier.call_path if dossier is not None else None
    if cp is None:
        return False
    return any(
        any(isinstance(c, SearchfoxCitation) for c in edge.citations)
        for edge in cp.edges
    )


def _downgrade_to_lead_or_abstain(dossier, seed, reason, abstain_reason):
    """Shared downgrade used by the off-stack precision gates: turn a
    ``strong-evidence`` verdict into a soft ``lead`` when a cited anchor
    (candidate/hunk/edge) still stands, else ``abstain``. Mirrors ``_skeptic_veto``'s
    reconstruction (soft, non-accusatory draft). Mutates ``dossier`` in place."""
    v = dossier.verdict
    if dossier._has_lead_anchor():
        dossier.verdict = Verdict(
            decision=Decision.lead,
            confidence=Confidence.medium,
            needinfo_draft=dossier._soft_lead_draft(),
            mechanism=v.mechanism,
            consistency=v.consistency,
        )
        logger.info("agent: %s -> lead for %s", reason, (seed or {}).get("uuid"))
    else:
        dossier.verdict = Verdict(
            decision=Decision.abstain,
            confidence=Confidence.low,
            abstain_reason=abstain_reason,
        )
        logger.info("agent: %s -> abstain for %s", reason, (seed or {}).get("uuid"))


def _apply_callpath_gate(dossier, seed):
    """SF-3 precision gate for OFF-STACK runs. An off-stack candidate has no on-stack
    anchor and no proximity score, so a searchfox-verified call path connecting it to a
    crash frame is the ONLY structural evidence it actually REACHES the crash. Require
    that for ``strong-evidence``; without it, downgrade to ``lead`` (cited anchor stands)
    or ``abstain``. This is what makes the study's near-0-FP off-stack precision reachable
    (today nothing requires a verified call path — ``_consistency_rule`` only checks
    confidence + cited mechanism/consistency). Mutates in place; never raises. MUST run
    BEFORE ``_apply_corroboration_gate`` so a strong->lead downgrade here is still eligible
    for a lead->probable bump on a fault-offset match."""
    if dossier is None:
        return
    v = dossier.verdict
    if v is None or v.decision != Decision.strong_evidence:
        return
    if _has_verified_callpath(dossier):
        dossier.corroborations = {**(dossier.corroborations or {}), "call_path_verified": True}
        return
    _downgrade_to_lead_or_abstain(
        dossier, seed,
        "SF-3 gate: off-stack strong-evidence has no searchfox-verified call path",
        "off-stack strong-evidence lacks a searchfox-verified call path to a crash "
        "frame, and no cited candidate/hunk/edge anchor remains",
    )


# Freed / poisoned / uninitialized memory sentinel BYTES: jemalloc junk-on-free 0xe5 and
# junk-on-alloc 0xe4/0x5a, MOZ/frame poison 0xdd, MSVC debug fills 0xcd/0xcc/0xfd/0xab,
# ASan 0xbe/0xfb. A fault address that is (almost) a run of one of these is a
# use-after-free / uninitialized read — a latent-bug pattern an off-stack candidate often
# merely EXPOSES rather than introduces.
_POISON_BYTES = frozenset({0xE5, 0xE4, 0x5A, 0xDD, 0xCD, 0xCC, 0xFD, 0xAB, 0xBE, 0xFB, 0xA5, 0x2B})


def _looks_poison(fault) -> bool:
    """True when the fault address looks like freed/poisoned/uninitialized memory: its
    bytes are dominated by one known-poison byte (allowing one off-byte for an offset into
    the poisoned object). Small addresses are handled by the field-offset corroboration,
    so require > one page here."""
    if fault is None or fault <= _MAX_FIELD_FAULT:
        return False
    parts = []
    x = fault
    while x:
        parts.append(x & 0xFF)
        x >>= 8
    if len(parts) < 2:
        return False
    top = max(set(parts), key=parts.count)
    # Require the poison byte to DOMINATE: allow one off-byte (an offset into the poisoned
    # object) but demand >= 2 matching bytes, so a 2-byte address can't qualify on a single
    # poison byte (e.g. 0xE512 is NOT poison — that would spuriously demote a real culprit).
    return top in _POISON_BYTES and parts.count(top) >= max(2, len(parts) - 1)


def _classify_exposer(dossier, seed):
    """Flag — and, on a STRONG signal, softly downgrade — an 'exposer, not cause' verdict:
    a changeset that EXPOSED a pre-existing latent bug (~30% of off-stack cases, the
    systematic false-positive source) rather than introducing it. Only LIVE-computable
    signals are used; the study's strongest discriminator ('fix diff disjoint from the
    regressor diff') needs the LANDED FIX, which does not exist at triage time, so it is
    deliberately kept OUT of here (offline eval only). Sets ``corroborations['exposer_*']``
    for the UI on ANY verdict; downgrades ``strong-evidence`` -> ``lead`` ONLY on a strong
    signal (a freed/poisoned fault address = a UAF the candidate most likely just exposed)
    so a weak hint never demotes a genuine culprit. Mutates in place; never raises."""
    if dossier is None:
        return
    try:
        signals = []
        strong = False
        fault = _fault_address((seed or {}).get("raw_crash"))
        if _looks_poison(fault):
            signals.append("poison/freed-memory fault address 0x{:x}".format(fault))
            strong = True
        crash = dossier.crash
        if crash is not None:
            if getattr(crash, "failure_class", None) == FailureClass.uaf:
                signals.append("failure_class=uaf")
            if crash.phc_free_stack:
                signals.append("PHC free stack present")
        df = dossier.data_flow
        if df is not None and (df.operation or "").lower() in (
            "uaf", "use_after_free", "free", "double_free"
        ):
            signals.append("data-flow operation={}".format(df.operation))
        if not signals:
            return
        dossier.corroborations = {
            **(dossier.corroborations or {}),
            "exposer_suspected": True,
            "exposer_signals": signals,
            "exposer_strong": strong,
        }
        v = dossier.verdict
        if strong and v is not None and v.decision == Decision.strong_evidence:
            _downgrade_to_lead_or_abstain(
                dossier, seed,
                "exposer classifier ({})".format("; ".join(signals)),
                "crash looks like a pre-existing latent bug the candidate only exposed "
                "(no cited anchor to hand over as a lead)",
            )
    except Exception:  # pragma: no cover - defensive; never break a run
        logger.warning("agent: exposer classification failed", exc_info=True)


def _reconcile_bridged_action(result):
    """After a deterministic gate may have DOWNGRADED the verdict (SF-3 or the exposer
    classifier: strong-evidence -> lead or abstain — the exposer now fires on-stack too), the
    needinfo action ``build_result`` synthesized from the ORIGINAL (pre-gate) verdict is stale
    — it still carries the assertive strong-evidence draft even though the verdict is now a
    soft lead (or abstain). Re-derive the auto-bridged needinfo from the FINAL dossier so an
    apply-eligible action can never contradict the verdict the gates settled on. Idempotent
    when nothing downgraded (re-derives the same action). Genuine recorder-sourced actions
    (the agent calling the actions tool) are preserved; only the auto-bridged needinfo is
    rebuilt. Never raises."""
    try:
        # triage is already imported by this point (run_crash_triage ran), so this is a
        # cached re-import and pulls no extra SDK cost.
        from crashclouseau.agent.triage import _needinfo_action

        kept = [
            a for a in result.actions
            if "auto-drafted from the verdict" not in ((a or {}).get("reasoning") or "")
        ]
        fresh = _needinfo_action(result.dossier)
        if fresh is not None:
            # Dedup against a genuine recorder action with the same (type, bug_id, text),
            # mirroring build_result — so we never re-introduce a duplicate the agent's own
            # recorded comment already covers.
            fp = fresh["params"]
            dup = any(a.get("type") == fresh["type"] and (a.get("params") or {}).get("bug_id") == fp["bug_id"] and (a.get("params") or {}).get("text") == fp["text"] for a in kept)
            if not dup:
                kept.append(fresh)
        result.actions = kept
    except Exception:  # pragma: no cover - defensive; never break a run
        logger.warning("agent: action reconcile failed", exc_info=True)


def _apply_offstack_observe_only(result):
    """OBSERVE-ONLY off-stack canary: keep the full dossier + logged verdict (so its
    calibration can be watched), but SUPPRESS any outward action — drop the synthesized
    needinfo so the human-confirmed apply UI has nothing to execute for an off-stack run.
    Flags the dossier for the UI. The verdict itself is intentionally left as-is."""
    dropped = len(result.actions)
    result.actions = []
    if result.dossier is not None:
        result.dossier.corroborations = {
            **(result.dossier.corroborations or {}),
            "offstack_observe_only": True,
        }
    logger.info("agent: off-stack observe-only — suppressed %d action(s)", dropped)


def _verdict_row(result):
    """Map a CrashTriageResult's dossier verdict onto the #04 Verdict columns."""
    dossier = result.dossier
    verdict = dossier.verdict if dossier else None
    if verdict is None:
        return {"verdict": "abstain", "confidence": None,
                "rationale": "no verdict produced", "evidence": []}
    if verdict.decision == Decision.strong_evidence:
        vt = "culprit"
        rationale = (verdict.mechanism.statement if verdict.mechanism else "") or ""
    elif verdict.decision == Decision.lead:
        vt = "lead"
        mech = verdict.mechanism.statement if verdict.mechanism else ""
        rationale = mech or verdict.needinfo_draft or "plausible related changeset; mechanism unverified"
    else:
        vt = "abstain"
        rationale = verdict.abstain_reason or ""
    conf = None
    if verdict.confidence is not None:
        conf = int(round(CONFIDENCE_SCORE.get(verdict.confidence, 0.0) * 100))
    return {"verdict": vt, "confidence": conf, "rationale": rationale,
            "evidence": _gather_evidence(dossier)}


def _apply_worth_investigating(dossier):
    """Populate ``Verdict.p_worth_investigating`` from the fitted calibration table (Phase-2):
    map the FINAL verdict's confidence rung (after every gate has settled it) to its empirical
    calibrated probability. No-op — leaves ``None`` — when no table is configured (the
    pre-calibration default), the verdict abstains (an abstain isn't reported, so it has no
    worth-investigating probability), or the rung isn't in the table. Runs LAST so it reads the
    shipped rung, not a pre-downgrade one."""
    if dossier is None or dossier.verdict is None:
        return
    v = dossier.verdict
    if v.decision == Decision.abstain or v.confidence is None:
        return
    table = config.get_agent_calibration()
    if not table:
        return
    score = int(round(CONFIDENCE_SCORE.get(v.confidence, 0.0) * 100))
    p = table.get(score)
    if p is not None:
        dossier.verdict = v.model_copy(update={"p_worth_investigating": float(p)})


def apply_deterministic_gates(result, seed):
    """Reshape the RAW agent verdict into the SHIPPED verdict with the deterministic,
    outside-the-LLM gates: attach area-experts, then the SF-3 call-path gate (off-stack
    only), the exposer classifier (all crashes), and the corroboration gate (fault-offset /
    prior-signature lead->probable bump); re-derive the bridged needinfo from the FINAL
    verdict; and apply off-stack observe-only last. Mutates ``result`` / ``result.dossier``
    in place and returns it.

    Extracted so the OFFLINE eval runner applies the exact same post-verdict reshaping as
    ``run_evidence_agent`` — the calibration must score the pipeline we SHIP, not the raw
    model output (a strong->lead downgrade or a lead->probable bump changes the confidence
    rung the calibration keys on). ``seed`` is the ``build_seed`` crash dict (needs
    ``is_offstack``, ``experts``, ``raw_crash``, ``candidates``, ``prior_*``)."""
    # Attach deterministic area-experts (#15 phase 2) to the dossier so a
    # knowledgeable person is surfaced for ANY verdict (including abstain).
    if result.dossier is not None and seed.get("experts"):
        result.dossier.area_experts = [AreaExpert(**e) for e in seed["experts"]]

    # Deterministic gates, all computed OUTSIDE the LLM and reflected in BOTH the
    # persisted payload and the Verdict row (they run BEFORE model_dump + _verdict_row).
    offstack_cfg = config.get_agent_offstack() if seed.get("is_offstack") else None
    if result.dossier is not None:
        # Deterministic gates run BEFORE the corroboration gate so a strong->lead
        # downgrade here is still eligible for a lead->probable bump.
        # SF-3 call-path gate is OFF-STACK ONLY: an on-stack candidate already has its
        # stack-frame anchor, so requiring a searchfox call path would wrongly demote it.
        if offstack_cfg is not None and offstack_cfg["require_callpath_for_strong"]:
            _apply_callpath_gate(result.dossier, seed)
        # Exposer classifier runs for ALL crashes: ~1-in-6 ON-stack line hits are exposers
        # too, and its signals (poison fault address / UAF / PHC free stack) are
        # stack-independent; it only downgrades strong-evidence->lead on a STRONG poison
        # signal (a weak hint just sets a UI chip). Off-stack keeps its config knob.
        if offstack_cfg is None or offstack_cfg["exposer_classifier"]:
            _classify_exposer(result.dossier, seed)
        # Corroboration gate: a fault-address<->struct-field-offset OR prior-signature
        # match raises a bare lead (medium/50%) to `probable` (70%).
        _apply_corroboration_gate(result.dossier, seed)
        # A gate may have downgraded the verdict (exposer can fire on-stack too now), so
        # re-derive the auto-bridged needinfo from the FINAL verdict — a downgraded verdict
        # must not ship the original strong-evidence action. Idempotent when nothing
        # downgraded (re-derives the same action).
        _reconcile_bridged_action(result)
        # OBSERVE-ONLY canary is OFF-STACK ONLY: on-stack verdicts stay apply-eligible
        # (the established production behavior). Runs last so nothing re-adds an action.
        if offstack_cfg is not None and offstack_cfg["observe_only"]:
            _apply_offstack_observe_only(result)
    # Calibrated worth-investigating probability, from the FINAL (post-gate) rung. Additive;
    # ``None`` until a calibration table is fit + wired. Runs after every gate so it reads the
    # shipped verdict, and outside the offstack-guarded block so on-stack runs get it too.
    _apply_worth_investigating(result.dossier)
    return result


def run_evidence_agent(uuid, force=False):
    """RQ entrypoint: run the triage agent for one UUID and persist the result.
    On failure it records the reason (dossier ``payload['error']``) and marks status; a
    TRANSIENT failure with RQ retries remaining is RE-RAISED so RQ requeues the job (the
    only case this raises — ingestion enqueues onto RQ and never runs this inline, so a
    raise can't break ingestion), otherwise it settles on ``error`` and returns.

    ``force`` (a tasks-view retrigger) re-runs this one explicit uuid: it bypasses the
    cost dedup (skip-existing / proto) early-out. It still goes through the atomic
    claim below — ``retrigger_agent`` first resets the dossier to ``pending`` so the
    claim can re-take it — so two concurrent retriggers collapse to a single run."""
    try:
        skip_dedup = config.get_agent_skip_if_existing()
        stale_after = config.get_agent_job_timeout() + _STALE_BUFFER_S
        # Cheap cost dedup early-out (already-done / a same-proto sibling). A forced
        # retrigger bypasses it; the atomic claim below is still the real guard.
        if skip_dedup and not force and (
            models.Dossier.skip_triage(uuid, stale_after) or _proto_already_triaged(uuid)
        ):
            logger.info("agent: dossier/proto-signature already triaged for %s; skipping", uuid)
            return

        seed = build_seed(uuid)
        if seed is None:
            return

        seed_score = _seed_score(uuid)
        # Atomically claim the run (sets status=running). This is the authoritative,
        # race-free guard: the skip_triage read above is only a cheap early-out, so two
        # agentworkers (or two concurrent retriggers) that both got this far for the same
        # uuid don't both run — exactly one wins claim_running, the loser skips (no double
        # token cost). Only the global "re-run everything" mode (skip_if_existing off, and
        # not a retrigger) force-marks running unconditionally.
        if skip_dedup or force:
            if not models.Dossier.claim_running(uuid, stale_after):
                logger.info("agent: %s claimed by another worker / settled; skipping", uuid)
                return
        else:
            models.Dossier.upsert(uuid, payload={}, status="running", seed_score=seed_score)

        # Record the RQ job id so a retrigger can stop this run mid-flight.
        _record_job_id(uuid)

        llm_cfg = config.get_llm()
        tools_cfg = config.get_agent()
        principal = llm_cfg.get("principal", {})
        roles = llm_cfg.get("roles") or {}
        recorder = ActionsRecorder()

        from crashclouseau.agent.triage import run_crash_triage  # lazy: pulls the SDK

        result = asyncio.run(
            run_crash_triage(
                crash=seed, tools_cfg=tools_cfg, llm_cfg=llm_cfg, recorder=recorder
            )
        )

        # Off-stack runs feed a ~112-candidate window, so they get a higher (still
        # log-only) cost ceiling than the scored-candidate default.
        cap = (
            config.get_agent_offstack_cost_cap()
            if seed.get("is_offstack")
            else llm_cfg.get("max_cost_usd_per_crash", _DEFAULT_COST_CAP)
        )
        cost = result.total_cost_usd
        over_budget = cap is not None and cost is not None and cost > cap
        if over_budget:
            logger.warning(
                "agent: %s over budget: $%.4f > $%s",
                uuid, result.total_cost_usd, cap,
            )

        # Reshape the raw agent verdict into the shipped verdict (area-experts + the
        # callpath/exposer/corroboration gates + needinfo reconcile + observe-only). Shared
        # with the offline eval runner so calibration scores the pipeline we ship.
        apply_deterministic_gates(result, seed)

        # ``result.actions`` is the single source of truth (build_result folds the
        # recorder's actions + the synthesized needinfo into it); model_dump already
        # carries it, so don't overwrite with the raw recorder here — that would drop
        # the bridged needinfo action the apply UI needs.
        payload = result.model_dump(mode="json")
        if over_budget:
            payload["over_budget"] = True

        worker_models = sorted(
            {_full_model(r.get("model")) for r in roles.values() if r.get("model")}
        )
        models.Dossier.upsert(
            uuid,
            payload=payload,
            status="done",
            worker_models=worker_models,
            seed_score=seed_score,
            cost_usd=result.total_cost_usd,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_read_tokens=result.cache_read_tokens,
        )

        row = _verdict_row(result)
        models.Verdict.set(
            uuid,
            verdict=row["verdict"],
            confidence=row["confidence"],
            principal_model=_full_model(principal.get("model", "opus")),
            rationale=row["rationale"],
            evidence=row["evidence"],
            effort=principal.get("effort"),
        )
        models.commit()
        logger.info(
            "agent: %s done (verdict=%s turns=%s cost=$%.4f)",
            uuid, row["verdict"], result.num_turns, result.total_cost_usd or 0.0,
        )
    except Exception as exc:
        logger.error("agent: run_evidence_agent failed for %s", uuid, exc_info=True)
        reason = "{}: {}".format(type(exc).__name__, exc)
        job = _current_job()
        retries_left = (getattr(job, "retries_left", 0) or 0) if job is not None else 0
        if _should_retry(exc) and retries_left > 0:
            # Transient blip with RQ retries remaining: reset to pending so the retry's
            # claim_running can re-take it, stash the reason, and RE-RAISE so RQ requeues
            # this same job. Only when retries are exhausted do we settle on `error`.
            logger.info(
                "agent: %s transient failure (%d retr%s left); requeuing",
                uuid, retries_left, "y" if retries_left == 1 else "ies",
            )
            try:
                models.Dossier.set_status(uuid, "pending", error=reason)
            except Exception:  # pragma: no cover - best-effort
                pass
            raise
        try:
            models.Dossier.set_status(uuid, "error", error=reason)
        except Exception:  # pragma: no cover - best-effort
            pass
        return


def _proto_already_triaged(uuid):
    """Best-effort: has this uuid's proto-signature already been triaged (a dossier for
    any same-``(signatureid, protohash)`` uuid)? Fails OPEN (returns False) on any DB
    error, so a dedup-check hiccup never skips a real crash or aborts a run — the cost
    of a rare duplicate run is far cheaper than silently dropping a crash."""
    try:
        return bool(models.UUID.proto_already_analyzed(uuid))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("agent: proto dedup check failed for %s: %s", uuid, exc)
        return False


def reap_stale_agent_jobs():
    """Re-enqueue crashes whose triage was orphaned — dossier stuck ``running`` past
    job_timeout + buffer because the worker died mid-run (e.g. Heroku restarts dynos
    ~daily / randomly, SIGKILLing before the exception handler can mark ``error``).
    Called periodically by the clock. Self-heals: the orphan is retried instead of
    blocking that crash forever, so its partial cost isn't wasted. A duplicate enqueue
    is cheap — run_evidence_agent skips a crash whose run is genuinely fresh. Best-effort
    (never raises out); returns how many were re-enqueued."""
    # Runs on the clock's APScheduler pool THREAD, which has no Flask app context (the
    # import-time app.app_context().push() is main-thread only), so the DB query would
    # raise "Working outside of application context" — push a context for the DB work.
    try:
        with app.app_context():
            stale_after = config.get_agent_job_timeout() + _STALE_BUFFER_S
            stale_running = models.Dossier.get_stale_running(stale_after)
            # Pending only comes from a retrigger reset; force so proto-dedup doesn't
            # skip the recovery (the operator explicitly asked to re-run this uuid).
            stale_pending = models.Dossier.get_stale_pending(stale_after)
            if not stale_running and not stale_pending:
                return 0
            queue = worker.get_queue(config.get_agent_queue())
            cap = config.get_agent_reap_max_attempts()
            reenqueued: list = []
            gaveup: list = []

            def _reap_one(uuid, force):
                # GIVE-UP CAP: a crash that keeps orphaning (e.g. OOMs on every run)
                # must not be re-enqueued forever (unbounded token burn). Count attempts
                # in the dossier payload; past the cap, fail it VISIBLY (status=error +
                # reason) instead of re-running — an operator can retrigger (which clears
                # the counter). The cap (>=2) still covers a one-off transient orphan.
                n = models.Dossier.bump_reap_attempts(uuid)
                if n > cap:
                    models.Dossier.set_status(
                        uuid, "error",
                        error="reaper gave up after {} re-enqueue attempts (likely OOM"
                              "/stall on every run); retrigger to retry".format(cap),
                    )
                    gaveup.append(uuid)
                    return
                queue.enqueue_call(
                    func=run_evidence_agent,
                    args=(uuid,),
                    kwargs={"force": force},
                    result_ttl=0,
                    timeout=config.get_agent_job_timeout(),
                )
                reenqueued.append(uuid)

            for uuid in stale_running:
                _reap_one(uuid, False)
            for uuid in stale_pending:  # from a retrigger reset; force past proto-dedup
                _reap_one(uuid, True)
            logger.warning(
                "agent: reaper re-enqueued %d, gave up on %d (cap=%d); reenq=[%s] "
                "gaveup=[%s]",
                len(reenqueued), len(gaveup), cap,
                ", ".join(reenqueued), ", ".join(gaveup),
            )
            return len(reenqueued)
    except Exception:  # pragma: no cover - defensive; never break the clock
        logger.error("agent: reap_stale_agent_jobs failed", exc_info=True)
        return 0


def enqueue_agent(uuid, channel=None, force=False):
    """Enqueue one triage run on the dedicated queue. No-op when the agent is disabled,
    when ``channel`` is outside the configured set (nightly only by default), or when
    this uuid's proto-signature has already been triaged (dedup across builds — the
    authoritative skip is in ``run_evidence_agent``; this just avoids queueing a job we
    would drop).

    ``force`` (a tasks-view retrigger of one explicit uuid) bypasses the channel and
    proto-dedup gates and tells ``run_evidence_agent`` to re-run past its own guards."""
    if not config.get_agent_enabled():
        return
    if not force:
        channels = config.get_agent_channels()
        if channel is not None and channels and channel not in channels:
            return
        if config.get_agent_skip_if_existing() and _proto_already_triaged(uuid):
            logger.info("agent: proto-signature already triaged for %s; not enqueuing", uuid)
            return
    queue = worker.get_queue(config.get_agent_queue())
    queue.enqueue_call(
        func=run_evidence_agent,
        args=(uuid,),
        kwargs={"force": force},
        result_ttl=0,
        # RQ's enqueue_call takes `timeout` (not `job_timeout`, which is the high-level
        # enqueue() param) — the wrong kwarg raised TypeError, was swallowed by the
        # caller's try/except, and silently dropped EVERY agent job. The value matters
        # too: without it RQ's 180s default would kill a ~20-min triage mid-run.
        timeout=config.get_agent_job_timeout(),
        # Auto-retry a transient blip (see _should_retry) up to twice, with backoff so a
        # provider/searchfox outage doesn't turn into a retry-storm. run_evidence_agent
        # re-raises only transient failures; a real error fails on the first attempt.
        retry=Retry(max=2, interval=[60, 300]),
    )


def _record_job_id(uuid):
    """Store this run's RQ job id on the dossier (best-effort), so a tasks-view
    retrigger can stop it mid-flight. Only meaningful inside an RQ worker (where
    ``get_current_job`` is set); a no-op in unit tests / direct calls."""
    try:
        from rq import get_current_job

        job = get_current_job()
        if job is not None:
            models.Dossier.set_job_id(uuid, job.id)
    except Exception:  # pragma: no cover - best-effort
        logger.debug("agent: could not record job id for %s", uuid, exc_info=True)


def cancel_running_job(uuid):
    """Best-effort stop of the in-flight RQ job for a ``running`` dossier so a retrigger
    doesn't leave the old (paid) run going. Returns True iff a stop command was issued.
    No-op when the dossier isn't running or its job id wasn't recorded."""
    d = models.Dossier.get_by_uuid(uuid)
    if d is None or d.status != "running":
        return False
    job_id = (d.payload or {}).get("job_id")
    if not job_id:
        return False
    try:
        from rq.command import send_stop_job_command

        send_stop_job_command(worker.conn, job_id)
        logger.info("agent: sent stop for job %s (uuid %s)", job_id, uuid)
        return True
    except Exception as exc:
        logger.warning("agent: could not stop job %s for %s: %s", job_id, uuid, exc)
        return False


def retrigger_agent(uuid, channel=None):
    """Operator action from the tasks view: re-run triage for one uuid, first stopping a
    still-running job so we don't pay for two. Forced past the nightly/proto/skip-existing
    gates since it targets one explicit uuid. Resets the dossier to ``pending`` so the
    re-run still goes through the atomic claim (concurrent retriggers collapse to one
    run). Returns a small status dict."""
    cancelled = cancel_running_job(uuid)
    models.Dossier.reset_for_retrigger(uuid)
    enqueue_agent(uuid, channel=channel, force=True)
    logger.info("agent: retriggered %s (cancelled_running=%s)", uuid, cancelled)
    return {"uuid": uuid, "cancelled": cancelled}
