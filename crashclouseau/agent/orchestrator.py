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

from crashclouseau import config, db, models, worker
from crashclouseau.agent.schema import CONFIDENCE_SCORE, Decision
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
    cand: dict = {}
    for f in frames:
        for node, cs in (f.get("changesets") or {}).items():
            score = cs.get("score") or 0
            if node not in cand or score > (cand[node].get("score") or 0):
                cand[node] = {
                    "node": node,
                    "score": score,
                    "bug": cs.get("bugid"),
                    "backedout": cs.get("backedout"),
                }
    candidates = sorted(cand.values(), key=lambda c: -(c.get("score") or 0))
    return {
        "uuid": uuid,
        "signature": info.get("signature", ""),
        "channel": info.get("channel", "nightly"),
        "product": info.get("product", ""),
        "buildid": info.get("buildid"),
        "version": info.get("version"),
        "frames": frames,
        "stack": stack_text,
        "candidates": candidates,
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
        if config.get_agent_skip_if_existing() and models.Dossier.get_by_uuid(uuid):
            logger.info("agent: dossier already exists for %s; skipping", uuid)
            return

        seed = build_seed(uuid)
        if seed is None:
            return

        seed_score = _seed_score(uuid)
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

        payload = result.model_dump(mode="json")
        payload["actions"] = list(recorder.actions)
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


def enqueue_agent(uuid):
    """Enqueue one triage run on the dedicated queue (no-op if disabled)."""
    if not config.get_agent_enabled():
        return
    queue = worker.get_queue(config.get_agent_queue())
    queue.enqueue_call(
        func=run_evidence_agent,
        args=(uuid,),
        result_ttl=0,
        job_timeout=config.get_agent_job_timeout(),
    )
