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

from rq import Retry

from crashclouseau import app, config, db, models, worker
from crashclouseau.agent.experts import area_experts
from crashclouseau.agent.schema import AreaExpert, CONFIDENCE_SCORE, Decision
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


def build_seed(uuid):
    """Assemble the ``crash=`` payload for ``run_crash_triage`` from the scored
    stack + processed crash. Returns None (logged) when nothing is scored to
    reason about (unknown UUID, no frames, or no changesets on any frame)."""
    res, uuid_info = models.CrashStack.get_by_uuid(uuid)
    frames = res.get("frames") if res else None
    if not frames:
        logger.warning("agent: no crash stack for %s; skipping", uuid)
        return None
    if not any(f.get("changesets") for f in frames):
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
    stack_text = "\n".join(
        "#{} {}  {}:{}".format(
            f.get("stackpos"), f.get("function"), f.get("filename"), f.get("line")
        )
        for f in frames
    )
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
    channel = info.get("channel", "nightly")
    experts = []
    try:
        authors = models.Node.authors_for([c["node"] for c in candidates[:10]], channel)
        experts = area_experts(candidates, authors, max_experts=3)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("agent: area-experts failed for %s: %s", uuid, exc)

    return {
        "uuid": uuid,
        "signature": info.get("signature", ""),
        "channel": channel,
        "product": info.get("product", ""),
        "buildid": info.get("buildid"),
        "version": info.get("version"),
        "frames": frames,
        "stack": stack_text,
        "candidates": candidates,
        "experts": experts,
        "raw_crash": raw_crash,
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

        cap = llm_cfg.get("max_cost_usd_per_crash", _DEFAULT_COST_CAP)
        cost = result.total_cost_usd
        over_budget = cap is not None and cost is not None and cost > cap
        if over_budget:
            logger.warning(
                "agent: %s over budget: $%.4f > $%s",
                uuid, result.total_cost_usd, cap,
            )

        # Attach deterministic area-experts (#15 phase 2) to the dossier so a
        # knowledgeable person is surfaced for ANY verdict (including abstain).
        if result.dossier is not None and seed.get("experts"):
            result.dossier.area_experts = [AreaExpert(**e) for e in seed["experts"]]

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
            for uuid in stale_running:
                queue.enqueue_call(
                    func=run_evidence_agent,
                    args=(uuid,),
                    kwargs={"force": False},
                    result_ttl=0,
                    timeout=config.get_agent_job_timeout(),
                )
            for uuid in stale_pending:
                queue.enqueue_call(
                    func=run_evidence_agent,
                    args=(uuid,),
                    kwargs={"force": True},
                    result_ttl=0,
                    timeout=config.get_agent_job_timeout(),
                )
            logger.warning(
                "agent: reaped %d orphaned (stale-running) + %d stuck (stale-pending) "
                "triage(s): %s",
                len(stale_running), len(stale_pending),
                ", ".join(stale_running + stale_pending),
            )
            return len(stale_running) + len(stale_pending)
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
