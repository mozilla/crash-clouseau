# 03 — Dossier schema & strong-evidence contract

## Objective
Define the typed, citation-bearing hand-off contract for the evidence-agent: a set of Pydantic models for the evidence dossier and the principal's verdict, plus the citation types and the non-negotiable validation rule that every claim carries a verifiable citation (searchfox permalink/symbol-id, exact diff line, exact stack frame). This contract is the swappable seam between every LLM seat (seniors → principal) and the anti-hallucination boundary: an uncited dossier field is rejected before it costs principal tokens.

## Scope
**In scope**
- Pure-data Pydantic models (no I/O, no DB, no LLM calls) in a new `crashclouseau/agent/schema.py`: `CrashBrief`, `Candidate`, `CallEdge`/`CallPath`, `DiffHunk`/`DiffLineCite`, `DataFlowHypothesis`, `SkepticResult`, `Verdict`, and the top-level `Dossier`.
- The citation type hierarchy (`SearchfoxCitation`, `DiffLineCitation`, `StackFrameCitation`) and a discriminated-union `Citation`.
- The `every-claim-has-a-citation` validation rule, enforced via Pydantic validators, plus a re-usable JSON-Schema export tuned for Anthropic strict structured outputs (`additionalProperties:false` on every object, no recursion, no numeric/string-length constraints).
- The enums for failure class, verdict decision (`strong-evidence`/`abstain`), confidence, and skeptic pass/fail.
- A thin `dossier_table` mapping spec (column names/types) for the additive persistence table — schema/contract only; the actual SQLAlchemy model + migration is owned by the persistence sub-plan, this unit only specifies the JSON column shape it serializes into.

**Out of scope (owned by other sub-plans)**
- `llm_call(role, ...)` abstraction, model-per-role config, `messages.parse`/`output_format`/`output_config` wiring, caching, batching (LLM-abstraction sub-plan).
- `searchfox-cli` subprocess adapter and permalink/symbol-id capture (call-graph adapter sub-plan) — this unit only defines the *shape* of the citation it returns.
- Crash Interpreter / Call-graph Explorer / Patch Scout / Data-flow Tracer / Skeptic / Principal agents that *fill in* the dossier (per-role sub-plans).
- The RQ job, the `update.py` enqueue hook, DB table creation/migration, UI evidence panel, and `report_bug.py` needinfo prefill (worker, persistence, UI, bug-filing sub-plans).

## Externalities

