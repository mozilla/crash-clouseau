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
    """Base ``agent.llm`` block with the sweep's role/principal overrides applied."""
    llm_cfg = dict(config.get_llm())
    if not sweep_config:
        return llm_cfg
    roles = {k: dict(v) for k, v in (llm_cfg.get("roles") or {}).items()}
    for role, model in (sweep_config.get("roles") or {}).items():
        roles.setdefault(role, {})["model"] = model
    llm_cfg["roles"] = roles
    if sweep_config.get("principal_model"):
        principal = dict(llm_cfg.get("principal") or {})
        principal["model"] = sweep_config["principal_model"]
        llm_cfg["principal"] = principal
    return llm_cfg


def _render_stack(crash_json_path):
    if not crash_json_path:
        return ""
    try:
        with open(crash_json_path) as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return ""
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
    return {
        "uuid": case.uuid,
        "signature": case.signature,
        "channel": case.channel,
        "stack": _render_stack(case.crash_json_path),
    }


async def rerun_corpus(cases, sweep_config=None):
    """Return ``{uuid -> CrashTriageResult | None}`` for the corpus under one sweep."""
    from crashclouseau.agent.triage import run_crash_triage  # lazy: pulls the SDK

    ecfg = config.get_eval()
    sem = asyncio.Semaphore(int(ecfg.get("max_concurrency", 3)))
    timeout = ecfg.get("per_case_timeout_s")
    llm_cfg = _sweep_llm_cfg(sweep_config)
    tools_cfg = config.get_agent()

    async def _one(case):
        async with sem:
            coro = run_crash_triage(
                crash=_case_to_crash(case),
                tools_cfg=tools_cfg,
                llm_cfg=llm_cfg,
                recorder=None,
            )
            if timeout:
                return await asyncio.wait_for(coro, timeout=timeout)
            return await coro

    async def _guarded(case):
        try:
            return case.uuid, await _one(case)
        except Exception as exc:
            logger.warning("eval: rerun failed for %s: %s", case.uuid, exc)
            return case.uuid, None

    pairs = await asyncio.gather(*[_guarded(c) for c in cases])
    return {uuid: result for uuid, result in pairs}
