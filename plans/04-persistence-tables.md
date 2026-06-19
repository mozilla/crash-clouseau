# 04 — Persistence: dossier/verdict tables

## Objective
Add additive SQLAlchemy models to `crashclouseau/models.py` that persist, per crash UUID, the grounded evidence dossier (JSONB), the principal's verdict, confidence, the model ids/versions used, token/cost accounting, and timestamps — all linked by foreign key to the existing `uuids` table. The schema must layer onto the live Heroku Postgres DB via the existing idempotent `models.create()` path without altering existing tables, and the rows must be hard-deleted in lockstep with the existing retention purge. **The retention purge that actually runs is `Node.clean()` (models.py:301), NOT `UUID.clean()` — see Risks: `UUID.clean()` is dead, never-called code that additionally references a non-existent `UUID.channel` column and would raise if invoked.** Because `Node.clean()` deletes `nodes` rows and the FK cascade chain `nodes → builds → uuids` already removes the parent `uuids` rows, an `ON DELETE CASCADE` FK from the new tables to `uuids.id` is purged transitively with zero edits to existing purge logic. This unit owns only storage and read/write helpers; it produces no LLM calls and no searchfox queries.

## Scope
**In scope**
- New `db.Model` subclasses `Dossier` and `Verdict` in `crashclouseau/models.py`.
- Columns: UUID foreign key, `pg.JSONB` dossier payload, verdict enum/string, confidence, model ids, schema version, token/cost counters, status, timestamps.
- ON DELETE CASCADE FK to `uuids.id` so the existing retention purge (`Node.clean()` → cascade `nodes → builds → uuids` → dossier/verdict rows) transitively removes dossier/verdict rows (no change to any `clean()` logic required).
- Read/write helper staticmethods (upsert, get-by-uuid, status transitions, list-for-build).
- Confirm the additive `create()` path materializes the tables on a fresh DB and document the one-time DDL for the existing prod DB.

**Out of scope (owned by other sub-plans)**
- The dossier JSON *content schema* / Pydantic models that validate each field — owned by the LLM-abstraction / dossier-builder sub-plan; this unit stores whatever JSON dict it is handed and only enforces top-level envelope fields it indexes on.
- Enqueue/orchestration hook in `update.py` `put_report()` / `analyze_one_report` — owned by the worker-orchestration sub-plan (this unit only supplies the write helpers it calls).
- The `config/global.json` `"agent"`/`"llm"` block definition and `config.py` getters — owned by the config/LLM-abstraction sub-plan (this unit only *reads* an optional `agent.dossier_schema_version` if a getter is later added).
- Web UI rendering of the dossier/verdict — owned by the UI sub-plan.
- searchfox-cli invocation, Claude API calls, prompt caching, batch eval — other sub-plans.

## Externalities

