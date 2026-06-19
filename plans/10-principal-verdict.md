# 10 — Principal: Claude verdict + abstain

## Objective
Consume the pre-verified evidence dossier produced by the seniors/skeptic/data-flow units and, using Claude Opus 4.8 (Fable 5 optional) via the shared `llm_call(role=...)` abstraction with adaptive thinking, render either STRONG EVIDENCE (a citation-backed causal chain from a candidate patch to the crash, a confidence value, and a needinfo draft) or a calibrated ABSTAIN. A schema-validating step drops any claim or citation not already present in the dossier's candidate/citation sets, so the principal can never invent artifacts. The unit writes only the `verdict` portion of the dossier and enqueues nothing beyond persistence.

## Scope
In scope:
- The single strong-model call that turns a finished dossier into a verdict (`crashclouseau/agent/principal.py`).
- The verdict Pydantic schema and the post-hoc grounding validator that drops out-of-set claims/citations.
- The needinfo/comment draft text rendered from validated content (display-only; no Bugzilla write).
- Reading `agent.llm.role == "principal"` config (model id, effort, Fable-5 toggle), and writing the `verdict` dossier field + the `Score`/`UUID` annotations that flag a strong verdict for the web UI.

Out of scope (owned by other sub-plans):
- The `llm_call()` provider abstraction, anthropic client construction, model/price config block, prompt-cache plumbing of the shared prefix — owned by the LLM-abstraction sub-plan. This unit only *calls* `llm_call`.
- Building the dossier (crash brief, candidate diffs, call-graph exploration, data-flow trace, skeptic pre-verification) — owned by the senior/skeptic/data-flow units. This unit consumes their output as read-only.
- searchfox-cli invocation — this unit never shells out; it relies on citations the dossier already captured.
- The RQ enqueue hook in `update.py`/`analyze_one_report` and the dossier dataclass/`DossierStore` definition — owned by the orchestration/dossier-model sub-plan. This unit imports the dossier type and the persistence call.
- Heuristic scorer demotion to a SEED — owned by the scoring sub-plan.
- Bugzilla filing (`report_bug.py`) — unchanged; the needinfo draft is surfaced, not posted.

## Externalities

