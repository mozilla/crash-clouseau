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
import threading
from contextlib import contextmanager
from datetime import timedelta

from rq import Retry

from crashclouseau import app, config, db, models, worker
from crashclouseau.agent.errors import MissingHandoffError
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
    if isinstance(exc, MissingHandoffError):
        # Not retried here, and NOT because it is deterministic — the residual family
        # (once the backgrounding cause is gone) is the model fumbling its own
        # serialization, which a re-roll would probably get right. It is that the retry
        # is the expensive way to buy that re-roll: an RQ retry re-runs the whole ~20-min
        # triage at full price immediately, whereas an `error` row costs nothing and is
        # already recoverable — `proto_already_analyzed` counts only `done`, so the
        # (signature, protohash) cluster stays UNPOISONED and the next crash sharing it
        # gets a fresh run. `skip_triage` does treat `error` as terminal for the same
        # uuid, so this trades an immediate paid re-roll for a free one later.
        #
        # The asymmetry is what settles it: if a CLI/SDK change ever re-breaks the
        # handoff wholesale (0.2.131 did, at 60 failures/day), retrying doubles the burn
        # on the exact failure that is hardest to notice.
        #
        # Classified by TYPE, before the substring match, and that ordering is load-
        # bearing: this failure is defined by what happened, not by what the message
        # says. The message is built from the agent's own run and could later be made to
        # quote its final text — and a crash-triage run talks about timeouts, streams and
        # connections for a living, so it would start matching _TRANSIENT_MARKERS by pure
        # coincidence. Test: ``test_missing_handoff_is_not_retryable``.
        return False
    blob = "{}: {}".format(type(exc).__name__, exc).lower()
    return any(m in blob for m in _TRANSIENT_MARKERS)


# Budget for the agent's final text on a failed row, split HEAD + TAIL rather than
# truncated to the first N. The handoff is the last thing the model writes, so a plain
# ``[:8000]`` throws away precisely the evidence: the biggest failure family is a fenced
# block whose JSON is malformed, and in prod those results run 8.5k-15.5k chars with the
# broken block at the very end. The head is worth keeping too — it says which candidate
# the run had converged on before it fumbled the serialization.
_RAW_HEAD, _RAW_TAIL = 2000, 6000


def _elide(text: str) -> str:
    text = text or ""
    if len(text) <= _RAW_HEAD + _RAW_TAIL:
        return text
    dropped = len(text) - _RAW_HEAD - _RAW_TAIL
    return "{}\n\n[... {} chars elided ...]\n\n{}".format(
        text[:_RAW_HEAD], dropped, text[-_RAW_TAIL:]
    )


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
    """Map stackpos -> [inlined function names] from the processed crash's analysed
    thread. The crash's real leaf functions are inlined and otherwise never reach the
    agent. Best-effort across the Socorro shapes (``json_dump.crashing_thread`` or
    ``json_dump.threads[crashing_thread]``); returns {} on any mismatch (never raises).

    Keyed on ``inspector.thread_for_analysis``, which is the SAME selection the frames
    themselves came from. It has to be: on a hang that function deliberately picks the hung
    main thread over the watchdog, and reading inlines from the watchdog would graft one
    thread's inlined leaf names onto another thread's frames by position — a wrong fact
    presented as a grounded one. Socorro's real payload expands ``json_dump.crashing_thread``
    into the full thread OBJECT, so the dict branch below would silently keep doing exactly
    that; the index lookup is tried first for that reason."""
    out: dict = {}
    try:
        from crashclouseau import inspector

        dump = (raw_crash or {}).get("json_dump") or {}
        threads = dump.get("threads")
        thread = None
        idx = inspector.thread_for_analysis(raw_crash or {})
        if isinstance(idx, int) and isinstance(threads, list) and 0 <= idx < len(threads):
            thread = threads[idx]
        if not isinstance(thread, dict):
            thread = dump.get("crashing_thread")
        if not isinstance(thread, dict):
            idx = dump.get("crashing_thread")
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


_AREA_EMAIL_RE = re.compile(r"<([^<>@\s]+@[^<>@\s]+)>")
_AREA_BUG_RE = re.compile(r"\bbug\s*(\d+)", re.I)


def _crashing_area_experts(frames, channel, node, *, max_experts=3, max_files=4):
    """OFF-STACK area-experts done right: BLAME the crashing lines (the crash-frame
    file:line pairs), as-of the build rev when it resolves, and surface the DISTINCT non-bot
    authors who WROTE that code — the people who genuinely worked on the crashing code,
    unlike the undifferentiated pushlog-window authors. Network (hg ``json-annotate``),
    best-effort: returns ``[]`` on any failure. ``node`` should be the pinned build rev so
    the crash line numbers line up with the blamed file (falls back to ``tip``)."""
    from crashclouseau.agent.experts import _is_bot

    targets, files = [], []
    for f in frames or []:
        fn = (f.get("filename") or "").strip()
        line = f.get("line")
        if not fn or not isinstance(line, int) or line <= 0:
            continue
        targets.append((fn, line))
        if fn not in files:
            files.append(fn)
    files = files[:max_files]
    if not files:
        return []
    try:
        from libmozdata.hgmozilla import Annotate
        blamed = Annotate.get(files, channel=channel, node=node or "tip")
    except Exception as exc:  # pragma: no cover - network/defensive
        logger.warning("agent: crashing-area blame failed: %s", exc)
        return []

    experts, seen = [], set()
    for fn, line in targets:
        if len(experts) >= max_experts or fn not in files:
            continue
        ann = (blamed.get(fn) or {}).get("annotate") or []
        # hg json-annotate lists lines in CURRENT-file order, so line N is entry index N-1.
        if not 0 < line <= len(ann):
            continue
        entry = ann[line - 1]
        author = (entry.get("author") or "").strip()
        m = _AREA_EMAIL_RE.search(author)
        email = m.group(1) if m else ""
        name = author.split("<", 1)[0].strip()
        ident = (email or name).lower()
        if not ident or ident in seen or _is_bot(email, name, ""):
            continue
        seen.add(ident)
        mb = _AREA_BUG_RE.search(entry.get("desc") or "")
        experts.append({
            "name": name,
            "email": email,
            "nick": "",
            "node": (entry.get("node") or "")[:12],
            "bug": int(mb.group(1)) if mb else None,
            "reason": "wrote {}:{}".format(fn, line),
        })
    return experts


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
    # ON-STACK: candidates are changesets that SCORED onto the crash frames, so their
    # authors genuinely worked near the crash. OFF-STACK candidates are the undifferentiated
    # first-bad-build pushlog window, ranked mostly by recency when nothing matches the
    # signature — their authors merely "landed a patch near this build", NOT people who
    # worked in the crashing area. So off-stack we instead BLAME the crashing lines
    # (``_crashing_area_experts``, below, once pin_rev is known) to surface who actually
    # wrote the crashing code.
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
    #
    # THIS USED TO BE GUARDED BY `git2hg(build_node)`, AND THAT GUARD NEVER ONCE PASSED. It
    # assumed the build node was a git hash, on the reasoning that Socorro hands those out
    # post-migration — but this node does not come from Socorro. It comes from OUR `nodes`
    # table, which is populated from the hg pushlog, so it is already an hg rev and lando
    # answers `LandoMissingCommit` for it every time. Measured 2026-08-21 against prod: 5 of 5
    # sampled build nodes and 5 of 5 crash-stack frame nodes resolve on hg.mozilla.org, 0 of 10
    # resolve on GitHub, and `pin_rev` was "" on every run since the migration.
    #
    # Nothing broke loudly, which is why it survived: `pin_node` reads an empty build_rev as
    # "use tip" and tip reads succeed, so the tools' leak-free-as-of-the-build guarantee was
    # silently void while their docstrings went on promising it — every blame and source read
    # has been showing post-build code, which is exactly the shape of the refactor+blame false
    # positive. No resolver call is needed here at all: `source`/`patch`/`history` each already
    # do `git2hg(node) or node`, so they accept a node from either forge.
    pin_rev = ""
    build_node = uuid_info.get("node", "")
    if build_node and (not is_offstack or offstack_cfg["pinned"]):
        pin_rev = build_node

    # Off-stack area-experts: blame the crashing lines (pinned to the build rev when it
    # resolves) so "recently worked in this area" names who actually wrote the crashing
    # code, not the pushlog-window authors.
    if is_offstack:
        experts = _crashing_area_experts(frames, channel, pin_rev)

    # When was this signature FIRST seen? One Socorro lookup, done here (once, at seed time)
    # rather than in the gates, because `apply_deterministic_gates` is shared with the OFFLINE
    # eval runner and must stay pure/deterministic — the same arrangement as `prior_hints`.
    # Offline the seed simply lacks these keys and the gate no-ops (a documented fidelity gap).
    # Best-effort: a lookup failure must never break a seed.
    sig_first_seen = None
    sig_first_seen_any = None
    sig_first_seen_ever = None
    sig_report_count = None
    # ONE request serves two gates. `first_seen` is the stale-signature downweight's clock;
    # `total` is how many reports the signature has ever had, which is the bit-flip gate's other
    # half (a flip score on a busy signature means one bad machine among many, not a bad crash).
    # Asked when EITHER gate is on, so disabling one does not silently blind the other.
    if config.get_agent_signature_age()["enabled"] or config.get_agent_bit_flip()["enabled"]:
        try:
            from crashclouseau import sigage

            history = sigage.signature_history(
                info.get("signature", ""),
                info.get("product") or uuid_info.get("product", "") or "Firefox",
                channel,
            )
            sig_first_seen = history["first_seen"]
            sig_first_seen_any = history["first_seen_any"]
            sig_report_count = history["total"]
            # A THIRD request, and deliberately not folded into `signature_history` above: it is
            # a different endpoint (`SignatureFirstDate`, a maintained table rather than a
            # SuperSearch), with its own failure mode, and that function's two queries are
            # batched into one round-trip on a contract this would quietly break. Its answer
            # feeds no gate today — see `sigage.first_seen_ever`.
            sig_first_seen_ever = sigage.first_seen_ever(
                [info.get("signature", "")]).get(info.get("signature", ""))
        except Exception as exc:  # pragma: no cover - defensive; never break a seed
            logger.warning("agent: signature history lookup failed for %s: %s", uuid, exc)
    # The gate needs the CHOSEN candidate's landing date, which is only known after the agent
    # runs — so hand it the map for every seeded candidate. Both candidate builders already
    # carry `pushdate` (DB datetime on-stack, hg [epoch, tz] off-stack), so this costs nothing.
    # A node the agent found some other way (e.g. via blame) is absent and the gate no-ops.
    candidate_pushdates = {
        c["node"]: c.get("pushdate")
        for c in (candidates or []) if c.get("node") and c.get("pushdate") is not None
    }

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
        # Stale-signature downweight (see `_apply_signature_age_gate`): the buildid this
        # signature was FIRST seen in, plus each seeded candidate's landing date, so the gate can
        # ask whether the chosen candidate landed after the crash already existed.
        "signature_first_seen_buildid": sig_first_seen,
        # The same question asked of Socorro's retention-free `SignatureFirstDate` table rather
        # than of a 364-day SuperSearch the ~178-day ES wall truncates. NOT the gate's clock —
        # `sigage.first_seen_ever` documents why substituting it there would clamp eight of the
        # sixteen filings a human acted on. Carried so the NOVELTY question ("is this signature
        # brand new?") has an instrument that cannot read 2017 as three days ago, and so the
        # error in the windowed clock is measurable in prod.
        "signature_first_seen_ever": sig_first_seen_ever,
        # The unfloored all-channel value from the SAME query as `signature_first_seen_buildid`,
        # so it is free. Only used to catch a re-signaturing (`_record_signature_age_facts`): the
        # floored value hides exactly the off-channel reports a rename leaves behind.
        "signature_first_seen_any": sig_first_seen_any,
        # How many reports this signature has EVER had (whole window, not from this build on —
        # `report_bug.fetch_signature_stats` computes that other quantity for the bug comment).
        # ``None`` means the lookup failed and must never read as "a singleton".
        "signature_report_count": sig_report_count,
        "candidate_pushdates": candidate_pushdates,
        # Learned crash archetypes that match this crash (`models.Archetype`): a recurring
        # shape plus what a reviewer told us to check when we see it. Handed to the agent as a
        # HINT — see `triage._archetype_lines`. Empty on any failure.
        "archetypes": _matching_archetypes(info, stack_text, raw_crash),
        # What else the machine that produced this crash has been crashing on — the bad-machine
        # gate's input (`machine.install_history`). Every value None when unknown.
        "install_history": _install_history(raw_crash),
        # What share of this SIGNATURE is hardware error rather than a software defect — the
        # signature-level half of the bit-flip gate (`sigage.hardware_noise`). All None when
        # unknown.
        "hardware_noise": _hardware_noise(info, channel),
    }


def _hardware_noise(info, channel):
    """This signature's hardware-error share, for ``_apply_bit_flip_gate``.

    ONE SuperSearch, alongside the two the seed already makes. Asked only when the gate that
    consumes it is on, and never raises — an unknown share leaves every verdict alone.

    Scoped to the crash's OWN product and channel, which is the whole rule rather than a
    refinement: re-measured 2026-08-21 over the canary's 52 filings, the same thresholds applied
    to an all-products/all-channels rate fire on 8 of them and suppress bug 2062219
    (``nsAtom::IsStatic``, RESOLVED FIXED), because release carries years of failing consumer
    hardware on a hot signature. See ``sigage.hardware_noise``. Passing the channel through
    rather than defaulting also keeps this correct for the non-Firefox products the pipeline is
    being extended to.

    The unknown answer is ``sigage.NO_HARDWARE_NOISE`` and not a second literal: this function
    and ``hardware_noise`` have to return the same KEYS, and a hand-copied key list is how a new
    one ends up missing from the disabled path only."""
    from crashclouseau import sigage

    empty = dict(sigage.NO_HARDWARE_NOISE)
    if not config.get_agent_bit_flip()["enabled"]:
        return empty
    try:
        return sigage.hardware_noise(
            info.get("signature", ""),
            product=info.get("product") or "Firefox",
            channel=channel or "nightly",
        )
    except Exception as exc:                            # pragma: no cover - never break a seed
        logger.warning("agent: hardware-noise lookup failed: %s", exc)
        return empty


def _install_history(raw_crash):
    """This installation's recent crash profile, for ``_apply_bad_machine_gate``.

    Costs ONE SuperSearch (~200ms warm) on a run that already takes ~20 minutes, and needs no
    extra crash fetch: ``install_time`` is already on the processed crash ``build_seed`` holds.
    Bounded at the crash's own ``date_processed`` so the answer is causal rather than hindsight.
    Never raises, and returns all-``None`` when disabled — the gate treats that as unknown."""
    cfg = config.get_agent_bad_machine()
    empty = {"distinct_signatures": None, "distinct_cpus": None,
             "crashes": None, "span_seconds": None}
    if not cfg["enabled"]:
        return empty
    raw = raw_crash or {}
    if not raw.get("install_time"):
        return empty
    try:
        from crashclouseau import machine

        return machine.install_history(
            raw.get("install_time"),
            product=raw.get("product") or "Firefox",
            channel=raw.get("release_channel") or "nightly",
            before=raw.get("date_processed"),
            days=cfg["lookback_days"],
        )
    except Exception as exc:                            # pragma: no cover - never break a seed
        logger.warning("agent: install history lookup failed: %s", exc)
        return empty


def _matching_archetypes(info, stack_text, raw_crash):
    """Archetypes matching this crash, as ``[{slug, title, guidance}, ...]``.

    Matched HERE rather than in the gates because the point is to change how the agent
    investigates, not how its answer is scored — a rule that only ran after the verdict would be
    a critic, and what bug 2062119 needed was a different first move. Never raises: a hint that
    cannot be fetched is a hint we do without.

    THIS DICT IS THE ENTIRE INPUT to every matcher (``models.Archetype.matches`` is a pure
    function of it), so a fact that is not here cannot be a condition — which is how
    `shutdown-singleton` came to assert a cleared-singleton mechanism on 23 crashes per 1051
    nightly reports, 21 of which had aborted deliberately and 3 of which were not in shutdown.
    ``shutdown_progress`` and ``moz_crash_reason`` are read with the SAME accessors that print
    them into the same prompt (``triage._crash_facts``: "Shutdown phase reached" and
    MOZ_CRASH_REASON), so the matcher and the brief cannot disagree about what the crash says.
    Both keys are always PRESENT, None included: ``no_moz_crash_reason`` treats an absent key as
    "nobody looked" and refuses it."""
    try:
        raw = raw_crash or {}
        dump = raw.get("json_dump") or {}
        crash_info = dump.get("crash_info") or {}
        return models.Archetype.for_crash({
            "signature": info.get("signature", ""),
            "stack": stack_text,
            "crash_type": crash_info.get("type") or raw.get("reason") or "",
            "fault_address": crash_info.get("address") or raw.get("address"),
            "shutdown_progress": raw.get("shutdown_progress"),
            "moz_crash_reason": raw.get("moz_crash_reason") or dump.get("moz_crash_reason"),
        })
    except Exception:                                   # pragma: no cover - defensive
        logger.warning("agent: archetype match failed", exc_info=True)
        return []


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


def _is_promotable_bare_lead(verdict):
    """May ``_apply_corroboration_gate`` raise this verdict to ``probable``?

    A ``lead`` below ``probable`` AND at/above the second opinion's ``min_boost_confidence``
    (50). THE FLOOR IS NOT NEW — it is the one ``_fold_second_opinion`` has always applied,
    whose written justification describes THIS gate word for word: "a boost would jump two
    rungs (low -> probable, p_worth 0.50 -> 0.72) ... the corroborate side was never part of
    the calibration fit". The corroboration gate is the pipeline's only PROMOTING gate and it
    lands a bare lead on exactly ``autofile.min_confidence`` (70, shipped in
    config/global.json), so before this a lead/low went 25 -> 70 deterministically, straight
    through the filing bar, on a signal that was never in the fit at all. Two promoters, one
    argument, one floor.

    COST, counted on corpus_ship/results.jsonl (90 verdicts): the promotable population is 9
    — 7 lead/medium and 2 lead/low — so 2 verdicts sit below the floor. That is the
    population AT RISK, not a measured loss: no fixture in any committed corpus carries a
    fault address (``fault_address_offset_match`` appears 0 times in
    results_gate_facts.jsonl), so the observed delta on that corpus is 0 of 90 and 2 is the
    ceiling. It cannot be more, because ``Verdict._consistency_rule`` clamps a lead at
    ``high`` back to ``probable``, leaving ``low`` (25) and ``medium`` (50) as the only
    bare-lead rungs.

    WHAT IT MUST NOT EAT: this is a floor on the PROMOTION, never on the flag.
    ``_corroborations`` still records ``fault_address_offset_match`` /
    ``prior_signature_match`` on a lead/low, the UI and the persisted dossier still show
    them, and a lead/low is still REPORTED (any lead is) and still buys a second opinion
    (``second_opinion.min_confidence`` is 25). All the floor withholds is the two-rung jump
    onto the autofile bar."""
    if verdict is None or verdict.decision != Decision.lead:
        return False
    if verdict.confidence == Confidence.probable:
        return False
    rung = int(round(CONFIDENCE_SCORE.get(verdict.confidence, 0.0) * 100))
    return rung >= config.get_agent_second_opinion()["min_boost_confidence"]