| Name | Kind | Version / Endpoint / Command | Status | Purpose |
|---|---|---|---|---|
| pydantic | python-lib | `pydantic>=2.9` (v2; needed for `model_validator`, discriminated unions, `model_json_schema`) | NEW (absent from requirements.txt) | Define & validate all dossier/verdict models; export strict JSON schema for Anthropic structured outputs |
| anthropic | python-lib | `anthropic` (no floor pinned here; the LLM sub-plan owns the version) | NEW (absent from requirements.txt) | NOT imported here, but the models are designed to be passed as `output_format=` to `client.messages.parse(...)`; constraint source for the strict-schema export rules |
| (stdlib) enum, typing, datetime | python-lib | stdlib (Python 3.14, per `.python-version`) | existing | `StrEnum`/`Enum` for failure class, decision, confidence; `Literal`/`Annotated` discriminated unions; timestamps |
| `claude-haiku-4-5` | llm-model | model id `claude-haiku-4-5`; $1/$5 per 1M tok; 200K ctx; strict structured outputs supported, NO `effort` (errors), no extended thinking | existing (NEW use) | Senior seats (crash-interpreter, patch-scout, skeptic) emit dossier fragments validated against these models. Min cacheable prefix 4096 tok |
| `claude-sonnet-5` | llm-model | model id `claude-sonnet-5`; $3/$15 per 1M tok; 1M ctx; adaptive thinking + `effort` (incl. `max`); min cacheable prefix 2048 tok | existing (NEW use) | Call-graph Explorer (Phase-0 navigator default) + optional Data-flow Tracer tier, emitting their dossier fragments (`Neighborhood`, `DataFlowHypothesis`) |
| `claude-opus-4-8` | llm-model | model id `claude-opus-4-8`; $5/$25 per 1M tok; 1M ctx; `thinking={"type":"adaptive"}` + `output_config={"effort":...}` (`budget_tokens` removed/400); min cacheable prefix 4096 tok | existing (NEW use) | Default principal; consumes full `Dossier`, emits `Verdict` via strict structured output. Also Data-flow Tracer fallback |
| `claude-fable-5` | llm-model | model id `claude-fable-5`; $10/$50 per 1M tok; 1M ctx; thinking ALWAYS on (`disabled` 400s — omit `thinking`); `effort` supported; requires 30-day data retention; min cacheable prefix 2048 tok | existing (NEW use) | Optional principal for hardest cases; same `Verdict` schema |
| `searchfox-cli` | CLI | `calls-from` / `calls-to` / `calls-between` (with depth), `define` (symbol/definition lookup + full-function source), text/regex search; repo selector (mozilla-central/autoland/beta/release); markdown output | NEW (external, github.com/padenot/searchfox-cli) | Source of `SearchfoxCitation` (permalink + symbol-id). This unit defines only the citation shape; the subprocess call belongs to the adapter sub-plan |
| libmozdata.socorro.ProcessedCrash | internal-module (libmozdata) | `libmozdata>=0.2.12`; accessed via `inspector.get_crash_data(uuid)` → `socorro.ProcessedCrash.get_processed(uuid)[uuid]` | existing | Origin of `CrashBrief` raw fields: `reason`, `crash_info.address`, `moz_crash_reason`, per-frame `inlines`/`trust`, `phc_alloc_stack`/`phc_free_stack`/`phc_kind`, `async_shutdown_timeout`, `json_dump.threads[N].frames` |
| libmozdata.hgmozilla.RawRevision | internal-module (libmozdata) | `libmozdata>=0.2.12`; `RawRevision.get_url(channel)` (raw unified diff text; used by `patch.parse` via `parsepatch`) | existing | Origin of `DiffHunk` text + `DiffLineCitation` line numbers/contents |
| crashclouseau.inspector | internal-module | functions `get_crash_data(uuid)`, `get_path_node(uri)`, `git2hg(git_hash)`; module-level regexes `HG_PAT`/`GIT_PAT` and constant `LANDO_GIT2HG`; an in-process `_GIT2HG_CACHE` | existing | Frame uri→(filename, hg node) parsing; `StackFrameCitation` reuses the `node`/`filename`/`line`/`stackpos` produced by `inspect_stacktrace` |
| crashclouseau.models | internal-module | `UUID`, `CrashStack` (`get_by_uuid`, `put_frames`), `Changeset` (`find`, `get_scores`, fields `added_lines`/`deleted_lines`/`touched_lines`/`isnew`/`analyzed`), `Node` (`node/channel/backedout/merge/bug/pushdate/hgauthor`), `Score` (`changesetid`/`crashstackid`/`score`), `HGAuthor` (`get_id(info)`) | existing | Seed source: `Candidate` is built from `Node`+`Changeset`; `StackFrameCitation` cross-checks `CrashStack` rows; `Score` is the demoted "seed strength" feature on `Candidate` |
| crashclouseau.utils | internal-module | `short_rev(rev)` (12-char truncate), `hash(s)`, `get_file_url(repo_url, filename, node, line, original)` (needs a repo URL arg) | existing | Normalize 12-char hg node in citations; build verifiable hg file URLs alongside searchfox permalinks (callers must supply `repo_url`) |
| crashclouseau.config | internal-module | `./config/global.json`; new `get_agent()` helper reading an `"agent"` block (mirrors existing `_get_global()[...]` helpers) | existing (NEW keys) | Tunable contract knobs: `min_citations_per_claim`, `abstain_below_confidence`, `schema_version` |
| Lando git2hg | REST-API | `GET https://lando.moz.tools/api/git2hg/firefox/{git_hash}` (template `inspector.LANDO_GIT2HG`, called by `inspector.git2hg`; returns `{"hg_hash": ...}`, 404 = non-Firefox source) | existing | Already used to normalize git→hg node; the contract stores the resulting hg node string in `StackFrameCitation.node` (no new call added by this unit) |
| config `agent.schema_version` | config | `global.json` → `agent.schema_version` (int, e.g. `1`) | NEW | Versions the dossier contract so persisted dossiers survive schema evolution |
| config `agent.min_citations_per_claim` | config | `global.json` → `agent.min_citations_per_claim` (int, default `1`) | NEW | Validation knob: minimum citations required on each claim-bearing field |
| config `agent.abstain_below_confidence` | config | `global.json` → `agent.abstain_below_confidence` (float, default `0.5`) | NEW | Verdict-consistency rule: `decision=strong-evidence` requires `confidence` at/above this floor |

