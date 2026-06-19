# 11 — Orchestration worker & seed seam

## Objective
Build the single RQ job that assembles seeds for one crash UUID and drives the full agent team (Crash Interpreter -> Call-graph Explorer -> Patch Scout -> Data-flow Tracer -> Skeptic -> Principal), persisting the resulting dossier and verdict. The seam is enqueued from `update.py:put_report()` only when a report was actually scored (`useless=False`), on a dedicated queue, with hard timeouts and failure isolation so any LLM/searchfox flakiness can never block or roll back ingestion.

## Scope
**In scope**
- New module `crashclouseau/agent/orchestrator.py`: the `run_evidence_agent(uuid)` RQ entrypoint that reads seeds, calls each role in order, and persists `Dossier`/`Verdict`.
- The seed-reader (`build_seed(uuid)`): turns `CrashStack.get_by_uuid` + `Score` + `Changeset`/`Node` rows into the typed seed object the Crash Interpreter and Patch Scout consume.
- The enqueue hook in `update.py:put_report()` (gated on `useless=False`) and a new dedicated queue `"agent"` in `worker.py`.
- Failure isolation (try/except around the whole run; mark agent state on the UUID; never re-raise into the ingestion path), per-job timeout, and idempotency (skip if a dossier already exists for the UUID).
- Persistence DAO calls for the new additive tables (writes only — schema/migration owned elsewhere, see below).

**Out of scope (owned by other sub-plans)**
- The `llm_call(role, ...)` abstraction, Pydantic dossier/verdict schemas, prompt caching, Claude SDK wiring — owned by the LLM-abstraction sub-plan.
- Each role's prompt + logic (Crash Interpreter, Call-graph Explorer, Patch Scout, Data-flow Tracer, Skeptic, Principal) — owned by their respective sub-plans; this unit only sequences them via a stable interface.
- The `searchfox-cli` Python adapter — owned by the searchfox-adapter sub-plan.
- The new SQLAlchemy table definitions + migration for `Dossier`/`Verdict` — owned by the persistence/schema sub-plan; this unit calls its DAO methods. (Note: the codebase has no Alembic migrations; new tables are created via `db.Model` subclass + `models.create()`/`db.create_all()`, gated on the `lastdate` table existing — see `models.create()`.)
- Web UI evidence panel and `report_bug.py` needinfo draft — owned by the UI/bug-filing sub-plan.
- The Batch-API offline eval re-run harness.

## Externalities