| Name | Kind | Version / Endpoint / Command | Status | Purpose |
|------|------|------------------------------|--------|---------|
| `anthropic` | python-lib | `anthropic>=0.69` (add to requirements.txt; NEW dep, shared with LLM-abstraction unit) | NEW | SDK behind `llm_call`; this unit does not construct the client directly |
| `pydantic` | python-lib | `pydantic>=2.7` (add to requirements.txt; NEW dep) | NEW | `Verdict` output schema for `messages.parse` + validation of dropped claims |
| Claude Opus 4.8 | llm-model | model id `claude-opus-4-8`; $5 / $25 per 1M tok (input/output); 1M ctx; 4096-token min cache prefix; effort `low\|medium\|high\|xhigh\|max` | existing (via NEW dep) | Default principal model; adaptive thinking + `effort` |
| Claude Fable 5 | llm-model | model id `claude-fable-5`; $10 / $50 per 1M tok; 1M ctx; 2048-token min cache prefix; thinking always-on (omit `thinking`; explicit `disabled` 400s); requires 30-day data retention (ZDR orgs 400) | existing (via NEW dep) | Optional hardest-case principal; `refusal` stop reason + server-side `fallbacks` to `claude-opus-4-8` via beta `server-side-fallback-2026-06-01` |
| `llm_call` | internal-module | `crashclouseau/agent/llm.py::llm_call(role, system, messages, output_schema=..., max_tokens=..., stream=...)` | NEW (other sub-plan) | Role→model resolution, adaptive thinking, `effort`, prompt caching, `messages.parse`/`output_config.format`, streaming; this unit is a caller only |
| Dossier model | internal-module | `crashclouseau/agent/dossier.py::Dossier`, `DossierStore.load(uuid)` / `.save(dossier)` | NEW (other sub-plan) | Read pre-verified dossier; write back the `verdict` field |
| `crashclouseau.config` | internal-module | `config.py` — add `get_agent_llm(role)` reading `agent.llm.<role>` from `./config/global.json` (loaded via existing `_get_global()`) | NEW key (config sub-plan owns the loader; this unit reads `principal` keys) | `model`, `effort`, `max_tokens`, `enabled`, `confidence_threshold` for the principal |
| `crashclouseau.models.UUID` | internal-module | `models.py:764` — `UUID.get_info(uuid)` (`:791`); add `UUID.set_verdict(uuid, label, confidence, commit=True)` | NEW method | Persist verdict label/confidence flag for reports list UI |
| `crashclouseau.models.Score` | internal-module | `models.py:1140` — `Score(changesetid, crashstackid, score)` (`__init__` `:1152`); add `Score.get_for_uuid(uuid)` | NEW method | Map dossier candidate changesets back to their heuristic `Score` rows for cross-checking the candidate set |
| `crashclouseau.models.CrashStack` | internal-module | `models.py:1176` — columns `filename`/`function`/`line`/`stackpos`/`node`/`uuidid`; `CrashStack.get_by_uuid(uuid)` (`:1264`) | existing | Read stored crash frames to validate "on-stack" citations |
| `crashclouseau.models.Changeset` | internal-module | `models.py:212` — `Changeset.find(filenames, mindate, maxdate, channel)` (`:338`), `Changeset.get_scores(filename, line, chgsets, csid)` (`:363`) | existing | Resolve candidate changeset metadata referenced in dossier citations |
| `crashclouseau.models.Node` | internal-module | `models.py:127` — `Node.get_bugid(node, channel)` (`:169`) returns the **bug id only** (int) | existing | Bug id for the needinfo draft |
| `crashclouseau.models.HGAuthor` | internal-module | `models.py:550` — author stored as `Node.hgauthor` FK to `HGAuthor` (`email`/`real`/`nick`); no by-id getter exists, so a join (or a NEW small helper) is needed to resolve the author | existing (needs a join/helper) | Patch author for the needinfo draft (NOT returned by `Node.get_bugid`) |
| `crashclouseau.models.commit` | internal-module | `models.py:1327` — `models.commit()` | existing | Flush verdict writes |
| `crashclouseau.logger` | internal-module | `logger.py::logger` (`logging.getLogger()`, level INFO) | existing | Structured logging of verdict label, confidence, dropped-claim count, token usage, `_request_id` |
| jinja2 | python-lib | `jinja2>=3.1.6` (already in requirements.txt) | existing | Render `templates/needinfo.txt` from validated verdict fields (via `jinja2.Environment(FileSystemLoader("templates"))`, the same pattern `report_bug.py` uses for `bug.txt`) |
| `templates/needinfo.txt` | config | new jinja2 template under `templates/` (repo-root `templates/`, loaded with a CWD-relative `FileSystemLoader("templates")` exactly as `report_bug.py` does) | NEW | needinfo/comment draft body (author, bug id, cited chain) |
| `crashclouseau.inspector` | internal-module | `inspector.py::get_path_node` (`:125`), frame `node`/`function`/`line` shape | existing (read-only) | Reference for the on-stack frame citation shape the validator checks against |
| Socorro ProcessedCrash | REST-API | via `inspector.get_crash_data` (`:58`) — NOT called here | existing (out of scope) | Crash data already captured in the dossier brief; principal does not re-fetch |
| searchfox-cli | CLI | `calls-from` / `calls-to` / `calls-between` / `define` — NOT invoked by this unit | existing (out of scope) | Call-graph evidence already in dossier citations; LIMITS (misses virtual/indirect/fn-ptr/template/macro & cross-language edges; indexes ~tip not the crash build node; gives edges, not data flow) apply at dossier-build time, not here |
| lando git2hg | REST-API | `https://lando.moz.tools/api/git2hg/firefox/{hash}` (`inspector.py:21` `LANDO_GIT2HG`) — NOT called here | existing (out of scope) | hg/git resolution already done upstream of the dossier |