## Deliverables
- **`crashclouseau/agent/__init__.py`** (NEW) — package marker for the evidence-agent code.
- **`crashclouseau/agent/schema.py`** (NEW) — all Pydantic models:
  - Enums: `FailureClass` (`uaf`/`null_deref`/`assertion`/`oob`/`shutdownhang`/`other`), `Decision` (`strong-evidence`/`abstain`), `Confidence` (`low`/`medium`/`high`), `SkepticStatus` (`pass`/`fail`/`unverifiable`), `CitationKind` (`searchfox`/`diff_line`/`stack_frame`).
  - Citation models: `SearchfoxCitation` (`kind: Literal["searchfox"]`, `permalink: str`, `symbol_id: str`, `repo: str`, `rev: str`), `DiffLineCitation` (`kind: Literal["diff_line"]`, `node: str`, `filename: str`, `line: int`, `side: Literal["added","deleted","context"]`, `content: str`), `StackFrameCitation` (`kind: Literal["stack_frame"]`, `uuid: str`, `stackpos: int`, `filename: str`, `function: str`, `line: int`, `node: str`). `Citation = Annotated[Union[...], Field(discriminator="kind")]`.
  - `Cited` mixin / base with `citations: list[Citation]` and a `@model_validator(mode="after")` enforcing `len(citations) >= min_citations_per_claim`.
  - Content models: `CrashBrief`, `Candidate`, `CallEdge`, `CallPath`, `DiffHunk`, `DataFlowHypothesis`, `SkepticResult`, `Verdict`, `Dossier`.
  - Functions: `strict_json_schema(model) -> dict` (returns Anthropic-compatible schema — see Implementation step 6 for the exact transform); `validate_dossier(obj) -> Dossier`; `dossier_to_db_json(d: Dossier) -> dict` / `dossier_from_db_json(d: dict) -> Dossier` (the JSON shape the persistence sub-plan stores in a `JSONB` column).
- **`crashclouseau/config.py`** (MODIFY) — add `get_agent()` returning the `"agent"` block, plus `get_agent_schema_version()`, `get_min_citations_per_claim()`, `get_abstain_below_confidence()` helpers (mirroring the existing `get_max_score()` / `_get_global()` pattern).
- **`config/global.json`** (MODIFY) — add an `"agent"` block with `schema_version`, `min_citations_per_claim`, `abstain_below_confidence`.
- **`tests/test_agent_schema.py`** (NEW) — unit tests for validation rules, discriminated-union round-trip, strict-schema export, and the verdict/confidence consistency rule.
- **`requirements.txt`** (MODIFY) — add `pydantic>=2.9` (and `anthropic` if not added by the LLM sub-plan first; coordinate to avoid a duplicate line). Both are genuinely absent from the current file.