| Name | Kind | Version / Endpoint / Command | Status | Purpose |
|------|------|------------------------------|--------|---------|
| `rq` | python-lib | `>=2.9.1` (in requirements.txt) | existing | Queue/job framework; `Queue.enqueue_call(func=..., args=(uuid,), result_ttl=0, job_timeout=...)` |
| `redis` | python-lib | `redis>=8.0.0` (in requirements.txt) | existing | RQ broker; conn from `REDIS_URL`/`config.get_redis()` in `worker.py` |
| `flask_sqlalchemy` / `sqlalchemy` | python-lib | `sqlalchemy>=2.0.51`, `flask_sqlalchemy>=3.1.1` (in requirements.txt) | existing | `db.session`, models, `models.commit()` |
| `libmozdata` | python-lib | `>=0.2.12` (in requirements.txt) | existing | Indirectly via `inspector.get_crash_data` (Socorro `ProcessedCrash`) and `hgmozilla.Mercurial.get_repo_url` used inside `CrashStack.get_by_uuid` (imported in models.py as `Mercurial`) |
| `python-dateutil` | python-lib | `>=2.9.0` (in requirements.txt) | existing | `relativedelta` already used in `update.py` |
| `anthropic` | python-lib | NEW (add to requirements.txt; no version pin exists yet) | NEW | Pulled in transitively via the `llm_call` abstraction; orchestrator does not import it directly |
| `pydantic` | python-lib | NEW (add to requirements.txt; ships as a dep of `anthropic` but pin it explicitly) | NEW | Dossier/Verdict/seed types the orchestrator passes between roles; orchestrator imports the model classes only |
| `crashclouseau.worker` | internal-module | `get_queue(name)`, module-level `listen=[...]`, `conn`, `black_hole` | existing (modify) | Add `"agent"` to `listen`; reuse `get_queue("agent")` and the `black_hole` exception handler. NOTE: `get_queue` memoizes the queue dict in `__QUEUE` keyed off `listen`, so `"agent"` must be present in `listen` *before the first* `get_queue()` call (it is, since `listen` is module-level). The `__main__` `Worker(...)` and `get_queue` use *different* timeout settings (`get_queue` builds queues with `default_timeout=6000`; `__main__` Worker queues use no explicit default) — per-job `job_timeout` overrides both. |
| `crashclouseau.update` | internal-module | `put_report()` (L54-98); enqueue after `models.UUID.set_analyzed(uuid, useless)` at L98 | existing (modify) | Hook point: enqueue `run_evidence_agent` when `useless=False`. `put_report` already imports `worker` at module top (L12), so reuse it; lazy-import the `agent` package to avoid a cycle. |
| `crashclouseau.models` | internal-module | `CrashStack.get_by_uuid(uuid)` (L1263), `UUID.get_info(uuid)` (L790), `UUID.set_analyzed` (L883), `Score` class, `Changeset`, `Node`, `commit()` (L1327) | existing | Seed reads |
| `crashclouseau.models` (new methods) | internal-module | `Dossier.put(...)`, `Verdict.put(...)`, `Dossier.exists(uuidid)`, `UUID.set_agent_state(uuid, state)` | NEW (defined by persistence sub-plan; called here) | Persist dossier+verdict; idempotency; agent run-state |
| `crashclouseau.inspector` | internal-module | `get_crash_data(uuid)` (L58) -> `socorro.ProcessedCrash.get_processed(uuid)[uuid]` | existing | Crash Interpreter input. Currently-discarded processed-crash fields available to pass through: `reason`, `crash_info.address`, `moz_crash_reason`, per-frame `inlines`/`trust`, `phc_alloc_stack`/`phc_free_stack`/`phc_kind`, `async_shutdown_timeout`, non-crashing `threads` — orchestrator passes raw data through verbatim |
| `crashclouseau.config` | internal-module | `_get_global()`; new `get_agent_*()` accessors | existing (modify) | New `"agent"` block in `config/global.json` |
| `crashclouseau.logger` | internal-module | `logger` | existing | Structured logging per run |
| `crashclouseau.agent.roles.*` | internal-module | `crash_interpreter(...)`, `callgraph_explorer(...)`, `patch_scout(...)`, `dataflow_tracer(...)`, `skeptic(...)`, `principal(...)` | NEW (other sub-plans) | The six role callables sequenced here |
| `searchfox-cli` | CLI | `searchfox-cli --calls-from <SYM>`, `--calls-to <SYM>`, `--calls-between <SOURCE,TARGET>`, `--define <SYM>`, `--symbol`, `--depth N` (default 1); repo selector `-R/--repo` with `mozilla-central` (default) / `mozilla-beta` / `mozilla-release` / `mozilla-esr*` / `comm-central` | NEW (external) | Invoked only inside role modules via the adapter, NOT by the orchestrator. (Operations are FLAGS, not subcommands. Repo values are `mozilla-beta`/`mozilla-release`, NOT `beta`/`release`; there is no `autoland` tree in searchfox.) |
| Claude Haiku 4.5 | llm-model | `claude-haiku-4-5` ($1/$5 per 1M, 200K ctx) | NEW | Senior roles (interpreter, call-graph explorer, patch scout, skeptic) — invoked inside role modules. Haiku 4.5 does NOT support the `effort` param (400) and has no extended thinking — senior calls must be plain. Min cacheable prefix 4096 tokens. |
| Claude Sonnet 4.6 | llm-model | `claude-sonnet-4-6` ($3/$15 per 1M, 1M ctx) | NEW | Data-flow tracer (mid tier option) — inside role module. Supports adaptive thinking + `effort` (incl. `max`). Min cacheable prefix 2048 tokens. |
| Claude Opus 4.8 | llm-model | `claude-opus-4-8` ($5/$25 per 1M, 1M ctx) | NEW | Principal default; data-flow tracer high-effort option — inside role module. Adaptive thinking only (`budget_tokens`/sampling params 400); `effort` low..max/xhigh. Min cacheable prefix 4096 tokens. |
| Claude Fable 5 | llm-model | `claude-fable-5` ($10/$50 per 1M, 1M ctx) | NEW | Optional principal for hardest cases — inside role module. Thinking always on (omit `thinking` / adaptive only; explicit `disabled` 400); safety classifiers may return `stop_reason:"refusal"`; requires 30-day data retention (not ZDR). Min cacheable prefix 2048 tokens. |
| Socorro ProcessedCrash | REST-API | via `libmozdata.socorro.ProcessedCrash.get_processed(uuid)` (inside `inspector.get_crash_data`) | existing | Crash brief source |
| lando git2hg | REST-API | `https://lando.moz.tools/api/git2hg/firefox/{hash}` (inside `inspector.get_path_node`) | existing | Already used during ingest; not re-called by this unit |
| `config/global.json` `"agent"` block | config | keys: `enabled`, `queue` (`"agent"`), `job_timeout` (s), `per_role_timeout` (s), `skip_if_existing`, `max_seed_frames`, `agent_version` | NEW | Drives the seam (queue choice, timeouts, kill-switch, idempotency key) |