## Deliverables
- `crashclouseau/agent/principal.py` (NEW):
  - `class CausalLink(pydantic.BaseModel)` — `candidate_changeset: str`, `from_symbol: str`, `to_symbol: str`, `relation: str` (enum: `calls`/`called_by`/`mutates_arg`/`frees`/`on_stack_frame`/`modifies_function`), `citation_id: str`, `rationale: str`.
  - `class Verdict(pydantic.BaseModel)` — `label: str` (enum: `strong_evidence`/`abstain`), `culprit_changeset: str | None`, `confidence: float` (0–1; validated client-side since JSON-schema `minimum`/`maximum` are unsupported by structured outputs), `causal_chain: list[CausalLink]`, `cited_artifacts: list[str]`, `abstain_reason: str | None`, `needinfo_summary: str`. All objects carry `additionalProperties: false` + `required` (set via pydantic `model_config = ConfigDict(extra="forbid")`).
  - `build_principal_messages(dossier) -> tuple[str, list[dict]]` — assembles the system prompt (anti-hallucination contract: "only cite IDs in `allowed_citations`; only name changesets in `allowed_candidates`; if evidence is insufficient, return `abstain`") and the user message wrapping the dossier JSON; volatile per-crash content last so the shared cached prefix (crash brief + candidate diffs) is reused.
  - `render_verdict(uuid, dossier) -> Verdict` — calls `llm_call(role="principal", output_schema=Verdict, stream=True)`, returns the parsed `Verdict`.
  - `validate_grounding(verdict, dossier) -> tuple[Verdict, list[str]]` — drops every `CausalLink` whose `citation_id ∉ dossier.allowed_citations` or whose `candidate_changeset ∉ dossier.allowed_candidates`, prunes `cited_artifacts` to the allowed set, and downgrades `label` to `abstain` (with `abstain_reason="all claims dropped by validator"`) if the chain becomes empty after pruning; returns the cleaned verdict and the list of dropped claim descriptions.
  - `produce_verdict(uuid) -> Verdict` — top-level entry: `DossierStore.load(uuid)` → guard on `config.get_agent_llm("principal")["enabled"]` and dossier readiness → `render_verdict` → `validate_grounding` → render `needinfo.txt` into `verdict.needinfo_summary` → `dossier.verdict = verdict; DossierStore.save(dossier)` → `UUID.set_verdict(uuid, verdict.label, verdict.confidence)` → `models.commit()`. Catches `anthropic.APIStatusError`/refusal (`stop_reason == "refusal"`) and logs+abstains rather than raising (the RQ job must not crash the chain).
- `crashclouseau/config.py` (MODIFY): add `get_agent_llm(role)` returning `_get_global()["agent"]["llm"][role]` with safe defaults (the config-block sub-plan owns the schema; this adds the reader if not already present). Mirrors the existing `_get_global()`-based getters in `config.py`.
- `crashclouseau/models.py` (MODIFY): add `UUID.set_verdict(...)` and `Score.get_for_uuid(uuid)`; optionally add a small `HGAuthor`/`Node` author-by-node helper for the needinfo draft (since no such accessor exists today).
- `templates/needinfo.txt` (NEW): jinja2 draft rendering `{{ author }}`, `{{ bugid }}`, `{{ confidence }}`, `{% for link in causal_chain %}` with each link's cited artifact and rationale.
- `config/global.json` (MODIFY): add a top-level `agent.llm.principal` block — `{"model": "claude-opus-4-8", "effort": "high", "max_tokens": 32000, "enabled": true, "confidence_threshold": 0.6, "use_fable_on_retry": false}`. (Current `global.json` has no `agent` key; this introduces it.)

## Interfaces
Inputs consumed (read-only from the dossier, fields owned by the dossier-model sub-plan):
- `dossier.crash_brief` — reason, crash address, moz_crash_reason, crashing-thread frames (file/function/line/node/inline/trust), phc_* fields, async_shutdown_timeout (the currently-discarded processed-crash fields surfaced by `inspector`).
- `dossier.candidates` — list of candidate changesets (each: node, bug, author, pushdate, touched functions, on-stack vs in-call-graph flag) with `dossier.allowed_candidates` = the set of legal changeset nodes.
- `dossier.candidate_diffs` — flat added/deleted/modified line numbers (from `crashclouseau.patch.parse`, which calls `parsepatch.patch.Patch.parse_changeset` in FLAT mode — line numbers only, no hunk text) plus any raw-diff snippets the seniors captured (via `libmozdata.hgmozilla.RawRevision`).
- `dossier.call_graph` — searchfox-cli `calls-from`/`calls-to`/`calls-between` edges and `define`/full-function bodies, each with a citation id.
- `dossier.data_flow` — the data-flow-tracer's arg-freed/arg-mutated findings, each cited.
- `dossier.skeptic` — pre-verification notes / already-refuted claims.
- `dossier.allowed_citations` — the authoritative set of citation ids the principal may reference; the grounding validator is keyed on this set.