## Interfaces
**Inputs consumed**
- Seed candidates from `CrashStack.get_by_uuid(uuid)` + `Score` (per-frame changesets with scores) and `Changeset.find`/`get_scores` → populate `Candidate.node/bug/author/seed_score` and the on-stack `StackFrameCitation`s. Off-stack candidates added by the Patch Scout reuse the same `Candidate` shape.
- Raw processed-crash fields from `inspector.get_crash_data(uuid)` (`reason`, `crash_info.address`, `moz_crash_reason`, per-frame `inlines`/`trust`, PHC stacks, `async_shutdown_timeout`) → `CrashBrief`.
- Frame uri parsing from `inspector.get_path_node` / `inspector.git2hg` → the normalized hg `node` stored in `StackFrameCitation`.
- searchfox-cli adapter output → `SearchfoxCitation` (permalink + symbol-id) on every `CallEdge`.
- `RawRevision`-derived diff text → `DiffHunk` + `DiffLineCitation`.

**Outputs produced**
- A validated `Dossier` instance (the principal's input) and a validated `Verdict` instance (the principal's output, embedded back into `Dossier.verdict`).
- Strict JSON schemas (`strict_json_schema(Dossier)`, `strict_json_schema(Verdict)`, and per-fragment schemas) for the LLM sub-plan to pass as `output_format`/`output_config`.
- `dossier_to_db_json(...)` / `dossier_from_db_json(...)` for the persistence sub-plan's JSONB column.

**Which dossier fields this unit reads/writes**
- This unit *writes the type of* every field and *validates* all of them; it does not populate domain content. It is the single owner of: `CrashBrief`, `Candidate`, `CallPath`/`CallEdge`, `DiffHunk`/`DiffLineCitation`, `DataFlowHypothesis`, `SkepticResult`, `Verdict` (`decision`/`confidence`/`needinfo_draft`/`abstain_reason`), and all three `Citation` subtypes.

**Depends on / feeds**
- Depends on: nothing at runtime (pure data) beyond `config`. Soft-depends on the searchfox adapter and LLM sub-plans agreeing to the citation/`output_format` shapes — coordinate field names early.
- Feeds: every agent-role sub-plan (they import these models), the LLM-abstraction sub-plan (`output_format`/`strict_json_schema`), the persistence sub-plan (`dossier_to_db_json`), the UI panel sub-plan (renders `Dossier`), and the bug-filing sub-plan (reads `Verdict.needinfo_draft` + `Candidate.author`).

## Implementation steps
1. Create `crashclouseau/agent/__init__.py` (empty) and add `pydantic>=2.9` to `requirements.txt`.
2. In `crashclouseau/agent/schema.py`, define the enums (`FailureClass`, `Decision`, `Confidence`, `SkepticStatus`, `CitationKind`) as `str`-valued so they serialize cleanly into JSON-schema `enum`s.
3. Define the three citation models with a `kind` `Literal` discriminator and the `Citation` discriminated union. Keep field names aligned with `inspector`/`CrashStack` (`node`, `filename`, `function`, `line`, `stackpos`) and the searchfox adapter (`permalink`, `symbol_id`, `repo`, `rev`).
4. Define a `Cited` base (or mixin) carrying `citations: list[Citation]` and a `@model_validator(mode="after")` that reads `config.get_min_citations_per_claim()` and rejects claim-bearing instances with too few citations. Apply it to `CallEdge`, `DiffHunk`, `DataFlowHypothesis`, and the `consistency`/`mechanism` claims inside `Verdict`.
5. Define content models:
   - `CrashBrief`: `failure_class`, `faulting_address`, `moz_crash_reason`, `reason`, `crashing_thread`, `signature`, `uuid`, `frames` (each with `stackpos`/`function`/`filename`/`line`/`node`/`inlines`/`trust`), optional `phc_kind`/`phc_alloc_stack`/`phc_free_stack`/`async_shutdown_timeout`.
   - `Candidate`: `node`, `bug`, `author` (display string from `HGAuthor`), `channel`, `pushdate`, `backedout`, `seed_score` (the demoted heuristic `Score`), `changed_functions`.
   - `CallEdge` (Cited): `caller_symbol`, `callee_symbol`, `via` (`calls-from`/`calls-to`/`calls-between`/`define`). `CallPath`: ordered `edges: list[CallEdge]` + `from_stackpos`/`to_symbol`.
   - `DiffHunk` (Cited): `node`, `filename`, `header` (the `@@ ... @@` line), `lines: list[DiffLineCitation]`.
   - `DataFlowHypothesis` (Cited): `summary`, `object_name`, `operation` (`free`/`mutate`/`null`/`oob`/`other`), `crash_site` (a `StackFrameCitation`).
   - `SkepticResult`: `claim_ref` (which dossier element), `status: SkepticStatus`, `note`, optional re-verification `citations`.
   - `Verdict`: `decision: Decision`, `confidence: Confidence`, optional `needinfo_draft`, optional `abstain_reason`, `mechanism`/`consistency` (Cited claims). `@model_validator`: if `decision == strong-evidence`, require non-empty `mechanism`/`consistency` citations and `confidence` at/above the floor from `config.get_abstain_below_confidence()` (using the categorical→numeric mapping below); if `decision == abstain`, require `abstain_reason` and forbid `needinfo_draft`.
   - `Dossier`: `schema_version`, `crash` (`CrashBrief`), `candidate` (`Candidate`), `call_path` (`CallPath`), `hunks: list[DiffHunk]`, `data_flow: DataFlowHypothesis | None`, `skeptic: list[SkepticResult]`, `verdict: Verdict | None`, `created` timestamp.