## Deliverables

- **`crashclouseau/agent/__init__.py`** — NEW package marker; re-exports `enqueue_agent` and `run_evidence_agent`.
- **`crashclouseau/agent/orchestrator.py`** — NEW:
  - `build_seed(uuid) -> Seed | None` — reads `CrashStack.get_by_uuid(uuid)` (returns `(res, uuid_info)`; both are `{}` when the UUID is unknown) and `UUID.get_info(uuid)` (buildid/product/channel/version/signature). Each frame in `res["frames"]` carries `stackpos`/`filename`/`function`/`line`/`node`/`original`/`internal`/`url` and a `changesets` OrderedDict mapping node -> `{score, backedout, pushdate, bugid}`. Returns `None` if `res` is empty, `res["frames"]` is empty, or no frame has any `changesets` (nothing scored to reason about).
  - `run_evidence_agent(uuid)` — the RQ entrypoint. Idempotency guard, build seed, fetch raw crash via `inspector.get_crash_data(uuid)`, sequence the six roles, persist, set agent state. All wrapped so it never raises out.
  - `enqueue_agent(uuid)` — thin helper: `worker.get_queue(config.get_agent_queue()).enqueue_call(func=run_evidence_agent, args=(uuid,), result_ttl=0, job_timeout=config.get_agent_job_timeout())`, no-op when `not config.get_agent_enabled()`.
- **`crashclouseau/update.py`** — MODIFY `put_report()`: after `models.UUID.set_analyzed(uuid, useless)` (L98), add `if not useless:` then lazy-import `from . import agent` and call `agent.enqueue_agent(uuid)`, wrapped in its own try/except so an enqueue failure cannot break ingestion (log only).
- **`crashclouseau/worker.py`** — MODIFY: add `"agent"` to the module-level `listen` list so the dedicated worker dyno processes it; `get_queue("agent")` then works (the dict comprehension over `listen` covers it).
- **`crashclouseau/config.py`** — MODIFY: add `get_agent_enabled()`, `get_agent_queue()`, `get_agent_job_timeout()`, `get_agent_per_role_timeout()`, `get_agent_skip_if_existing()`, `get_agent_max_seed_frames()`, `get_agent_version()` reading the `"agent"` block of `_get_global()`.
- **`config/global.json`** — MODIFY: add the `"agent"` block.
- **`tests/test_orchestrator.py`** — NEW: seed-builder, idempotency, failure-isolation, and enqueue-gating tests with role callables mocked.

## Interfaces

**Inputs consumed**
- `uuid` (str) — the job argument.
- From `CrashStack.get_by_uuid(uuid)`: `(res, uuid_info)`. `res` is `{"frames": [...]}` (and `res, uuid_info` are both `{}` for an unknown UUID). Each frame has `stackpos`, `filename`, `function`, `line`, `node`, `original`, `internal`, `url`, and `changesets` (OrderedDict node -> `{score, backedout, pushdate, bugid}`). The `changesets` entries ARE the heuristic seeds; `bugid` lives inside each changeset entry, not at frame top level.
- From `UUID.get_info(uuid)`: `buildid` (formatted via `utils.get_buildid`), `product`, `channel`, `version`, `signature`.
- From `inspector.get_crash_data(uuid)`: raw processed crash (for the discarded fields listed in Externalities) — passed verbatim to the Crash Interpreter role; this unit does not parse them.

