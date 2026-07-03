# 11 — Orchestration worker & seed seam

> **SUBSTRATE DECISION (2026-07-02): adopt `mozilla/bugbug` "hackbot" (see #02).** This unit was
> originally specced to *drive the agent team in order* via a bespoke `llm_call(role, ...)`
> sequence with a `UsageAccumulator` built here. That is **superseded.** The RQ job no longer
> sequences roles: it builds the crash **seed**, an `ActionsRecorder`, and calls
> `asyncio.run(run_crash_triage(...))` (from #02). The **Claude Agent SDK** drives the roles
> internally — the principal spawns the senior subagents (`crash-interpreter` →
> `call-graph-explorer` → `patch-scout` → `data-flow-tracer` → `skeptic`) via the built-in
> **`Task`** tool and folds their results back automatically. This unit owns only the *outside* of
> that call: enqueue gating, seed building, one `asyncio.run` invocation, persistence, cost-cap
> enforcement from `ResultMessage.total_cost_usd`, idempotency, timeout, and failure isolation.

## Objective
Build the single RQ job that assembles the seed for one crash UUID and runs the hackbot crash-triage
agent for it, persisting the resulting dossier and verdict. The job builds `build_seed(uuid)` from
`CrashStack`/`Score`/`Changeset`/`Node`, constructs an `ActionsRecorder`, and calls
`asyncio.run(run_crash_triage(crash=seed, recorder=..., llm_cfg=..., tools_cfg=...))` (#02) — the
Claude Agent SDK runs the multi-turn tool-use + subagent loop internally. The job then persists
`result.model_dump()` (a `CrashTriageResult`) plus `recorder.actions` into the #04 additive tables,
enforces the per-crash cost cap using `result.total_cost_usd`, and sets the dossier lifecycle status.
The seam is enqueued from `update.py:put_report()` only when a report was actually scored
(`useless=False`), on a dedicated queue, with a hard job timeout and failure isolation so any
LLM/SDK/searchfox flakiness can never block or roll back ingestion.

## Scope
**In scope**
- New module `crashclouseau/agent/orchestrator.py`: the `run_evidence_agent(uuid)` RQ entrypoint that
  builds the seed, builds an `ActionsRecorder`, runs the triage agent via
  `asyncio.run(run_crash_triage(...))`, and persists `Dossier`/`Verdict` + recorded actions.
- The seed-reader (`build_seed(uuid)`): turns `CrashStack.get_by_uuid` + `Score` + `Changeset`/`Node`
  rows into the typed `crash` seed object `run_crash_triage` consumes (passed as the `crash=` kwarg).
- The enqueue hook in `update.py:put_report()` (gated on `useless=False`) and a new dedicated queue
  `"agent"` in `worker.py`.
- Failure isolation (try/except around the whole run; mark dossier status on the UUID; never re-raise
  into the ingestion path), per-job timeout, and idempotency (skip if a dossier already exists for the
  UUID).
- Per-crash cost-cap enforcement using `CrashTriageResult.total_cost_usd` (surfaced from the SDK
  `ResultMessage`) against `agent.llm.max_cost_usd_per_crash`.
- Persistence DAO calls for the new additive tables (writes only — schema/migration owned by #04).

**Out of scope (owned by other sub-plans)**
- `run_crash_triage(...)` itself — the `ClaudeAgentOptions` assembly, `ClaudeSDKClient` drive loop,
  per-role `AgentDefinition` tiering, the in-process MCP tool servers, and the best-effort
  trailing-` ```json ` → `CrashTriageResult` parse — owned by **#02**. This unit only *calls* it.
- The dossier/verdict Pydantic models + every-claim-has-a-citation validation — owned by **#03**
  (validated inside the roles/handoff, not re-checked here).
- Each role's prompt + evidence logic (`crash-interpreter`, `call-graph-explorer`, `patch-scout`,
  `data-flow-tracer`, `skeptic`, principal) — owned by **#05–#10**; they are SDK-native subagents
  (`AgentDefinition`s registered in #02), NOT callables this unit sequences.
- The `searchfox-cli` Python adapter (#01) — `@tool`-wrapped and exposed to the SDK inside #02, never
  invoked by this unit.
- The new SQLAlchemy `Dossier`/`Verdict` tables + DAO methods (`Dossier.upsert`/`set_status`/
  `add_usage`/`get_by_uuid`, `Verdict.set`) — owned by **#04**; this unit calls them. (Note: the
  codebase has no Alembic migrations; new tables are created via `db.Model` subclass +
  `models.create()`/`db.create_all()`, gated on the `lastdate` table existing — see `models.create()`.)
- The `actions` MCP server wiring (`actions_server_for(recorder, types=[...])`) that lets the agent
  *record* a needinfo/comment — owned by **#02**/**#12**; this unit only constructs the `recorder`,
  passes it in, and persists `recorder.actions` afterward.
- Web UI evidence panel and `report_bug.py` needinfo draft + the human-confirm apply/replay — owned by
  **#12** (reads the persisted dossier/verdict + recorded actions).
- The offline eval re-run harness — **#13**.

## Externalities

| Name | Kind | Version / Endpoint / Command | Status | Purpose |
|------|------|------------------------------|--------|---------|
| `rq` | python-lib | `>=2.9.1` (in requirements.txt) | existing | Queue/job framework; `Queue.enqueue_call(func=..., args=(uuid,), result_ttl=0, job_timeout=...)` |
| `redis` | python-lib | `redis>=8.0.0` (in requirements.txt) | existing | RQ broker; conn from `REDIS_URL`/`config.get_redis()` in `worker.py` |
| `flask_sqlalchemy` / `sqlalchemy` | python-lib | `sqlalchemy>=2.0.51`, `flask_sqlalchemy>=3.1.1` (in requirements.txt) | existing | `db.session`, models, `models.commit()` |
| `libmozdata` | python-lib | `>=0.2.12` (in requirements.txt) | existing | Indirectly via `inspector.get_crash_data` (Socorro `ProcessedCrash`) and `hgmozilla.Mercurial.get_repo_url` used inside `CrashStack.get_by_uuid` (imported in models.py as `Mercurial`) |
| `python-dateutil` | python-lib | `>=2.9.0` (in requirements.txt) | existing | `relativedelta` already used in `update.py` |
| `asyncio` | python-lib | stdlib (Python 3.14) | existing | The RQ job body is a sync function that calls `asyncio.run(run_crash_triage(...))` (fresh event loop per forked job). |
| `crashclouseau.agent.triage` | internal-module | `run_crash_triage(*, crash, tools_cfg, llm_cfg, recorder=None, extra=None) -> CrashTriageResult` | NEW (#02) | The one crash-triage agent coroutine; **imported lazily inside `run_evidence_agent`** so the enqueue path and the web dyno never pull `claude-agent-sdk`/spawn the bundled CLI at import time. |
| `crashclouseau.agent.result` | internal-module | `CrashTriageResult(HackbotAgentResult)` — `.model_dump()`, `.total_cost_usd`, `.num_turns` (+ usage if surfaced) | NEW (#02) | Typed hand-off persisted here; `total_cost_usd` drives the cost cap. |
| `hackbot_runtime` | vendored-lib | `ActionsRecorder` (from #02's vendored copy under `crashclouseau/vendor/`) | NEW (#02) | This unit constructs `ActionsRecorder()`, passes it to `run_crash_triage`, and persists `recorder.actions` (`[{type, params, reasoning[, attachments]}]`). **Do NOT use `hackbot_runtime.run`/`run_async` as the job body** — they `raise SystemExit` + call `asyncio.run` internally and are unusable as an RQ entry. |
| `claude-agent-sdk` | python-lib | `claude-agent-sdk>=0.2` (bundles the Claude Code CLI; #02) | NEW (#02) | Pulled in transitively through `run_crash_triage`; **this unit never imports it directly.** The SDK spawns the bundled CLI subprocess inside `run_crash_triage` — the `"agent"` dyno must permit subprocess spawn (see Risks). |
| `pydantic` | python-lib | `pydantic>=2` (added by #01/#03) | existing (by then) | `CrashTriageResult` type this unit receives + `.model_dump()`s; orchestrator imports the model class only. |
| `crashclouseau.worker` | internal-module | `get_queue(name)`, module-level `listen=[...]`, `conn`, `black_hole` | existing (modify) | Add `"agent"` to `listen`; reuse `get_queue("agent")` and the `black_hole` exception handler. NOTE: `get_queue` memoizes the queue dict in `__QUEUE` keyed off `listen`, so `"agent"` must be present in `listen` *before the first* `get_queue()` call (it is, since `listen` is module-level). `get_queue` builds queues with `default_timeout=6000`; the `__main__` `Worker(...)` queues use no explicit default — per-job `job_timeout` overrides both. |
| `crashclouseau.update` | internal-module | `put_report()` (L54-98); enqueue after `models.UUID.set_analyzed(uuid, useless)` at L98 | existing (modify) | Hook point: enqueue `run_evidence_agent` when `useless=False`. `put_report` already imports `worker` at module top (L12), so reuse it; lazy-import the `agent` package to avoid a cycle. |
| `crashclouseau.models` | internal-module | `CrashStack.get_by_uuid(uuid)` (L1263), `UUID.get_info(uuid)` (L790), `UUID.set_analyzed` (L883), `UUID.max_score`, `Score`, `Changeset`, `Node`, `commit()` (L1327) | existing | Seed reads |
| `crashclouseau.models` (#04 DAO) | internal-module | `Dossier.upsert(uuid, payload, status, worker_models, seed_score, tokens, cost)`, `Dossier.set_status(uuid, status)`, `Dossier.add_usage(...)`, `Dossier.get_by_uuid(uuid)`, `Verdict.set(uuid, verdict, confidence, principal_model, rationale, evidence, effort, dossierid=None)`; enums `AGENT_STATUS_TYPE` (`pending`/`running`/`done`/`error`), `VERDICT_TYPE` (`culprit`/`unrelated`/`abstain`/`error`) | NEW (#04; called here) | Persist dossier+verdict; idempotency via `get_by_uuid`; lifecycle via `Dossier.status` (there is **no** `UUID.set_agent_state` — the run-state lives on the `dossiers.status` column). |
| `crashclouseau.inspector` | internal-module | `get_crash_data(uuid)` (L58) -> `socorro.ProcessedCrash.get_processed(uuid)[uuid]` | existing | Raw processed crash folded into the seed. Currently-discarded fields available to pass through: `reason`, `crash_info.address`, `moz_crash_reason`, per-frame `inlines`/`trust`, `phc_alloc_stack`/`phc_free_stack`/`phc_kind`, `async_shutdown_timeout`, non-crashing `threads` — orchestrator passes raw data through verbatim to the seed. |
| `crashclouseau.config` | internal-module | existing `get_agent()`/`get_searchfox()` (#01) + `get_llm()`/`get_llm_role()` (#02); new `get_agent_*()` orchestration accessors | existing (modify) | Read the orchestration keys of the `"agent"` block; read `agent.llm` (incl. `max_cost_usd_per_crash`) via #02's `get_llm()`. |
| `crashclouseau.logger` | internal-module | `logger` | existing | Structured logging per run |
| `searchfox-cli` | CLI | `--calls-from` / `--calls-to` / `--calls-between` / `--define` / `--symbol` / `--depth`; repo `-R mozilla-central`(default)`\|mozilla-beta\|mozilla-release\|mozilla-esr*\|comm-central` | NEW (external) | Invoked only inside #02's `@tool`-wrapped searchfox client (an in-process MCP server the SDK calls), **not** by this unit. (Flags, not subcommands; `mozilla-beta`/`mozilla-release`, no `autoland`.) |
| Claude Haiku 4.5 | llm-model | `claude-haiku-4-5` ($1/$5 per 1M, 200K ctx) | NEW | Senior tier (crash-interpreter, patch-scout, skeptic) — set per-role in #02 via `AgentDefinition(model="haiku")`; recorded in `Dossier.worker_models`. Not called here. |
| Claude Sonnet 5 | llm-model | `claude-sonnet-5` ($3/$15 per 1M, 1M ctx) | NEW | Navigator (`call-graph-explorer`, Phase-0 default, effort=high) + data-flow-tracer — #02. Not called here. |
| Claude Opus 4.8 | llm-model | `claude-opus-4-8` ($5/$25 per 1M, 1M ctx) | NEW | Principal default; recorded in `Verdict.principal_model`. Not called here. |
| Claude Fable 5 | llm-model | `claude-fable-5` ($10/$50 per 1M, 1M ctx) | NEW | Optional hardest-case principal — #02. Not called here. |
| Socorro ProcessedCrash | REST-API | via `libmozdata.socorro.ProcessedCrash.get_processed(uuid)` (inside `inspector.get_crash_data`) | existing | Raw crash folded into the seed |
| lando git2hg | REST-API | `https://lando.moz.tools/api/git2hg/firefox/{hash}` (inside `inspector.get_path_node`) | existing | Already used during ingest; not re-called by this unit |
| `config/global.json` `"agent"` block | config | orchestration keys: `enabled`, `queue` (`"agent"`), `job_timeout` (s), `skip_if_existing`, `max_seed_frames`, `agent_version`; the `agent.llm` sub-block (models/effort/max_turns/pricing/`max_cost_usd_per_crash`) is owned by #02 | existing (extend) | Drives the seam (queue choice, whole-run timeout, kill-switch, idempotency key, cost cap). The `"agent"` block already exists (created by #01/#02); this unit **adds** the orchestration keys. |

## Deliverables

- **`crashclouseau/agent/__init__.py`** — MODIFY (the package already exists from #02): additionally
  re-export `enqueue_agent` and `run_evidence_agent`. The `enqueue_agent` re-export must NOT transitively
  import `triage`/`claude-agent-sdk` (keep the `run_crash_triage` import lazy — inside
  `run_evidence_agent`), so `update.py`'s enqueue path stays SDK-free.
- **`crashclouseau/agent/orchestrator.py`** — NEW:
  - `build_seed(uuid) -> Seed | None` — reads `CrashStack.get_by_uuid(uuid)` (returns `(res, uuid_info)`;
    both `{}` when the UUID is unknown) + `UUID.get_info(uuid)` (buildid/product/channel/version/
    signature) + `inspector.get_crash_data(uuid)` (raw processed crash, passed through verbatim). Each
    frame in `res["frames"]` carries `stackpos`/`filename`/`function`/`line`/`node`/`original`/
    `internal`/`url` and a `changesets` OrderedDict mapping node -> `{score, backedout, pushdate,
    bugid}`. Returns `None` (logged) if `res` is empty, `res["frames"]` is empty, or no frame has any
    `changesets` (nothing scored to reason about). Trim to `get_agent_max_seed_frames()`. The returned
    object is the `crash=` payload for `run_crash_triage`.
  - `run_evidence_agent(uuid)` — the RQ entrypoint. Idempotency guard, build seed, construct an
    `ActionsRecorder`, resolve `llm_cfg`/`tools_cfg` from config, run
    `asyncio.run(run_crash_triage(crash=seed, tools_cfg=..., llm_cfg=..., recorder=recorder))`, enforce
    the cost cap, persist `Dossier`/`Verdict` + `recorder.actions`, set dossier status. All wrapped so
    it never raises out.
  - `enqueue_agent(uuid)` — thin helper: `worker.get_queue(config.get_agent_queue()).enqueue_call(
    func=run_evidence_agent, args=(uuid,), result_ttl=0, job_timeout=config.get_agent_job_timeout())`,
    no-op when `not config.get_agent_enabled()`.
- **`crashclouseau/update.py`** — MODIFY `put_report()`: after `models.UUID.set_analyzed(uuid, useless)`
  (L98), add `if not useless:` then lazy-import `from . import agent` and call
  `agent.enqueue_agent(uuid)`, wrapped in its own try/except so an enqueue failure cannot break
  ingestion (log only).
- **`crashclouseau/worker.py`** — MODIFY: add `"agent"` to the module-level `listen` list so the
  dedicated worker dyno processes it; `get_queue("agent")` then works (the dict comprehension over
  `listen` covers it).
- **`crashclouseau/config.py`** — MODIFY: add `get_agent_enabled()`, `get_agent_queue()`,
  `get_agent_job_timeout()`, `get_agent_skip_if_existing()`, `get_agent_max_seed_frames()`,
  `get_agent_version()` reading the `"agent"` block of `_get_global()` (via the existing `get_agent()`
  helper). The per-crash cost cap is read from `agent.llm` via #02's `get_llm()` — no new getter needed
  (fallback constant if the key is absent).
- **`config/global.json`** — MODIFY: add the orchestration keys to the existing `"agent"` block
  (`enabled`, `queue:"agent"`, `job_timeout`, `skip_if_existing`, `max_seed_frames`, `agent_version`).
  The `agent.llm` sub-block (incl. `max_cost_usd_per_crash`) is added by #02.
- **`tests/test_orchestrator.py`** — NEW: seed-builder, idempotency, failure-isolation, enqueue-gating,
  cost-cap, and recorded-actions tests with **`run_crash_triage` mocked** (no live SDK call, no CLI).

## Interfaces

**Inputs consumed**
- `uuid` (str) — the job argument.
- From `CrashStack.get_by_uuid(uuid)`: `(res, uuid_info)`. `res` is `{"frames": [...]}` (both `{}` for an
  unknown UUID). Each frame has `stackpos`, `filename`, `function`, `line`, `node`, `original`,
  `internal`, `url`, and `changesets` (OrderedDict node -> `{score, backedout, pushdate, bugid}`). The
  `changesets` entries ARE the heuristic seeds; `bugid` lives inside each changeset entry, not at frame
  top level.
- From `UUID.get_info(uuid)`: `buildid` (formatted via `utils.get_buildid`), `product`, `channel`,
  `version`, `signature`; and `UUID.max_score` (the demoted heuristic seed strength → `Dossier.seed_score`).
- From `inspector.get_crash_data(uuid)`: raw processed crash (for the discarded fields listed in
  Externalities) — folded into the seed verbatim; this unit does not parse them.

**Agent call (the seam — assembled in #02, invoked here)**
- `result = asyncio.run(run_crash_triage(crash=seed, tools_cfg=config.get_agent(), llm_cfg=config.get_llm(),
  recorder=recorder))` → a `CrashTriageResult`. The SDK drives the multi-turn tool-use + subagent loop
  inside this single coroutine; **there is no per-role call and no `UsageAccumulator` here** — cost/turns
  come back on `result.total_cost_usd`/`result.num_turns` (surfaced from the terminal `ResultMessage`).
- `recorder = hackbot_runtime.ActionsRecorder()` — constructed here, passed in; #02 exposes it to the
  agent as the `actions` MCP server. After the run, `recorder.actions` holds the *recorded* (never
  executed) Bugzilla actions — needinfo is `bugzilla.update_bug` with
  `changes={'flags':[{'name':'needinfo','status':'?','requestee':...}]}`; there is no dedicated needinfo
  action. #12 renders these behind a human-confirm UI and replays them via libmozdata.
- For read-only crash triage the searchfox/crash tools need **no local Firefox checkout**, so this unit
  does not build a `HackbotContext`; searchfox tree/repo selection rides in `tools_cfg` (the `agent`
  block). If a future tool needs `source_repo`, thread it via `tools_cfg`/`extra` (or a minimal
  `HackbotContext`) rather than env.

**Dossier/verdict fields this unit reads/writes**
- *Reads:* none from a prior dossier (it creates the first one, subject to idempotency). It reads the
  seed object + `UUID.max_score` only.
- *Writes (via the #04 DAO):*
  - `Dossier.upsert(uuid, payload=result.model_dump() + {"actions": recorder.actions}, status="done",
    worker_models=<senior ids from llm_cfg.roles>, seed_score=UUID.max_score, tokens=<from result.usage
    if surfaced else 0>, cost=result.total_cost_usd)`.
  - `Verdict.set(uuid, verdict=<mapped from result: strong-evidence→"culprit"/"unrelated", else
    "abstain">, confidence=..., principal_model=<llm_cfg.principal.model>, rationale=result.summary,
    evidence=<citations from result>, effort=<llm_cfg.principal.effort>, dossierid=...)`.
  - `Dossier.set_status(uuid, ...)` transitions: `"running"` at start → `"done"` on success →
    `"error"` on failure. The status lives on the `dossiers.status` column (`AGENT_STATUS_TYPE`), not on
    `UUID`. A `build_seed`-`None` UUID persists no dossier (logged and skipped — nothing scored).
- The orchestrator keys the row by `uuid` (correlation id for the offline eval, #13).

**Delegation contract (the SDK drives the roles internally — role bodies + order owned by #02/#05–#10)**
- The principal session (options assembled in #02) spawns the senior subagents via the built-in `Task`
  tool and folds each child's final text back automatically. Intended flow: `crash-interpreter` →
  `call-graph-explorer` (adds off-stack candidates) → `patch-scout` → `data-flow-tracer` → `skeptic`
  (drops uncited claims) → principal (strong-evidence or calibrated abstain).
- **This unit does not call any role, does not see intermediate handoffs, and does not enforce the
  order** — it consumes only the terminal `CrashTriageResult`. The role tiering (Phase-0: navigator =
  `sonnet`, other seniors = `haiku`, principal = `opus`) is per-role `AgentDefinition(model=...)` in #02,
  config-driven via `agent.llm`.

**Depends on:** #02 (`run_crash_triage` + `CrashTriageResult` + tools + roles + tiering), #04
(`Dossier`/`Verdict` tables + DAO), #03 (dossier/verdict field shape, validated inside the roles), #01
(searchfox client, `@tool`-wrapped in #02).
**Feeds:** #12 UI evidence-panel + `report_bug.py` needinfo-draft (read the persisted dossier/verdict +
`recorder.actions`), and the offline eval harness #13 (keyed by `uuid`).

## Implementation steps
1. Add the orchestration keys to the existing `"agent"` block in `config/global.json` (`enabled:true`,
   `queue:"agent"`, `job_timeout`, `skip_if_existing:true`, `max_seed_frames`, `agent_version`) and the
   matching accessors in `config.py`. (The `agent.llm` sub-block, incl. `max_cost_usd_per_crash`, is
   added by #02.)
2. Add `"agent"` to module-level `listen` in `worker.py`; confirm `get_queue("agent")` works (the dict
   comprehension already covers all `listen` names) and add `agent` to the Procfile worker invocation in
   a follow-up (note for ops, out of strict scope). The `"agent"` dyno must allow the SDK to spawn the
   bundled Claude Code CLI subprocess (see Risks).
3. Extend `crashclouseau/agent/__init__.py` to re-export `enqueue_agent` and `run_evidence_agent`
   (keeping the `run_crash_triage` import lazy inside the entrypoint — the enqueue path must not pull the
   SDK).
4. Implement `build_seed(uuid)` in `orchestrator.py`: call `CrashStack.get_by_uuid` + `UUID.get_info` +
   `inspector.get_crash_data`; guard the empty-dict return; return `None` and log a warning if
   `res.get("frames")` is empty or no frame has any `changesets` (nothing to reason about). Trim to
   `get_agent_max_seed_frames()`. Return the `crash` seed object.
5. Implement `run_evidence_agent(uuid)`:
   a. `if config.get_agent_skip_if_existing() and models.Dossier.get_by_uuid(uuid)`: log + return
      (idempotency for re-runs / at-least-once delivery). (Open question — version-aware skip; see Risks.)
   b. `seed = build_seed(uuid)`; if `None`, log + return (no dossier persisted — nothing scored).
   c. `models.Dossier.upsert(uuid, payload={}, status="running", seed_score=UUID.max_score...)` (or
      `set_status(uuid, "running")` after creating the row) to mark the run in-flight.
   d. `recorder = hackbot_runtime.ActionsRecorder()`.
   e. `from .triage import run_crash_triage` (lazy) → `result = asyncio.run(run_crash_triage(
      crash=seed, tools_cfg=config.get_agent(), llm_cfg=config.get_llm(), recorder=recorder))`.
   f. Enforce the cost cap: if `result.total_cost_usd` exceeds
      `config.get_llm().get("max_cost_usd_per_crash", <fallback>)`, log a warning and mark the dossier
      over-budget (a flag in the payload / a distinct log line for ops + eval). The run is a single
      atomic SDK session, so this is a **reactive** check — see Risks for the proactive lever.
   g. Persist: `models.Dossier.upsert(uuid, payload={**result.model_dump(), "actions":
      recorder.actions}, status="done", worker_models=..., seed_score=..., tokens=..., cost=
      result.total_cost_usd)`, then `models.Verdict.set(...)`, then `models.commit()`.
6. Wrap the whole body of `run_evidence_agent` in `try/except Exception`: log with `exc_info=True`,
   `models.Dossier.set_status(uuid, "error")` (best-effort, its own try/except), and `return` — never
   re-raise (RQ's `black_hole` exception handler is a backstop, but the agent path must not depend on it
   for ingestion safety). Write no partial `Verdict` on failure.
7. Implement `enqueue_agent(uuid)` honoring `get_agent_enabled()` and using
   `get_queue(get_agent_queue())` with `result_ttl=0` and `job_timeout=get_agent_job_timeout()`.
8. Modify `put_report()`: after `set_analyzed(uuid, useless)` (L98), `if not useless:` lazy-import
   `from . import agent` and call `agent.enqueue_agent(uuid)`. Wrap in try/except so an enqueue failure
   can't break ingestion (log only). (`worker` is already imported at the top of `update.py`; the lazy
   import is for the `agent` package only.)
9. Tests: mock `agent.triage.run_crash_triage` (return a fake `CrashTriageResult`) and
   `inspector.get_crash_data`; assert (a) `enqueue_agent` is not called when `useless=True`, (b) a
   `run_crash_triage` exception (or an `asyncio.run` failure) leaves dossier status `"error"`, writes no
   verdict, and does not propagate, (c) a second run is skipped when a dossier exists, (d) `build_seed`
   returns `None` for an unscored/unknown UUID, (e) `result.total_cost_usd` over the cap is logged/flagged
   but the row still persists, (f) `recorder.actions` are stored in the persisted dossier payload.

## Risks & open questions
- **The SDK spawns the Claude Code CLI as a subprocess (inside `run_crash_triage`).** The `"agent"` dyno
  must allow subprocess spawn and have the ~74MB wheel's bundled CLI on `PATH` in the venv (no Node
  needed for read-only triage). This is verified in #02; this unit's only obligation is to run the
  coroutine via `asyncio.run(...)` in the forked RQ job (a fresh event loop per job — no nested-loop
  hazard). **Never** use `hackbot_runtime.run`/`run_async` as the job body (they `raise SystemExit` and
  call `asyncio.run` themselves).
- **Import cycle / import-time cost:** `update.py` already imports `worker` at module top; importing the
  `agent` package at module top could cycle (`agent` -> `orchestrator` -> `models`/`inspector`/`worker`)
  and would drag `claude-agent-sdk` into the web dyno. Mitigation: lazy `from . import agent` inside
  `put_report`, and keep `from .triage import run_crash_triage` lazy inside `run_evidence_agent`.
- **Cost cap is post-hoc, not mid-run.** With hackbot the whole triage is one atomic SDK session; there
  is no per-role checkpoint at which to abort on cumulative spend (the old `UsageAccumulator`-between-roles
  design is gone). Enforcement is therefore two-sided: **proactive** — thread `max_turns` (and `effort`)
  from `agent.llm` into `run_crash_triage` (the only in-loop levers the SDK exposes; assembled in #02) —
  and **reactive** — compare `result.total_cost_usd` against `max_cost_usd_per_crash` after the run, log +
  flag over-budget, and (optionally) gate re-runs / downgrade the principal tier on subsequent runs. No
  mid-run kill from this unit.
- **Timeout granularity:** the whole-run `job_timeout` (enforced by RQ, kills a wedged job) must exceed
  the expected SDK session wall-clock — the multi-turn subagent loop with a `sonnet` navigator at
  `effort=high` can run minutes. There is **no `per_role_timeout`** anymore (no per-role calls); the
  in-loop bound is `max_turns` + the SDK request timeout (owned by #02). Set `job_timeout` generously
  above `max_turns`-worth of turns, and keep it on the dedicated `"agent"` queue so a long run never
  starves ingestion.
- **Structured output is best-effort (#02).** If the principal omits/malforms its final ` ```json `
  block, `CrashTriageResult`'s typed fields come back `None`/empty; this unit must persist that as an
  **abstain** verdict (map empty result → `Verdict(verdict="abstain", ...)`) and never crash on missing
  fields. The every-claim-has-a-citation rule (#03) is enforced upstream in the roles; this unit trusts
  the returned handoff.
- **At-least-once delivery / re-enqueue on dyno restart:** RQ may re-run a job; idempotency via
  `Dossier.get_by_uuid` is the guard. Open question: key on `uuid` alone or `(uuid, agent_version)`/
  `schema_version` so a re-run after a prompt/tier change re-analyzes? The `"agent"` block carries
  `agent_version`; #04 must decide whether the skip check is version-aware.
- **Queue starvation vs. ingestion:** the agent is slow (SDK subprocess + searchfox); its own `"agent"`
  queue (own dyno) keeps `high/default/low` free. `get_agent_enabled()` plus not scaling the worker is
  the kill-switch (queue simply backs up / no-ops at enqueue when disabled).
- **Seed completeness:** `get_by_uuid` joins `Score`, so a frame's `changesets` map only contains scored
  changesets. Off-stack culprits are added later *inside* the SDK by the `call-graph-explorer` subagent,
  so a frame with empty `changesets` still yields a usable seed as long as at least one stack frame
  scored — consistent with the `useless=False` gate.
- **Build node drift / searchfox limits:** searchfox indexes ~tip of the selected tree, not the crash
  build node, and its call graph misses virtual/indirect/fn-pointer/template/macro and cross-language
  (JS↔C++↔Rust) edges. The seed carries `node`/`channel` so the SDK roles can flag drift; this unit
  cannot fix it (a known constraint for #06, not a CLI guarantee).
- **DB retention window:** `Node`/`UUID` rows are hard-deleted after ~30 days (`Node.clean` cascade,
  #04); a re-enqueued job for an aged-out UUID must tolerate `get_by_uuid`/`get_info` returning empty and
  skip cleanly (covered by the `build_seed -> None -> return` path). Dossier/verdict rows are purged
  transitively by the same `uuids` FK cascade — no clean() edit here.

## Acceptance criteria
- Ingesting a scored Nightly report (`useless=False`) enqueues exactly one `run_evidence_agent` job on
  the `"agent"` queue; an unscored/`useless=True` report enqueues none. Verify via
  `len(worker.get_queue("agent"))` and a unit test on `put_report`.
- `build_seed(uuid)` for a real scored UUID returns a seed whose frames mirror
  `CrashStack.get_by_uuid` (same `stackpos`/`function`/`line`/`node` and the per-node `score` inside
  `changesets`) and carries the raw processed-crash pass-through; returns `None` for an unscored or
  unknown UUID.
- With `run_crash_triage` mocked to return a `CrashTriageResult`, `run_evidence_agent(uuid)` calls it via
  `asyncio.run` **exactly once** (never `run_async`), persists one `Dossier` + one `Verdict`, stores
  `recorder.actions` in the dossier payload, sets dossier status `"done"`, and `models.Dossier.get_by_uuid`
  is true afterward.
- `run_crash_triage` (or `asyncio.run`) raising any exception leaves dossier status `"error"`, writes no
  `Verdict`, logs with traceback, and `run_evidence_agent` returns normally (no exception escapes) —
  proving ingestion isolation.
- Running the same UUID twice with `skip_if_existing:true` invokes `run_crash_triage` only once.
- `config.get_agent_enabled() == False` makes `enqueue_agent` a no-op (kill-switch) without touching
  ingestion.
- A `CrashTriageResult` with `total_cost_usd > max_cost_usd_per_crash` is logged/flagged over-budget, yet
  the dossier + verdict still persist (reactive cost cap; no mid-run abort).
- An empty/all-`None` `CrashTriageResult` (missing final JSON block) persists as an `abstain` verdict
  without raising.

Relevant paths: `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/update.py` (hook at L98, `worker` imported L12), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/worker.py` (`listen` L12, `get_queue` L32-36, `black_hole` L24-29), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/models.py` (`CrashStack.get_by_uuid` L1263, `UUID.get_info` L790, `UUID.set_analyzed` L883, `UUID.max_score`, `commit` L1327, `create` L1331; new `Dossier`/`Verdict` DAO from #04), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/inspector.py` (`get_crash_data` L58, `get_crash` L64), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/config.py`, `/home/calixte/dev/mozilla/crash-clouseau/config/global.json`, new `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/agent/orchestrator.py`, `crashclouseau/agent/triage.py` + `crashclouseau/agent/result.py` (from #02).
</content>
</invoke>
