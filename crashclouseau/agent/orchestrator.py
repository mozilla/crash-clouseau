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


def run_evidence_agent(uuid):
    """RQ entrypoint: run the triage agent for one UUID and persist the result.
    Never raises out (ingestion isolation); marks dossier status on failure."""
    try:
        stale_after = config.get_agent_job_timeout() + _STALE_BUFFER_S
        if config.get_agent_skip_if_existing() and (
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
        # agentworkers that both passed it for the same stale uuid don't both run —
        # exactly one wins claim_running, the loser skips (no double token cost). When
        # skip_if_existing is off (force re-run), just mark it running unconditionally.
        if config.get_agent_skip_if_existing():
            if not models.Dossier.claim_running(uuid, stale_after):
                logger.info("agent: %s claimed by another worker / settled; skipping", uuid)
                return
        else:
            models.Dossier.upsert(uuid, payload={}, status="running", seed_score=seed_score)

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
    except Exception:
        logger.error("agent: run_evidence_agent failed for %s", uuid, exc_info=True)
        try:
            models.Dossier.set_status(uuid, "error")
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
            uuids = models.Dossier.get_stale_running(stale_after)
            if not uuids:
                return 0
            queue = worker.get_queue(config.get_agent_queue())
            for uuid in uuids:
                queue.enqueue_call(
                    func=run_evidence_agent,
                    args=(uuid,),
                    result_ttl=0,
                    timeout=config.get_agent_job_timeout(),
                )
            logger.warning(
                "agent: reaped %d orphaned (stale-running) triage(s): %s",
                len(uuids), ", ".join(uuids),
            )
            return len(uuids)
    except Exception:  # pragma: no cover - defensive; never break the clock
        logger.error("agent: reap_stale_agent_jobs failed", exc_info=True)
        return 0


def enqueue_agent(uuid, channel=None):
    """Enqueue one triage run on the dedicated queue. No-op when the agent is disabled,
    when ``channel`` is outside the configured set (nightly only by default), or when
    this uuid's proto-signature has already been triaged (dedup across builds — the
    authoritative skip is in ``run_evidence_agent``; this just avoids queueing a job we
    would drop)."""
    if not config.get_agent_enabled():
        return
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
        result_ttl=0,
        # RQ's enqueue_call takes `timeout` (not `job_timeout`, which is the high-level
        # enqueue() param) — the wrong kwarg raised TypeError, was swallowed by the
        # caller's try/except, and silently dropped EVERY agent job. The value matters
        # too: without it RQ's 180s default would kill a ~20-min triage mid-run.
        timeout=config.get_agent_job_timeout(),
    )