**Dossier fields this unit reads/writes**
- *Reads:* none from a prior dossier (it creates the first one). It reads the seed object only.
- *Writes (via the persistence DAO):* the assembled `Dossier` (crash brief + candidate hunks + traced call path with per-hop source + data-flow hypothesis + skeptic notes) and `Verdict` (`verdict`, `confidence`, `culprit_node`, `culprit_bug`, citations), plus `UUID.set_agent_state(uuid, ...)` (`pending`/`done`/`error`/`abstain`/`skipped`). The orchestrator assigns `custom_id`/correlation = `uuid` so the run is keyable for the offline eval.

**Sequencing contract (stable, role bodies owned elsewhere)**
1. `crash_interpreter(seed, raw_crash) -> CrashBrief`
2. `callgraph_explorer(crash_brief, seed) -> ExpandedFrames` (adds off-stack candidates)
3. `patch_scout(expanded_frames, seed) -> CandidatePatches`
4. `dataflow_tracer(crash_brief, candidate_patches) -> DataflowHypotheses` (per (patch, frame))
5. `skeptic(dossier_draft) -> SkepticNotes` (drops uncited claims before principal)
6. `principal(validated_dossier) -> Verdict` (strong-evidence or calibrated abstain)

**Depends on:** LLM-abstraction sub-plan (Pydantic types + `llm_call`), persistence/schema sub-plan (`Dossier`/`Verdict` tables + DAO), searchfox-adapter sub-plan, all six role sub-plans.
**Feeds:** UI evidence-panel sub-plan and `report_bug.py` needinfo-draft sub-plan (read the persisted dossier/verdict), and the offline eval harness (keyed by `uuid`).

## Implementation steps
1. Add the `"agent"` block to `config/global.json` (`enabled:true`, `queue:"agent"`, `job_timeout`, `per_role_timeout`, `skip_if_existing:true`, `max_seed_frames`, `agent_version`) and the matching accessors in `config.py`.
2. Add `"agent"` to module-level `listen` in `worker.py`; confirm `get_queue("agent")` works (the dict comprehension already covers all `listen` names) and add `agent` to the Procfile worker invocation in a follow-up (note for ops, out of strict scope).
3. Create `crashclouseau/agent/__init__.py` re-exporting `enqueue_agent` and `run_evidence_agent`.
4. Implement `build_seed(uuid)` in `orchestrator.py`: call `CrashStack.get_by_uuid` + `UUID.get_info`; guard the empty-dict return, then return `None` and log a warning if `res.get("frames")` is empty or no frame has any `changesets` (nothing to reason about). Trim to `get_agent_max_seed_frames()`.
5. Implement `run_evidence_agent(uuid)`:
   a. `if config.get_agent_skip_if_existing() and models.Dossier.exists(uuidid_or_uuid)`: log + return (idempotency for re-runs/at-least-once delivery).
   b. `models.UUID.set_agent_state(uuid, "pending")`.
   c. `seed = build_seed(uuid)`; if `None`, set state `"skipped"` and return.
   d. `raw = inspector.get_crash_data(uuid)`.
   e. Call roles 1->6 in order, passing the per-role timeout from config into the role callables.
   f. `models.Dossier.put(...)`, `models.Verdict.put(...)`, `models.commit()`, `set_agent_state(uuid, verdict.state)`.