Outputs produced:
- `dossier.verdict: Verdict` — the only dossier field this unit writes.
- `UUID.set_verdict(uuid, label, confidence)` — flag consumed by the reports/crashstack UI sub-plan to badge a strong-evidence report.
- Returned `Verdict` (verbatim) for any caller/test harness.

Depends on: LLM-abstraction unit (`llm_call`), dossier-model unit (`Dossier`/`DossierStore` + the `allowed_*` sets and pre-verified sub-fields), config-block unit (`agent.llm` schema). Feeds: orchestration unit (which invokes `produce_verdict(uuid)` as the final RQ step after the dossier is assembled — enqueued via the existing `worker.get_queue().enqueue_call(func=..., args=(...), result_ttl=0)` pattern, hooked from `update.py::put_report`/`analyze_one_report` by that unit), the web-UI unit (verdict badge + needinfo draft display), and the offline-eval unit (which may batch `build_principal_messages` outputs via the Batch API).

## Implementation steps
1. Confirm the dossier-model sub-plan's field names (`allowed_candidates`, `allowed_citations`, `crash_brief`, `candidates`, `candidate_diffs`, `call_graph`, `data_flow`, `skeptic`, `verdict`) and the `llm_call(role, system, messages, output_schema, max_tokens, stream)` signature; encode any mismatches as a thin local adapter rather than forking either contract.
2. Define `CausalLink` and `Verdict` Pydantic models; set `model_config = ConfigDict(extra="forbid")` so the emitted JSON schema carries `additionalProperties: false`; enforce the `confidence` 0–1 range with a `field_validator` (not JSON-schema `minimum`/`maximum`, which are unsupported by structured outputs); make `relation`/`label` `Literal` enums.
3. Write `build_principal_messages(dossier)`: system prompt states the grounding contract and the strong-evidence vs abstain decision rule (including the common "modified a function in the call graph but not on the crash stack" case); place the large reusable prefix (crash brief + candidate diffs + call graph) first and the volatile instruction/question last, so the LLM-abstraction unit's `cache_control` on the shared prefix hits (min 4096 tok on Opus 4.8; 2048 on Fable 5 — note the shared prefix must clear the larger 4096-token bar for Opus to cache).
4. Write `render_verdict`: call `llm_call(role="principal", system=..., messages=..., output_schema=Verdict, max_tokens=config_max_tokens, stream=True)` (stream because `max_tokens` ≈ 32000 > ~16000); rely on the abstraction's adaptive thinking + `effort`; return `response.parsed_output` as a `Verdict`. Do not set `thinking`/`effort`/`temperature` here — those live in `llm_call` (and `temperature` would 400 on Opus 4.8/Fable 5 anyway).
5. Write `validate_grounding`: iterate `causal_chain`, drop links failing the `allowed_citations`/`allowed_candidates` membership test, prune `cited_artifacts`, recompute `culprit_changeset` (must remain in `allowed_candidates` and be referenced by a surviving link else `None`), and force `abstain` when the chain is emptied or when `culprit_changeset is None`. Log each dropped claim.
6. Add `UUID.set_verdict` and `Score.get_for_uuid` to `models.py` following existing `set_analyzed(uuid, useless, commit=True)`/`get_scores` patterns (SQLAlchemy 2.0 style, `commit=True` default, `models.commit()`). For the needinfo author, add (or reuse) a join from `Node.hgauthor` to `HGAuthor` — `Node.get_bugid` returns only the bug id, so it cannot supply the author.
7. Add `templates/needinfo.txt`; render it in `produce_verdict` (via `jinja2.Environment(FileSystemLoader("templates"))`, same as `report_bug.py`) and store the result in `verdict.needinfo_summary` (display only — `report_bug.py` confirms needinfo cannot be prefilled in the `enter_bug` URL, so we surface the draft + author rather than posting).
8. Write `produce_verdict`: load dossier, honor `enabled` and `confidence_threshold` (a strong verdict below threshold is downgraded to abstain), call render→validate→render-template→save→flag, wrap LLM/refusal/`APIStatusError` exceptions into an abstain verdict so the RQ chain never crashes.
9. Add the `agent.llm.principal` block to `config/global.json` (introducing the `agent` key); add `get_agent_llm` to `config.py` if the config sub-plan hasn't landed it yet.
10. Add `anthropic>=0.69` and `pydantic>=2.7` to `requirements.txt` (coordinate to avoid a duplicate line with the LLM-abstraction unit).
11. Tests: a fixture dossier with known `allowed_*` sets; assert (a) a verdict citing an out-of-set id is dropped and downgraded; (b) an all-valid chain survives with `label=strong_evidence`; (c) confidence below threshold downgrades to abstain; (d) a simulated `refusal`/`APIStatusError` yields an abstain verdict, not an exception. Mock `llm_call` so tests make no network call.