| Name | Kind | Version / Endpoint / Command | Status | Purpose |
|------|------|------------------------------|--------|---------|
| `flask_sqlalchemy` | python-lib | `flask_sqlalchemy>=3.1.1` (pinned floor in requirements.txt); provides `db` from `crashclouseau/__init__.py` (`db = SQLAlchemy(app)`) | existing | `db.Model` base, `db.session`, `db.create_all()`/`db.drop_all()`, `db.Column`, `db.ForeignKey`, `db.Enum`, `db.func`. |
| `sqlalchemy` | python-lib | `sqlalchemy>=2.0.51` (pinned floor) | existing | Core ORM; `inspect()`, `func`, query API used by helpers (imported in models.py as `from sqlalchemy import inspect, func`). |
| `sqlalchemy.dialects.postgresql` | internal-module (of sqlalchemy) | imported as `pg` in models.py (`import sqlalchemy.dialects.postgresql as pg`) | existing | `pg.JSONB` column type, `pg.insert(...).on_conflict_do_update(...)` upsert (same pattern as `Stats.add` at models.py:750, `UUID.add` at models.py:842). |
| `psycopg2-binary` | python-lib | `psycopg2-binary>=2.9.12` (pinned floor) | existing | Postgres driver; JSONB support. |
| `pytz` | python-lib | transitive (via libmozdata; NOT listed in requirements.txt but imported in models.py) | existing | UTC normalization of timestamps on read (`.astimezone(pytz.utc)`), matching `LastDate.get`/`Node.get_min_date`. |
| `python-dateutil` | python-lib | `python-dateutil>=2.9.0` (pinned floor); `relativedelta` imported in models.py | existing | Only relevant if a standalone `Dossier.clean()` mirror is added (NOT needed — purge is via FK cascade from `Node.clean()`). |
| `crashclouseau.config` | internal-module | `crashclouseau/config.py` | existing | Retention parity is owned by `Node.clean()`, which calls `config.get_ndays_of_data()` (`max_ndays`=30). This unit does NOT need to read it. Optionally read `agent.dossier_schema_version` via a future getter (owned by config sub-plan) with a hardcoded fallback constant. NOTE: `config.get_ndays()` is `backward_lookup_ndays`=3, a DIFFERENT value — do not confuse the two. |
| `crashclouseau.models.UUID` | internal-module | `crashclouseau/models.py` (`uuids` table; `UUID.get_id` at :1064 returns a scalar int via `.first()[0]`) | existing | FK target `uuids.id`; `UUID.get_id(uuid)` resolves uuid string → id (int) for write helpers. NOTE: the `uuids` table is itself purged transitively by `Node.clean()` cascade (`nodes → builds.nodeid → uuids.buildid`), NOT by `UUID.clean()` (dead code — see Risks). |
| `crashclouseau.models.Node.clean` | internal-module | `models.py:178` `Node.clean(date, channel)`, called from `Changeset.add` at models.py:301 | existing | The ACTUAL retention purge. Deletes `nodes` older than `config.get_ndays_of_data()` (30 days); FK cascades down to `builds`, then `uuids`, then `crashstack`/`scores` and (new) `dossiers`/`verdicts`. |
| `crashclouseau.models.commit` | internal-module | `models.commit()` (models.py:1327) → `db.session.commit()` | existing | Commit convention reused by helpers (`commit=True` kwarg pattern, as in `Stats.add`/`UUID.add`). |
| `crashclouseau.models.create` / `bin/init.py` | internal-module / CLI | `models.create()` (models.py:1331, gated on `inspect(db.engine).has_table("lastdate")`); invoked by `python bin/init.py` | existing | Idempotent fresh-DB schema creation; new tables get created automatically on a fresh DB. Does NOT add tables to an already-initialized DB (see Implementation steps for prod DDL). |
| `crashclouseau.models.clear` / `crashclouseau/create.py` | internal-module | `models.clear()` (models.py:1340) = `db.drop_all()`; `create.py:create()` does `clear()` then `create()` | existing | Full rebuild path; new tables auto-dropped/recreated. |
| Heroku Postgres | data-source | `DATABASE_URL` env (`postgres://` rewritten to `postgresql://` in `__init__.py`) | existing | Live DB the additive DDL must run against. |
| `claude-opus-4-8` | llm-model | id string only — stored, never called here ($5/$25 per 1M, 1M ctx, default principal) | existing (as a stored string value) | Recorded in `Verdict.principal_model`; this unit never invokes it. |
| `claude-haiku-4-5` | llm-model | id string only — stored, never called here ($1/$5 per 1M, 200K ctx, seniors) | existing (as a stored string value) | Recorded in `Dossier.worker_models` (JSONB list); this unit never invokes it. |
| Dossier JSON envelope | config | top-level keys this unit indexes: `schema_version` (and optionally a verdict/confidence summary the orchestrator hands separately) | NEW | The minimal contract this unit relies on; full content schema owned by dossier-builder sub-plan. |

No NEW python libraries, CLIs, or REST endpoints are introduced by this unit (`anthropic`/`pydantic` are added by other sub-plans; this unit stores their *output strings/JSON* only).

## Deliverables
**Modify `crashclouseau/models.py`** — add two model classes plus a module-level constant:

- `DOSSIER_SCHEMA_VERSION = 1` (module constant; the dossier-builder sub-plan may bump via config, this is the fallback).
- `VERDICT_TYPE = db.Enum("culprit", "unrelated", "abstain", "error", name="VERDICT_TYPE")` — verdict enum. NOTE: PLAN.md frames the two first-class outcomes as "strong evidence" vs "ABSTAIN"; `"culprit"`/`"unrelated"` is this unit's internal normalization of "strong evidence (is the culprit)" / "strong evidence (not the culprit)". Confirm label set with the principal-analysis sub-plan.
- `AGENT_STATUS_TYPE = db.Enum("pending", "running", "done", "error", name="AGENT_STATUS_TYPE")` — lifecycle status the orchestrator transitions.
- `class Dossier(db.Model)` — `__tablename__ = "dossiers"`:
  - `id` (PK, autoincrement)
  - `uuidid` (`db.ForeignKey("uuids.id", ondelete="CASCADE")`, indexed, unique — one dossier per UUID)
  - `schema_version` (`db.Integer`, default `DOSSIER_SCHEMA_VERSION`)
  - `payload` (`pg.JSONB`) — the full grounded dossier dict
  - `status` (`AGENT_STATUS_TYPE`, default `"pending"`)
  - `worker_models` (`pg.JSONB`, default `[]`) — list of senior model ids used
  - `seed_score` (`db.Integer`, nullable) — the heuristic `UUID.max_score` SEED carried in at dossier creation
  - `input_tokens` / `output_tokens` / `cache_read_tokens` (`db.Integer`, default 0)
  - `cost_usd` (`db.Numeric(10, 4)`, nullable)
  - `created` / `updated` (`db.DateTime(timezone=True)`, `server_default=db.func.now()`, `onupdate=db.func.now()`)
  - `__table_args__ = (db.UniqueConstraint("uuidid", name="uix_dossiers_uuidid"),)`
  - staticmethods: `upsert(uuid, payload, status, worker_models, seed_score, tokens, cost, commit=True)`, `set_status(uuid, status, commit=True)`, `add_usage(uuid, input_tokens, output_tokens, cache_read_tokens, cost, commit=True)`, `get_by_uuid(uuid)`, `get_pending(limit=1)`.
- `class Verdict(db.Model)` — `__tablename__ = "verdicts"`:
  - `id` (PK)
  - `uuidid` (`db.ForeignKey("uuids.id", ondelete="CASCADE")`, indexed, unique)
  - `dossierid` (`db.ForeignKey("dossiers.id", ondelete="CASCADE")`, nullable)
  - `verdict` (`VERDICT_TYPE`)
  - `confidence` (`db.Integer` 0–100, nullable) — numeric normalization of PLAN's `high`/`medium`/`low` label; confirm representation with principal-analysis sub-plan (may prefer a small enum/string instead).
  - `principal_model` (`db.String(64)`) — e.g. `"claude-opus-4-8"`
  - `rationale` (`db.Text`) — short grounded summary
  - `evidence` (`pg.JSONB`, default `[]`) — list of cited-artifact references
  - `effort` (`db.String(16)`, nullable) — Opus/Fable effort tier used (Haiku has no effort param; principal is Opus/Fable so this column is meaningful)
  - `created` (`db.DateTime(timezone=True)`, `server_default=db.func.now()`)
  - `__table_args__ = (db.UniqueConstraint("uuidid", name="uix_verdicts_uuidid"),)`
  - staticmethods: `set(uuid, verdict, confidence, principal_model, rationale, evidence, effort, dossierid=None, commit=True)`, `get_by_uuid(uuid)`, `get_for_build(buildid, product, channel)` (join `UUID`→`Build` mirroring `UUID.get_uuids_from_buildid` at models.py:970).

**No change required** to `models.create()`, `models.clear()`, `Node.clean()`, `bin/init.py`, `crashclouseau/create.py` — the new classes are picked up automatically by `db.create_all()`/`db.drop_all()` and purged by the existing `Node.clean()` cascade chain. (`UUID.clean()` is dead code and is irrelevant; do NOT rely on or attempt to use it.)

**New (one-time, not committed as runtime code):** a documented `CREATE TYPE`/`CREATE TABLE` DDL snippet (or a tiny `bin/migrate_dossier.py` using `db.create_all()`) to apply the two tables + enum types to the already-initialized prod DB — see Implementation steps.

## Interfaces
**Inputs consumed**
- From worker-orchestration sub-plan: `uuid` (string), the assembled dossier `payload` (dict), `status` transitions, `worker_models` list, and token/cost accounting → written via `Dossier.upsert` / `Dossier.set_status` / `Dossier.add_usage`.
- From principal-analysis sub-plan: `verdict`, `confidence`, `principal_model` (e.g. `claude-opus-4-8`), `rationale`, `evidence[]` (cited artifacts), `effort` → written via `Verdict.set`.
- From existing models: `UUID.get_id(uuid)` (uuid→id, returns scalar int), `UUID.max_score` (the demoted heuristic SEED, stored as `Dossier.seed_score`).

**Dossier fields this unit reads/writes**
- *Writes (envelope)*: `schema_version`, full `payload` JSONB blob (opaque), `status`, `worker_models`, `seed_score`, token counters, `cost_usd`.
- *Reads (envelope only)*: `schema_version` (to detect stale dossiers on read), and for `Verdict` the linkage `dossierid`. The inner `payload` (claims, call-graph paths, diffs) is treated as an opaque blob — its field-level schema is owned and validated by the dossier-builder sub-plan before being handed here.