6. Wrap the whole body of `run_evidence_agent` in `try/except Exception`: log with `exc_info=True`, `models.UUID.set_agent_state(uuid, "error")` (best-effort, its own try/except), and `return` — never re-raise (RQ's `black_hole` exception handler is a backstop, but the agent path must not depend on it for ingestion safety).
7. Implement `enqueue_agent(uuid)` honoring `get_agent_enabled()` and using `get_queue(get_agent_queue())` with `result_ttl=0` and `job_timeout=get_agent_job_timeout()`.
8. Modify `put_report()`: after `set_analyzed(uuid, useless)` (L98), `if not useless:` lazy-import `from . import agent` and call `agent.enqueue_agent(uuid)`. Wrap in try/except so an enqueue failure can't break ingestion (log only). (`worker` is already imported at the top of `update.py`; the lazy import is for the `agent` package only.)
9. Tests: mock all six role callables and `inspector.get_crash_data`; assert (a) `enqueue_agent` is not called when `useless=True`, (b) a raised role exception leaves UUID state `"error"` and does not propagate, (c) second run is skipped when a dossier exists, (d) `build_seed` returns `None` for an unscored/unknown UUID.

## Risks & open questions
- **Import cycle:** `update.py` already imports `worker` at module top; importing the `agent` package at module top could cycle (`agent` -> `orchestrator` -> `models`/`inspector`/`worker`). Mitigation: lazy import `from . import agent` inside `put_report`.
- **At-least-once delivery / re-enqueue on dyno restart:** RQ may re-run a job; idempotency via `Dossier.exists` is the guard. Open question: do we key on `uuidid` alone or on `(uuidid, agent_version)` so a re-run after a prompt change re-analyzes? The `"agent"` block carries an `agent_version` config key for this; the persistence sub-plan must decide whether `Dossier.exists` is version-aware.
- **Queue starvation vs. ingestion:** the agent is slow (LLM + searchfox); putting it on its own `"agent"` queue (own dyno) keeps `high/default/low` free. Open question: should the agent dyno be optional (scale-to-zero) and the queue simply back up when disabled? `get_agent_enabled()` plus not scaling the worker covers the kill-switch.
- **Timeout granularity:** `job_timeout` (whole run, enforced by RQ) vs `per_role_timeout` — the latter must be enforced inside role callables/adapter (subprocess + SDK request `timeout`), not by RQ; confirm the LLM/searchfox sub-plans honor it. (The Anthropic SDK `timeout` default is 10 min; large `max_tokens` non-streaming requests raise unless streamed — role modules must stream long outputs.)
- **Seed completeness:** `get_by_uuid` joins `Score` (`.join(Score)`), so the `changesets` map on each frame only contains scored changesets. Off-stack culprits are added later by the Call-graph Explorer, so a frame with empty `changesets` still yields a usable seed as long as at least one stack frame scored — consistent with the `useless=False` gate (a report is only `useless=False` if `CrashStack.put_frames` stored frames).
- **Heroku build node drift / searchfox revision:** searchfox indexes ~tip of the selected tree, not the crash build node; the orchestrator passes `node`/`channel` through so roles can flag drift, but cannot fix it here. NOTE: searchfox call graphs miss virtual/indirect/function-pointer/template/macro and cross-language (JS<->C++<->Rust) edges — the searchfox README does not document these limits, so this is captured here as a known constraint for the role sub-plans, not a CLI-advertised guarantee.
- **DB retention window:** `Node`/`UUID` rows are hard-deleted after ~30 days (`max_ndays`); a re-enqueued agent job for an aged-out UUID must tolerate `get_by_uuid`/`get_info` returning empty and abstain cleanly (covered by the `build_seed -> None -> "skipped"` path).

## Acceptance criteria
- Ingesting a scored Nightly report (`useless=False`) enqueues exactly one `run_evidence_agent` job on the `"agent"` queue; an unscored/`useless=True` report enqueues none. Verify via `len(worker.get_queue("agent"))` and a unit test on `put_report`.
- `build_seed(uuid)` for a real scored UUID returns a seed whose frames mirror `CrashStack.get_by_uuid` (same `stackpos`/`function`/`line`/`node` and the per-node `score` inside `changesets`); returns `None` for an unscored or unknown UUID.
- With all six roles mocked, `run_evidence_agent(uuid)` persists one `Dossier` + one `Verdict` and sets `UUID` agent state `"done"`/`"abstain"`; `models.Dossier.exists` is true afterward.
- A role raising any exception leaves agent state `"error"`, writes no partial verdict, logs with traceback, and `run_evidence_agent` returns normally (no exception escapes) — proving ingestion isolation.
- Running the same UUID twice with `skip_if_existing:true` performs the LLM work only once.
- `config.get_agent_enabled() == False` makes `enqueue_agent` a no-op (kill-switch) without touching ingestion.

Relevant paths: `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/update.py` (hook at L98, `worker` imported L12), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/worker.py` (`listen` L12, `get_queue` L32-36, `black_hole` L24-29), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/models.py` (`CrashStack.get_by_uuid` L1263, `UUID.get_info` L790, `UUID.set_analyzed` L883, `commit` L1327, `create` L1331), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/inspector.py` (`get_crash_data` L58, `get_crash` L64), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/config.py`, `/home/calixte/dev/mozilla/crash-clouseau/config/global.json`, new `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/agent/orchestrator.py`.
