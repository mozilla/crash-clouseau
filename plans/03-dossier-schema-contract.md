# 03 — Dossier schema & strong-evidence contract

> **SUBSTRATE ALIGNMENT (2026-07-02): validate the Agent-SDK best-effort handoff, not
> `messages.parse`.** This unit was originally specced to export a strict JSON schema that the
> raw `anthropic` **Messages** SDK would enforce via `messages.parse(output_format=...)`. That is
> **superseded.** Under the vendored hackbot + Claude Agent SDK path (#02) there is **no**
> `output_format`/`output_config`/`messages.parse`: the principal ends its final message with a
> fenced ` ```json ` block that #02 parses **best-effort** (`re` + `json.loads`, last block wins —
> mirrors bugbug's `frontend_triage.parse_plan`) into `CrashTriageResult(HackbotAgentResult)`.
> This unit therefore owns the Pydantic models that block is **validated against** and the
> parse-and-validate helper that **ABSTAINS** when a claim lacks its citation — it no longer
> produces a schema for the API to enforce. The Pydantic models + the every-claim-has-a-citation
> validators are unchanged and remain the anti-hallucination boundary. See the verified API notes
> in memory `clouseau-hackbot-substrate-api` and the substrate decision in #02.

## Objective
Define the typed, citation-bearing hand-off contract for the crash-triage agent: a set of Pydantic models for the evidence dossier and the principal's verdict, plus the citation types and the non-negotiable validation rule that every claim carries a verifiable citation (searchfox permalink/symbol-id, exact diff line, exact stack frame). Under the Claude Agent SDK (#02) the principal emits its verdict as a trailing ` ```json ` block that #02 parses best-effort into `CrashTriageResult(HackbotAgentResult)`; this unit owns the models that parsed dict (and each role's parsed sub-block) is validated against, and the `parse_and_validate()` helper that ABSTAINS rather than propagating an uncited claim. This contract is the swappable seam between every LLM seat (seniors → principal) and the anti-hallucination boundary: an uncited dossier field never survives into the persisted verdict.

## Scope
**In scope**
- Pure-data Pydantic models (no I/O, no DB, no LLM/SDK calls) in a new `crashclouseau/agent/schema.py`: `CrashBrief`, `Candidate`, `CallEdge`/`CallPath`, `DiffHunk`/`DiffLineCite`, `DataFlowHypothesis`, `SkepticResult`, `Verdict`, and the top-level `Dossier`.
- The citation type hierarchy (`SearchfoxCitation`, `DiffLineCitation`, `StackFrameCitation`) and a discriminated-union `Citation`.
- The `every-claim-has-a-citation` validation rule, enforced via Pydantic validators, plus a `parse_and_validate()` helper that validates the best-effort JSON handoff #02 parses from the SDK `ResultMessage` (and each role's parsed sub-block) and ABSTAINS — never lets a hallucinated/uncited claim reach the verdict — when a claim lacks its citation.
- The enums for failure class, verdict decision (`strong-evidence`/`abstain`), confidence, and skeptic pass/fail.
- A thin `dossier_table` mapping spec (column names/types) for the additive persistence table — schema/contract only; the actual SQLAlchemy model + migration is owned by the persistence sub-plan, this unit only specifies the JSON column shape it serializes into (mirrors `CrashTriageResult.model_dump()` / hackbot's `summary.json['findings']`).

**Out of scope (owned by other sub-plans)**
- The agent substrate — `ClaudeAgentOptions`/`ClaudeSDKClient` assembly, per-role `AgentDefinition` tiering + options-level `effort`, the trailing-` ```json ` extraction from `ResultMessage`, in-process MCP tool wiring, and `CrashTriageResult` construction (#02 agent-substrate sub-plan). This unit only defines the models #02 validates the parsed handoff against and the parse/abstain helper it calls.
- `searchfox-cli` subprocess adapter and permalink/symbol-id capture (call-graph adapter sub-plan, #01) — this unit only defines the *shape* of the citation it returns.
- Crash Interpreter / Call-graph Explorer / Patch Scout / Data-flow Tracer / Skeptic / Principal roles (SDK-native `AgentDefinition`s) that *fill in* the dossier (per-role sub-plans #05–#10).
- The RQ job, the `update.py` enqueue hook, DB table creation/migration, UI evidence panel, and `report_bug.py` needinfo prefill (worker #11, persistence, UI, bug-filing sub-plans).

## Externalities

| Name | Kind | Version / Endpoint / Command | Status | Purpose |
|---|---|---|---|---|
| pydantic | python-lib | `pydantic>=2` (v2; needed for `model_validator` + discriminated unions) | added by #01 | Define & validate all dossier/verdict models; validate the best-effort JSON handoff #02 parses from the SDK `ResultMessage`. No strict-schema export is produced (see #02). |
| claude-agent-sdk | python-lib | `claude-agent-sdk>=0.2` (owned by #02; **not imported here**) | NEW (via #02) | NOT imported by this unit. The principal ends its final message with a best-effort trailing ` ```json ` block in `ResultMessage.result`; #02 parses it and this unit validates the parsed dict against these models. There is NO `messages.parse`/`output_format`/`output_config` in this path, so no strict JSON-schema export is generated. |
| (stdlib) enum, typing, datetime, json, re | python-lib | stdlib (Python 3.14, per `.python-version`) | existing | `StrEnum`/`Enum` for failure class, decision, confidence; `Literal`/`Annotated` discriminated unions; timestamps; `json`/`re` for the trailing-` ```json ` parse helper (mirrors bugbug's `_JSON_BLOCK` / `parse_plan`) |
| `claude-haiku-4-5` | llm-model | model id `claude-haiku-4-5`; $1/$5 per 1M tok; 200K ctx; NO `effort` (errors), no extended thinking; min cacheable prefix 4096 tok | existing (NEW use) | Senior seats (crash-interpreter, patch-scout, skeptic) emit dossier fragments as trailing ` ```json ` validated against these models |
| `claude-sonnet-5` | llm-model | model id `claude-sonnet-5`; $3/$15 per 1M tok; 1M ctx; adaptive thinking + `effort` (incl. `max`); min cacheable prefix 2048 tok | existing (NEW use) | Call-graph Explorer (Phase-0 navigator default) + optional Data-flow Tracer tier, emitting their dossier fragments (`CallPath`, `DataFlowHypothesis`) |
| `claude-opus-4-8` | llm-model | model id `claude-opus-4-8`; $5/$25 per 1M tok; 1M ctx; `thinking={"type":"adaptive"}` + options-level `effort` (`budget_tokens` removed/400); min cacheable prefix 4096 tok | existing (NEW use) | Default principal; consumes the full `Dossier`, emits the `Verdict` in its final ` ```json ` handoff (validated here). Also Data-flow Tracer fallback |
| `claude-fable-5` | llm-model | model id `claude-fable-5`; $10/$50 per 1M tok; 1M ctx; thinking ALWAYS on (`disabled` 400s — omit `thinking`); `effort` supported; requires 30-day data retention; min cacheable prefix 2048 tok | existing (NEW use) | Optional principal for hardest cases; same `Verdict` schema |
| `searchfox-cli` | CLI | `calls-from` / `calls-to` / `calls-between` (with depth), `define` (symbol/definition lookup + full-function source), text/regex search; repo selector (mozilla-central/beta/release/esr/comm-central — NO autoland); markdown output | NEW (external, github.com/padenot/searchfox-cli) | Source of `SearchfoxCitation` (synthesized source URL + symbol-id). This unit defines only the citation shape; the subprocess call belongs to the adapter sub-plan (#01) |
| libmozdata.socorro.ProcessedCrash | internal-module (libmozdata) | `libmozdata>=0.2.12`; accessed via `inspector.get_crash_data(uuid)` → `socorro.ProcessedCrash.get_processed(uuid)[uuid]` | existing | Origin of `CrashBrief` raw fields: `reason`, `crash_info.address`, `moz_crash_reason`, per-frame `inlines`/`trust`, `phc_alloc_stack`/`phc_free_stack`/`phc_kind`, `async_shutdown_timeout`, `json_dump.threads[N].frames` |
| libmozdata.hgmozilla.RawRevision | internal-module (libmozdata) | `libmozdata>=0.2.12`; `RawRevision.get_url(channel)` (raw unified diff text; used by `patch.parse` via `parsepatch`) | existing | Origin of `DiffHunk` text + `DiffLineCitation` line numbers/contents |
| crashclouseau.inspector | internal-module | functions `get_crash_data(uuid)`, `get_path_node(uri)`, `git2hg(git_hash)`; module-level regexes `HG_PAT`/`GIT_PAT` and constant `LANDO_GIT2HG`; an in-process `_GIT2HG_CACHE` | existing | Frame uri→(filename, hg node) parsing; `StackFrameCitation` reuses the `node`/`filename`/`line`/`stackpos` produced by `inspect_stacktrace` |
| crashclouseau.models | internal-module | `UUID`, `CrashStack` (`get_by_uuid`, `put_frames`), `Changeset` (`find`, `get_scores`, fields `added_lines`/`deleted_lines`/`touched_lines`/`isnew`/`analyzed`), `Node` (`node/channel/backedout/merge/bug/pushdate/hgauthor`), `Score` (`changesetid`/`crashstackid`/`score`), `HGAuthor` (`get_id(info)`) | existing | Seed source: `Candidate` is built from `Node`+`Changeset`; `StackFrameCitation` cross-checks `CrashStack` rows; `Score` is the demoted "seed strength" feature on `Candidate` |
| crashclouseau.utils | internal-module | `short_rev(rev)` (12-char truncate), `hash(s)`, `get_file_url(repo_url, filename, node, line, original)` (needs a repo URL arg) | existing | Normalize 12-char hg node in citations; build verifiable hg file URLs alongside searchfox permalinks (callers must supply `repo_url`) |
| crashclouseau.config | internal-module | existing `_get_global()` + the `get_agent()` helper added by #01 (reads the `"agent"` block) | existing (extend) | Tunable contract knobs read from the existing `agent` block: `schema_version`, `min_citations_per_claim`, `abstain_below_confidence` |
| Lando git2hg | REST-API | `GET https://lando.moz.tools/api/git2hg/firefox/{git_hash}` (template `inspector.LANDO_GIT2HG`, called by `inspector.git2hg`; returns `{"hg_hash": ...}`, 404 = non-Firefox source) | existing | Already used to normalize git→hg node; the contract stores the resulting hg node string in `StackFrameCitation.node` (no new call added by this unit) |
| config `agent.schema_version` | config | `global.json` → `agent.schema_version` (int, e.g. `1`) | NEW key | Versions the dossier contract so persisted dossiers survive schema evolution |
| config `agent.min_citations_per_claim` | config | `global.json` → `agent.min_citations_per_claim` (int, default `1`) | NEW key | Validation knob: minimum citations required on each claim-bearing field |
| config `agent.abstain_below_confidence` | config | `global.json` → `agent.abstain_below_confidence` (float, default `0.5`) | NEW key | Verdict-consistency rule: `decision=strong-evidence` requires `confidence` at/above this floor |

## Deliverables
- **`crashclouseau/agent/__init__.py`** (NEW, shared with #02 — create if absent) — package marker for the evidence-agent code.
- **`crashclouseau/agent/schema.py`** (NEW) — all Pydantic models:
  - Enums: `FailureClass` (`uaf`/`null_deref`/`assertion`/`oob`/`shutdownhang`/`other`), `Decision` (`strong-evidence`/`abstain`), `Confidence` (`low`/`medium`/`high`), `SkepticStatus` (`pass`/`fail`/`unverifiable`), `CitationKind` (`searchfox`/`diff_line`/`stack_frame`).
  - Citation models: `SearchfoxCitation` (`kind: Literal["searchfox"]`, `permalink: str`, `symbol_id: str`, `repo: str`, `rev: str`), `DiffLineCitation` (`kind: Literal["diff_line"]`, `node: str`, `filename: str`, `line: int`, `side: Literal["added","deleted","context"]`, `content: str`), `StackFrameCitation` (`kind: Literal["stack_frame"]`, `uuid: str`, `stackpos: int`, `filename: str`, `function: str`, `line: int`, `node: str`). `Citation = Annotated[Union[...], Field(discriminator="kind")]`.
  - `Cited` mixin / base with `citations: list[Citation]` and a `@model_validator(mode="after")` enforcing `len(citations) >= min_citations_per_claim`.
  - Content models: `CrashBrief`, `Candidate`, `CallEdge`, `CallPath`, `DiffHunk`, `DataFlowHypothesis`, `SkepticResult`, `Verdict`, `Dossier`.
  - Functions:
    - `validate_dossier(obj: dict) -> Dossier` — `Dossier.model_validate(obj)`; the citation validators raise `pydantic.ValidationError` on any uncited claim (the strict gate).
    - `parse_and_validate(result: str | dict) -> Dossier` — extract the last ` ```json ` block if given raw text, validate, and **ABSTAIN**: on a missing/malformed block, invalid JSON, or an uncited claim return a `Dossier` whose `verdict` is `Decision.abstain` with an `abstain_reason` naming the failure, so hallucinated content never survives. This is the SDK-path anti-hallucination boundary #02 calls on `ResultMessage.result`.
    - `validate_role_fragment(role: str, obj: dict)` — validate one role's parsed sub-block against its fragment model (call-graph-explorer→`CallPath`, patch-scout→`list[DiffHunk]`, data-flow-tracer→`DataFlowHypothesis`, skeptic→`SkepticResult`).
    - `dossier_to_db_json(d: Dossier) -> dict` / `dossier_from_db_json(d: dict) -> Dossier` — the JSON shape the persistence sub-plan stores in a `JSONB` column (mirrors `CrashTriageResult.model_dump()`).
- **`crashclouseau/config.py`** (MODIFY) — `get_agent()` already exists (added by #01); add `get_agent_schema_version()`, `get_min_citations_per_claim()`, `get_abstain_below_confidence()` reading its sub-keys (mirroring the existing `get_max_score()` / `_get_global()` pattern).
- **`config/global.json`** (MODIFY) — add `schema_version`, `min_citations_per_claim`, `abstain_below_confidence` to the existing `"agent"` block (added by #01).
- **`tests/test_agent_schema.py`** (NEW) — unit tests for validation rules, discriminated-union round-trip, the parse-and-validate/abstain path, and the verdict/confidence consistency rule.
- **`requirements.txt`** — no change: `pydantic>=2` is already present (added by #01). No `anthropic` line — the Agent SDK dep (`claude-agent-sdk`) is owned by #02 and is not imported here.

## Interfaces
**Inputs consumed**
- Seed candidates from `CrashStack.get_by_uuid(uuid)` + `Score` (per-frame changesets with scores) and `Changeset.find`/`get_scores` → populate `Candidate.node/bug/author/seed_score` and the on-stack `StackFrameCitation`s. Off-stack candidates added by the Patch Scout reuse the same `Candidate` shape.
- Raw processed-crash fields from `inspector.get_crash_data(uuid)` (`reason`, `crash_info.address`, `moz_crash_reason`, per-frame `inlines`/`trust`, PHC stacks, `async_shutdown_timeout`) → `CrashBrief`.
- Frame uri parsing from `inspector.get_path_node` / `inspector.git2hg` → the normalized hg `node` stored in `StackFrameCitation`.
- searchfox-cli adapter output (#01) → `SearchfoxCitation` (synthesized source URL + symbol-id) on every `CallEdge`.
- `RawRevision`-derived diff text → `DiffHunk` + `DiffLineCitation`.
- **The principal's final message** (`ResultMessage.result`, parsed best-effort by #02) → `parse_and_validate` validates the trailing ` ```json ` block against `Dossier`/`Verdict`.

**Outputs produced**
- A validated `Dossier` instance — the content #02 folds into `CrashTriageResult` and #11 persists — and a validated `Verdict` (embedded back into `Dossier.verdict`).
- `parse_and_validate(result)` / `validate_dossier(obj)` / `validate_role_fragment(role, obj)` — the helpers #02 calls on the parsed trailing-` ```json ` handoff and per-role sub-blocks; `parse_and_validate` returns a validated `Dossier` or an abstaining one when a claim lacks its citation.
- `dossier_to_db_json(...)` / `dossier_from_db_json(...)` for the persistence sub-plan's JSONB column (mirrors `CrashTriageResult.model_dump()` / hackbot's `summary.json['findings']`).

**Which dossier fields this unit reads/writes**
- This unit *writes the type of* every field and *validates* all of them; it does not populate domain content. It is the single owner of: `CrashBrief`, `Candidate`, `CallPath`/`CallEdge`, `DiffHunk`/`DiffLineCitation`, `DataFlowHypothesis`, `SkepticResult`, `Verdict` (`decision`/`confidence`/`needinfo_draft`/`abstain_reason`), and all three `Citation` subtypes.

**Depends on / feeds**
- Depends on: nothing at runtime (pure data) beyond `config`. Soft-depends on the searchfox adapter (#01) and the agent substrate (#02) agreeing on the citation/handoff field names — coordinate field names early.
- Feeds: every agent-role sub-plan (they import these models), the agent-substrate sub-plan #02 (calls `parse_and_validate`/`validate_role_fragment` on the parsed handoff), the persistence sub-plan (`dossier_to_db_json`), the UI panel sub-plan (renders `Dossier`), and the bug-filing sub-plan (reads `Verdict.needinfo_draft` + `Candidate.author`).

## Implementation steps
1. Create `crashclouseau/agent/__init__.py` if absent (shared with #02). `pydantic>=2` is already in `requirements.txt` (from #01) — no dep change.
2. In `crashclouseau/agent/schema.py`, define the enums (`FailureClass`, `Decision`, `Confidence`, `SkepticStatus`, `CitationKind`) as `str`-valued so they serialize cleanly into JSON.
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
6. Implement the parse-and-validate helpers (the SDK-path anti-hallucination boundary — no schema is exported to the API):
   - A private `_extract_last_json_block(text)` locates the LAST ` ```json ` fence and `json.loads` it (regex `r"```json\s*(\{.*?\})\s*```"`, `re.DOTALL`, `matches[-1]` — the same shape as bugbug's `frontend_triage._JSON_BLOCK`/`parse_plan`); returns `None` on no match or a `json.JSONDecodeError`.
   - `validate_dossier(obj)` runs `Dossier.model_validate(obj)`; the `Cited` validators raise `pydantic.ValidationError` on any uncited claim.
   - `parse_and_validate(result)` composes them and, on ANY failure (no block, malformed JSON, or a `ValidationError` from a missing citation), returns an abstaining `Dossier` — `verdict=Verdict(decision=Decision.abstain, abstain_reason=<what failed>)`, retaining whatever sub-sections validated where possible — instead of propagating uncited content. #02 calls this on `ResultMessage.result`; it must NEVER raise into the RQ job.
   - `validate_role_fragment(role, obj)` validates a single role's parsed sub-block against its fragment model (call-graph-explorer→`CallPath`, patch-scout→`list[DiffHunk]`, data-flow-tracer→`DataFlowHypothesis`, skeptic→`SkepticResult`).
7. Implement `dossier_to_db_json` (`model_dump(mode="json")` with stable field order + `schema_version`) and `dossier_from_db_json` (`model_validate`, raising on a version outside the supported range).
8. Add `schema_version`/`min_citations_per_claim`/`abstain_below_confidence` to the existing `"agent"` block in `config/global.json` and the `get_agent_schema_version()`/`get_min_citations_per_claim()`/`get_abstain_below_confidence()` helpers to `config.py`.
9. Write `tests/test_agent_schema.py`: (a) a fully-cited dossier validates; (b) a `CallEdge`/`DiffHunk`/`Verdict` claim with zero citations raises `ValidationError` from `validate_dossier`; (c) discriminated-union JSON round-trips (`searchfox`/`diff_line`/`stack_frame`); (d) `parse_and_validate` returns an **abstaining** `Dossier` (never raises) on a missing block, a malformed ` ```json ` block, AND on a dossier whose `CallEdge`/`DiffHunk`/`Verdict` claim is uncited — while `validate_dossier` on the same inputs DOES raise; (e) `decision=strong-evidence` with low confidence or empty mechanism citations raises; `decision=abstain` requires `abstain_reason`.
10. Run `flake8` (repo `.flake8`) and the new tests; confirm no import of `db`/`claude-agent-sdk`/`requests`/network leaked into `schema.py` (keep it pure data + stdlib `json`/`re` + `config`).

## Risks & open questions
- **Best-effort handoff, not a schema-enforced one.** The SDK path has no `output_format`: the principal may omit the ` ```json ` block, emit malformed JSON, or cite nothing. `parse_and_validate` MUST abstain (never raise into the RQ job, never pass uncited content downstream) — verify the missing-block, malformed-JSON, and uncited-claim paths all yield `decision=abstain`. Discriminated-union parsing: a `Citation` dict without a valid `kind` must fail validation (→ abstain), not silently drop.
- **Last-block-wins ambiguity.** The parser takes the *last* ` ```json ` fence (matching #02/`parse_plan`); a role that prints an illustrative JSON block after the real handoff would shadow it. Keep the principal prompt instructing a single final block; treat extra blocks as a prompt bug, not a schema change.
- **Citation granularity for off-stack candidates.** A `Candidate` that is off-stack has no `StackFrameCitation` of its own; the grounding is the `CallPath`'s searchfox citations linking it to an on-stack frame. Confirm the validation rule does not wrongly demand a stack-frame citation on off-stack candidates.
- **Revision drift in `SearchfoxCitation.rev`.** Searchfox indexes ~tip while the crash is a specific build node; the citation stores the searchfox `rev` so reviewers can see the gap. Open question: should validation warn (not fail) when `SearchfoxCitation.rev != Candidate.node`? Proposal: store both, do not fail (drift is expected on nightly).
- **`needinfo_draft` contract.** `report_bug.py` cannot prefill a needinfo flag in the `enter_bug` URL — it sets `query["blocked"] = "clouseau,{bugid}"` (the bug's `blocked` field, the `clouseau` blocker alias) and returns the changeset's Bugzilla `assigned_to` as the display author. The needinfo field is therefore a *draft string* plus `Candidate.author` for display. Confirm the bug-filing sub-plan only consumes these as text. (In the recorded-action path #12, needinfo is a `bugzilla.update_bug` action, not a schema field.)
- **Min cacheable prefix differs by model** (4096 for Opus 4.8/Haiku 4.5, 2048 for Sonnet 5/Fable 5) — affects how #02 splits the shared prefix, not this schema; noted to avoid a field that forces volatile data into the cached prefix.
- **`Confidence` enum vs. numeric threshold.** `abstain_below_confidence` is a float but `Confidence` is categorical; need an explicit `low/medium/high → 0.25/0.5/0.85` mapping (define it in `schema.py`, document it).
- **Fable-5-specific API behavior** (informational; owned by #02, not this unit): thinking is always-on (`thinking={"type":"disabled"}` 400s — omit the param), and the org must have ≥30-day data retention or every request 400s. Mentioned so no schema field assumes a `disabled`-thinking principal call.

## Acceptance criteria
- `from crashclouseau.agent.schema import Dossier, Verdict, Citation, validate_dossier, parse_and_validate` imports with no DB/SDK/network dependency.
- A hand-built fully-cited `Dossier` validates; removing the citations from any `CallEdge`, `DiffHunk`, or `Verdict` mechanism/consistency claim raises `pydantic.ValidationError` from `validate_dossier`.
- `parse_and_validate` returns an **abstaining** `Dossier` (`verdict.decision == Decision.abstain` with a populated `abstain_reason`) — never a raised error — on (a) a missing ` ```json ` block, (b) a malformed/invalid-JSON block, and (c) a parsed dossier with an uncited `CallEdge`/`DiffHunk`/`Verdict` claim; `validate_dossier` on the same inputs DOES raise.
- All three citation kinds round-trip through `model_dump(mode="json")` → `model_validate` and are correctly discriminated by `kind`.
- `Verdict` consistency rule holds: `strong-evidence` requires cited mechanism + consistency and confidence at/above the configured floor; `abstain` requires `abstain_reason` and rejects a `needinfo_draft`.
- `dossier_to_db_json(d)` produces JSON-serializable output carrying `schema_version` (mirroring `CrashTriageResult.model_dump()`), and `dossier_from_db_json(dossier_to_db_json(d)) == d`.
- `config.get_agent()` returns the block; the new helpers return the configured `schema_version`/`min_citations_per_claim`/`abstain_below_confidence`.
- `tests/test_agent_schema.py` passes and `flake8` is clean on the new/modified files.

Relevant paths: `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/agent/schema.py` (NEW), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/agent/__init__.py` (NEW, shared with #02), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/config.py`, `/home/calixte/dev/mozilla/crash-clouseau/config/global.json`, `/home/calixte/dev/mozilla/crash-clouseau/tests/test_agent_schema.py` (NEW). Grounding sources read: `crashclouseau/models.py`, `crashclouseau/inspector.py`, `crashclouseau/config.py`, `crashclouseau/worker.py`, `crashclouseau/update.py`, `crashclouseau/report_bug.py`, `crashclouseau/utils.py`, `config/global.json`, `requirements.txt`, `plans/02-llm-abstraction-tiering.md`, the vendored hackbot substrate (`/tmp/bugbug/libs/hackbot-runtime/hackbot_runtime/results.py` = `HackbotAgentResult`; `/tmp/bugbug/agents/frontend-triage/.../agent.py` = `parse_plan`/`_JSON_BLOCK`), memory `clouseau-hackbot-substrate-api`, and the claude-api skill (authoritative model facts).
</content>
</invoke>
