# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Corpus re-runner (#13).

Drives #02's ``run_crash_triage`` once per frozen corpus case under a
bounded-concurrency ``asyncio.gather`` (semaphore = ``eval.max_concurrency``,
per-case timeout). Each ``CrashTriageResult`` is associated to its case ``uuid`` by
construction — no Batch API / ``custom_id`` remap (the SDK/CLI path has none, so
re-runs are full-price, paced by local concurrency). A per-case failure/timeout
degrades to ``None`` for that uuid rather than sinking the sweep.

``run_crash_triage`` is imported lazily so importing this module never pulls
``claude-agent-sdk`` / spawns the bundled CLI."""
from __future__ import annotations

import asyncio
import json

from crashclouseau import config
from crashclouseau.logger import logger


def _sweep_llm_cfg(sweep_config):
    """Base ``agent.llm`` block with the sweep's role/principal/effort overrides applied."""
    llm_cfg = dict(config.get_llm())
    if not sweep_config:
        return llm_cfg
    roles = {k: dict(v) for k, v in (llm_cfg.get("roles") or {}).items()}
    for role, model in (sweep_config.get("roles") or {}).items():
        roles.setdefault(role, {})["model"] = model
    principal = dict(llm_cfg.get("principal") or {})
    if sweep_config.get("principal_model"):
        principal["model"] = sweep_config["principal_model"]
    eff = sweep_config.get("effort")
    if eff:  # apply one effort to the principal AND every role
        principal["effort"] = eff
        for r in roles.values():
            r["effort"] = eff
    llm_cfg["roles"] = roles
    llm_cfg["principal"] = principal
    return llm_cfg


def _load_crash(crash_json_path):
    if not crash_json_path:
        return {}
    try:
        with open(crash_json_path) as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def _render_stack(data):
    dump = data.get("json_dump", {})
    ct = dump.get("crash_info", {}).get("crashing_thread", 0) or 0
    threads = dump.get("threads", [])
    frames = threads[ct]["frames"] if ct < len(threads) else []
    return "\n".join(
        "#{} {}  {}:{}".format(
            f.get("frame", "?"), f.get("function") or f.get("module") or "?",
            f.get("file") or "", f.get("line") or "",
        )
        for f in frames[:20]
    )


def _case_to_crash(case):
    # Mirror build_seed's crash shape so the SAME _user_prompt / _crash_facts path runs
    # under eval as in prod. The frozen processed_crash.json IS the "raw_crash" dict the
    # facts reader expects (buildid lives under `build`). Without raw_crash here, the
    # crash-facts block renders empty and an A/B would be blind to that prompt input.
    raw = _load_crash(case.crash_json_path)
    return {
        "uuid": case.uuid,
        "signature": case.signature,
        "channel": case.channel,
        "stack": _render_stack(raw),
        "product": raw.get("product", ""),
        "buildid": raw.get("build", ""),
        "version": raw.get("version", ""),
        "raw_crash": raw,
        # Seed candidates frozen at mine time (approx build_seed) so the agent gets the
        # same KIND of seed as prod instead of running cold.
        "candidates": list(getattr(case, "candidates", None) or []),
        # P1 off-stack markers so the eval exercises the SAME prompt framing (off-stack funnel
        # vs scored-candidate) + PINNED reads as prod's build_seed. A study-fixture corpus sets
        # these; the legacy A/B corpus leaves them falsy => the on-stack path, unchanged.
        "is_offstack": bool(getattr(case, "is_offstack", False)),
        "pin_rev": getattr(case, "pin_rev", "") or "",
        "build_node": getattr(case, "pin_rev", "") or "",
        # Prior-signature corroboration needs a live Socorro+Bugzilla lookup; left empty
        # offline (documented fidelity gap — the fault-offset corroboration arm still fires).
        "prior_hints": [],
        "prior_regressor_bugs": [],
        "experts": [],
    }


def _apply_report_thresholds(result, thresholds):
    """Eval-only sweep knob (``SweepConfig.confidence_thresholds``): DOWNGRADE a reported verdict
    whose confidence rung-score (0-100, = ``CONFIDENCE_SCORE`` * 100) is below the configured
    minimum to ``abstain`` — so a sweep can measure a report-THRESHOLD policy end to end (recall /
    false-investigate / person-precision) without re-running the agent. Keys: a decision value
    (``"lead"`` / ``"strong-evidence"``), with ``"report"`` as the fallback for any reported
    verdict; values: the minimum 0-100 score. No-op when ``thresholds`` is empty, the dossier is
    missing, or the verdict already abstains. Mutates ``result.dossier`` in place; returns it."""
    if not thresholds:
        return result
    from crashclouseau.agent.schema import CONFIDENCE_SCORE, Decision, Verdict
    dossier = getattr(result, "dossier", None)
    verdict = dossier.verdict if dossier is not None else None
    if verdict is None or verdict.decision == Decision.abstain or verdict.confidence is None:
        return result
    minimum = thresholds.get(verdict.decision.value, thresholds.get("report"))
    if minimum is None:
        return result
    score = int(round(CONFIDENCE_SCORE.get(verdict.confidence, 0.0) * 100))
    if score < minimum:
        dossier.verdict = Verdict(
            decision=Decision.abstain,
            abstain_reason="below the configured report threshold "
                           "(score {} < {})".format(score, minimum),
        )
    return result


async def rerun_corpus(cases, sweep_config=None, concurrency=None):
    """Return ``{uuid -> CrashTriageResult | None}`` for the corpus under one sweep.

    ``concurrency`` overrides ``eval.max_concurrency`` (parallel cases). The run is
    tool-I/O-bound, so wall-clock scales ~1/concurrency; the ceiling is host RAM (each case
    spawns a ``claude`` CLI tree) and the Anthropic API rate/overload limit, NOT CPU."""
    from crashclouseau.agent.triage import run_crash_triage  # lazy: pulls the SDK

    ecfg = config.get_eval()
    sem = asyncio.Semaphore(int(concurrency or ecfg.get("max_concurrency", 3)))
    timeout = ecfg.get("per_case_timeout_s")
    llm_cfg = _sweep_llm_cfg(sweep_config)
    tools_cfg = config.get_agent()

    # Lazy: apply_deterministic_gates lives in orchestrator (pulls models/DB); import once
    # here rather than at module load so importing this module stays light.
    from crashclouseau.agent.orchestrator import apply_deterministic_gates
    report_thresholds = (sweep_config or {}).get("confidence_thresholds") or {}

    async def _one(case):
        async with sem:
            crash = _case_to_crash(case)
            coro = run_crash_triage(
                crash=crash,
                tools_cfg=tools_cfg,
                llm_cfg=llm_cfg,
                recorder=None,
            )
            result = (await asyncio.wait_for(coro, timeout=timeout) if timeout
                      else await coro)
            # Apply the SAME post-verdict deterministic reshaping prod applies (callpath /
            # exposer / corroboration), so calibration scores the shipped verdict, not the
            # raw model output.
            apply_deterministic_gates(result, crash)
            # Optional sweep report-threshold policy (downgrade sub-threshold reports to abstain);
            # no-op unless the sweep sets ``confidence_thresholds``.
            return _apply_report_thresholds(result, report_thresholds)

    async def _guarded(case):
        try:
            return case.uuid, await _one(case)
        except Exception as exc:
            logger.warning("eval: rerun failed for %s: %s", case.uuid, exc)
            return case.uuid, None

    pairs = await asyncio.gather(*[_guarded(c) for c in cases])
    return {uuid: result for uuid, result in pairs}