def _corroborations(dossier, seed):
    """Deterministic, non-LLM corroboration flags for a dossier. Currently the
    fault-address<->struct-field-offset match: a NON-ZERO small faulting address N that
    equals the byte offset of a field cited via a ``struct_layout`` citation is a
    machine-verifiable null-deref of THAT field (the ab3238a5 0x8==mLength signal).

    THE MATCH IS ONLY DETERMINISTIC IF SOMEONE CHECKS IT, and until this landed nobody did.
    ``triage._crash_facts`` prints ``Fault address: 0xN`` straight into the prompt and
    ``agent/roles.py`` tells the data-flow tracer to answer it with a ``struct_layout``
    citation naming the field at byte N — so ``cit.offset == fault`` was
    agreement-by-construction: the model was shown the number it had to reproduce, and the
    gate's docstring nonetheless called it "a signal the model cannot fabricate". It now is
    one: ``_resolve_struct_layout`` re-derives the layout from searchfox and this reads the
    answer. FAIL CLOSED — no verification, no flag (see that function for why the promoting
    gate takes the opposite failure direction from the suppressing ones).

    STRUCTURAL LIMIT, recorded because it bounds what the flag can ever mean: this loop tests
    only ``cit.offset == fault``, so nothing here links the matched field to the CANDIDATE.
    Verified or not, the match is a fact about the CRASH ("a null-deref of field X of class
    T") — which the signature usually already states — and says nothing about the changeset.
    Live proof: ``mozilla::dom::ThreadSafeWorkerRef::Private`` puts ``mRef`` at offset 8 and
    is 48.9% 0x8 all-channel (116/237 since 2026-01-01); 36 of its 60 nightly reports since
    2026-07-01 fault at 0x8 across 9 DISTINCT buildids = 9 distinct pushlog windows = 9
    distinct candidate sets promoted identically by one offset fact. The sibling flag below,
    ``prior_signature_match``, does tie to the candidate (``cand.bug in pbugs``) and carries
    a focus guard; this one does not.

    DO NOT CLOSE THAT GAP BY REQUIRING THE CANDIDATE'S DIFF TO TOUCH THE MATCHED
    STRUCT/FIELD — measured and refuted on the motivating case. Bug 2053521 ("crash at null
    [@ ComputeKeyHash]", VERIFIED/FIXED, ``regressed_by`` 2053211) is a true 0x8 ==
    ``nsTStringRepr::mLength`` match, and its real regressor d86be929745b touches
    dom/base/Element.cpp, dom/base/Element.h and dom/html/nsGenericHTMLElement.cpp with ZERO
    occurrences of ``mLength`` or ``nsTString``. That rule would suppress the one
    Bugzilla-verified case the gate exists for. The open question is DISCRIMINATIVENESS
    instead (is this offset the signature's modal fault?), and it needs a panel: the two
    known points are that same bug's signature at 86.7% 0x0 (so 0x8 was informative there)
    and ``ThreadSafeWorkerRef::Private`` at 48.9% 0x8 (so 0x8 is the signature restating
    itself). Those are the panel's first two MEMBERS, not the fit — do not read a threshold
    off them.

    Never raises."""
    flags: dict = {}
    try:
        fault = _fault_address((seed or {}).get("raw_crash"))
        if fault is not None and 0 < fault <= _MAX_FIELD_FAULT:
            layout = (seed or {}).get("struct_layout") or {}
            confirmed = {
                (e.get("type"), e.get("field"), e.get("offset"))
                for e in (layout.get("verified") or [])
            }
            for cit in _iter_dossier_citations(dossier):
                if not (isinstance(cit, StructLayoutCitation) and cit.offset == fault):
                    continue
                if (cit.type_name, cit.field, cit.offset) not in confirmed:
                    continue  # fail closed: unverified is not corroboration
                flags["fault_address_offset_match"] = True
                flags["fault_offset"] = fault
                flags["fault_field"] = cit.field
                flags["fault_type"] = cit.type_name
                break
            if layout and not flags.get("fault_address_offset_match"):
                # COUNTABILITY. A citation the model made and searchfox would not back is the
                # exact event this verification exists to find, and an absent flag cannot be
                # told apart from "the model cited no layout at all" in the persisted
                # dossier. ``refuted`` vs ``unresolved`` also separates "the model invented an
                # offset" from "searchfox could not answer", which is the difference between
                # a model problem and an infrastructure problem.
                flags["fault_offset_unverified"] = layout.get("status") or "unresolved"
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
    ``probable`` is unchanged. Returns the flags. Mutates ``dossier`` in place; never raises.

    TWO CORRECTIONS THIS GATE CARRIED FOR A LONG TIME, both now fixed in the helpers it calls.
    (a) "A signal the model cannot fabricate" was not true: nothing verified a
    ``struct_layout`` citation against searchfox while the prompt handed the model the fault
    address to match — see ``_resolve_struct_layout``. (b) Promoting had NO rung floor while
    ``_fold_second_opinion`` refused the identical two-rung jump — see
    ``_is_promotable_bare_lead``. The mitigation that WAS already right stays:
    ``_will_corroboration_promote`` (and it mirrors the same predicate) makes sure a
    corroboration-rescued lead still buys the blind second opinion, so a medium-confidence SO
    refutation can clamp it back."""
    if dossier is None:
        return {}
    flags = _corroborations(dossier, seed)
    # MERGE, don't replace: the off-stack SF-3 / exposer gates run BEFORE this one and have
    # already stashed flags on the dossier (call_path_verified / exposer_*). Overwriting
    # dossier.corroborations here would silently drop them from the persisted payload/UI.
    dossier.corroborations = {**(dossier.corroborations or {}), **flags}
    v = dossier.verdict
    is_bare_lead = _is_promotable_bare_lead(v)
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


# Whether a DELIBERATE suppression may be UNDONE by an independent second-opinion
# corroboration, keyed by the flag the suppressing gate writes. `_record_suppression` refuses to
# write a flag that is not declared here, so a new gate has to CHOOSE: before this table the
# fold tested ONE hard-coded flag name and every other suppression silently inherited
# "boostable".
#
# The value is an argument about the AXIS the gate ruled on, never a preference:
#
# * ``block`` — the gate found the changeset RELATED BUT NOT THE CAUSE (SF-3's missing call
#   path, the exposer classifier). An SO ``corroborates`` says "this changeset can plausibly
#   cause this crash", i.e. relatedness — the one thing already granted — so it carries no
#   information on the axis the gate ruled on. That is what `ce6b8fc` blocked, and an exposer is
#   the case in point: reverting it DOES stop the crash, and it must still stay a medium lead
#   rather than become a probable cause. SINCE 2026-08-21 THE EXPOSER LANDS ON ``probable``
#   ITSELF (`_downgrade_to_lead_or_abstain`'s `rung`), so on the DIRECT path this entry has
#   nothing to block: the fold's `is_bare_lead` test is False at `probable` and never consults
#   the table. DO NOT READ THAT AS DEAD. `_apply_signature_age_gate` runs between the exposer
#   and the fold and clamps `probable` -> `medium`; on that path the lead is bare again and
#   THIS entry — not `stale_signature_clamped`, which is `allow` — is what refuses the boost,
#   because `_so_boost_blocked_by` returns the first `block` flag present. It is also written
#   by SF-3, by the absent-thread gate and by the SO's own refutation, all three of which
#   still land on `medium`.
# * ``allow`` — the gate ruled on a DIFFERENT axis from the one the SO reviews, so a blind
#   agreement is genuinely new evidence. `_apply_signature_age_gate` rules on ORIGIN ("landing
#   late disproves ORIGIN, not relevance", its own docstring); the SO reviews the MECHANISM. And
#   the agreement is independent evidence only BECAUSE `_maybe_run_second_opinion` runs before
#   these gates and never sees the clamp — telling it would destroy the very property that
#   licenses ``allow``.
#
# WIDENING THE BLOCK TO "ANY SUPPRESSION" IS REFUTED, cost measured on the 11 stale filings of
# 2026-08: it loses 3 FIXED bugs (2 topcrash, all three with a HUMAN-set `regressed_by` naming
# exactly the changeset Clouseau named — bugs 2062219, 2061960, 2063809) plus 1 ASSIGNED
# (2061127), and gains 6 low-value filings avoided; the stale cohort is acted on MORE often
# (4/11 = 36%) than the fresh one (9/39 = 23%). Do not re-derive it, and do not "fail safe" here.
#
# Only a gate running BEFORE `_fold_second_opinion` in `apply_deterministic_gates` can be seen
# here. The nine suppressions BELOW the fold (backed-out, is-backout x2, bit-flip, broken-cpu,
# hardware-noise, bad-machine, absent-thread, compiled-out) are unreachable by construction and
# are deliberately absent rather than declared ``block``.
#
# UNEXAMINED COINCIDENCE, recorded so it is not mistaken for a fit: `min_boost_confidence` (50)
# and the rung the stale clamp lands a `probable` lead on (`medium`, 50) are the same number by
# accident — neither was fit against the other, and a one-rung move in either would end the
# clamp-then-boost round trip outright. n=9 clamps / 3 reversals is enough to notice and nowhere
# near enough to tune; re-measure at n>=30 before touching either.
_SO_BOOST_POLICY = {
    "downgraded_from_strong": "block",
    "stale_signature_clamped": "allow",
}


def _record_suppression(dossier, flag):
    """Record that a deterministic gate deliberately moved this verdict DOWN, under a flag whose
    second-opinion boost policy is declared in ``_SO_BOOST_POLICY``.

    Raises ``KeyError`` for an undeclared flag, and that raise is the point of the function: it
    is the only thing that makes the author of the NEXT suppressing gate pick an axis instead of
    inheriting "boostable" from a flag name they never read. It cannot fire in production — both
    call sites pass a literal and both are pinned by tests — so it is a development-time failure
    by construction, which is what leaves the two calling gates their "never raises".

    Mutates ``dossier`` in place."""
    if flag not in _SO_BOOST_POLICY:
        raise KeyError(
            "{}: a deliberate suppression must declare in _SO_BOOST_POLICY whether an "
            "independent second-opinion corroboration may undo it — 'block' when the gate ruled "
            "on the same axis the second opinion reviews, 'allow' when it did not".format(flag))
    dossier.corroborations = {**(dossier.corroborations or {}), flag: True}


def _so_boost_blocked_by(corroborations):
    """The suppression flag that forbids a corroborating second opinion from re-inflating this
    lead, or ``None``.

    Replaces a hard-coded ``downgraded_from_strong`` test and is behaviour-identical to it
    today, exactly one entry in ``_SO_BOOST_POLICY`` being a ``block``. That identity is measured
    rather than asserted: across the 1996 persisted prod dossiers of 2026-07-06 → 2026-08-05
    ``downgraded_from_strong`` appears on 2, and on BOTH the second opinion REFUTED (the other
    branch of the fold) — so the guard blocked a boost 0 times and there is no live behaviour for
    this refactor to change. Any difference you can produce is a bug in it.

    What the flag name hid: the guard read as "a deliberately suppressed lead stays suppressed"
    but named only ONE suppression, so `stale_signature_clamped` — added three days later, and
    the only other suppression that runs before the fold — escaped it silently. It is now
    ``allow`` on the argument in the table, which is a decision rather than an omission."""
    c = corroborations or {}
    return next((f for f, p in _SO_BOOST_POLICY.items() if p == "block" and c.get(f)), None)


def _downgrade_to_lead_or_abstain(dossier, seed, reason, abstain_reason,
                                  rung=Confidence.medium):
    """Shared downgrade used by the precision gates: turn a ``strong-evidence`` verdict into
    a soft ``lead`` when a cited anchor (candidate/hunk/edge) still stands, else ``abstain``.
    Mirrors ``_skeptic_veto``'s reconstruction (soft, non-accusatory draft). Mutates
    ``dossier`` in place.

    ``rung`` IS HOW FAR DOWN, WHICH IS A DIFFERENT QUESTION FROM WHETHER. The default
    ``medium`` (50) is what SF-3, a confident second-opinion refutation and the absent-thread
    gate have always done, and it sits BELOW ``autofile.min_confidence`` (70, shipped in
    config/global.json) -- so for those three "downgrade to a soft lead" also means "file
    nothing", which is intended: each of them found the MECHANISM broken. The exposer
    classifier passes ``probable`` (70) because it found the opposite, and its docstring
    carries the evidence. Neither number is fitted here: they are the two rungs the rest of
    the pipeline already uses -- ``_apply_corroboration_gate`` promotes a bare lead to exactly
    ``probable``, and 70 is the shipped filing floor.

    THE RUNG SAYS NOTHING ABOUT ``_SO_BOOST_POLICY``. That table answers "may an independent
    corroboration UNDO this suppression?" and stays ``block`` for ``downgraded_from_strong``
    on every caller, this one included -- an SO ``corroborates`` grants relatedness, which is
    the one thing an exposer already has. At ``probable`` the block has nothing left to block
    ON THE DIRECT PATH: ``_fold_second_opinion``'s own ``is_bare_lead`` test (confidence
    strictly below ``probable``) is already False, so the branch that consults
    ``_so_boost_blocked_by`` is never taken. It is NOT inert in general, and that is worth
    knowing before anyone deletes the entry: ``_apply_signature_age_gate`` runs between this
    call and the fold and clamps ``probable`` -> ``medium``, and on THAT path the lead is bare
    again and this entry is what refuses the boost (verified end to end; see
    ``test_the_exposer_block_is_live_again_after_the_stale_clamp``).

    THE REFUTE DIRECTION CHANGES SHAPE, WHICH THE RUNG MOVE DOES NOT ADVERTISE. A medium/high
    blind refutation clamps a lead ABOVE ``medium`` back to ``medium`` -- so at ``probable`` an
    exposer lead survives as an unfiled lead@50. At ``medium`` there is no lower band and the
    fold ABSTAINS the lead instead, which is what a poison exposer used to get. So the
    refutation still bites (both outcomes are under ``autofile.min_confidence``, i.e. nothing
    is filed either way) but it bites one notch less hard: the crash is now still REPORTED
    with a soft lead where it used to vanish. That is the pivot's own preference (system.md:
    "prefer a cited ``lead`` over an ``abstain`` whenever something would genuinely help"),
    stated here rather than discovered later."""
    v = dossier.verdict
    if dossier._has_lead_anchor():
        dossier.verdict = Verdict(
            decision=Decision.lead,
            confidence=rung,
            needinfo_draft=dossier._soft_lead_draft(),
            mechanism=v.mechanism,
            consistency=v.consistency,
        )
        # Mark that this lead is a PRECISION-DOWNGRADE of a strong-evidence verdict (SF-3 /
        # exposer / a confident second-opinion refutation) and, through `_SO_BOOST_POLICY`,
        # declare that a corroborating second opinion may NOT undo it: this gate ruled on
        # RELATEDNESS, which is the one thing an SO `corroborates` already grants — an exposer IS
        # "related" (reverting it stops the crash), and relatedness is not causation. WHAT THAT
        # BUYS NOW DIFFERS BY CALLER: at the default `medium` rung the flag keeps a suppressed
        # lead suppressed, while the exposer lands on `probable` (see `rung` above) and so is
        # not a bare lead for the fold to boost at all — until `_apply_signature_age_gate`
        # clamps it back to `medium`, at which point this flag is what refuses the boost.
        # The fold used to test this flag BY NAME, which is why the
        # stale-signature clamp escaped it; it now reads the table, so a gate that ruled on
        # another axis is no longer forced into this gate's answer.
        _record_suppression(dossier, "downgraded_from_strong")
        logger.info("agent: %s -> lead/%s for %s", reason, rung.value,
                    (seed or {}).get("uuid"))
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


# Freed / poisoned / uninitialized memory sentinel BYTES. A fault address that is (almost) a
# run of one of these is a use-after-free / uninitialized read — a latent-bug pattern a
# candidate often merely EXPOSES rather than introduces.
#
# THE PANEL is tests/poison/poison_fault_panel.json: a CENSUS, not a sample, of every
# Firefox-nightly crash report of 2026-05-24..2026-08-20 — 162,485 reports over 5,234
# signatures, 158,285 of them with a parseable fault address — reduced to the 150 distinct
# addresses that pass `_looks_poison`'s own dominance test. No other address in the census can
# fire for ANY byte set, so the per-byte counts are exact. tests/test_exposer_poison.py
# recomputes every number in this comment from that file.
#
# THE SET, WITH ITS FIREFOX PROVENANCE and its 89-day fire count (js/src/util/Poison.h and
# memory/build/mozjemalloc.h on firefox-main, read 2026-08-21):
#   0xe5  mozjemalloc `kAllocPoison`, mozjemalloc.h:143       2,913 reports / 99 signatures
#   0xcc  JS_SCOPE_DATA_TRAILING_NAMES_PATTERN, Poison.h:76      58 reports / 6 signatures
#   0x4b  JS_SWEPT_TENURED_PATTERN, Poison.h:60                  35 reports / 13 signatures
#         over 22 build days, 24 of them `0x4b4b4b4b4b4b4b4b`, mostly `JS::Value::isGCThing`
#   0x2b  JS_SWEPT_NURSERY_PATTERN, Poison.h:56                   0
#   0xab  JS_FREED_BUFFER_PATTERN, Poison.h:66                    0
#   0xcd  JS_LIFO_UNDEFINED_PATTERN, Poison.h:71                  0
#   0xe4  mozjemalloc `kAllocJunk`, mozjemalloc.h:146             0
#   0xdd  MOZ frame poison — no in-tree literal to cite: `mozPoisonValue()` is computed at
#         runtime (mfbt/Poison.h), so 0xdd is the conventional value, not a constant   0
#   0x5a  upstream jemalloc junk-on-FREE, and a common uninitialized fill elsewhere     0
#   0xfd  MSVC debug heap fill                                                          0
#   0xbe 0xfb  ASan redzone / freed                                                     0
#
# THE NULL RESULT, recorded so the next session does not re-derive it: TEN of the twelve bytes
# this set shipped with fire ZERO times over 162,485 reports. That is NOT a reason to delete
# them — a byte that never fires costs no precision — so the set is cut on PROVENANCE instead,
# and exactly one byte fails that test. 0xA5 is gone: it is upstream jemalloc's alloc-junk
# byte, Firefox ships mozjemalloc whose constants are 0xe5/0xe4, and it appears in no Firefox
# poison header (the only in-tree hit is a `fillValue` in mfbt/tests/TestEndian.cpp). Its own
# 95% rule-of-three upper bound was 1 fire in 52,761 reports. The comment this replaces called
# 0x5a "jemalloc junk-on-alloc", which was wrong twice over, and never mentioned 0xA5 or 0x2B
# at all — both had been in the frozenset with no stated reason since `c89dc9b`.
#
# WHY THE SET IS NOT IMPORTED FROM js/src/util/Poison.h — the obvious fix, REFUTED by the same
# panel. That header also defines `JS_OOB_PARSE_NODE_PATTERN = 0xFF` (Poison.h:70), and 0xFF is
# the second most common dominant byte in the census: 1,001 reports, essentially all of them
# small negatives (`vk_common_DeviceWaitIdle` @0xffffffffffffffcc 350, `ff_decode_frame_props`
# @0xffffffffffffffff 181, `Pickle::BeginWrite` 49, `EnterJit` 45) — -1 and friends, not
# poison. Importing the header mechanically would re-rate 1,001 reports' worth of crashes on a
# sentinel meaning "no value"; 0x00 (219 reports) is the same trap from the other end. "Add
# every byte that dominates" dies on those two as well.
#
# WHAT ADDING 0x4B MUST NOT EAT, and why the rung change above had to land FIRST: at the old
# rung a poison hit was a suppression, so adding a byte SUBTRACTED filings. On the 52 bugs
# filed since 2026-08-05, 0 addresses fire under the old set and 0 under this one — the
# widening costs that panel nothing — while `0x4b4b4b4b4b4b4b4b`, which files as `culprit@85`
# today, becomes a filed `lead@70` carrying the exposer note instead of the silence it would
# have become in the other order. Study bug 1980730 is that case with a known answer: 0x4b4b,
# `exposer_not_cause=False`, VERIFIED FIXED, regressed_by 1960461 accepted. The byte is also
# the one spike/STRATEGY_REPORT.md:450 names outright ("0x4b4b4b4b swept-tenured GC-UAF")
# while the set omitted it. 0x49 / 0xED / 0xDB are its Poison.h siblings and are deliberately
# NOT added, and the reason is asymmetric rather than principled: the census counts them
# EXACTLY (3, 3 and 1 over 89 days — this is a census, so those are not estimates), and adding
# a byte that fires re-rates real verdicts while adding one that never fires costs nothing. So
# the actual rule is "in-tree provenance, and either zero measured fires or a standing pattern
# worth the re-rating (0x4B: 35 reports / 13 signatures / 22 build days)"; these three sit in
# the gap and are left out until something asks for them. Said plainly because "the set is cut
# on provenance" alone would not predict their exclusion.
_POISON_BYTES = frozenset({0xE5, 0xE4, 0x5A, 0xDD, 0xCD, 0xCC, 0xFD, 0xAB, 0xBE, 0xFB, 0x2B, 0x4B})


def _looks_poison(fault) -> bool:
    """True when the fault address looks like freed/poisoned/uninitialized memory: its
    bytes are dominated by one known-poison byte (allowing one off-byte for an offset into
    the poisoned object). Small addresses are handled by the field-offset corroboration,
    so require > one page here.

    Fires on 2,971 of the 158,285 nightly reports with a parseable address over the 89 days
    of tests/poison/poison_fault_panel.json (1.88%), all of them 0xE5 (2,913) or 0xCC (58);
    with 0x4B it is 3,006. The two-byte guard below is why a 0x1001..0xffff fault almost
    never qualifies: over those 89 days exactly ONE two-byte address in the whole census has
    the 0xXYXY shape the guard would accept, and it is 0xA4A4 (one report,
    `CContext::UMQueryPS_Shader_`) — a byte in no version of the set."""
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
    """Flag — and, on a STRONG signal, re-rate as a ``lead`` — an 'exposer, not cause'
    verdict: a changeset that EXPOSED a pre-existing latent bug (86 of the 289-bug study's
    fixed regressions, 30%) rather than introducing it. Only LIVE-computable signals are
    used; the study's strongest discriminator ('fix diff disjoint from the regressor diff')
    needs the LANDED FIX, which does not exist at triage time, so it is deliberately kept OUT
    of here (offline eval only). Sets ``corroborations['exposer_*']`` on ANY verdict -- until
    2026-08-21 that was WRITE-ONLY (this docstring used to say "for the UI"; crashstack.html
    names 16 corroboration keys explicitly and none of them is ``exposer_*``), so the flags
    reached nothing but the persisted JSONB. ``report_bug.build_exposer_note`` is now their
    first reader; on a strong signal (a freed/poisoned
    fault address = a UAF the candidate most likely just exposed) it turns ``strong-evidence``
    into a ``lead`` at ``probable``, so a weak hint never demotes a genuine culprit. Mutates
    in place; never raises.

    IT LANDS ON ``probable`` (70), NOT ``medium`` (50), AND THAT IS THE FIX. Until 2026-08-21
    this gate hard-set ``medium``, which is under ``autofile.min_confidence`` (70) — so "soft
    downgrade" was in fact a silent SUPPRESSION: no bug, no needinfo, no ``Feedback`` row,
    nothing anyone could disagree with. Both re-inflation paths were closed by construction
    (``_apply_corroboration_gate`` needs ``0 < fault <= _MAX_FIELD_FAULT``, which a poison
    address never is; the SO boost is blocked by the very ``downgraded_from_strong`` this call
    writes), so 85 -> 50 was terminal. Reproduced end to end: ``strong-evidence/high`` + fault
    ``0xe5e5e5e5e5e5e5ed`` -> ``lead/medium`` -> row ``lead@50`` -> below the filing floor.

    WHAT THE SUPPRESSION RESTED ON, AND WHY IT NO LONGER HOLDS. spike/STRATEGY_REPORT.md
    splits its own finding BY GOAL at :143-144 — "Goal = nominate ``regressed_by`` / needinfo
    the author -> the exposer IS the right answer" vs "Goal = localize the defect/fix -> the
    exposer is *wrong* ~30% of the time". This gate shipped 2026-07-22 13:02 under the second
    goal; the project pivoted to the first NINE hours later (``b9485c3``, 22:12 the same day;
    system.md:1-11 now
    reads "The real deliverable is a USEFUL LEAD") and the filer WRITES ``regressed_by``
    (``bugzilla_apply._link_regressed_by``). Checked against BMO rather than inferred: of the
    study's 86 exposers, 84 (98%) carry an accepted ``regressed_by``, statistically identical
    to 196/203 (97%) of its non-exposers. And :146 prescribes this exact rung — "never
    auto-upgrade a proximity hit to 'culprit' when exposer corroborators fire — emit ``lead``
    + needinfo the owner". ``Decision.lead`` is preserved, so ``_verdict_row`` can never
    publish ``culprit``; only the rung moves.

    THE DISCRIMINATOR HAS NO MEASURED VALIDITY, which is why the answer is "file it as a
    lead", not "believe it". In the study a poison-byte literal appears in 1/86 exposers vs
    4/203 non-exposers (Fisher p=1.00). Over the 104 signatures that produced a poison fault
    in the 89-day nightly census vs 104 volume-matched non-poison controls from the same
    census (median volume 4 vs 4), an accepted ``regressed_by`` lands on 16 vs 13 (p=0.69) and
    FIXED on 25 vs 18 (p=0.30) — a poison fault predicts a filing's outcome no better than the
    matched control does. Panel: tests/poison/poison_fault_panel.json.

    WHAT IT MUST NOT EAT — the four study bugs whose evidence quotes a poison literal and
    whose named regressor was ACCEPTED as the cause: 2000421 (0xe5e5e5, the study's own note
    is "the regressor touched the exact crashing function, nsWindow::Destroy, frame 1", FIXED,
    regressed_by 1998657), 1991950 (0xe5e5, VERIFIED FIXED, 1979546), 1980730 (0x4b4b,
    VERIFIED FIXED, 1960461) and 2000425 (0xe5e5, FIXED, 1998657). All four are
    ``exposer_not_cause=False`` in the study's labels; all four are what a 50-rung clamp files
    nothing about. The fifth, 2041907, IS an exposer and is FIXED with regressed_by 2011326
    accepted anyway — 5/5 wanted the nomination.

    WHAT IT COSTS, said honestly. On the only outcome panel that exists — the 52 bugs filed
    since 2026-08-05 — 0 of the 52 analysed reports carried a poison fault and 0 of the 8 bad
    outcomes (7 INVALID + 1 WORKSFORME) did either, so this gate has ZERO measured saves. The
    suppressed channel is invisible (a suppression reaches no ``Feedback`` row), so that is
    "no evidence of a save", not "no save". Against it, the census rate puts the change at AT
    MOST about one extra filing per 16 days (the length of that panel) — and ~90% of that
    expectation is ONE third-party signature, ``libvulkan_radeon.so``. The census says why:
    1,668 of its 2,913 0xE5 reports sit on a single address, ``0xe5e5e5e5e5e5e5b1``, across
    three AMD/Mesa Vulkan-driver signatures (``libvulkan_radeon.so`` 896, ``<name omitted> |
    vk_common_DeviceWaitIdle`` 739, ``vk_queue_to_handle`` 33). Excluding third-party modules
    the expectation is ~0.09 filings per 16 days — roughly one a year. Each carries
    ``build_exposer_note``'s paragraph.

    NOT GATED ON ``seed['is_offstack']``, deliberately, although the rule was born off-stack
    (``c89dc9b`` 13:02, extended to all crashes by ``cda321e`` 15:12 the same day).
    ``offstack.enabled`` is false in config/global.json, so ``build_seed`` returns None for
    every off-stack crash and ``is_offstack`` is False on 100% of prod runs: gating the rung
    on it would ship a change that reaches nothing. That is precisely the defect the softening
    prompt had — it lived under system.md's ``## Off-stack mode`` heading and so reached 0
    runs while this gate reached all of them — and it is unconditional now.

    NOT CALIBRATED, and worth saying out loud: rung 70 maps to p_worth 0.9714, the same value
    as 85. No committed fixture carries a fault address (0 of 1,257 ``processed_crash.json``
    files), so this gate has never fired in any eval or calibration run and the 70 bucket was
    fit without one exposer-downgraded verdict in it. That bucket was already unvalidated for
    gate-promoted leads (see ``_is_promotable_bare_lead``); this adds a second such population
    to it, and ``build_exposer_note`` is what tells the reader."""
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
                rung=Confidence.probable,
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


def _fold_second_opinion(dossier, second_opinion, seed, status=None):
    """Fold a blind second-opinion (#SO) into a REPORTED verdict, ASYMMETRICALLY
    (precision-first, recall-safe). ALWAYS stores it on the dossier (a measurement rides
    every dossier it ran on, even one the other gates then abstained); the confidence band
    only MOVES in VERIFY mode, where ``corroborates`` is a real signal:

    * corroborates AND the SO is itself at least MEDIUM-confident -> "independently
      confirmed": raise a bare ``lead`` (below ``probable``) to ``probable``, mirroring the
      deterministic corroboration gate — an independent blind agreement is a signal the
      first pipeline cannot have fabricated. Never touches strong-evidence or a lead already
      at/above ``probable`` (already maxed for a lead). Flagged for the UI.
    * a refutation the SO is itself at least MEDIUM-confident about -> the confident-wrong
      catch, SYMMETRIC with the boost above (it used to demand ``high``, which made a medium
      refutation a total no-op while a medium agreement moved a whole band): clamp a ``lead``
      above ``medium`` down to a plain ``medium`` lead, and ABSTAIN a lead at/below ``medium``,
      where there is no lower band left — the weakest leads were otherwise the ones an
      independent refutation could never touch. Downgrading ``strong-evidence`` still requires
      ``high``: that verdict carries a fully cited, skeptic-survived chain and is the most
      consequential thing to move, so the biggest hammer keeps the higher bar.
    * anything else (unsure / a LOW-confidence refutation / MECHANISM mode, where
      ``corroborates`` is ``None`` and the two mechanisms aren't auto-compared) -> leave the
      verdict untouched; the independent read is still surfaced in the panel.

    Mutates ``dossier`` in place; never raises. ``second_opinion`` is ``None`` (a no-op fold)
    for the offline eval runner and for a disabled/skipped pass — but the assignment below
    still runs then, AUTHORITATIVELY clearing any ``second_opinion`` the primary model may have
    injected into its own handoff JSON (it is a defined ``Dossier`` field, so pydantic would
    otherwise populate it). The SO field is thus only ever set HERE, never trusted from the
    model — preserving the blind-independent guarantee.

    ``status`` is ``_maybe_run_second_opinion``'s outcome string (``ok`` / ``failed`` /
    ``skipped_*``), stored alongside so a null ``second_opinion`` is DIAGNOSABLE: the pass is
    best-effort, so without it a prod-only break looks exactly like an ineligible verdict.
    ``None`` (the eval runner's default) records "the gate never ran"."""
    if dossier is None:
        return
    # Authoritative: overwrite (with the real SO or None) so a model-injected value can never
    # masquerade as an independent second opinion. Must precede every early return below.
    dossier.second_opinion = second_opinion
    dossier.second_opinion_status = status
    so = second_opinion
    v = dossier.verdict
    if so is None or v is None:
        return
    # Only a REPORTED verify-mode SO moves the band. VERIFY mode always implies a candidate
    # anchor (so these gates downgrade to lead, never abstain) — the abstain guard is defensive
    # against future refactors. MECHANISM mode carries no corroborate/refute signal.
    if v.decision == Decision.abstain or so.mode != "verify":
        return
    conf = (so.confidence or "").strip().lower()
    flags: dict = {}
    if so.corroborates is True and conf in ("medium", "high"):
        flags["second_opinion_corroborated"] = True
        blocked_by = _so_boost_blocked_by(dossier.corroborations)
        is_bare_lead = v.decision == Decision.lead and CONFIDENCE_SCORE.get(v.confidence, 0.0) < CONFIDENCE_SCORE[Confidence.probable]
        # Measuring a lead does NOT license re-ranking it: below `min_boost_confidence` a boost
        # would jump two rungs (low -> probable, p_worth 0.50 -> 0.72) on the weaker of the SO's
        # two signals — the corroborate side was never part of the calibration fit, while a
        # refutation is measured at specificity 1.00. Record the agreement, leave the band alone.
        # (This floor is NOT what keeps the bottom rung symmetric any more: a refutation there
        # now abstains the lead. Promote conservatively, suppress readily — see `config.py`.)
        rung = int(round(CONFIDENCE_SCORE.get(v.confidence, 0.0) * 100))
        boostable = rung >= config.get_agent_second_opinion()["min_boost_confidence"]
        if is_bare_lead and not boostable:
            logger.info(
                "agent: second-opinion corroborated but lead rung %s is below the boost floor; "
                "recording agreement WITHOUT raising the band for %s",
                rung, (seed or {}).get("uuid"),
            )
        elif is_bare_lead and blocked_by is None:
            dossier.verdict = v.model_copy(update={"confidence": Confidence.probable})
            # Records that the boost was APPLIED, which `second_opinion_corroborated` does not:
            # that flag marks the SO's OPINION and is set even when the band never moved. Without
            # this, "the SO boosted medium->probable" and "the corroboration gate boosted it while
            # the SO's agreement was inert" persist identically, and `raw_verdict` cannot tell
            # them apart either (it is one snapshot of ALL gates' net effect).
            flags["second_opinion_boosted"] = True
            logger.info(
                "agent: second-opinion corroborated -> lead raised to probable for %s",
                (seed or {}).get("uuid"),
            )
        elif is_bare_lead and blocked_by:
            # A gate suppressed this lead on the axis the SO itself reviews and declared `block`
            # in `_SO_BOOST_POLICY` (SF-3 / exposer): an independent "it's related" agreement
            # can't tell an exposer from a root cause, so keep it suppressed (record the
            # agreement, don't re-inflate the band). A suppression on a DIFFERENT axis — the
            # stale-signature clamp, `allow` — deliberately does not land here.
            logger.info(
                "agent: second-opinion corroborated but %s suppressed this lead on the axis the "
                "second opinion cannot speak to; NOT re-inflating to probable for %s",
                blocked_by, (seed or {}).get("uuid"),
            )
    elif so.corroborates is False and conf in ("medium", "high"):
        # ``medium`` counts, symmetrically with the corroborate branch above. It did not, and the
        # asymmetry was the bug: a medium AGREEMENT could raise a lead a whole band while a
        # medium REFUTATION — often a specific, checkable "this diff does not touch the crashing
        # path" — did nothing at all, not even a flag or a chip. Seen on
        # ``dcfc4da0-7015-4845-8494-ec3380260729``. The measured instrument supports it: SO
        # specificity is 1.00 against corpus ground truth, i.e. when it refutes it is right.
        flags["second_opinion_refuted"] = True
        if v.decision == Decision.strong_evidence and conf == "high":
            _downgrade_to_lead_or_abstain(
                dossier, seed,
                "second-opinion confidently refuted the mechanism",
                "an independent blind review confidently found the candidate cannot "
                "explain the crash, and no cited candidate/hunk/edge anchor remains",
            )
            flags["second_opinion_downgraded_strong"] = True
        elif v.decision == Decision.strong_evidence:
            # `conf` is `medium` here -- the outer branch admits only medium/high. This case used
            # to set the flag and move NOTHING, so the STRONGEST verdict was the only one a blind
            # refutation could not touch: measured on the 500-dossier snapshot of 2026-08-24,
            # `ca6ebc17-0d23-483b-8b02-302180260822` shipped culprit/85 holding
            # `second_opinion_refuted`, 578 days of staleness, and an SO refutation that read
            # "an expression 9239a97af457 left byte-for-byte unchanged (it only renamed the LHS)
            # ... introduced earlier by bug 2012042, not by this changeset" -- which is bug
            # 2065373's error verbatim, independently witnessed, and discarded.
            #
            # One band down, symmetric with the lead clamp below, and recall-safe for the same
            # reason: strong-evidence had more to begin with, so it stays reportable at
            # `probable`. `high` is not available to a lead by construction (see
            # CONFIDENCE_SCORE), so the demotion of the DECISION is forced, not a choice.
            dossier.verdict = v.model_copy(update={"decision": Decision.lead,
                                                   "confidence": Confidence.probable})
            flags["second_opinion_clamped_strong"] = True
            logger.info(
                "agent: second-opinion refuted at medium -> strong-evidence clamped to a "
                "probable lead for %s", (seed or {}).get("uuid"),
            )
        elif v.decision == Decision.lead and (
            CONFIDENCE_SCORE.get(v.confidence, 0.0) > CONFIDENCE_SCORE[Confidence.medium]
        ):
            # Recall-safe: the lead survives (we don't drop a report on one blind
            # disagreement), but its band drops to a plain medium lead to reflect it.
            dossier.verdict = v.model_copy(update={"confidence": Confidence.medium})
            # See `second_opinion_boosted`: this marks the clamp as APPLIED. Needed because the
            # corroboration gate runs FIRST and can raise the same lead to `probable`, so a
            # clamp back down to `medium` leaves raw_verdict == shipped == `medium` — identical
            # to the SO having done nothing at all. From `low` the pair even INVERTS, reading as
            # "the gates raised this verdict" when the SO in fact clamped it.
            flags["second_opinion_clamped"] = True
            logger.info(
                "agent: second-opinion refuted -> lead clamped to medium for %s",
                (seed or {}).get("uuid"),
            )
        elif v.decision == Decision.lead:
            # At or below `medium` there is no lower band to clamp to, and the old floor
            # ("never drop a report on one blind disagreement") made a refutation of the
            # WEAKEST leads a guaranteed no-op — the leads least worth a human's time were the
            # ones the independent check could not touch. A rung-25 lead already means "we
            # barely believe this"; a refutation on top leaves nothing to report.
            why = "an independent blind review found the candidate cannot explain this " \
                  "crash, and the lead was too weak to survive it"
            if so.refutation:
                why = "{}: {}".format(why, so.refutation)
            dossier.verdict = Verdict(
                decision=Decision.abstain,
                confidence=Confidence.low,
                abstain_reason=why,
                mechanism=v.mechanism,
                consistency=v.consistency,
            )
            flags["second_opinion_abstained"] = True
            logger.info(
                "agent: second-opinion refuted a %s lead with nothing left to clamp -> "
                "abstain for %s", v.confidence.value, (seed or {}).get("uuid"),
            )
    dossier.corroborations = {**(dossier.corroborations or {}), **flags}


_STALE_SIGNATURE_CLAMP = {
    Confidence.probable: Confidence.medium,
    Confidence.medium: Confidence.low,
}


def _apply_signature_age_gate(dossier, seed):
    """DOWNWEIGHT a lead whose CANDIDATE landed after the crash already existed.

    If the signature was first seen more than ``min_age_days`` BEFORE the candidate landed, that
    candidate cannot be the crash's ORIGIN, however plausible its diff looks. This is the single
    most common way the pipeline is wrong: 10 of 10 high-confidence blind-second-opinion
    refutations argued exactly this, and all 10 verified deterministically (median gap 178 days).

    The comparison is candidate-pushdate vs first-seen, NOT first-seen vs the crash's BUILD date.
    That distinction is the whole gate: ~two thirds of triaged signatures are old, so a
    build-based rule fires on ~83% of independently CORROBORATED leads too and discriminates
    nothing. Back-tested on 23 real prod leads with the second opinion as an independent
    yardstick, this comparison at 7 days fires on 10/10 high-confidence refutations while sparing
    5 of 6 corroborated leads.

    Deliberately a downweight, ONE rung, never below a still-reportable lead:

    * SIGNATURE REUSE is real — an old signature can acquire a new cause, and a rare
      pre-existing crash can be made *frequent* by a new change (a volume regression). Landing
      late disproves ORIGIN, not relevance.
    * 1 of 6 INDEPENDENTLY-CONFIRMED leads still trips this, so a hard "stale -> abstain" rule
      would destroy real leads.

    Strong-evidence is FLAGGED but not downgraded: that verdict already requires a fully cited,
    skeptic-survived chain, and neither prod culprit showed the pattern. A confident
    second-opinion refute already covers the case where such a chain is genuinely wrong.

    Keyed off pre-computed seed keys, so it is a no-op OFFLINE (eval seeds carry no
    ``signature_first_seen_buildid``, and the check for it comes first). Online, a candidate the
    agent found outside the seeded window — via blame, so with no pre-computed landing date —
    gets ONE hg ``json-rev`` lookup (``sigage.pushdate_for_node``) rather than being skipped:
    that silent skip let a 126-day-stale lead ship at 80% worth-investigating in prod
    (``0cf2a052-2eae-4228-824f-6284d0260728``). Timing that still cannot be resolved is a no-op —
    unknown timing must not penalise a verdict. Mutates ``dossier`` in place; never raises."""
    v = dossier.verdict if dossier is not None else None
    if v is None:
        return
    cfg = config.get_agent_signature_age()
    if not cfg["enabled"]:
        return
    first_seen = (seed or {}).get("signature_first_seen_buildid")
    cand = dossier.candidate
    if not first_seen or cand is None or not cand.node:
        return
    from crashclouseau import sigage

    pushdate = ((seed or {}).get("candidate_pushdates") or {}).get(cand.node)
    if pushdate is None:
        # The agent chose a candidate that was not in the seeded pushlog window (it found it
        # via blame), so no landing date was pre-computed for it. Resolve it now with ONE hg
        # lookup. This only ever runs online: an offline seed carries no
        # `signature_first_seen_buildid` and returned above, so the gate stays a no-op there.
        pushdate = sigage.pushdate_for_node(cand.node, (seed or {}).get("channel"))
    if pushdate is None:
        return
    landed_after = sigage.days_landed_after_first_seen(first_seen, pushdate)
    if landed_after is None or landed_after <= cfg["min_age_days"]:
        return
    flags = {
        "stale_signature": True,
        "candidate_landed_after_first_seen_days": landed_after,
        "signature_first_seen_buildid": first_seen,
    }
    dossier.corroborations = {**(dossier.corroborations or {}), **flags}
    if v.decision != Decision.lead:
        # strong-evidence / abstain: record the fact, do not move the band (see the docstring).
        return
    clamped = _STALE_SIGNATURE_CLAMP.get(v.confidence)
    if clamped is None:
        return
    dossier.verdict = v.model_copy(update={"confidence": clamped})
    # ``allow`` in `_SO_BOOST_POLICY`: this gate rules on ORIGIN and the blind second opinion
    # reviews the MECHANISM, so an independent agreement is evidence on the other axis and MAY
    # undo this clamp. Measured, not assumed — blocking it would have cost 3 FIXED bugs (2
    # topcrash) whose `regressed_by` a HUMAN set to exactly the changeset named here, and bought
    # 6 low-value filings avoided. The flag is also the label the filed bug and the UI chip read
    # to tell the recipient the timing evidence ran against this changeset.
    _record_suppression(dossier, "stale_signature_clamped")
    logger.info(
        "agent: candidate %s landed %.1fd AFTER this signature was first seen (%s) -> lead %s "
        "clamped to %s for %s",
        cand.node, landed_after, first_seen, v.confidence.value, clamped.value,
        (seed or {}).get("uuid"),
    )


def _resolve_candidate_backout(dossier, seed):
    """Ask hg whether the chosen candidate was BACKED OUT, and store the backout sha on it.

    ONLINE ONLY, and deliberately not inside ``apply_deterministic_gates``: that function is
    shared with the offline eval runner, which must stay network-free. Splitting resolve (here)
    from decide (``_apply_backout_gate``) is what keeps the gate a pure, offline-safe no-op.

    Runs BEFORE the second-opinion pass so a doomed lead never buys a ~$1 independent review of
    a changeset we are about to suppress. Costs nothing: ``json_rev`` is cached per node and
    ``_resolve_candidate_git_commit`` fetches the same URL for the same node later in the run.

    Best-effort — a failed lookup leaves ``backedout_by`` empty, which the gate treats as "not
    backed out". That asymmetry is deliberate: never suppress a verdict on a lookup failure."""
    cand = dossier.candidate if dossier is not None else None
    if cand is None or not cand.node or cand.backedout_by:
        return
    try:
        from crashclouseau import sigage

        # ``seed["channel"]`` — NOT ``cand.channel``, which is a model-supplied schema field and
        # is empty on every dossier (nothing fills it). An empty channel makes json_rev skip the
        # request entirely and cache {}, which would poison the lookup for the rest of the run.
        backedout_by = sigage.backedout_by_for_node(cand.node, (seed or {}).get("channel"))
    except Exception:
        logger.warning("agent: backout lookup failed for %s", cand.node, exc_info=True)
        return
    if backedout_by:
        dossier.candidate = cand.model_copy(update={"backedout_by": backedout_by})
        # Already doomed by ``_apply_backout_gate``; don't buy a request for the mirror
        # predicate below on a verdict that is about to be suppressed outright.
        return
    _resolve_candidate_is_backout(dossier, seed)


def _resolve_candidate_is_backout(dossier, seed):
    """Ask hg whether the chosen candidate IS ITSELF a backout, and if so whether the patch it
    reverts landed in its OWN push.

    THE MIRROR of ``_resolve_candidate_backout``, and the hole it left. hg answers "was this
    backed out?" with a field; "is this a backout?" is only in the description, so the two
    predicates come from different places and only the first was ever gated. On
    ``00b44d2a-4343-4caa-9e12-907550260802`` a sheriff's revert had an EMPTY ``backedoutby``
    (it is a backout, it was not itself backed out), sailed past the gate, and shipped as a
    strong-evidence culprit at 97% for a signature 283 days older than it.

    Same shape as its mirror: ONLINE ONLY, deliberately outside ``apply_deterministic_gates``
    so that ladder stays network-free for ``eval/runner.py``, and best-effort — a failed
    lookup leaves both fields unset, which the gate reads as "not a backout".

    The description is FREE (the cached ``json-rev`` this run already fetched). The same-push
    lookup is one extra request, paid only when the candidate really is a backout — 10 of 1947
    canary dossiers, so ~0.5% of runs."""
    cand = dossier.candidate if dossier is not None else None
    if cand is None or not cand.node:
        return
    try:
        from crashclouseau import pushlog, sigage

        # ``seed["channel"]``, never ``cand.channel`` — see ``_resolve_candidate_backout``.
        channel = (seed or {}).get("channel")
        desc = sigage.desc_for_node(cand.node, channel)
        if not desc or not pushlog.is_backed_out(desc):
            return
        target = sigage.same_push_backout_target(cand.node, channel)
    except Exception:
        logger.warning("agent: is-backout lookup failed for %s", cand.node, exc_info=True)
        return
    dossier.candidate = cand.model_copy(
        update={"is_backout": True, "backout_of_same_push": target or ""}
    )


def _apply_backout_gate(dossier, seed):
    """SUPPRESS a verdict whose candidate was BACKED OUT: there is nothing left to act on.

    A backed-out changeset is not in the tree. Whatever it did, no one can fix it, and a patch
    is usually backed out precisely BECAUSE it was wrong — so naming one costs a triager's
    attention and returns nothing. Unlike the stale-signature downweight this is not a
    confidence question, so it is not a one-rung clamp but an outright abstain: 14 of the 17
    measured cases sat at rung 25 or 50 anyway, and rung 25 has nowhere lower to go.

    Measured on the canary DB (1501 dossiers, 847 reported verdicts): 17 reported leads named a
    candidate the model itself flagged as backed out, and hg confirmed 19 of those 20 distinct
    nodes were genuinely backed out (the 20th IS a revert, equally dead). But the model's flag
    also MISSES them — 1 of 12 sampled `backedout: false` reported leads was in fact backed out
    (`fa615d158e7b`, backed out by `9367c2806d2f`) — which extrapolates to ~9% of reported leads
    rather than 2%. Hence a deterministic check on OUR OWN hg lookup, never on the model's flag.

    Deliberately UNCONDITIONAL on the backout's timing. A patch backed out after the triaged
    build could still be that build's true cause (``plans/09-skeptic-verifier.md:88``), but it is
    equally unactionable, and resolving the backout's own landing date costs a second request.

    Reads only ``candidate.backedout_by``, which ``_resolve_candidate_backout`` sets online, so
    this is a natural no-op in the offline eval. Mutates in place; never raises."""
    v = dossier.verdict if dossier is not None else None
    cand = dossier.candidate if dossier is not None else None
    if v is None or cand is None or not cand.backedout_by:
        return
    sha = cand.backedout_by
    dossier.corroborations = {
        **(dossier.corroborations or {}),
        "candidate_backedout": True,
        "candidate_backedout_by": sha,
    }
    if v.decision == Decision.abstain:
        # Already not reported — record the fact for the page and leave the reason alone.
        return
    # A NEW Verdict, not ``model_copy``: an abstain must not carry the needinfo_draft
    # (``Verdict._consistency_rule`` rejects that outright) and must not inherit
    # ``p_worth_investigating`` from the verdict it replaces. mechanism/consistency are kept so
    # the page can still explain what was found and why it was dropped.
    dossier.verdict = Verdict(
        decision=Decision.abstain,
        confidence=Confidence.low,
        abstain_reason=(
            "candidate {} was BACKED OUT (by {}) — a backed-out changeset is not in the "
            "tree, so there is nothing to act on; suppressed rather than reported".format(
                cand.node, sha[:12]
            )
        ),
        mechanism=v.mechanism,
        consistency=v.consistency,
    )
    dossier.corroborations = {
        **dossier.corroborations, "candidate_backedout_suppressed": True
    }
    logger.info(
        "agent: candidate %s was backed out by %s -> %s/%s suppressed to abstain for %s",
        cand.node, sha[:12], v.decision.value, v.confidence.value, (seed or {}).get("uuid"),
    )


# A thread named in prose, in either order: `"MediaTrackGrph"` thread / thread named `Foo`. Up to
# two delimiter characters each side because the agent nests them — bug 2064436's mechanism wrote
# ``the `"MediaTrackGrph"` thread``. QUOTED is the whole point: quoting a name is the model
# asserting a literal runtime identifier, which is checkable, whereas prose about a subsystem
# ("a MediaTrackGraph would be shut down here") is not and must not be gated on.
#
# ``(?<![-\w])`` and NOT ``\b``, because a word boundary sits after a HYPHEN and an X-thread
# compound is ordinary writing: "main-thread", "off-thread", "background-thread". Replaying this
# gate over all 52 filings, its entire lifetime reach is 3 quoted-thread matches and this bug was
# 1 of them: bug 2062286's consistency statement, "Crash is a main-thread
# `EXCEPTION_ACCESS_VIOLATION_READ` reading a `char16_t` buffer", read the EXCEPTION NAME as a
# claimed thread, found it absent from an 86-thread inventory that is complete, and clamped 97%
# to medium — under `autofile.min_confidence`. That filing is the one BMO resolves FIXED with a
# `regressed_by` naming exactly the changeset we named, so the clamp would have cost the single
# confirmed-correct filing in the corpus. 10 of the 52 filings use an X-thread compound, so the
# construction is generic and live, not one typo. The other 2 matches are correct (2064436 fires,
# 2065075 does not).
_QUOTED_THREAD_RE = re.compile(
    r"""(?: ["'`]{1,2}([^"'`\n]{2,40})["'`]{1,2}\s+threads?\b
          | (?<![-\w])threads?(?:\s+named)?\s+["'`]{1,2}([^"'`\n]{2,40})["'`]{1,2} )""",
    re.I | re.X,
)
# Below this, a squashed name is too generic to check ("ui", "io", "gpu").
_MIN_THREAD_NAME = 4
# Linux caps a pthread name at 15 bytes and Socorro elides the middle ("Shutdow~minator"), so an
# inventory name may be a HEAD fragment of what the agent wrote. Compare that many leading chars.
_TRUNCATED_PREFIX = 6


def _squash(text):
    return "".join(c for c in str(text).lower() if c.isalnum())


def _thread_name_matches(claimed, known):
    """Does a squashed claimed name refer to the squashed inventory name ``known``?

    Substring either way, so ``"BgIOThreadPool"`` matches ``BgIOThreadPool #2`` and ``"main"``
    matches ``MainThread``; plus a head-prefix test for the Linux 15-byte truncation, where the
    inventory holds ``Shutdow~minator`` for what the agent would call
    ``"Shutdown Hang Terminator"``."""
    if claimed in known or known in claimed:
        return True
    if len(known) < _TRUNCATED_PREFIX:
        return False
    return claimed.startswith(known[:_TRUNCATED_PREFIX])


def _process_thread_names(seed):
    """``(set of squashed thread names, complete)`` for the crashing process.

    ``complete`` is ``triage._inventory_complete`` — literally the call the prompt block makes,
    over the same name list, not a re-implementation of it. It used to be a re-implementation and
    the two disagreed: the prompt applied the ceiling to RAW distinct names while this applied it
    to SQUASHED ones, so a process whose names collide under ``_squash`` (``DNS Resolver #1`` and
    ``dnsresolver1``) could be shown a list labelled TRUNCATED and still have its verdict clamped
    for an absence from it. The old comment here — "the SAME ceiling ... so the gate and the agent
    cannot disagree" — was false above the ceiling. Squashing survives for MATCHING, which is what
    it is for."""
    from crashclouseau.agent import triage

    raw = (seed or {}).get("raw_crash") or {}
    names, _unnamed = triage._thread_names(raw)
    squashed = {s for s in (_squash(name) for name in names) if s}
    return squashed, triage._inventory_complete(raw)


def _absent_named_threads(dossier, seed):
    """Thread names the verdict asserts that are NOT in this process, as written. ``[]`` when the
    inventory is missing or truncated (an incomplete list proves no absence)."""
    names, complete = _process_thread_names(seed)
    if not names or not complete:
        return []
    v = dossier.verdict
    texts = [
        (v.mechanism.statement if v.mechanism else "") or "",
        (v.consistency.statement if v.consistency else "") or "",
        (dossier.data_flow.summary if dossier.data_flow else "") or "",
    ]
    absent = []
    for text in texts:
        for match in _QUOTED_THREAD_RE.finditer(text):
            claimed = match.group(1) or match.group(2) or ""
            squashed = _squash(claimed)
            if len(squashed) < _MIN_THREAD_NAME:
                continue
            if any(_thread_name_matches(squashed, n) for n in names):
                continue
            if claimed.strip() not in absent:
                absent.append(claimed.strip())
    return absent


def _apply_absent_thread_gate(dossier, seed):
    """DOWNWEIGHT a verdict whose mechanism runs through a thread this process never had.

    Bug 2064436. The verdict explained a shutdown hang through "the `MediaTrackGrph` thread owned
    by `ThreadedDriver`" and shipped at 97% worth-investigating. Andreas Pehrson closed it INVALID
    the same evening: "No MediaTrackGrph thread, no GraphRunner thread." The minidump's own list of
    46 threads was in the payload the whole time; nothing read it, and the "skeptic" that signed
    the mechanism off checks plausibility, not existence.

    The primary fixes for that bug are elsewhere and are the ones that matter:
    ``inspector.thread_for_analysis`` stops handing the agent the watchdog's stack, and
    ``triage._thread_inventory`` puts the thread list in front of BOTH the agent and the blind
    second opinion. This gate is the backstop for when they are ignored — a prompt is advice and a
    named entity that does not exist is a fact.

    A CLAMP, NOT AN ABSTAIN, and never below a reportable ``medium`` lead, because the check reads
    prose and can be wrong in one specific way: a mechanism may legitimately name a thread in
    ANOTHER process ("the content process's `MediaDecoderStateMachine` thread"), which this
    process's inventory says nothing about. Andreas raised exactly that caveat — "There may be a
    MediaTrackGraph in a content process but then the shutdown blocker would live there too" — so
    the possibility is real even though it did not save this verdict. Bounding the action to one
    rung means a false fire costs an automatic FILING (rung 70) and not the lead itself, while the
    2064436 case, which shipped at the top of the scale, is still caught. That caveat is MEASURED,
    not merely plausible: 113 of the 159 thread families seen >=10x over 840 nightly crashes (71%)
    are >=95% confined to one process type, and p(MediaTrackGrph) is 0.00 in a parent process
    against 0.05 in a content one — so a cross-process claim is the common shape, not the exotic
    one, which is why ``triage._thread_inventory`` now asks the agent for Pehrson's second
    argument in the prompt rather than handing it the refutation outright.

    Only QUOTED names are considered (see ``_QUOTED_THREAD_RE``) and only against a COMPLETE
    inventory, so this is a no-op on the offline eval, on Java crashes and on any payload without
    ``json_dump.threads``. Mutates ``dossier`` in place; never raises."""
    v = dossier.verdict if dossier is not None else None
    if v is None or v.decision == Decision.abstain:
        return
    absent = _absent_named_threads(dossier, seed)
    if not absent:
        return
    dossier.corroborations = {
        **(dossier.corroborations or {}), "absent_named_threads": absent}
    logger.info(
        "agent: verdict names thread(s) %s absent from this process's %d threads for %s",
        absent, len((((seed or {}).get("raw_crash") or {}).get("json_dump") or {})
                    .get("threads") or []), (seed or {}).get("uuid"))
    if v.decision == Decision.strong_evidence:
        _downgrade_to_lead_or_abstain(
            dossier, seed, "mechanism names a thread absent from this process",
            "the mechanism relies on a thread that is not running in this process")
        return
    if v.confidence in (Confidence.probable, Confidence.high):
        dossier.verdict = v.model_copy(update={"confidence": Confidence.medium})
        dossier.corroborations = {
            **dossier.corroborations, "absent_thread_clamped": True}
        logger.info("agent: lead %s clamped to medium (absent thread) for %s",
                    v.confidence.value, (seed or {}).get("uuid"))


def _apply_bad_machine_gate(dossier, seed):
    """SUPPRESS a verdict whose crash came from a machine that is scattering unrelated
    signatures: the machine is broken, not the code.

    Jan de Mooij wrote this rule for us on bug 2062168: "I think this one is bad hardware rather
    than a regression. It's just one crash report and that installation has multiple crashes with
    distinct signatures." That installation — one 2011 Sandy Bridge — produced 21 crashes across
    20 distinct signatures in two days, spanning JS GC, jemalloc, heap free, Intel graphics and
    Windows display, and we filed TWO bugs out of it on the same day (2062168, 2062173). The same
    reviewer had already told us on bug 2061124 that "crashes with very few reports in common code
    paths are often hardware related".

    THREE CONDITIONS, and each is load-bearing (see ``config.get_agent_bad_machine`` for the
    measurements behind every threshold):

    * ``distinct_signatures`` — DIVERSITY, never volume. One machine crashing 100 times on ONE
      signature is a reproducible bug: bug 2060924 is exactly that (5 crashes, 1 signature) and is
      ASSIGNED. Crash count predicts nothing at any threshold.
    * ``distinct_cpus <= 1`` — the mechanism test. ``install_time`` is a (machine, build) id that
      COLLIDES; 11% of scattering ids span several CPU models. Bug 2061961's carries 4 CPUs and 3
      operating systems, so its scatter is an artifact and this gate correctly leaves it to the
      bit-flip gate, which catches it for the right reason.
    * ``span_seconds`` — a failing machine scatters over days; a cascading session scatters in
      minutes. Bug 2047016 (RESOLVED FIXED, 682 crashes, 23 installs) had its first crash on a
      machine that emitted 5 signatures in 22 minutes as one Wayland/video stack unwound.

    An ABSTAIN, not a downweight, and that is not a preference. A one-rung downweight of a
    strong-evidence verdict lands on ``probable`` (70) — still exactly ``autofile.min_confidence``
    — so it would not have stopped bug 2062173, which shipped at 97%. Placed LAST, after the
    second-opinion fold, for the same reason as ``_apply_backout_gate``: no amount of independent
    agreement makes a broken machine's crash a code defect.

    Tri-state on every input, all failing toward REPORTING: an absent ``install_time`` (3% of
    nightly crashes), a failed lookup, or an unknown CPU count each leave the verdict alone. The
    CPU condition is a POSITIVE requirement precisely so an unknown cannot satisfy it. Reads only
    seed keys, so it is a natural no-op offline. Mutates in place; never raises."""
    v = dossier.verdict if dossier is not None else None
    if v is None:
        return
    cfg = config.get_agent_bad_machine()
    if not cfg["enabled"]:
        return
    hist = (seed or {}).get("install_history") or {}
    sigs = hist.get("distinct_signatures")
    cpus = hist.get("distinct_cpus")
    span = hist.get("span_seconds")
    if sigs is None:
        return
    # Recorded for EVERY verdict, fired or not: without the flags there is no way to count how
    # often the pipeline is looking at a broken machine, and no way to score the threshold
    # against `models.Feedback` outcomes later.
    flags = {"machine_distinct_signatures": sigs}
    if cpus is not None:
        flags["machine_distinct_cpus"] = cpus
    if hist.get("crashes") is not None:
        flags["machine_crash_count"] = hist["crashes"]
    if span is not None:
        flags["machine_span_seconds"] = round(span)
    dossier.corroborations = {**(dossier.corroborations or {}), **flags}
    if sigs < cfg["min_signatures"]:
        return
    if cpus is None or cpus > cfg["max_cpu_infos"]:
        return
    if span is None or span < cfg["min_span_seconds"]:
        return
    if v.decision == Decision.abstain:
        return
    dossier.verdict = Verdict(
        decision=Decision.abstain,
        confidence=Confidence.low,
        abstain_reason=(
            "this crash came from an installation already producing {} DIFFERENT crash "
            "signatures over {:.0f}h on a single CPU — the profile of a failing machine, not of "
            "a code defect; suppressed rather than reported".format(sigs, (span or 0) / 3600.0)
        ),
        mechanism=v.mechanism,
        consistency=v.consistency,
    )
    dossier.corroborations = {**dossier.corroborations, "bad_machine_suppressed": True}
    logger.info(
        "agent: install has %s distinct signatures over %.0fh on %s cpu(s) -> %s/%s suppressed "
        "to abstain for %s",
        sigs, (span or 0) / 3600.0, cpus, v.decision.value, v.confidence.value,
        (seed or {}).get("uuid"),
    )


def _record_window_membership(dossier, seed):
    """Record whether the chosen candidate was in the build's PUSHLOG WINDOW at all.

    The pipeline's premise is "the regressor is somewhere in this build's pushlog window", and
    the filed bug asserts it: ``build_bug_preview`` sets the ``regression`` keyword because
    "the whole pipeline only looks inside a build's pushlog window, so every candidate it names
    is a suspected regression". Measured over the canary's first 22 filings that premise held
    THREE times. The rest named code from outside the window — 24 days, 58 days, and in one case
    a changeset from 2022-12-13, 1335 days before the build.

    Naming old code is not itself the mistake; bug 2062119 did exactly that and still got a real
    fix written, because a knowledgeable person read it and found the true origin. The mistake is
    ASSERTING a regression we have no recency evidence for — the `regression` keyword, the
    blocks-link, and the words "Suspected regressor" all say something the pipeline did not
    establish, and on bug 2062119 something its own skeptic pass explicitly contradicted ("no
    landings near 2026-08-08 touching the relevant lines ... a pre-existing latent race, not a
    new regression"). Jens Stutte's first reply was "I do not think bug 1768581 is the
    regressor", and the feedback that followed was to be more speculative on the unsure parts.

    So this only labels; it moves no rung. ``seed["candidates"]`` IS the pushlog window
    (``build_seed`` builds it from the pushlog), so membership is a set lookup — no network,
    offline-safe, and unrecorded (rather than ``False``) when there is nothing to consult,
    because the bug comment must not claim a window it never had.

    KEYED ON THE CANDIDATE SET, NOT ON ``candidate_pushdates``, which is the same window minus
    every candidate whose landing date is unknown (``build_seed`` drops ``pushdate is None``).
    Two reasons, and the second is the load-bearing one. A candidate with no pushdate WAS in the
    window, so scoring it ``False`` under-claims — and this flag decides whether the filed bug
    may say "regression" at all. And ``eval/study_corpus.py`` writes ``"pushdate": None`` for
    every candidate it freezes (0 of corpus_ship's 6873 have one), so under the map this flag
    could NEVER be recorded offline: it is absent from all 90 rows of corpus_ship/results.jsonl,
    which is precisely why the two-arm calibration split had to be backfilled by hand rather than
    read off the corpus (``config.get_agent_calibration``, ``spike/window_arm_null.py``). A fact
    the eval harness cannot record is a fact no fit can ever be validated against."""
    cand = dossier.candidate if dossier is not None else None
    if cand is None or not cand.node:
        return
    window = {c.get("node") for c in ((seed or {}).get("candidates") or [])
              if isinstance(c, dict) and c.get("node")}
    window |= {node for node in ((seed or {}).get("candidate_pushdates") or {}) if node}
    if not window:
        return
    dossier.corroborations = {
        **(dossier.corroborations or {}),
        "candidate_in_pushlog_window": cand.node in window,
    }


def _record_signature_age_facts(dossier, seed):
    """Record how old this signature really was, on BOTH clocks. Moves no rung.

    Recorded rather than acted on, deliberately and in that order. The windowed clock
    (``signature_first_seen_buildid``, a 364-day SuperSearch that Socorro's ~178-day ES retention
    truncates further) is the one ``_apply_signature_age_gate`` reasons from, and it is measurably
    wrong: on the ten `Core :: JavaScript*` filings a module owner rejected it read 2017-10-28 as
    2025-12-27 and 2023-01-04 as three days ago. The unbounded clock
    (``signature_first_seen_ever``, Socorro's ``SignatureFirstDate`` table) is right, and cannot
    simply be substituted — see ``sigage.first_seen_ever`` for the eight FIXED/DUPLICATE filings
    that would start tripping the gate if it were.

    So both are written on every reported verdict, with the age each implies. That makes "how far
    apart are the two clocks, and on which crashes?" answerable from prod data before any rung
    depends on the answer — the `nshare` arrangement, and the lesson of `31b5f3b`, where a gate
    returned before recording and spent nine days unmeasurable.

    Unrecorded (rather than zero) whenever a lookup did not answer: a signature whose age we could
    not establish must never read as a new one."""
    if dossier is None or seed is None:
        return
    from crashclouseau import sigage

    windowed = seed.get("signature_first_seen_buildid")
    # THE INVERSION, and it is a proof rather than a heuristic. `SignatureFirstDate` claims to be
    # an all-time minimum, and Elasticsearch behind SuperSearch only reaches back ~178 days, so ES
    # holding an OLDER build than the all-time minimum is arithmetically impossible — unless those
    # documents were re-signatured onto this name after the fact. Socorro's cron walks a ~90-minute
    # rolling window of `date_processed`, and `date_processed` is the SUBMITTED timestamp and is
    # never refreshed on reprocessing, so a reprocessed old crash never re-enters that window and
    # the all-time minimum silently stops being one.
    #
    # That is Calixte's worry made computable: a signature is only "new" if nobody renamed an old
    # crash onto it. Measured over 500 live Firefox-nightly signatures it fires on 6 (1.2%), all
    # six confirmed against `processor_history`, with 0 false positives on 450 non-novel controls.
    #
    # ONE-DIRECTIONAL, and the gap matters more than the sign. An older report PROVES a rename;
    # no older report proves nothing, because both clocks are floored by the same retention wall
    # and because the biggest artefact classes — a `MOZ_DIAGNOSTIC_ASSERT` prepended to an ancient
    # crash site, a driver frame decorating it — mint a name that genuinely never existed before
    # and leave no inversion at all. So this may suppress a novelty claim and must never
    # corroborate one. The 30-day floor keeps the ~1-day cron lag out (measured: the only control
    # that fired at any gap was 1 day).
    #
    # The drift is recorded whenever both clocks answered, fired or not, so "how far apart are the
    # two clocks across the whole population?" stays answerable from prod — the flag alone would
    # only ever show the tail it already decided to call a rename.
    #
    # The arithmetic itself lives in `sigage.age_facts`, because the crash brief and the filed bug
    # now state the same numbers to a model and to a human and the three must not drift apart.
    facts = sigage.age_facts(
        seed.get("buildid"),
        windowed,
        seed.get("signature_first_seen_ever"),
        # The inversion reads the UNFLOORED all-channel value, not the gate's floored `first_seen`.
        # A rename shows up as a handful of off-channel reports on old builds, and the floor
        # (`other_channel_floor`, 20) is designed to ignore exactly that many.
        observed=seed.get("signature_first_seen_any") or windowed,
    )
    if facts:
        dossier.corroborations = {**(dossier.corroborations or {}), **facts}


def _signature_is_mostly_hardware(sample, flip_rate, cpu_rate, cfg):
    """Has this signature's hardware-error share cleared bugbot's line, on a big enough sample?

    Both rate tests are POSITIVE requirements and the sample test is a floor, so every unknown
    (a failed ``sigage.hardware_noise`` leaves all three ``None``) answers False and the verdict
    is reported. Either rate alone is enough: the two signals are nearly disjoint, so requiring
    both would miss each other's cases: re-measured 2026-08-21, bug 2064600's signature trips
    only the flip rate (60% flips, 20% Raptor Lake over 5 nightly reports) and
    `js::jit::CompilerFrameInfo::sync` — bug 2063364, INVALID — trips only the CPU rate (0%
    flips, 79% Raptor Lake, 15 of 19 reports).

    THE 71% THIS DOCSTRING QUOTED FOR THAT SECOND SIGNATURE DOES NOT REPRODUCE under any of the
    eight product/channel/window combinations tried (Firefox/nightly at 364d 0.789, 180d 0.789,
    28d 0.000; Firefox/all-channels 0.203; all-products/all-channels 0.188), and the shipped
    figure itself moved 4.4pp in a fortnight: 15 of 18 = 0.833 until 2026-08-14, 15 of 19 =
    0.789 since. So this arm is FITTED ON n=1 and the panel cannot separate any threshold in
    (0.302, 0.789] — bug 2062219 (FIXED) sits at 0.302, bug 2063364 (INVALID) at 0.789, and
    nothing on the 52 filings lands between them. 0.7 stays because it is bugbot's published
    line and eats 0 of the 19 FIXED/DUPLICATE/ASSIGNED controls, not because this panel chose
    it. Lowering it to 0.5 buys 0 extra bad filings and newly suppresses three background
    signatures that all carry FIXED bugs: `arena_dalloc | BaseAllocator::free |
    MozJemalloc::free` at 0.57 (1893270/1893340/1962421), `mozilla::detail::VectorImpl<T>::new_`
    at 0.60 (1890909) and `webrender_bindings::moz2d_renderer` at 0.56 (1967071/2002983).

    CPU CONCENTRATION WAS TRIED AS A THIRD ARM HERE AND KILLED, 2026-08-21. Bug 2065373's
    signature reads `broken_cpu_rate` 0.0 while 58 of its 58 reports sit on ONE processor model,
    so "the top `cpu_info` share" looks like the arm this gate is missing. It is not: on the 52
    filings at the same sample floor every threshold from 0.40 to 0.95 suppresses at least one
    control — 0.50 eats five (2062052, 2063678, 2063809 FIXED; 2061180, 2063864 DUPLICATE) —
    while catching at most the one INVALID the CPU rate already catches, and it reads AUC 0.333
    on 3 bad against 13 controls. The only variant that eats no control, `cpu_terms == 1 and
    reports >= 20`, fires on 20 of 174 eligible background signatures and 8 of those carry a
    real Firefox bug, including bug 2056116 — the off-stack pref-flip archetype this repo ships.
    The share is REPORTED instead, always beside its 0.32 population median, by
    `triage._cpu_spread_line` and `report_bug.build_hardware_note`. See `sigage.hardware_noise`
    for the panels."""
    if sample is None or sample < cfg["min_signature_reports"]:
        return False
    if flip_rate is not None and flip_rate >= cfg["max_bit_flip_rate"]:
        return True
    return cpu_rate is not None and cpu_rate >= cfg["max_broken_cpu_rate"]


def _apply_bit_flip_gate(dossier, seed):
    """SUPPRESS a verdict whose crash was probably HARDWARE, not software: there is no bug at all.

    Bug 2061961 is why. Crash ff888d42-ce3e-4308-8c2f-b3f060260807 faulted at
    ``0x00000001000000d0`` — one flipped bit from ``0xd0``, i.e. a NULL base plus a struct offset,
    and had the pointer really been null the code would have taken its ``None`` branch and not
    crashed. Socorro had already worked this out and published
    ``possible_bit_flips_max_confidence: 62``. Nothing in the pipeline read it, so the agent
    produced a fluent, fully-cited use-after-free story, the blind second opinion (which shares
    the same crash brief, and so the same blind spot) agreed and BOOSTED the rung from medium to
    probable — exactly the filing threshold — and a developer was needinfo'd about a mechanical
    refactor of his. Two people closed it INVALID in two days on this one field.

    THREE INDEPENDENT TRIGGERS, in order. The first two ask about the ONE report being triaged;
    the third asks about the signature, and they are not interchangeable — bug 2064600 is a
    report with no flip annotation, on a healthy CPU, sitting on a signature that is mostly
    hardware noise, and only the third sees it.

    (1) FLIP SCORE + SINGLETON, the original rule. A flip score alone is not enough: the same
    score is common on high-volume signatures, where it means one flaky machine among many rather
    than a bad crash. So this fires only when Socorro's confidence clears ``min_confidence`` AND
    the signature has never crashed more than ``max_reports`` people. Of the 21 bugs the canary
    had filed when this was written, 3 carried the field (66, 62, 25) and the rule fires on 2 —
    2061961, and 2061726, which is a single crash on a signature whose other reports are on
    RELEASE and predate the nightly changeset it blames.

    (2) THIS MACHINE'S CPU IS DEFECTIVE + SINGLETON. ``sigage.BROKEN_CPUS`` is Intel Raptor Lake,
    whose documented instability corrupts computation on healthy software (meta bug 1975808).
    The SAME conjunction as (1), for the same reason and it is just as load-bearing: 4.1% of
    nightly crash reports come from one of these machines, so suppressing on the CPU alone would
    throw away roughly one real bug in twenty-four. Paired with "nobody else has ever hit this", it
    identifies the case where the only evidence of a bug is one report from a machine known to
    invent them.

    (3) THE SIGNATURE IS MOSTLY HARDWARE NOISE — new, and the one bug 2064600 needed. Timothy
    Nikkel, twenty minutes after we filed: "About 50% of the crashes with this signature have
    non-zero bit flip probability. That might be something you want to include in your llm prompt
    to consider. And there is also several of the known buggy family 6 model 183 stepping 1
    without a bit flip annotation. ... I always look for these two things in crash reports."
    Neither check was reachable from what this gate read: the flip field was consulted for the
    triaged report only, and ``cpu_info`` was not consulted at all. Measured on that signature,
    3 of its 6 nightly reports carry a flip annotation and 1 is a Raptor Lake, versus a nightly
    background of 2.5% and 4.1%. ``sigage.hardware_noise`` computes both, on the crash's OWN
    product and channel — read that function before touching the denominator, because the wider
    one suppresses bug 2062219, which was FIXED. The thresholds are bugbot's, not ours (see
    ``config.get_agent_bit_flip``).

    AN ABSTAIN, not a downweight, for the same reason as ``_apply_backout_gate``: this is not a
    question of how confident to be in the candidate, it is that there is nothing to act on. And
    LAST, after the second-opinion fold, because no amount of independent agreement can turn a
    hardware fault into a software bug — the fold is precisely what pushed 2061961 over the line.

    TRIGGER (3) IS DELIBERATELY NOT IN ``models._INSTANCE_SUPPRESSED``, and (1) and (2) are.
    That list exists so a verdict suppressed for a reason peculiar to ONE report leaves the
    proto-signature cluster open for the next one — a broken installation says nothing about the
    next crash from a healthy machine. A signature that is half bit flips is not that: the finding
    is equally true for every report in the cluster, so it closes it, exactly as the backout gate
    does and for exactly the same reason. Getting this backwards would make us re-pay ~$3 a
    report to re-derive the same answer forever.

    NOT a volume gate in disguise. A single crash is normal and is the whole point of triaging
    nightly: bug 2062119 named the wrong changeset on a one-report signature and still got a real
    fix written. Volume only ever QUALIFIES a hardware signal here — in (1) and (2) as a ceiling
    on how many people have seen the crash, in (3) as ``min_signature_reports``, a floor below
    which a percentage computed from three reports is not evidence of anything.

    Tri-state on every input, and every one fails toward REPORTING. Socorro omits the flip field
    entirely (it is never 0) when the stackwalker found no candidate, ``signature_report_count``
    is ``None`` when that lookup failed, ``cpu_info`` can be absent, and every
    ``hardware_noise`` value is ``None`` when its query failed — none of them may read as a hit,
    and the two rate tests are POSITIVE requirements so an unknown cannot satisfy them. Reads
    only the seed, so it is a natural no-op offline where the corpus's stub crashes carry no
    ``crash_info`` and no hardware read was ever made. Mutates in place; never raises."""
    v = dossier.verdict if dossier is not None else None
    if v is None:
        return
    cfg = config.get_agent_bit_flip()
    if not cfg["enabled"]:
        return
    from crashclouseau import sigage

    raw = (seed or {}).get("raw_crash") or {}
    try:
        confidence = raw.get("possible_bit_flips_max_confidence")
        confidence = None if confidence is None else int(confidence)
    except (TypeError, ValueError):
        confidence = None
    reports = (seed or {}).get("signature_report_count")
    # `cpu_info` lives at the top level of the processed crash and again under the minidump's
    # `system_info`; take either, because the offline corpus's stubs carry only the latter.
    sysinfo = (raw.get("json_dump") or {}).get("system_info") or {}
    cpu = str(raw.get("cpu_info") or sysinfo.get("cpu_info") or "").strip()
    noise = (seed or {}).get("hardware_noise") or {}
    sample = noise.get("reports")
    flip_rate = noise.get("bit_flip_rate")
    cpu_rate = noise.get("broken_cpu_rate")

    # Recorded for EVERY verdict, fired or not: without the flags there is no way to count how
    # often the pipeline is looking at probable hardware, which is the measurement that would
    # settle these thresholds against `models.Feedback` outcomes later.
    flags = {}
    if confidence is not None:
        flags["possible_bit_flip_confidence"] = confidence
    if reports is not None:
        flags["signature_report_count"] = reports
    if cpu:
        # The flag keeps what Socorro actually said, verbatim -- it is the evidence, and the
        # renderings differ by build architecture. The COMPARISON is on the normalised identity,
        # because `BROKEN_CPUS` holds the amd64 rendering only and this read False on every 32-bit
        # Raptor Lake report until 2026-08-24 (bug 2065969); see `sigage.cpu_model`.
        flags["cpu_info"] = cpu
        flags["report_on_broken_cpu"] = sigage.cpu_model(cpu) in sigage.BROKEN_CPU_MODELS
    if sample is not None:
        flags["signature_hardware_sample"] = sample
    if flip_rate is not None:
        flags["signature_bit_flip_rate"] = round(flip_rate, 3)
    if cpu_rate is not None:
        flags["signature_broken_cpu_rate"] = round(cpu_rate, 3)
    # The CPU-model spread, recorded and never compared with a threshold: it is what the filed
    # bug prints (`report_bug.build_hardware_note`) and, in prod, the only way to ask later
    # whether concentration predicts anything at all. Swept as a suppressor and killed — see
    # `_signature_is_mostly_hardware`.
    top_share = noise.get("top_cpu_share")
    if top_share is not None:
        flags["signature_cpu_reports"] = noise.get("cpu_reports")
        flags["signature_cpu_terms"] = noise.get("cpu_terms")
        flags["signature_top_cpu_term"] = noise.get("top_cpu_term")
        flags["signature_top_cpu_share"] = round(top_share, 3)
    dossier.corroborations = {**(dossier.corroborations or {}), **flags}
    if v.decision == Decision.abstain:
        return

    # "Nobody else has ever hit this", which is what makes a per-report hardware signal decisive
    # rather than incidental. `None` (the lookup failed) is NOT a singleton.
    singleton = reports is not None and reports <= cfg["max_reports"]
    # Each branch names its flag as a LITERAL dict key and the merge below splats it, rather than
    # the three sharing a `key` variable: a key built in a variable is invisible to the registry
    # scanner (tests/test_corroboration_registry.py), and all three of these were declared,
    # written, firing in prod, and unseen by it. The name still reaches the log line, off the dict.
    suppressed = reason = None
    if confidence is not None and confidence >= cfg["min_confidence"] and singleton:
        suppressed = {"possible_bit_flip_suppressed": True}
        reason = (
            "Socorro rates the faulting address a possible hardware BIT FLIP (confidence {}%) "
            "and this signature has only ever been reported {} time(s) — the likeliest "
            "explanation is one bad machine, not a bug anyone can fix; suppressed rather than "
            "reported".format(confidence, reports)
        )
    elif flags.get("report_on_broken_cpu") and singleton:
        suppressed = {"broken_cpu_suppressed": True}
        reason = (
            "this crash came from a {} CPU — Intel Raptor Lake, whose documented instability "
            "corrupts computation on healthy software (meta bug 1975808) — and this signature "
            "has only ever been reported {} time(s); the likeliest explanation is the hardware, "
            "not a bug anyone can fix; suppressed rather than reported".format(cpu, reports)
        )
    elif _signature_is_mostly_hardware(sample, flip_rate, cpu_rate, cfg):
        suppressed = {"hardware_noise_signature_suppressed": True}
        reason = (
            "this SIGNATURE is mostly hardware error, whatever this particular report looks "
            "like: of its {} reports on this channel, {:.0f}% carry a Socorro bit-flip "
            "annotation (nightly background {:.0f}%) and {:.0f}% come from a known-defective "
            "Raptor Lake CPU (background {:.0f}%, meta bug 1975808). mozilla/bugbot declines to "
            "file past these same thresholds; suppressed rather than reported".format(
                sample, 100 * (flip_rate or 0), 100 * sigage.POPULATION_BIT_FLIP_RATE,
                100 * (cpu_rate or 0), 100 * sigage.POPULATION_BROKEN_CPU_RATE,
            )
        )
    if suppressed is None:
        return
    key = next(iter(suppressed))
    # A NEW Verdict, not ``model_copy``: an abstain must not carry the needinfo_draft
    # (``Verdict._consistency_rule`` rejects that outright) nor inherit ``p_worth_investigating``.
    dossier.verdict = Verdict(
        decision=Decision.abstain,
        confidence=Confidence.low,
        abstain_reason=reason,
        mechanism=v.mechanism,
        consistency=v.consistency,
    )
    dossier.corroborations = {**dossier.corroborations, **suppressed}
    logger.info(
        "agent: %s (flip conf %s, cpu %r, %s report(s), signature %s/%s hardware) -> %s/%s "
        "suppressed to abstain for %s",
        key, confidence, cpu, reports,
        "?" if flip_rate is None else "{:.0%}".format(flip_rate),
        "?" if cpu_rate is None else "{:.0%}".format(cpu_rate),
        v.decision.value, v.confidence.value, (seed or {}).get("uuid"),
    )


# How many distinct cited types one run may buy a ``searchfox --field-layout`` lookup for. The
# citation list is unbounded MODEL OUTPUT and every lookup is a subprocess, so without a cap a
# verdict could spend a run on CLI invocations. This is a COST BOUND, not a fitted threshold —
# there is no reachable corpus of ``struct_layout`` citations to fit it on (see
# ``_resolve_struct_layout``) — and its failure direction is the safe one: a citation past the
# cap is simply unverified, which under fail-closed means no promotion, never a wrong one.
_MAX_LAYOUT_LOOKUPS = 4


def _resolve_struct_layout(dossier, seed):
    """Re-derive the cited C++ layout from searchfox so the corroboration gate's fault-address
    match is a machine fact, and leave the answer on the seed for ``_corroborations``.

    WHY. The gate promoted on "the faulting address equals the byte offset of a field the
    model cited", and called that "a signal the model cannot fabricate". It was not one:
    ``triage._crash_facts`` prints ``Fault address: 0xN`` into the prompt, ``agent/roles.py``
    instructs the data-flow tracer to CONFIRM that fault by calling
    ``mcp__searchfox__field_layout`` and to emit a matching ``struct_layout`` citation, and
    nothing downstream re-derived anything — so the equality compared the model's number with
    the model's number, and a model that simply echoed the prompt scored identically to one
    that really looked. One CLI call closes it. Measured cost: 1.5s cold for
    ``mozilla::detail::nsTStringRepr`` (mData 0 / mLength 8 / mDataFlags 12 / mClassFlags 14 —
    the motivating case, bug 2053521, verifies) and 2.1s for ``nsINode``, against a ~20-minute
    run, and only on the runs that actually carry a fault-matching citation.

    FAIL CLOSED, WHICH IS THE OPPOSITE OF ``_resolve_compiled_out`` ON PURPOSE. Both obey one
    rule — a lookup we could not make must never MOVE a verdict — and they come out mirrored
    because that one suppresses and this one promotes: compiled-out says "never suppress on a
    lookup failure", so this says "never promote on one". searchfox-cli missing
    (``SearchfoxNotFound`` at construction), a timeout (retries+1 = 3 attempts x
    ``agent.searchfox.timeout_secs``=60 = ~3 min PER LOOKUP, so ~12 min if all four hang —
    against ``agent.job_timeout``=1800s on a ~20-min run, which is the tail worth watching;
    the measured happy path is 1.0-2.1s and the agent's own tool calls already run on the
    same budget many more times), a non-zero exit,
    or ``No field layout information found`` — which the CLI returns with EXIT 0 for a
    template, an under-qualified name or a non-class symbol, verified on
    ``nsTStringRepr<char>`` and ``mozilla::NoSuchTypeXyz`` — all land in ``unresolved``, and
    the lead keeps its raw rung. Nothing is lost when that happens: ANY lead is reported
    (there is no separate report gate), so the only thing withheld is the promotion to
    ``probable``, i.e. the autofile bar. That is the direction ``config`` already argues for
    in ``get_agent_second_opinion``: "promote conservatively, suppress readily".

    A REFUTATION IS NOT A SUPPRESSION. When searchfox answers and the cited field is NOT at
    that offset we record ``refuted`` (with the real field name at that offset in ``actual``)
    and withhold the promotion — we do not abstain the verdict. There is no measurement
    saying a bad layout citation predicts a bad lead, and inventing one would be the same
    n=1 error this audit is about.

    WHAT THIS COSTS, NAMED RATHER THAN ASSUMED AWAY. Verified end to end against the real
    binary: ``mozilla::detail::nsTStringRepr`` + ``mLength`` + 0x8 -> verified in 1.45s
    (bug 2053521 promotes, as it must); ``nsINode`` + ``mChildCount`` + 0x44 -> verified in
    2.12s, which is the deliberate proof that NO 8-alignment rule was shipped — nsINode really
    does place a 4-aligned uint32 there, and production shows live 4-aligned small faults
    (0x1c, 0x2c); ``mDataFlags`` at 0x8 -> refuted (the real member is ``mLength``); and the
    one that costs recall — the BARE ``nsTStringRepr`` -> unresolved, because
    ``--field-layout`` needs the fully-qualified name and answers "No field layout information
    found" for an under-qualified one. That failure is real enough that ``roles.py`` spends
    three lines warning the model about it, so some true matches WILL now be withheld for a
    spelling. Two things make that acceptable and both are recorded rather than hoped:
    the loss is a promotion, never a report, and ``fault_offset_unverified`` = ``unresolved``
    makes each one countable — if that count is large in prod, the fix is to re-qualify the
    symbol from the crash signature, which is a measurement the next session can now make and
    could not before.

    AND THE WRONG-DIRECTION CASE THIS CANNOT SEE: ``--field-layout`` prints INHERITED members
    in a separate "Base Classes" table that ``_parse_field_layout`` drops, so ``layout.fields``
    is the class's OWN members only. Measured live 2026-08-21: ``nsINode``'s own fields start
    at 48, ``mozilla::dom::Element``'s at 120 — so on a deeply-derived type every small fault
    sits in a region searchfox told us nothing about. Those record ``unresolved``, not
    ``refuted``, deliberately: it is a limit of the tool, not a model that invented an offset,
    and the two must not share a bucket or the ``refuted`` count means nothing. It is still a
    recall cost — a real base-class field deref cannot be promoted today — and it is the one
    most likely to dominate the ``unresolved`` column in prod.

    A CITATION THAT NAMES NO FIELD IS ``unresolved``, NOT ``verified``, AND THAT IS THE WHOLE
    POINT. ``StructLayoutCitation.field`` defaults to ``""``, so "the fault address is an
    offset into T" is a legal citation — and it asserts only the number ``_crash_facts``
    already printed. Verifying it against "T has SOME field starting there" would leave the
    door this function exists to close wide open, and cheaply: the audit measured that the
    mean class has a field starting on 33% of production's 13-value small-fault alphabet, and
    ``nsPresContext`` on 93.5% of it. So a field-less citation buys no lookup and no
    promotion. It costs recall only for a model that declines to say what it found, and it is
    countable like every other unresolved.

    ONLINE ONLY, and deliberately not inside ``apply_deterministic_gates`` — the same split as
    ``_resolve_candidate_backout`` / ``_resolve_compiled_out``, for the same reason (that
    ladder is shared with the offline eval runner). It runs BEFORE
    ``_maybe_run_second_opinion`` so ``_will_corroboration_promote``'s peek and the gate see
    the same answer. ``eval/runner.py`` calls it too — it already runs the fully online agent
    — because otherwise the change that makes this gate honest would also make it permanently
    unmeasurable offline, which is the opposite of what this audit needs.

    Resolves for any non-abstaining verdict carrying a fault-matching citation, not only for a
    promotable lead: ``dossier.corroborations`` is the only registry of what the gate saw, and
    a refuted citation on a strong-evidence verdict is exactly the event worth counting.
    Never raises."""
    v = getattr(dossier, "verdict", None)
    if v is None or v.decision == Decision.abstain:
        return
    fault = _fault_address((seed or {}).get("raw_crash"))
    if fault is None or not (0 < fault <= _MAX_FIELD_FAULT):
        return
    wanted, seen = [], set()
    for cit in _iter_dossier_citations(dossier):
        if not (isinstance(cit, StructLayoutCitation) and cit.offset == fault):
            continue
        key = (cit.type_name, cit.field)
        if key in seen:
            continue
        seen.add(key)
        wanted.append(cit)
    if not wanted:
        return
    wanted = wanted[:_MAX_LAYOUT_LOOKUPS]
    out = {"fault": fault, "verified": [], "refuted": [], "unresolved": []}

    from crashclouseau.searchfox import SearchfoxClient

    try:
        client = SearchfoxClient()
    except Exception as exc:  # pragma: no cover - binary missing / misconfigured
        logger.warning("agent: no searchfox client for struct-layout verification: %s", exc)
        client = None
    for cit in wanted:
        entry = {"type": cit.type_name, "field": cit.field, "offset": cit.offset}
        if not (cit.field or "").strip():
            # NOTHING TO RE-DERIVE. ``StructLayoutCitation.field`` defaults to "", so a
            # citation may name only a type and an offset — and the offset is the number the
            # prompt handed the model. Accepting it because the class happens to have SOME
            # field starting there would make the whole verification opt-out-able by omitting
            # one optional key, at the audit's own measured coincidence rate (mean 34% of
            # classes have a field starting on a given member of production's 13-value
            # small-fault alphabet; nsPresContext 93.5%). Unverifiable is unresolved.
            out["unresolved"].append({**entry, "reason": "citation names no field"})
            continue
        if client is None:
            out["unresolved"].append({**entry, "reason": "no searchfox client"})
            continue
        try:
            layout = client.field_layout(cit.type_name)
        except Exception as exc:
            # SearchfoxNoResult (template / under-qualified / non-class), timeout, non-zero
            # exit: all "we could not check", all fail closed.
            out["unresolved"].append({**entry, "reason": str(exc)[:200]})
            continue
        # EXACT field START, not ``field_at`` — that helper falls back to the field whose
        # [offset, offset+size) range CONTAINS the address, and a hit inside a 40-byte member
        # is not the "byte N is where field F begins" claim the gate promotes on.
        at = next((f for f in layout.fields if f.offset == cit.offset), None)
        if at is not None:
            if at.name.strip() != cit.field.strip():
                out["refuted"].append({**entry, "actual": at.name})
            else:
                out["verified"].append({**entry, "actual": at.name})
        elif not layout.fields or cit.offset < min(f.offset for f in layout.fields):
            # BELOW THE FIRST OWN FIELD = inside the BASE-CLASS SUBOBJECT, and searchfox
            # prints those in a SEPARATE "Base Classes" table that `_parse_field_layout`
            # drops (it keeps only 4-column rows). Measured against the real binary
            # 2026-08-21: `nsINode`'s own fields start at 48 and `mozilla::dom::Element`'s at
            # 120, so on a deeply-derived type EVERY small fault lands in a region the tool
            # told us nothing about. Calling that `refuted` would be a claim we cannot make,
            # and it would poison the one number this whole function exists to produce —
            # `refuted` is supposed to count "the model invented an offset". Unresolved, so
            # it fails closed like every other thing we could not check.
            out["unresolved"].append(
                {**entry, "reason": "offset is inside the unenumerated base-class subobject"}
            )
        else:
            out["refuted"].append({**entry, "actual": None})
    out["status"] = (
        "verified" if out["verified"] else "refuted" if out["refuted"] else "unresolved"
    )
    seed["struct_layout"] = out
    logger.info(
        "agent: struct-layout verification for %s at fault 0x%x -> %s (%d verified, "
        "%d refuted, %d unresolved)",
        (seed or {}).get("uuid"), fault, out["status"],
        len(out["verified"]), len(out["refuted"]), len(out["unresolved"]),
    )


def _resolve_compiled_out(dossier, seed):
    """Ask searchfox whether the mechanism's own machinery is compiled into this build, and
    store the answer on the seed for ``_apply_compiled_out_gate`` to act on.

    ONLINE ONLY, and deliberately not inside ``apply_deterministic_gates`` — the same split as
    ``_resolve_candidate_backout``, and for the same reason: that function is shared with the
    offline eval runner, which must stay network-free. Resolve here, decide there.

    Costs a handful of ``searchfox --define`` lookups plus one cached ``raw-rev`` fetch, and only
    for a verdict that is still reportable after every free gate above has had its chance — the
    ~80% of runs that abstain pay nothing. Best-effort: a failed lookup records nothing, and the
    gate then treats the mechanism as fine. Never suppress on a lookup failure."""
    v = getattr(dossier, "verdict", None)
    if v is None or v.decision == Decision.abstain or getattr(v, "mechanism", None) is None:
        return
    from crashclouseau import compiled_out
    from crashclouseau.agent import patch_extract

    channel = (seed or {}).get("channel") or "nightly"
    cand = getattr(dossier, "candidate", None)
    diff = ""
    if cand is not None and getattr(cand, "node", ""):
        diff = patch_extract.fetch_raw_diff(cand.node, channel) or ""
    symbols = compiled_out.mechanism_symbols(v.mechanism, diff)
    if not symbols:
        return
    rev = (seed or {}).get("pin_rev") or ""
    try:
        hollow = compiled_out.hollow_symbols(symbols, channel=channel, rev=rev)
    except Exception:  # pragma: no cover - defensive; never break a verdict
        logger.warning("agent: compiled-out lookup failed for %s",
                       (seed or {}).get("uuid"), exc_info=True)
        return
    if hollow:
        symbol, found = sorted(hollow.items())[0]
        # WHICH SENTENCE THE GATE MAY PUBLISH, and the clock it read the switch at. Both were
        # free and neither was recorded: `mechanism_symbols` draws half its input from the
        # candidate's diff ranked by occurrence count, and the published prose names 13% of
        # those (`compiled_out.statement_provenance`). `rev` is the build node the
        # `moz.configure` answer came from -- the same switch reads False at a rev three days
        # older (see `compiled_out._LITERAL_OFF`), so an unstamped answer is unreproducible.
        seed["compiled_out"] = {
            "symbol": symbol,
            "provenance": compiled_out.statement_provenance(symbol, v.mechanism),
            "rev": rev,
            **found,
        }


def _apply_compiled_out_gate(dossier, seed):
    """SUPPRESS a verdict whose mechanism rests on code that is not in the build that crashed.

    Three of the four module-owner refutations of our `Core :: JavaScript*` filings are this, all
    three from Jon Coppeard and all three about concurrent marking — "It does not" (2063782),
    "Concurrent marking is not compiled in by default" (2063902), "not present in any release
    builds so is not relevant to these crashes" (2062114). Two of them named the SAME changeset,
    by Coppeard: a subsystem behind a default-off flag fills the pushlog window with commits whose
    code cannot run, so it manufactures false regressors faster than anything else we have seen.

    THE TEST IS ON THE SYMBOL'S BODY, not on the citation's line, and that distinction was
    measured rather than assumed — see ``compiled_out``. PURE: ``_resolve_compiled_out`` did the
    looking up, so offline the seed simply lacks the key and this no-ops, the same documented
    fidelity gap as ``prior_hints`` and the backout gate.

    Back-tested over all 56 filings: fires on exactly 2, both refuted by their module owner for
    exactly this reason, and on 0 of the 16 a human FIXED or duplicated. That is why it suppresses
    rather than merely recording — but the corroborations are written whenever a hollow symbol is
    found, so a future false positive is countable rather than invisible.

    THE REASON STRING IS SPLIT BY PROVENANCE, because for a month it published a sentence its
    own predicate did not establish. ``compiled_out.mechanism_symbols`` draws half its input from
    the top-8 identifiers by OCCURRENCE COUNT IN THE CANDIDATE'S DIFF, and on the 52-filing panel
    the published analysis names only 35 of 269 such slots (13%); 45 of the 52 filings carry at
    least one diff-derived symbol the prose never mentions. On the two filings that fire, NEITHER
    reaches ``gc::AutoMarkingLock`` through a citation independent of the changeset -- 2063782
    only through the diff top-8, 2063902 through a ``diff_line`` citation whose content is a
    DELETED line of the candidate's own patch. So the diff-derived wording now says what the
    predicate actually shows: the CANDIDATE is mostly about a subsystem that cannot run in this
    build, which is a reason to doubt the candidate, not a refutation of the mechanism.
    ``compiled_out.statement_provenance`` decides, off the text we really posted (2 of 2 hit,
    0 of 50 false).

    TWO DIFFERENT AXES, AND THEY DISAGREE ON THE MOTIVATING CASE -- say which one is which or
    the next reader clamps the wrong branch. SYMBOL SOURCE (citation vs diff top-8) is what
    ``mechanism_symbols`` did: by that axis 2063782 is diff-only. STATEMENT PROVENANCE is what
    this reason string switches on, and by that axis 2063782 is ``mechanism``, because its
    published mechanism paragraph does name ``gc::AutoMarkingLock`` ("reached from
    `ParallelMarkTask` marking via `ICScript::trace`'s `gc::AutoMarkingLock`-guarded walk"). So
    ON TODAY'S PANEL THE DIFF BRANCH FIRES ON 0 OF 52: both real suppressions keep the mechanism
    wording, and the diff wording is provision for the 234-of-269 case, not a re-labelling of
    either counter-example.

    BOTH PROVENANCES STILL SUPPRESS, and clamping the diff-derived case instead is refused on
    two grounds, neither of them a back-test (there is nothing on the panel to back-test):
    a clamp would be untestable here, and ``autofile.min_confidence: 70`` means a clamp to
    ``probable`` still FILES -- it would needinfo the author of a default-off subsystem about
    his own inert code rather than stopping the bug. If the diff branch ever fires in prod,
    ``compiled_out_provenance`` makes it countable; decide the clamp on those, not on this
    paragraph.

    The reason also names the SWITCH and the BUILD REV. Both were already in hand and both were
    dropped: the same switch answers "not established off" at a rev three days older
    (``compiled_out._LITERAL_OFF``), so an answer with no rev on it cannot be reproduced, and
    "a moz.configure switch that is off unless someone asks for it" is not something a module
    owner can check without going to find which switch."""
    v = getattr(dossier, "verdict", None)
    found = (seed or {}).get("compiled_out")
    if v is None or v.decision == Decision.abstain or not found:
        return
    symbol, macro = found.get("symbol"), found.get("macro")
    provenance = found.get("provenance") or "diff"
    switch, rev = found.get("switch") or "", found.get("rev") or ""
    dead = (
        "`{}`, which is a NO-OP in this build: its {} body is entirely inside `#ifdef {}`, and "
        "`{}` {}{}"
        .format(symbol, "/".join((found.get("functions") or [])[:3]), macro, macro,
                "is off unless someone passes `{}`".format(switch) if switch else
                "comes from a moz.configure switch that is off unless someone asks for it",
                " (read from the moz.configure of the build that crashed, "
                "rev `{}`)".format(rev[:12]) if rev else "")
    )
    if provenance == "mechanism":
        reason = (
            "the mechanism rests on {}. The symbol is real and compiles; it simply does nothing "
            "here, so no story that depends on it can be what crashed. Suppressed rather than "
            "reported".format(dead)
        )
    else:
        reason = (
            "the candidate's changeset is mostly about {}. The published mechanism never names "
            "`{}` — it was found by ranking the changeset's own diff — so this is a reason to "
            "doubt the CANDIDATE rather than a refutation of the mechanism; a changeset whose "
            "subject cannot run in the build that crashed is not something to send anyone after. "
            "Suppressed rather than reported".format(dead, symbol)
        )
    dossier.verdict = Verdict(
        decision=Decision.abstain,
        confidence=Confidence.low,
        abstain_reason=reason,
        mechanism=v.mechanism,
        consistency=v.consistency,
    )
    dossier.corroborations = {
        **(dossier.corroborations or {}),
        "compiled_out_symbol": symbol,
        "compiled_out_macro": macro,
        "compiled_out_provenance": provenance,
        "compiled_out_rev": rev,
        "compiled_out_suppressed": True,
    }
    logger.info("agent: compiled_out_suppressed (%s is a no-op without %s, %s-derived, "
                "switch %s @ rev %s) -> %s/%s suppressed to abstain for %s",
                symbol, macro, provenance, switch or "?", rev[:12] or "?",
                v.decision.value, v.confidence.value, (seed or {}).get("uuid"))


def _apply_is_backout_gate(dossier, seed):
    """The candidate IS ITSELF a backout. Two different things follow, so this is two rules.

    NET-ZERO -> ABSTAIN. When the patch it reverts landed in the SAME push, no build ever
    shipped that patch: the tree's content is identical before the push and after it, so the
    candidate provably changed nothing and cannot have changed crash behaviour. This is a
    fact, not a confidence question, hence an outright abstain — the same treatment
    ``_apply_backout_gate`` gives a changeset that is no longer in the tree. Verified on
    ``00b44d2a-4343-4caa-9e12-907550260802``: fix and revert both in mozilla-central push
    44977, and ``dom/onnx/InferenceSession.cpp`` is byte-identical (sha1 ``1eea729e…``) at the
    fix's parent, at the revert, and at the rev the crashing build was made from.

    OTHERWISE -> CAP THE DECISION AT ``lead``. A backout whose reverted patch DID ship really
    can make a crash reappear, and "reland the fix" is actionable — the 2026-07-29 owner
    decision to keep such a changeset REPORTABLE stands, and a blunt is-a-backout->abstain
    rule would destroy exactly that value. But a backout restores prior behaviour rather than
    introducing new behaviour, so it is never the ORIGIN, and ``strong-evidence`` claims a
    verified end-to-end chain to a cause. The rung is left alone: how much a human should care
    is unchanged, only the claim about what was proven.

    Reads only ``candidate.is_backout`` / ``backout_of_same_push``, both set online by
    ``_resolve_candidate_is_backout``, so this is a no-op in the offline eval. Runs AFTER
    ``_apply_backout_gate`` (a candidate that was itself backed out is already suppressed, and
    a WAS-backed-out abstain must keep its own reason). Mutates in place; never raises."""
    v = dossier.verdict if dossier is not None else None
    cand = dossier.candidate if dossier is not None else None
    if v is None or cand is None or not cand.is_backout:
        return
    reverted = cand.backout_of_same_push
    dossier.corroborations = {
        **(dossier.corroborations or {}),
        "candidate_is_backout": True,
        **({"candidate_backout_same_push": reverted} if reverted else {}),
    }
    if v.decision == Decision.abstain:
        # Already not reported — record the fact for the page, leave the reason alone.
        return
    if reverted:
        # A NEW Verdict, not ``model_copy``, for the same reason as ``_apply_backout_gate``:
        # an abstain must not carry a needinfo_draft or inherit ``p_worth_investigating``.
        dossier.verdict = Verdict(
            decision=Decision.abstain,
            confidence=Confidence.low,
            abstain_reason=(
                "candidate {} is a BACKOUT of {}, and both landed in the SAME push — the "
                "tree never differed, so this changeset cannot have changed crash "
                "behaviour; suppressed rather than reported".format(
                    cand.node, reverted[:12]
                )
            ),
            mechanism=v.mechanism,
            consistency=v.consistency,
        )
        dossier.corroborations = {
            **dossier.corroborations, "candidate_backout_suppressed": True
        }
        logger.info(
            "agent: candidate %s backs out %s from its OWN push (net-zero) -> %s/%s "
            "suppressed to abstain for %s",
            cand.node, reverted[:12], v.decision.value, v.confidence.value,
            (seed or {}).get("uuid"),
        )
        return
    if v.decision != Decision.strong_evidence:
        return
    # The draft was written to name a CAUSE, and this branch is also where an UNRESOLVED
    # same-push lookup lands (``None`` -> ``""``), so the draft can assert a regression on a
    # check that never completed. Say what the changeset actually is, in front of it.
    draft = v.needinfo_draft
    if draft:
        draft = (
            "(This changeset is a backout: it restores earlier behaviour rather than "
            "introducing new behaviour, so at most it made the crash reappear — it is not "
            "its origin.)\n\n"
        ) + draft
    dossier.verdict = v.model_copy(update={
        "decision": Decision.lead,
        "needinfo_draft": draft,
        # ``high`` is reserved for a verified strong-evidence chain, and a lead's ``high`` is
        # clamped to ``probable`` by ``Verdict._consistency_rule`` — which ``model_copy`` does
        # NOT re-run, so do it here or the dossier ships an impossible ``lead``/``high``.
        "confidence": (
            Confidence.probable if v.confidence == Confidence.high else v.confidence
        ),
    })
    dossier.corroborations = {**dossier.corroborations, "candidate_backout_capped": True}
    logger.info(
        "agent: candidate %s is itself a backout -> strong-evidence/%s capped to lead/%s "
        "for %s",
        cand.node, v.confidence.value, dossier.verdict.confidence.value,
        (seed or {}).get("uuid"),
    )


def _will_corroboration_promote(dossier, seed):
    """Peek at whether ``_apply_corroboration_gate`` will raise this bare lead to ``probable``
    (a fault-address<->struct-offset or prior-signature match). Used ONLY by
    ``_maybe_run_second_opinion`` to decide whether a sub-threshold RAW lead still warrants a
    second opinion — the corroboration gate is an UPGRADE that runs AFTER the SO is dispatched,
    so a raw lead below ``min_confidence`` can still ship as a REPORTED ``probable`` lead.
    Mirrors the gate's ``is_bare_lead`` + strong-flag condition; ``_corroborations`` never
    raises. A no-op read (does not mutate the dossier). Shares ``_is_promotable_bare_lead``
    with the gate rather than restating it: when the two drifted, a lead the gate could no
    longer promote would still have bought a ~$1 independent review for a promotion that
    cannot happen (and, in the other direction, a promotable lead would ship unreviewed)."""
    v = dossier.verdict if dossier is not None else None
    if not _is_promotable_bare_lead(v):
        return False
    flags = _corroborations(dossier, seed)
    return bool(flags.get("fault_address_offset_match") or flags.get("prior_signature_match"))


def _maybe_run_second_opinion(result, seed):
    """Run the blind second-opinion (#SO) pass for a would-be-REPORTED lead. Returns
    ``(second_opinion, status)`` — the parsed ``SecondOpinion`` (``None`` when it did not run
    or the run failed) plus WHY, so a null SO is diagnosable in the persisted dossier rather
    than silently ambiguous between "ineligible" and "broken in prod". ``status`` is one of
    ``ok`` / ``failed`` / ``skipped_disabled`` / ``skipped_no_verdict`` / ``skipped_abstain`` /
    ``skipped_backedout`` / ``skipped_backout_netzero`` / ``skipped_below_threshold``.

    Prod-only and env-gated (``SECOND_OPINION_ENABLED``, default off); the offline
    eval runner never calls this. Keyed on the RAW (pre-gate) verdict: a raw abstain can never
    become a report (the SF-3 / exposer gates only ever DOWNGRADE), so we skip it; but the
    ``_apply_corroboration_gate`` UPGRADE can promote a sub-``min_confidence`` bare lead to
    ``probable`` (a reported rung), so we ALSO run when that promotion is pending — else a
    corroboration-rescued lead would ship reported with no independent check. Running here (in
    the async home) lets the SO be folded at the right point INSIDE ``apply_deterministic_gates``
    (after the corroboration gate settles the rung, before worth-investigating reads it).
    ``candidate`` is the dossier's candidate (``None`` => generator/mechanism mode). Best-effort:
    any failure returns ``None`` and the primary verdict is left untouched."""
    cfg = config.get_agent_second_opinion()
    if not cfg["enabled"]:
        return None, "skipped_disabled"
    dossier = result.dossier
    v = dossier.verdict if dossier is not None else None
    if v is None:
        return None, "skipped_no_verdict"
    if v.decision == Decision.abstain:
        return None, "skipped_abstain"
    cand = dossier.candidate
    if cand is not None and cand.backedout_by:
        # ``_apply_backout_gate`` will suppress this verdict outright, so an independent review
        # of the changeset costs ~$1 to measure something we are about to drop. Resolved before
        # this call in ``run_evidence_agent``, precisely so the money is never spent.
        return None, "skipped_backedout"
    if cand is not None and cand.backout_of_same_push:
        # Same reasoning for the net-zero backout: ``_apply_is_backout_gate`` is about to
        # suppress this outright. Worth its own status rather than reusing the one above —
        # the two are different failures and a merged label makes neither measurable.
        return None, "skipped_backout_netzero"
    rung = int(round(CONFIDENCE_SCORE.get(v.confidence, 0.0) * 100))
    if rung < cfg["min_confidence"]:
        # A pending deterministic corroboration bump (-> probable/70) still makes this a
        # reported lead that must get a second opinion; only skip when no such bump is due.
        promoted = int(round(CONFIDENCE_SCORE[Confidence.probable] * 100))
        if not (promoted >= cfg["min_confidence"] and _will_corroboration_promote(dossier, seed)):
            return None, "skipped_below_threshold"
    cand = dossier.candidate
    candidate = {"node": cand.node, "bug": cand.bug} if (cand and cand.node) else None
    try:
        # Lazy import: pulls claude-agent-sdk only on the worker path that actually runs it
        # (mirrors run_crash_triage), so the enqueue path / web dyno never load the SDK.
        from crashclouseau.agent.second_opinion import run_second_opinion

        so = asyncio.run(run_second_opinion(seed, candidate))
    except Exception:
        logger.warning(
            "agent: second-opinion pass failed for %s",
            (seed or {}).get("uuid"), exc_info=True,
        )
        return None, "failed"
    if so is None:
        # ``run_second_opinion`` swallows its own errors and returns None (errored / empty /
        # unparseable run). Log it here too: an eligible lead that got no second opinion is a
        # PROD BREAK, and it must not look like a skip.
        logger.warning(
            "agent: second-opinion returned no result for %s (eligible lead, rung=%s)",
            (seed or {}).get("uuid"), rung,
        )
        return None, "failed"
    logger.info(
        "agent: second-opinion for %s -> mode=%s corroborates=%s confidence=%s cost=$%s",
        (seed or {}).get("uuid"), so.mode, so.corroborates, so.confidence, so.cost_usd,
    )
    return so, "ok"


def apply_deterministic_gates(result, seed, second_opinion=None, second_opinion_status=None):
    """Reshape the RAW agent verdict into the SHIPPED verdict with the deterministic,
    outside-the-LLM gates: attach area-experts, then the SF-3 call-path gate (off-stack
    only), the exposer classifier (all crashes), the corroboration gate (fault-offset /
    prior-signature lead->probable bump) and the stale-signature downweight; fold the blind
    second opinion when one was run; suppress a backed-out candidate and a probable hardware bit
    flip; re-derive the bridged needinfo from the FINAL verdict; and apply off-stack observe-only
    last. Mutates ``result`` / ``result.dossier`` in place and returns it.

    Extracted so the OFFLINE eval runner applies the exact same post-verdict reshaping as
    ``run_evidence_agent`` — the calibration must score the pipeline we SHIP, not the raw
    model output (a strong->lead downgrade or a lead->probable bump changes the confidence
    rung the calibration keys on). ``seed`` is the ``build_seed`` crash dict (needs
    ``is_offstack``, ``experts``, ``raw_crash``, ``candidates``, ``prior_*``). ``second_opinion``
    is the parsed ``SecondOpinion`` from ``_maybe_run_second_opinion`` (``None`` for eval and
    when the pass is disabled/didn't run) and ``second_opinion_status`` is that call's outcome
    string (``None`` for eval, where the gate never runs)."""
    # Snapshot the RAW, pre-gate verdict FIRST — before any gate below can mutate it — so the
    # gates' NET effect stays auditable in the persisted dossier. Without it a second-opinion
    # clamp to `medium` is indistinguishable from a no-op on a lead that was already `medium`,
    # and "how often does a gate actually MOVE the verdict?" is unanswerable from prod data.
    # ``model_copy(deep=True)`` so the snapshot does not alias the Claim objects the gates
    # rebuild, and does NOT re-run validators (a gate may legitimately produce a combination
    # the raw-verdict rules would reject, e.g. a downgraded strong-evidence).
    if result.dossier is not None and result.dossier.verdict is not None:
        result.dossier.raw_verdict = result.dossier.verdict.model_copy(deep=True)

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
        # signal (a weak hint just sets a UI chip). Off-stack keeps its config knob. The
        # downgrade lands on `probable` (70), NOT `medium` (50): at 50 it sat under
        # `autofile.min_confidence` and the "soft lead" was a silent suppression.
        if offstack_cfg is None or offstack_cfg["exposer_classifier"]:
            _classify_exposer(result.dossier, seed)
        # Corroboration gate: a fault-address<->struct-field-offset OR prior-signature
        # match raises a bare lead (medium/50%) to `probable` (70%).
        _apply_corroboration_gate(result.dossier, seed)
        # Stale-signature downweight: the signature already existed long before this build, so
        # no window changeset introduced it. Runs AFTER the corroboration gate deliberately —
        # that gate's evidence (a fault-address <-> struct-offset match) is about the MECHANISM,
        # while this is about whether the window can contain the origin at all, so the timing
        # clamp should get the last word on the rung. Running it first would just hand a
        # downweighted lead back to the bump.
        _apply_signature_age_gate(result.dossier, seed)
        # Second-opinion fold: an independent blind re-analysis corroborates (boost) or
        # confidently refutes (downgrade) the reported lead. Runs AFTER the corroboration
        # gate (so a corroboration-bumped lead is what's boosted/refuted) and BEFORE the
        # needinfo reconcile + worth-investigating below (so a downgrade re-derives the
        # action and worth-investigating reads the final rung). No-op when no SO was run.
        _fold_second_opinion(
            result.dossier, second_opinion, seed, status=second_opinion_status
        )
        # Backed-out candidate -> nothing to act on -> abstain. LAST of the verdict gates, and
        # after the second-opinion fold, because it is the only unconditional one: no boost,
        # corroboration or independent agreement can make a changeset that is not in the tree
        # actionable, so it must get the final word on the rung. A pure read of
        # ``candidate.backedout_by`` (resolved online by ``_resolve_candidate_backout``), hence a
        # no-op in the offline eval and no network call on this shared path.
        _apply_backout_gate(result.dossier, seed)
        # The MIRROR predicate, and the last word after it: the candidate IS ITSELF a backout.
        # Net-zero (it reverts a patch from its own push) -> abstain; otherwise cap the
        # decision at `lead`, because a backout restores behaviour and is never an origin.
        # After `_apply_backout_gate` so a WAS-backed-out abstain keeps its own reason.
        _apply_is_backout_gate(result.dossier, seed)
        # Hardware, not software: Socorro says the fault address is one flipped bit from a
        # plausible value and nobody else has ever hit this signature. Last of all, because it
        # is the only gate whose finding is "there is no bug here" rather than "this candidate
        # is wrong" — and because the second-opinion boost above it is exactly what pushed bug
        # 2061961 to the filing threshold.
        _apply_bit_flip_gate(result.dossier, seed)
        # The machine, not the crash: an installation already scattering unrelated signatures is
        # broken hardware. Alongside the bit-flip gate rather than folded into it because the two
        # are DISJOINT on real filings — the ones carrying a flip score have a clean machine, and
        # the scattering ones have no flip score at all.
        _apply_bad_machine_gate(result.dossier, seed)
        # The mechanism runs through a thread this process never had (bug 2064436). After the
        # hardware gates because it is a weaker instrument than they are — it reads the verdict's
        # prose rather than a Socorro field — and so it is a clamp, not a suppression.
        _apply_absent_thread_gate(result.dossier, seed)
        # The mechanism's own machinery is not in this build (bug 2063782 and two siblings).
        # LAST of the gates: it is the only one that spends network per verdict, and it returns
        # at once on an abstain, so everything above it has already had its chance to abstain
        # for free.
        _apply_compiled_out_gate(result.dossier, seed)
        # Not a gate — a label. Whether the candidate came from this build's pushlog window is
        # what decides if the filed bug may call it a "regression" at all.
        _record_window_membership(result.dossier, seed)
        # Also not a gate: how old the signature was on each of the two clocks. The windowed one
        # is what `_apply_signature_age_gate` above already reasoned from; the unbounded one says
        # how wrong that was. Recorded for every verdict so the gap is measurable before any rung
        # depends on it.
        _record_signature_age_facts(result.dossier, seed)
        # Which learned archetypes were in front of the agent. Recorded even when none matched
        # (as an empty list) so "this run saw no hints" and "this run predates the feature" stay
        # distinguishable — `Feedback` joins on this to score a rule against real outcomes.
        fired = [h.get("slug") for h in (seed.get("archetypes") or []) if h.get("slug")]
        if seed.get("archetypes") is not None:
            result.dossier.corroborations = {
                **(result.dossier.corroborations or {}), "archetypes": fired}
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


def _resolve_candidate_git_commit(dossier, seed):
    """Store the chosen candidate's GIT sha and AUTHOR EMAIL on the dossier — the first for
    the filed bug's ``(gh)`` link, the second for its needinfo.

    Done HERE, once per run, rather than when the bug comment is rendered: hg's ``json-rev``
    takes 8-13s, which is fine inside a ~20-minute analysis and unacceptable on a page view
    (it made a cold crashstack render take 15s). The stale-signature gate has usually just
    fetched the same URL for this same node, so this is normally a cache hit and costs nothing.

    The author email has to come from hg. The model supplies ``candidate.author`` as a bare
    display name ("Jon Coppeard", "stransky") and the local ``Node.authors_for`` record is
    empty for most candidates, so a dry run over 12 recent rung-70 leads resolved an email
    for only 3 — the other 9 would have filed a bug with NO needinfo, which is most of the
    point. hg's ``user`` field ("Jon Coppeard <jcoppeard@mozilla.com>") is authoritative and
    already in the response this function fetches.

    Best-effort and additive — an unresolved sha omits the gh link, an unresolved email omits
    the needinfo. Never raises."""
    cand = dossier.candidate if dossier is not None else None
    if cand is None or not cand.node or (cand.git_commit and cand.author_email):
        return
    try:
        from crashclouseau import sigage

        rev = sigage.json_rev(cand.node, (seed or {}).get("channel")) or {}
    except Exception:
        logger.warning("agent: json-rev lookup failed for %s", cand.node, exc_info=True)
        return
    update = {}
    if not cand.git_commit and rev.get("git_commit"):
        update["git_commit"] = rev["git_commit"]
    if not cand.author_email:
        email = _email_from_hg_user(rev.get("user"))
        if email:
            update["author_email"] = email
    if update:
        dossier.candidate = cand.model_copy(update=update)


_HG_USER_EMAIL = re.compile(r"<([^<>@\s]+@[^<>@\s]+)>")


def _email_from_hg_user(user):
    """The email out of an hg ``user`` field, ``"Real Name <a@b.c>"`` -> ``"a@b.c"``.

    Requires the angle-bracket form on purpose. Some hg users are a bare address and a few
    are a bare name; accepting anything that merely contains an ``@`` would eventually
    needinfo a string that is not a person."""
    m = _HG_USER_EMAIL.search(user or "")
    return m.group(1) if m else ""


_HEARTBEAT_INTERVAL_S = 120


@contextmanager
def _heartbeat(uuid):
    """Stamp ``Dossier.updated`` every couple of minutes for as long as the triage runs, so
    ``updated`` means "last known alive" instead of "started".

    The triage is one ~20-minute ``asyncio.run`` with no natural checkpoint to stamp from, so
    this is a daemon thread rather than a hook in the pipeline. It pushes its OWN Flask app
    context per beat — the import-time ``app.app_context().push()`` is main-thread only (the
    same reason ``reap_stale_agent_jobs`` pushes one), and a fresh context per beat also avoids
    holding a DB session open and idle across the whole run.

    A failing beat is logged and the loop CONTINUES: a transient DB blip must not silently end
    the heartbeat, because a heartbeat that stops while the run lives is exactly what would let
    the reaper duplicate live work. Daemon + a bounded join, so it can never hold up a worker."""
    stop = threading.Event()

    def beat():
        while not stop.wait(_HEARTBEAT_INTERVAL_S):
            try:
                with app.app_context():
                    models.Dossier.heartbeat(uuid)
            except Exception:
                logger.warning("agent: heartbeat failed for %s", uuid, exc_info=True)

    t = threading.Thread(target=beat, name="hb-{}".format(str(uuid)[:8]), daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=5)


def _autofile(uuid, payload, row):
    """Hand a settled, PERSISTED run to the automatic Bugzilla filer.

    Deliberately swallows everything. The dossier is already committed by the time this
    runs, so any exception escaping here would turn a successful analysis into a run the
    caller marks ``error`` — and then the reaper would re-run it and pay for it twice.
    Gating lives entirely in ``bugzilla_apply.autofile_bug``; this only supplies the crash
    context it needs and keeps the failure contained."""
    try:
        from crashclouseau import bugzilla_apply
        stack, uuid_info = models.CrashStack.get_by_uuid(uuid)
        if not uuid_info:
            return
        res = bugzilla_apply.autofile_bug(
            uuid, uuid_info, stack, payload.get("dossier") or {},
            row["verdict"], row["confidence"],
        )
        if res.get("filed"):
            logger.info("agent: %s filed bug %s (%s)", uuid, res["bug"], res["mode"])
        elif res.get("skipped") not in (None, "autofile disabled"):
            logger.info("agent: %s not filed — %s", uuid, res["skipped"])
    except Exception:                                    # pragma: no cover - defensive
        logger.error("agent: autofile raised for %s (analysis is safe)", uuid, exc_info=True)


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
        # Our own RQ job id, so both liveness decisions below can recognise a `running`
        # row this very job left behind when its worker was SIGKILLed (every deploy) and
        # re-take it instead of waiting out the reaper. Needed HERE as well as at the
        # claim: this early-out runs first, so without it the claim's arm is unreachable
        # for every non-forced job. See ``Dossier.claim_running``.
        own_job_id = getattr(_current_job(), "id", None)
        # Cheap cost dedup early-out (already-done / a same-proto sibling). A forced
        # retrigger bypasses it; the atomic claim below is still the real guard.
        if skip_dedup and not force and (
            models.Dossier.skip_triage(uuid, stale_after, own_job_id=own_job_id) or _proto_already_triaged(uuid)
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
            if not models.Dossier.claim_running(
                uuid, stale_after, own_job_id=own_job_id
            ):
                logger.info("agent: %s claimed by another worker / settled; skipping", uuid)
                return
        else:
            models.Dossier.upsert(uuid, payload={}, status="running", seed_score=seed_score)

        # Record the RQ job id so a retrigger can stop this run mid-flight; it also stamps
        # when THIS attempt started. Read back so the settling write can carry it forward
        # (that write replaces the payload wholesale).
        _record_job_id(uuid)
        run_started = (getattr(models.Dossier.get_by_uuid(uuid), "payload", None) or {}).get(
            "run_started"
        )

        # Heartbeat the WHOLE run, not just the agent call. `updated` is the only liveness signal
        # a dossier has: RQ's SIGKILL at job_timeout beats the error handler, so an abandoned run
        # leaves `error`/`cost_usd` NULL, and without beats `updated` stays frozen at the start —
        # "died at minute 2" reads the same as "died at minute 29". Started AFTER claim_running (a
        # beat is guarded on status=running, so beating before we own the row could keep ANOTHER
        # worker's orphan looking alive) and held past the final write, because the tail — backout
        # resolve, the ~$1 second opinion with its own API retries, the gates, the git-sha lookup —
        # is minutes long, and a live run that stops beating is exactly what would let the reaper
        # duplicate work in flight.
        with _heartbeat(uuid):
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

            # Was the chosen candidate backed out — or is it ITSELF a backout? Resolved HERE,
            # before the second opinion, so a candidate we are about to suppress never buys a
            # ~$1 independent review. Online only (a cached hg lookup, plus one json-pushes
            # request on the ~0.5% of runs that name a backout); the gates that act on the
            # answers live in the shared ladder.
            _resolve_candidate_backout(result.dossier, seed)

            # Is the mechanism's own machinery even in this build? Online (searchfox + one
            # cached raw-rev), and beside the backout resolver for the same reason: the gate
            # that acts on the answer lives in the shared, offline-safe ladder. After the
            # backout resolve so a candidate we are about to suppress buys no lookups.
            _resolve_compiled_out(result.dossier, seed)

            # Does the cited struct layout actually say what the model said it says? One
            # `searchfox --field-layout` call, and only when a `struct_layout` citation
            # already matches the fault address. BEFORE the second opinion because
            # `_will_corroboration_promote` peeks at the same answer to decide whether a
            # sub-threshold lead is worth reviewing.
            _resolve_struct_layout(result.dossier, seed)

            # Blind second-opinion (#SO): an independent, no-context re-analysis of a
            # would-be-reported lead, run from the RAW verdict (async home) and folded inside the
            # gates below. Prod-only / env-gated (SECOND_OPINION_ENABLED); None otherwise.
            second_opinion, second_opinion_status = _maybe_run_second_opinion(result, seed)

            # Reshape the raw agent verdict into the shipped verdict (area-experts + the
            # callpath/exposer/corroboration gates + the second-opinion fold + needinfo reconcile
            # + observe-only). Shared with the offline eval runner so calibration scores the
            # pipeline we ship (the eval runner passes no second opinion).
            apply_deterministic_gates(
                result, seed,
                second_opinion=second_opinion,
                second_opinion_status=second_opinion_status,
            )
            # Resolve the candidate's git sha for the filed bug's (gh) link. Deliberately OUTSIDE
            # apply_deterministic_gates: that function is shared with the offline eval runner, and
            # an hg json-rev call (8-13s) per corpus crash would wreck an eval run's runtime and
            # its determinism. Online only, once per run, usually a cache hit from the gate above.
            _resolve_candidate_git_commit(result.dossier, seed)

            # ``result.actions`` is the single source of truth (build_result folds the
            # recorder's actions + the synthesized needinfo into it); model_dump already
            # carries it, so don't overwrite with the raw recorder here — that would drop
            # the bridged needinfo action the apply UI needs.
            payload = result.model_dump(mode="json")
            if over_budget:
                payload["over_budget"] = True

            # Did the reaper put us here? The upsert below replaces `payload` WHOLESALE, so a
            # recovered run erased its own attempt counter on the way out — which is what made
            # the reaper's recovery rate unmeasurable: the counter survived only on runs that
            # FAILED, so "0 of 28 reaped dossiers ever reached done" was a tautology, not a
            # measurement. Carrying it forward makes `where payload ? 'reap_attempts' group by
            # status` finally answer "how often does the reaper actually recover a run?".
            #
            # Read HERE and not at the claim, deliberately: this is the last thing before the
            # settling write, so the SELECT's transaction closes a few statements later. Read
            # at the claim, it would be the last statement on this session before the ~20-minute
            # agent call (nothing in the agent phase touches db.session, and the heartbeat runs
            # on its own), leaving the connection idle-in-transaction for the whole run and
            # pinning the xmin horizon against autovacuum.
            reap_attempts = models.Dossier.get_reap_attempts(uuid)
            if reap_attempts:
                payload["reap_attempts"] = reap_attempts
            # Same carry-forward, same reason: this upsert REPLACES the payload wholesale,
            # so without it `run_started` survives only on rows that failed. A `done` row
            # would fall back to `created` — the crash's first ingest, which a retrigger
            # leaves untouched — and go on rendering a 16-minute run as "29h", which is
            # also what feeds the fleet's average-duration stat.
            if run_started:
                payload["run_started"] = run_started

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
            # File the bug LAST, after the analysis is committed. A filing failure must
            # never cost us the run: the dossier is already durable, and `autofile_bug`
            # returns rather than raises, so the worst case is a crash we analysed and
            # didn't report — recoverable — instead of one we analysed and lost.
            _autofile(uuid, payload, row)
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
            if isinstance(exc, MissingHandoffError):
                # The run reached a terminal ResultMessage and we PAID for it — it just
                # never emitted a handoff. This row used to go down the `done` path,
                # which records cost/tokens; `set_status` records neither, so moving the
                # failure to `error` would otherwise LOSE the spend that the old silent
                # abstain at least accounted for. (The 0.2.131 regression's ~$68 was
                # always in the DB — what went unnoticed was the failure, not the money.
                # Keep both.) The final text is the whole diagnosis: a progress note, a
                # truncated verdict and a malformed block are three different bugs and
                # they are told apart by reading it.
                #
                # Via `merge_payload`, NOT `upsert`: upsert replaces the payload
                # wholesale and would drop the `error` string `set_status` just wrote
                # (plus job_id / reap_attempts).
                models.Dossier.upsert(
                    uuid,
                    cost_usd=exc.cost_usd,
                    input_tokens=exc.input_tokens,
                    output_tokens=exc.output_tokens,
                    cache_read_tokens=exc.cache_read_tokens,
                )
                models.Dossier.merge_payload(
                    uuid,
                    {"result": _elide(exc.raw_result), "num_turns": exc.num_turns},
                )
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


def _live_job_uuids(queue):
    """UUIDs that already have a live job on the agent queue — waiting for a free worker,
    started, parked in the scheduler for a retry, or deferred.

    A ``pending`` dossier means "a job was enqueued for this uuid and has not claimed it
    yet", and the reaper reads that as a LOST job. It usually is. It is not when the job
    is simply behind other jobs: three agentworkers at ~16 min a run drain ~11 jobs an
    hour, so in any batch bigger than that the tail waits longer than
    ``job_timeout + _STALE_BUFFER_S`` (35 min) purely by queueing. The reaper then
    re-enqueues it — putting it at the BACK of the same queue, which makes the wait worse
    — and after ``reap_max_attempts`` marks it ``error``. An operator bulk-retrigger of 83
    uuids lost 68 that way, none of which had anything wrong with them.

    Elapsed time cannot tell the two apart; the queue can. Matching is on the job's first
    arg because ``enqueue_agent`` enqueues ``run_evidence_agent(uuid)`` and the payload's
    ``job_id`` is only written once the run STARTS — a queued job has no id recorded
    anywhere, which is exactly the state in question.

    Raises nothing: the caller must treat a failure as "cannot tell", not as "none alive".
    """
    from rq.registry import (
        DeferredJobRegistry,
        ScheduledJobRegistry,
        StartedJobRegistry,
    )

    job_ids = list(queue.get_job_ids())
    for reg in (StartedJobRegistry, ScheduledJobRegistry, DeferredJobRegistry):
        job_ids.extend(reg(queue=queue).get_job_ids())
    if not job_ids:
        return set()
    jobs = queue.job_class.fetch_many(
        job_ids, connection=queue.connection, serializer=queue.serializer
    )
    return {j.args[0] for j in jobs if j is not None and j.args}


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

            # Drop the pending dossiers whose job is alive and merely queued. Fails SAFE:
            # if the queue cannot be read we do not know whether they are lost, so we skip
            # the pending sweep this pass rather than assume the worst — the reaper runs
            # again shortly, while a wrong give-up is terminal. Only `pending` is filtered;
            # a `running` dossier's job sits in StartedJobRegistry whether its worker is
            # alive or SIGKILLed, so queue membership proves nothing there and the
            # heartbeat stays the signal.
            if stale_pending:
                try:
                    live = _live_job_uuids(queue)
                except Exception:
                    logger.warning(
                        "agent: reaper could not read the queue; skipping the pending "
                        "sweep (%d candidates) rather than risk failing queued runs",
                        len(stale_pending), exc_info=True,
                    )
                    stale_pending = []
                else:
                    queued = [u for u in stale_pending if u in live]
                    if queued:
                        logger.info(
                            "agent: reaper left %d pending run(s) alone — still queued "
                            "behind a backlog, not lost: %s",
                            len(queued), ", ".join(queued),
                        )
                    stale_pending = [u for u in stale_pending if u not in live]
            if not stale_running and not stale_pending:
                return 0
            reenqueued: list = []
            gaveup: list = []

            def _reap_one(uuid, force):
                # GIVE-UP CAP: a crash that keeps orphaning must not be re-enqueued
                # forever (unbounded token burn). Count attempts in the dossier payload;
                # past the cap, fail it VISIBLY (status=error + reason) instead of
                # re-running — an operator can retrigger (which clears the counter). The
                # cap (>=2) still covers a one-off transient orphan.
                #
                # The reason states only what we actually know — that the dossier stopped
                # beating and never came back. It used to assert "likely OOM/stall on every
                # run", which sent weeks of investigation after memory limits when the real
                # cause was this reaper re-enqueuing runs that skipped themselves.
                n = models.Dossier.bump_reap_attempts(uuid)
                if n > cap:
                    models.Dossier.set_status(
                        uuid, "error",
                        error="reaper gave up after {} re-enqueue attempts: no heartbeat "
                              "for {}s on each try, so the run never came back alive; "
                              "retrigger to retry".format(cap, stale_after),
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


_SWEEP_MARK = "untriaged"


def sweep_untriaged_crashes():
    """Offer the agent the crashes it was never given: ingested, has a stack, no dossier at all,
    and a proto-signature cluster that has never been usably triaged. Called periodically by the
    clock. Best-effort (never raises out); returns how many were enqueued.

    THIS RECOVERS A LOSS THAT LEAVES NO TRACE. A queued agent job lives only in Redis, and this
    deployment's Redis is the Mini plan — ``Persistence: None`` — so a restart drops the queue
    entirely. Nothing records it: the dossier row is created by ``claim_running`` when a worker
    PICKS UP the job, so a job that never runs leaves no row, no error, and no log line. On prod
    2026-08-12 there were 86 such crashes (~3.4/day, in bursts of 8 — the shape of a discrete loss
    event), and 16 of them carried an on-stack score, meaning ``build_seed`` would have produced a
    seed and the run WOULD have written a dossier. Those 16 were dropped, not declined. The
    reaper cannot help: it works from dossier rows, and there are none.

    BOUNDED THREE WAYS, because this spends money unattended:

    * ``SweepMark`` — each crash is offered at most ONCE, ever. The candidate set cannot
      distinguish a lost job from a run that returned before writing anything (``build_seed``
      found nothing to reason about), and the latter would be re-offered on every tick forever
      for a guaranteed no-op. See ``models.SweepMark``.
    * ``sweep_max_per_run`` — a cap per tick, so the backlog drains at a rate an operator chose
      rather than as one surprise bill.
    * ``sweep_min_age_s`` — a grace period, so the sweep never re-enqueues the LIVE queue. A
      merely-queued crash also has no dossier row.

    Enqueued NOT forced: the proto gate gets to refuse, which is what keeps this from paying for a
    cluster something else has triaged in the meantime."""
    try:
        with app.app_context():  # clock thread has no Flask app context; see the reaper
            cfg = config.get_agent_sweep()
            if not cfg["enabled"]:
                return 0
            after = models.SweepMark.get(_SWEEP_MARK)
            candidates = models.UUID.untriaged(
                after, cfg["min_age_s"], cfg["max_age_s"], cfg["max_per_run"],
                channels=config.get_agent_channels(),
            )
            if not candidates:
                return 0

            # Belt and braces over the grace period: a candidate whose job is genuinely still on
            # the queue must not be enqueued twice. Fails SAFE the same way the reaper's pending
            # sweep does — if the queue cannot be read we do not know, so we skip this pass
            # rather than risk duplicating work. Skipping costs one tick; the mark is not
            # advanced, so nothing is lost.
            queue = worker.get_queue(config.get_agent_queue())
            try:
                live = _live_job_uuids(queue)
            except Exception:
                logger.warning(
                    "agent: sweep could not read the queue; skipping this pass (%d "
                    "candidate(s)) rather than risk a duplicate run", len(candidates),
                    exc_info=True,
                )
                return 0

            enqueued, skipped = [], []
            for _id, uuid, channel in candidates:
                if uuid in live:
                    skipped.append(uuid)
                    continue
                try:
                    enqueue_agent(uuid, channel)
                    enqueued.append(uuid)
                except Exception:
                    logger.warning("agent: sweep could not enqueue %s", uuid, exc_info=True)

            # Advance past everything CONSIDERED, including what the gate or the queue filter
            # declined: they were examined, and re-examining them next tick is the loop this
            # mark exists to prevent. A crash still queued now would have been analysed by then
            # anyway.
            models.SweepMark.set(_SWEEP_MARK, max(c[0] for c in candidates))
            logger.info(
                "agent: sweep enqueued %d untriaged crash(es) past id %d%s%s",
                len(enqueued), after,
                "; still queued: {}".format(", ".join(skipped)) if skipped else "",
                ": {}".format(", ".join(enqueued)) if enqueued else "",
            )
            return len(enqueued)
    except Exception:  # pragma: no cover - defensive; the clock must survive
        logger.error("agent: sweep failed", exc_info=True)
        return 0


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