**Dependencies / feeds**
- Depends on: existing `UUID` model + `models.create()`; the `Node.clean()` cascade chain for retention; optionally the config sub-plan for a future `agent.dossier_schema_version` getter (graceful fallback to the module constant).
- Feeds: worker-orchestration (status/usage persistence), principal-analysis (verdict persistence), UI sub-plan (`Dossier.get_by_uuid`, `Verdict.get_by_uuid`, `Verdict.get_for_build`), and the offline-eval sub-plan (reads stored verdicts to score against ground truth).

## Implementation steps
1. In `crashclouseau/models.py`, after the existing imports, add module constants `DOSSIER_SCHEMA_VERSION`, `VERDICT_TYPE`, `AGENT_STATUS_TYPE` near `CHANNEL_TYPE`/`PRODUCT_TYPE` (lines 16–17).
2. Add `class Dossier(db.Model)` with the columns listed in Deliverables; use `pg.JSONB` (not `pg.JSON`) for `payload`/`worker_models`/`evidence` for indexability and Postgres efficiency.
3. Implement `Dossier.upsert` using the `pg.insert(Dossier)...on_conflict_do_update(index_elements=["uuidid"], set_=...)` pattern already used by `Stats.add` (models.py:750) / `UUID.add` (models.py:842); resolve `uuidid` via `UUID.get_id(uuid)`. Set `updated` on conflict (or rely on `onupdate=db.func.now()`).
4. Implement `Dossier.set_status`, `Dossier.add_usage` (increment counters with a `Dossier.input_tokens + bound` style SQL update or read-modify-write), `Dossier.get_by_uuid` (join through `UUID.uuid`), `Dossier.get_pending(limit)`.
5. Add `class Verdict(db.Model)` with columns and the unique `uuidid` constraint; implement `Verdict.set` (upsert on `uuidid`), `Verdict.get_by_uuid`, `Verdict.get_for_build` (join `UUID`→`Build`, filter on `Build.buildid/product/channel` mirroring `UUID.get_uuids_from_buildid` at models.py:970; note that method converts the buildid via `utils.get_build_date`).
6. Normalize all `DateTime(timezone=True)` reads with `.astimezone(pytz.utc)` in getters, matching `LastDate.get`/`Node.get_min_date`.
7. Confirm retention coexistence: because both FKs are `ondelete="CASCADE"` to `uuids.id`, and `uuids` rows are themselves deleted by the cascade chain when `Node.clean()` deletes their parent `nodes`→`builds`, the new dossier/verdict rows are removed transitively — **no edit to any `clean()` needed**. Do NOT reference `UUID.clean()`: it is never called and would raise (`UUID` has no `channel` column). The cascade depends on Postgres enforcing `ON DELETE CASCADE`, which it does for the existing FKs (`builds.nodeid`, `uuids.buildid`, `crashstack.uuidid`, `scores.crashstackid`).
8. Verify fresh-DB path: `models.create()` (gated on absence of `lastdate`, models.py:1331) calls `db.create_all()`, which now also creates `dossiers`, `verdicts`, and the two new enum types. `crashclouseau/create.py` (clear+create) and `bin/init.py` work unchanged.
9. Prod DB (already initialized, so `create()` is a no-op for new tables): apply the two tables once. Recommended: add `bin/migrate_dossier.py` that does `from crashclouseau import models; models.db.create_all()` within the already-pushed app context (`__init__.py` does `app.app_context().push()`) — `create_all` is additive (`checkfirst=True` by default) and creates only missing tables/enums, leaving existing ones untouched. Document running it as a one-off Heroku dyno (`heroku run python bin/migrate_dossier.py`). Provide the equivalent raw `CREATE TYPE ... CREATE TABLE ...` SQL in the migration doc as a fallback.
10. Add unit tests under `tests/` for upsert idempotency, status transitions, usage accumulation, and cascade-on-parent-delete. NOTE: existing `tests/` (`test_buildhub.py`, `test_java.py`) are not DB-integration tests and there is no Postgres test fixture in the repo today — a real Postgres-backed fixture (or a transactional rollback harness) must be introduced to exercise FK `ON DELETE CASCADE`, since SQLite does not enforce it by default and the cascade is the load-bearing behavior.