## Risks & open questions
- Dossier field-name/contract drift: the `allowed_candidates`/`allowed_citations` sets are the linchpin of the validator; if the dossier sub-plan names them differently or doesn't expose them, the anti-hallucination guarantee is unenforceable. Must be pinned in step 1.
- Citation id scheme: the validator assumes citation ids are stable, dossier-assigned strings. If citations are structured objects instead, the membership test and `cited_artifacts` need adapting.
- needinfo author resolution: there is no `author-by-node` accessor in `models.py` today (`Node.get_bugid` returns only the bug id; the author is an `hgauthor` FK to `HGAuthor`, which has no by-id getter). Either add a small join helper or have the dossier carry the author in `dossier.candidates`; pin which source the template uses.
- Fable 5 refusal handling: if `use_fable_on_retry` is enabled, recover via the abstraction's server-side `fallbacks` to `claude-opus-4-8` (beta header `server-side-fallback-2026-06-01`; opt-in, not automatic) — but whether `llm_call` exposes fallbacks is owned by the LLM-abstraction unit; until then, a Fable refusal (`stop_reason == "refusal"`, checked before reading `content`) should map to abstain. Fable 5 also requires 30-day data retention (ZDR orgs 400) and rejects an explicit `thinking: {type: "disabled"}` — both are `llm_call` concerns.
- Cost/latency: a 1M-context Opus 4.8 call per scored nightly crash at $5/$25 per 1M tok is the dominant cost; the `enabled` gate + scorer SEED demotion (other unit) must keep principal calls rare. Live calls must not use the Batch API (latency); offline eval may.
- Confidence calibration: `confidence_threshold` (0.6) is a guess; needs tuning against the offline historical corpus before trusting the strong-evidence badge.
- Should a downgraded-to-abstain verdict still persist the dropped chain for debugging? Proposed: keep dropped claims in the log + an optional `dossier.verdict.dropped` audit field, not in the user-facing needinfo draft.

## Acceptance criteria
- `produce_verdict(uuid)` returns a `Verdict` whose every surviving `CausalLink.citation_id` ∈ `dossier.allowed_citations` and `candidate_changeset` ∈ `dossier.allowed_candidates` — verified by the out-of-set-drop test.
- Given a dossier with no sufficient evidence (or after all claims are dropped), the verdict is `label="abstain"` with a populated `abstain_reason` and `culprit_changeset=None`.
- A strong verdict below `confidence_threshold` is emitted as `abstain`.
- The structured-output call uses `messages.parse`/`output_config.format` via `llm_call` with `additionalProperties:false` schema (no `minimum`/`maximum`/`minLength`), and streams (because `max_tokens` > ~16000); confirmed by inspecting the call args in the mocked test.
- A simulated Claude `refusal` (HTTP 200, `stop_reason == "refusal"`) and a simulated `anthropic.APIStatusError` each yield an abstain verdict and a logged `_request_id`, with no exception propagating to the RQ job.
- `dossier.verdict` is persisted via `DossierStore.save`, and `UUID.set_verdict` flips the report's UI flag; reloading the dossier returns the same verdict.
- `templates/needinfo.txt` renders the author (resolved via the `HGAuthor` join / dossier candidate, not `Node.get_bugid`), bug id, confidence, and one line per surviving cited causal link; no Bugzilla write occurs.
- `anthropic` and `pydantic` appear exactly once in `requirements.txt`; `config/global.json` has a valid `agent.llm.principal` block; no live network call is made in the test suite.