6. Implement `strict_json_schema(model)` on top of `model.model_json_schema()`. Per the authoritative Anthropic structured-output limits, the export MUST: set `additionalProperties: false` on every object; strip the genuinely-unsupported keywords (`minLength`, `maxLength`, `minimum`, `maximum`, `multipleOf`, and other numeric/string-length/array constraints); and assert the schema has no recursive cycle (raise if found). Note: `$ref`/`$defs`, `anyOf`, `allOf`, `enum`, `const`, and the listed string `format`s are SUPPORTED under strict mode — `$ref` inlining is therefore optional, not required (keep `$defs`/`$ref` if convenient; only flatten if it simplifies cycle detection). Also note that the official Python `anthropic` SDK already auto-strips unsupported constraints client-side when using `messages.parse(output_format=...)`; this helper is a defense-in-depth/audit tool so the schema is provably clean regardless of SDK path. Add a docstring tying each rule to the Anthropic JSON-schema limits.
7. Implement `validate_dossier`, `dossier_to_db_json` (`model_dump(mode="json")` with stable field order + `schema_version`), and `dossier_from_db_json` (`model_validate`, raising on version mismatch beyond the supported range).
8. Add the `"agent"` block to `config/global.json` and the `get_agent*` helpers to `config.py`.
9. Write `tests/test_agent_schema.py`: (a) a fully-cited dossier validates; (b) a `CallEdge`/`DiffHunk`/`Verdict` claim with zero citations raises; (c) discriminated-union JSON round-trips (`searchfox`/`diff_line`/`stack_frame`); (d) `strict_json_schema(Dossier)` contains none of `minLength`/`maxLength`/`minimum`/`maximum`/`multipleOf` and every object has `additionalProperties:false`; (e) `decision=strong-evidence` with low confidence or empty mechanism citations raises; `decision=abstain` requires `abstain_reason`.
10. Run `flake8` (repo `.flake8`) and the new tests; confirm no import of `db`/`anthropic`/`requests` leaked into `schema.py` (keep it pure).