## Risks & open questions
- **`UUID.clean()` is dead, broken code (PLAN-CRITICAL CORRECTION):** the original sub-plan claimed the purge fires via `UUID.clean()` on a 30-day window using `max_ndays`. In fact `UUID.clean()` (models.py:1055) is never called anywhere in the codebase, references a non-existent `UUID.channel` column (so it would raise immediately), and uses `config.get_ndays()` (=`backward_lookup_ndays`=3), not `max_ndays`. The only retention purge that runs is `Node.clean()` (models.py:178, called from `Changeset.add` at :301), which uses `config.get_ndays_of_data()` (=30) and cascades `nodes → builds → uuids → children`. The new FKs to `uuids.id` therefore still get purged, but the design must depend on `Node.clean()`, not `UUID.clean()`.
- **`create_all` enum reuse**: Postgres `CREATE TYPE` for `VERDICT_TYPE`/`AGENT_STATUS_TYPE` can fail with "type already exists" if a partial migration ran. Mitigation: enum names are unique and new; if re-running, `create_all`'s default `checkfirst=True` skips existing tables but enum-type creation guarding is less robust — use `DROP TYPE IF EXISTS` / `CREATE TYPE IF NOT EXISTS`-style guards in the SQL fallback, and on a partial failure clean up the orphaned type before re-running.
- **No Alembic in the repo**: migrations are done via `db.create_all()` (additive) and manual `clear()`/`create()` (destructive). This unit deliberately stays additive so prod can adopt it with a single `create_all` run and zero downtime; an open question is whether the maintainer wants to introduce Alembic now — recommend NOT, to match existing convention.
- **One-dossier-per-UUID assumption**: unique constraint on `uuidid` means re-analysis overwrites. If the eval sub-plan needs historical re-runs preserved, it should write to its own table (batch eval), not overwrite live dossiers — confirm with that sub-plan.
- **`seed_score` source timing**: `UUID.max_score` is set inside `CrashStack.put_frames` (models.py:1261), which runs in `update.put_report` before `UUID.set_analyzed`. The orchestrator must read `max_score` after frames are stored; if a dossier is created before scoring, `seed_score` may be 0/None — define ordering with the orchestration sub-plan.
- **JSONB size**: full call-graph dossiers could be large. Postgres TOASTs JSONB transparently; acceptable, but the dossier-builder should keep `payload` bounded (cited references, not full file bodies).
- **`dossier_schema_version` config getter** does not exist yet (owned by config sub-plan) — `config.py` currently has no `agent`/`llm` getter at all. Risk of import-time coupling — mitigate by reading it lazily inside the helper with a `try/except`/`getattr` fallback to `DOSSIER_SCHEMA_VERSION`.
- **`confidence` representation**: PLAN.md uses a categorical label (`confidence: high`); this unit proposes `Integer` 0–100. If principal-analysis emits labels, either store the label string or map it. Confirm before locking the column type.

## Acceptance criteria
- `crashclouseau/models.py` imports cleanly and `Dossier`, `Verdict`, `VERDICT_TYPE`, `AGENT_STATUS_TYPE`, `DOSSIER_SCHEMA_VERSION` are defined.
- On a fresh DB, `python bin/init.py` (which calls `models.create()`) creates `dossiers` and `verdicts` tables with the two enum types; verified via `inspect(db.engine).has_table("dossiers")` and `has_table("verdicts")` returning `True`.
- On an already-initialized DB, running `bin/migrate_dossier.py` (or the documented `db.create_all()` one-off) creates only the two new tables/enums and leaves all existing tables and rows untouched (row counts of `uuids`, `crashstack`, `scores`, etc. unchanged).
- Round-trip test: `Dossier.upsert(uuid, payload={...,"schema_version":1})` then `Dossier.get_by_uuid(uuid)` returns the same JSON; a second `upsert` for the same uuid updates in place (single row, `updated > created`).
- `Verdict.set(uuid, "culprit", 90, "claude-opus-4-8", ...)` then `Verdict.get_by_uuid(uuid)` returns the stored verdict, confidence, and `principal_model`; `Verdict.get_for_build(buildid, product, channel)` returns it joined to the build.
- Cascade test: deleting the parent `UUID` row (or, end-to-end, running `Node.clean(date, channel)` so the `nodes → builds → uuids` cascade fires past the 30-day window of `config.get_ndays_of_data()`) removes the associated `dossiers`/`verdicts` rows automatically; verified by querying counts before/after against a real Postgres backend (SQLite will not enforce the cascade).
- No existing test regresses; no existing model, any `clean()`, `create()`, or `clear()` behavior is modified.
