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
            sig_report_count = history["total"]
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
        # How many reports this signature has EVER had (whole window, not from this build on —
        # `report_bug.fetch_signature_stats` computes that other quantity for the bug comment).
        # ``None`` means the lookup failed and must never read as "a singleton".
        "signature_report_count": sig_report_count,
        "candidate_pushdates": candidate_pushdates,
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
        # Mark that this lead is a PRECISION-DOWNGRADE of a strong-evidence verdict (SF-3 /
        # exposer / a confident second-opinion refutation). The second-opinion boost keys on
        # this so an independent "it's related" agreement can NOT re-inflate a deliberately
        # suppressed verdict back to `probable` — e.g. an exposer IS "related" (reverting it
        # stops the crash) yet must stay a medium lead, not become a probable cause.
        dossier.corroborations = {
            **(dossier.corroborations or {}), "downgraded_from_strong": True
        }
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
        downgraded = bool((dossier.corroborations or {}).get("downgraded_from_strong"))
        is_bare_lead = v.decision == Decision.lead and CONFIDENCE_SCORE.get(v.confidence, 0.0) < CONFIDENCE_SCORE[Confidence.probable]
        # Measuring a lead does NOT license re-ranking it: below `min_boost_confidence` a boost
        # would jump two rungs (low -> probable, p_worth 0.50 -> 0.97) on the weaker of the SO's
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
        elif is_bare_lead and not downgraded:
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
        elif is_bare_lead and downgraded:
            # A precision gate (SF-3 / exposer) demoted a strong verdict to this lead; an
            # independent "it's related" agreement can't tell an exposer from a root cause, so
            # keep it suppressed (record the agreement, don't re-inflate the band).
            logger.info(
                "agent: second-opinion corroborated but lead was precision-downgraded from "
                "strong-evidence; NOT re-inflating to probable for %s",
                (seed or {}).get("uuid"),
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
    flags["stale_signature_clamped"] = True
    dossier.corroborations = {**dossier.corroborations, **flags}
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

    So this only labels; it moves no rung. ``seed["candidate_pushdates"]`` holds exactly the
    seeded window candidates (``build_seed`` builds it from the pushlog), so membership is a dict
    lookup — no network, offline-safe, and unrecorded (rather than ``False``) when there is no
    map to consult, because the bug comment must not claim a window it never had."""
    cand = dossier.candidate if dossier is not None else None
    if cand is None or not cand.node:
        return
    pushdates = (seed or {}).get("candidate_pushdates")
    if not pushdates:
        return
    dossier.corroborations = {
        **(dossier.corroborations or {}),
        "candidate_in_pushlog_window": cand.node in pushdates,
    }


def _apply_bit_flip_gate(dossier, seed):
    """SUPPRESS a verdict whose crash was probably a HARDWARE bit flip: there is no bug at all.

    Bug 2061961 is why. Crash ff888d42-ce3e-4308-8c2f-b3f060260807 faulted at
    ``0x00000001000000d0`` — one flipped bit from ``0xd0``, i.e. a NULL base plus a struct offset,
    and had the pointer really been null the code would have taken its ``None`` branch and not
    crashed. Socorro had already worked this out and published
    ``possible_bit_flips_max_confidence: 62``. Nothing in the pipeline read it, so the agent
    produced a fluent, fully-cited use-after-free story, the blind second opinion (which shares
    the same crash brief, and so the same blind spot) agreed and BOOSTED the rung from medium to
    probable — exactly the filing threshold — and a developer was needinfo'd about a mechanical
    refactor of his. Two people closed it INVALID in two days on this one field.

    THE CONJUNCTION IS THE RULE. A flip score alone is not enough: the same score is common on
    high-volume signatures, where it means one flaky machine among many rather than a bad crash.
    So this fires only when Socorro's confidence clears ``min_confidence`` AND the signature has
    never crashed more than ``max_reports`` people. Of the 21 bugs the canary had filed when this
    was written, 3 carried the field (66, 62, 25) and the rule fires on 2 — 2061961, and 2061726,
    which is a single crash on a signature whose other reports are on RELEASE and predate the
    nightly changeset it blames.

    An ABSTAIN, not a downweight, for the same reason as ``_apply_backout_gate``: this is not a
    question of how confident to be in the candidate, it is that there is nothing to act on. And
    LAST, after the second-opinion fold, because no amount of independent agreement can turn a
    hardware fault into a software bug — the fold is precisely what pushed 2061961 over the line.

    NOT a volume gate in disguise. A single crash is normal and is the whole point of triaging
    nightly: bug 2062119 named the wrong changeset on a one-report signature and still got a real
    fix written. Volume only ever qualifies the flip signal here; it never suppresses on its own.

    Tri-state on both inputs, and both fail toward REPORTING. Socorro omits the field entirely
    (it is never 0) when the stackwalker found no candidate, and ``signature_report_count`` is
    ``None`` when the lookup failed — neither may read as a hit. Reads ``seed["raw_crash"]``,
    already in hand, so no network call and a natural no-op offline where the corpus's stub
    crashes carry no ``crash_info``. Mutates in place; never raises."""
    v = dossier.verdict if dossier is not None else None
    if v is None:
        return
    cfg = config.get_agent_bit_flip()
    if not cfg["enabled"]:
        return
    raw = (seed or {}).get("raw_crash") or {}
    try:
        confidence = raw.get("possible_bit_flips_max_confidence")
        confidence = None if confidence is None else int(confidence)
    except (TypeError, ValueError):
        return
    if confidence is None:
        return
    reports = (seed or {}).get("signature_report_count")
    # Recorded for EVERY verdict, including the ones left alone: without the flag there is no way
    # to count how often the pipeline is looking at probable hardware, which is the measurement
    # that would settle the threshold.
    flags = {"possible_bit_flip_confidence": confidence}
    if reports is not None:
        flags["signature_report_count"] = reports
    dossier.corroborations = {**(dossier.corroborations or {}), **flags}
    if confidence < cfg["min_confidence"]:
        return
    if reports is None or reports > cfg["max_reports"]:
        return
    if v.decision == Decision.abstain:
        return
    dossier.verdict = Verdict(
        decision=Decision.abstain,
        confidence=Confidence.low,
        abstain_reason=(
            "Socorro rates the faulting address a possible hardware BIT FLIP (confidence {}%) "
            "and this signature has only ever been reported {} time(s) — the likeliest "
            "explanation is one bad machine, not a bug anyone can fix; suppressed rather than "
            "reported".format(confidence, reports)
        ),
        mechanism=v.mechanism,
        consistency=v.consistency,
    )
    dossier.corroborations = {**dossier.corroborations, "possible_bit_flip_suppressed": True}
    logger.info(
        "agent: possible bit flip (confidence %s, %s report(s) for this signature) -> %s/%s "
        "suppressed to abstain for %s",
        confidence, reports, v.decision.value, v.confidence.value, (seed or {}).get("uuid"),
    )


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
    raises. A no-op read (does not mutate the dossier)."""
    v = dossier.verdict if dossier is not None else None
    if v is None or v.decision != Decision.lead or v.confidence == Confidence.probable:
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
        # signal (a weak hint just sets a UI chip). Off-stack keeps its config knob.
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
        # Not a gate — a label. Whether the candidate came from this build's pushlog window is
        # what decides if the filed bug may call it a "regression" at all.
        _record_window_membership(result.dossier, seed)
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