## Risks & open questions
- **Anthropic strict-schema limits vs. Pydantic output.** Pydantic v2 emits `$defs`/`$ref` and may add `minLength`/`format`. `$defs`/`$ref`/`anyOf`/`allOf` are accepted by Anthropic strict mode, so they need NOT be inlined; the length/numeric constraints DO need stripping. Discriminated unions become `anyOf` — verify the SDK accepts `anyOf` with `additionalProperties:false` under strict mode (the LLM sub-plan should smoke-test one `messages.parse` call against `strict_json_schema(Verdict)`).
- **No recursion allowed** — `CallPath` is a flat `list[CallEdge]` (not a tree) specifically to avoid the unsupported recursive-schema case; confirm no future field reintroduces recursion.
- **Citation granularity for off-stack candidates.** A `Candidate` that is off-stack has no `StackFrameCitation` of its own; the grounding is the `CallPath`'s searchfox citations linking it to an on-stack frame. Confirm the validation rule does not wrongly demand a stack-frame citation on off-stack candidates.
- **Revision drift in `SearchfoxCitation.rev`.** Searchfox indexes ~tip while the crash is a specific build node; the citation stores the searchfox `rev` so reviewers can see the gap. Open question: should validation warn (not fail) when `SearchfoxCitation.rev != Candidate.node`? Proposal: store both, do not fail (drift is expected on nightly).
- **`needinfo_draft` contract.** `report_bug.py` cannot prefill a needinfo flag in the `enter_bug` URL — it sets `query["blocked"] = "clouseau,{bugid}"` (the bug's `blocked` field, the `clouseau` blocker alias) and returns the changeset's Bugzilla `assigned_to` as the display author. The needinfo field is therefore a *draft string* plus `Candidate.author` for display. Confirm the bug-filing sub-plan only consumes these as text.
- **Min cacheable prefix differs by model** (4096 for Opus 4.8/Haiku 4.5, 2048 for Sonnet 5/Fable 5) — affects how the LLM sub-plan splits the shared prefix, not this schema; noted to avoid a field that forces volatile data into the cached prefix.
- **`Confidence` enum vs. numeric threshold.** `abstain_below_confidence` is a float but `Confidence` is categorical; need an explicit `low/medium/high → 0.25/0.5/0.85` mapping (define it in `schema.py`, document it).
- **Fable-5-specific API behavior** (informational; owned by the LLM sub-plan, not this unit): thinking is always-on (`thinking={"type":"disabled"}` 400s — omit the param), and the org must have ≥30-day data retention or every request 400s. Mentioned so no schema field assumes a `disabled`-thinking principal call.

## Acceptance criteria
- `from crashclouseau.agent.schema import Dossier, Verdict, Citation, strict_json_schema, validate_dossier` imports with no DB/LLM/network dependency.
- A hand-built fully-cited `Dossier` validates; removing the citations from any `CallEdge`, `DiffHunk`, or `Verdict` mechanism/consistency claim raises `pydantic.ValidationError`.
- `strict_json_schema(Dossier)` and `strict_json_schema(Verdict)`: every object node has `additionalProperties:false`, contain none of `minLength`/`maxLength`/`minimum`/`maximum`/`multipleOf`, and contain no recursive `$ref` cycle (function raises on recursion). `$ref`/`$defs`/`anyOf` may legitimately remain.
- All three citation kinds round-trip through `model_dump(mode="json")` → `model_validate` and are correctly discriminated by `kind`.
- `Verdict` consistency rule holds: `strong-evidence` requires cited mechanism + consistency and confidence at/above the configured floor; `abstain` requires `abstain_reason` and rejects a `needinfo_draft`.
- `dossier_to_db_json(d)` produces JSON-serializable output carrying `schema_version`, and `dossier_from_db_json(dossier_to_db_json(d)) == d`.
- `config.get_agent()` returns the new block; helpers return the configured `schema_version`/`min_citations_per_claim`/`abstain_below_confidence`.
- `tests/test_agent_schema.py` passes and `flake8` is clean on the new/modified files.

Relevant paths: `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/agent/schema.py` (NEW), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/agent/__init__.py` (NEW), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/config.py`, `/home/calixte/dev/mozilla/crash-clouseau/config/global.json`, `/home/calixte/dev/mozilla/crash-clouseau/tests/test_agent_schema.py` (NEW), `/home/calixte/dev/mozilla/crash-clouseau/requirements.txt`. Grounding sources read: `crashclouseau/models.py`, `crashclouseau/inspector.py`, `crashclouseau/config.py`, `crashclouseau/worker.py`, `crashclouseau/update.py`, `crashclouseau/report_bug.py`, `crashclouseau/utils.py`, `config/global.json`, `requirements.txt`, and the claude-api skill (authoritative model facts).
