# 10 — Principal: Claude verdict + abstain

## Objective
Own the **principal seat** of the crash-triage agent: the top-level `ClaudeSDKClient` session that
consumes the seniors' pre-verified evidence and renders either STRONG EVIDENCE (a citation-backed
causal chain from a candidate patch to the crash, a confidence value, and a needinfo draft) or a
calibrated ABSTAIN. Concretely, this unit contributes the two principal-specific pieces of the #02
`run_crash_triage` loop: **(a)** the principal `ClaudeAgentOptions` assembly — the principal system
prompt (the anti-hallucination contract + the strong-evidence-vs-abstain decision rule) and the
delegation allowlist of subagent roles it may spawn via the built-in `Task` tool — and **(b)** a
post-hoc **grounding validator** applied to the parsed `CrashTriageResult`/`Verdict` (reusing #03's
`parse_and_validate`/`validate_dossier`) that drops any citation the principal did not inherit from
the dossier and ABSTAINS when a load-bearing claim loses its citation, so the principal can never
invent artifacts. This unit **loads and persists nothing** — persistence is #11's.

> **Substrate (adopt hackbot, see #02):** unlike the seniors, the principal is **not** a
> `Task`-spawned `AgentDefinition` subagent — it **is** the #02 principal loop itself: the top-level
> `ClaudeSDKClient` drive over a `ClaudeAgentOptions` assembled with `model="opus"` (Fable 5 optional)
> per Phase-0/#02 tiering and options-level `effort`. It *owns* the `Task` tool in `allowed_tools`
> that spawns the seniors and folds their trailing ` ```json ` hand-offs back automatically. There is
> **no** `llm_call("principal",...)` / raw `anthropic` / `messages.parse` / `output_schema` and **no**
> manual `cache_control` — prompt caching is handled by the Agent SDK / bundled Claude Code CLI. The
> verdict is the terminal `ResultMessage`'s trailing ` ```json ` block, parsed best-effort by #02 into
> `CrashTriageResult(HackbotAgentResult)` and validated against #03's `Verdict`/`Dossier` models; this
> unit's post-hoc grounding validator (drop out-of-set citations, abstain if a load-bearing claim
> empties or confidence is below the floor) still applies to that parsed hand-off. Needinfo/comment
> drafts are **recorded** via the `actions` MCP server (`actions_server_for`, e.g.
> `bugzilla.update_bug`/`bugzilla.add_comment`), never posted here; the human-confirm/replay UI is #12.

## Scope
In scope:
- The **principal system prompt** (`prompts/system.md`): the anti-hallucination contract, the
  strong-evidence-vs-abstain decision rule (incl. the "modified a function in the call graph but not
  on the crash stack" off-stack case), the intended delegation flow, and the single-final-` ```json `
  `Verdict` instruction (with a needinfo draft on strong evidence).
- The **principal `ClaudeAgentOptions` assembly contribution** (`crashclouseau/agent/principal.py`):
  the delegation allowlist (`PRINCIPAL_DELEGATES` — which senior roles the principal may `Task`) and a
  `principal_options(...)` helper that resolves `model`/`effort`/`max_turns` from
  `agent.llm.principal` and returns the principal-specific option kwargs #02's `run_crash_triage`
  folds into `ClaudeAgentOptions`.
- The **post-hoc grounding validator** (`validate_grounding`) applied to the parsed
  `CrashTriageResult`/`Verdict`: reuses #03's `parse_and_validate`/`validate_dossier`, drops every
  citation not already present in the dossier's senior-evidence set, and downgrades the verdict to
  `abstain` when a load-bearing claim empties or confidence is below the configured floor.

Out of scope (owned by other sub-plans):
- The agent substrate — `ClaudeSDKClient`/`ClaudeAgentOptions` machinery, the `AgentDefinition`
  factory (`roles.py`), the trailing-` ```json ` extraction, `CrashTriageResult` construction, per-role
  tiering, in-process MCP tool wiring, and `get_llm()`/`get_llm_role()` — owned by **#02**. This unit
  only supplies the principal prompt, the delegation allowlist, `principal_options(...)`, and
  `validate_grounding`, which #02's `run_crash_triage` wires in.
- The `Dossier`/`Verdict`/`Citation` Pydantic models, `parse_and_validate`, `validate_dossier`, and
  the confidence floor `agent.abstain_below_confidence` — owned by **#03**. This unit imports and
  reuses them; it defines **no** verdict/citation model of its own.
- Building the dossier (crash brief, candidate diffs, call-graph exploration, data-flow trace,
  skeptic pre-verification) — owned by the senior/skeptic/data-flow units (#05–#09). This unit
  consumes their output as read-only evidence for the grounding set.
- searchfox-cli invocation — this unit never shells out; it relies on citations the dossier already
  captured (#06 wraps searchfox as an in-process MCP tool inside #02).
- The RQ enqueue hook, `build_seed`, the `asyncio.run(run_crash_triage(...))` invocation, the cost
  cap, and **all** `Dossier`/`Verdict` persistence (`Dossier.upsert`/`set_status`, `Verdict.set`, the
  `dossiers.status` lifecycle) — owned by **#11** (writes) / **#04** (schema). This unit persists
  nothing.
- Heuristic scorer demotion to a seed — owned by the scoring sub-plan; the seed `Score` reads live in
  #11's `build_seed`, not here.
- Bugzilla filing / needinfo UI + replay — owned by **#12**; the needinfo draft is an LLM-emitted
  `Verdict.needinfo_draft` field recorded via the `actions` server, not rendered or posted here.

## Externalities

| Name | Kind | Version / Endpoint / Command | Status | Purpose |
|------|------|------------------------------|--------|---------|
| `claude-agent-sdk` | python-lib | `claude-agent-sdk>=0.2` (owned by #02; NOT a new dep here) | existing (via #02) | The principal seat IS the #02 `ClaudeSDKClient` drive over `ClaudeAgentOptions`; this unit supplies the principal system prompt, the `Task` delegation allowlist, and the grounding validator — it never constructs the client and adds no raw `anthropic` dependency. |
| `pydantic` | python-lib | `pydantic>=2` (owned by #01/#03; NOT a new dep here) | existing (via #01/#03) | Type of the `Dossier`/`Verdict`/`Citation` models this unit validates against; imported for type hints + `validate_dossier`. No new `requirements.txt` line. |
| `crashclouseau/agent/schema.py` | internal-module (#03) | `Dossier`, `Verdict` (`decision`/`confidence`/`mechanism`/`consistency`/`needinfo_draft`/`abstain_reason`), `Citation`, `Decision`, `validate_dossier`, `parse_and_validate` | NEW (#03) | The models the principal's hand-off is validated against; `validate_grounding` reuses `parse_and_validate`/`validate_dossier`. This unit imports — never defines — these models. |
| `crashclouseau/agent/triage.py` | internal-module (#02) | `run_crash_triage(*, crash, tools_cfg, llm_cfg, recorder=None, extra=None) -> CrashTriageResult` | NEW (#02) | The principal loop. `run_crash_triage` assembles `ClaudeAgentOptions` from this unit's `principal_options(...)` + prompt, drives `ClaudeSDKClient`, and applies this unit's `validate_grounding` to the parsed result before returning. |
| `crashclouseau/agent/result.py` | internal-module (#02) | `CrashTriageResult(HackbotAgentResult)` — the parsed `Verdict`/dossier fields + `num_turns`/`total_cost_usd` | NEW (#02) | The typed hand-off `validate_grounding` grounds and returns; #11 persists it. |
| `crashclouseau/agent/roles.py` | internal-module (#02) | per-role `AgentDefinition` factory (`crash-interpreter`/`call-graph-explorer`/`patch-scout`/`data-flow-tracer`/`skeptic`) | NEW (#02) | The subagents the principal may spawn via `Task`; `PRINCIPAL_DELEGATES` selects which of these `run_crash_triage` registers under `agents=`. |
| Claude Opus 4.8 | llm-model | model id `claude-opus-4-8`; $5 / $25 per 1M tok (in/out); 1M ctx; options-level `effort` `low\|medium\|high\|xhigh\|max`; adaptive thinking; min cacheable prefix 4096 tok | existing (NEW use) | **Default** principal model (`agent.llm.principal.model="opus"`); adaptive thinking + options-level `effort`. |
| Claude Fable 5 | llm-model | model id `claude-fable-5`; $10 / $50 per 1M tok; 1M ctx; thinking always-on (omit `thinking`; explicit `disabled` 400s); `effort` supported; requires 30-day data retention (ZDR orgs 400) | existing (NEW use) | Optional hardest-case principal (opt-in via config); a `refusal` stop reason maps to abstain. Tier selection/fallback owned by #02. |
| `crashclouseau/config.py` | internal-module | `get_llm()` / `get_llm_role("principal")` (#02) → `agent.llm.principal`; `get_abstain_below_confidence()` (#03) | existing (read-only) | Resolve `model`/`effort`/`max_turns` for the principal and the strong-evidence confidence floor. This unit adds **no** config getter. |
| `config/global.json` `agent.llm.principal` | config | `{"model":"opus","effort":"high","max_turns":40}` (block owned by #02) | existing (read-only) | Principal tier. This unit reads it; it does **not** add `max_tokens`/`stream`/`enabled`/`confidence_threshold`. |
| `prompts/system.md` | config | principal system prompt content authored by this unit; loaded/rendered by #02's `run_crash_triage` (`str.format`, literal braces doubled) | NEW (authored here) | The anti-hallucination + strong-evidence-vs-abstain contract + delegation flow. Single source of the principal prompt (coordinate with #02 step 7 — #02 provides the loader, this unit provides the content). |
| actions MCP server | internal-module (#02/#12) | `actions_server_for(recorder, types=[...])`; `bugzilla.update_bug`/`bugzilla.add_comment` recordable actions | NEW (#02/#12) | The principal *records* (never posts) the needinfo/comment draft; #02 wires these tool ids into `allowed_tools` when a `recorder` is present. The apply/replay UI is #12. |
| `crashclouseau/logger` | internal-module | `logger.py::logger` (`logging.getLogger()`, level INFO) | existing | Structured logging of the verdict decision, confidence, dropped-out-of-set-citation count, and abstain reason. |
| searchfox-cli | CLI | `calls-from` / `calls-to` / `calls-between` / `define` — NOT invoked by this unit | existing (out of scope) | Call-graph evidence already captured (with citations) in the dossier by #06; the principal never shells out. LIMITS (misses virtual/indirect/fn-ptr/template/macro & cross-language edges; indexes ~tip not the crash build node) apply at dossier-build time, not here. |
| Socorro ProcessedCrash / lando git2hg | REST-API | via `inspector` / `LANDO_GIT2HG` — NOT called here | existing (out of scope) | Crash/hg data already folded into the dossier upstream (#11 seed + seniors); the principal does not re-fetch. |

## Deliverables
- `prompts/system.md` (NEW — authored here, loaded by #02):
  - The principal system prompt as a `str.format`-ready template (literal `{{ }}` doubled). States the
    **anti-hallucination contract**: "only cite artifacts already present in the dossier's evidence;
    only name candidate changesets the seniors surfaced; if the evidence is insufficient, return an
    `abstain` verdict — never invent a citation or a candidate."
  - States the **strong-evidence-vs-abstain decision rule**, including the common off-stack case
    ("modified a function that is in the call graph reaching a crashing frame but is not itself on the
    crash stack") and the requirement that a `strong-evidence` verdict carry a cited `mechanism` and
    `consistency` claim plus a needinfo draft addressed to the patch author.
  - Describes the **delegation flow** the principal drives via `Task`: `crash-interpreter` →
    `call-graph-explorer` (adds off-stack candidates) → `patch-scout` → `data-flow-tracer` →
    `skeptic`, then a single final ` ```json ` `Verdict` block.
- `crashclouseau/agent/principal.py` (NEW):
  - `PRINCIPAL_DELEGATES: list[str]` — the ordered allowlist of senior role names the principal may
    spawn via `Task` (`["crash-interpreter", "call-graph-explorer", "patch-scout",
    "data-flow-tracer", "skeptic"]`), matching the `AgentDefinition`s #02 registers in `roles.py`.
  - `build_principal_prompt() -> str` — loads `prompts/system.md` and returns the rendered principal
    system prompt string (the value #02 passes as `ClaudeAgentOptions(system_prompt=...)`).
  - `principal_options(*, llm_cfg, agents, mcp_tool_ids, recorder=None) -> dict` — returns the
    principal-specific `ClaudeAgentOptions` kwargs #02's `run_crash_triage` folds in:
    `system_prompt=build_principal_prompt()`, `agents={r: agents[r] for r in PRINCIPAL_DELEGATES}`,
    `allowed_tools=[*mcp_tool_ids, "Task", *(<actions tool ids> if recorder else [])]`,
    `model=llm_cfg["principal"].get("model", "opus")`, `max_turns=llm_cfg["principal"]["max_turns"]`,
    and options-level `effort` only when `llm_cfg["principal"].get("effort")` is set. It sets **no**
    `max_tokens`/`stream`/`thinking`/`temperature` (those either live at the SDK/options level or would
    400 on Opus 4.8 / Fable 5) and **no** manual `cache_control` (caching is the SDK's job).
  - `validate_grounding(result: CrashTriageResult, dossier: Dossier) -> CrashTriageResult` — the
    post-hoc grounding gate applied to the parsed hand-off: builds the **allowed-citation set** (every
    `Citation` attached to the dossier's `CallEdge`/`DiffHunk`/`DataFlowHypothesis`/`SkepticResult`/
    stack frames) and the **allowed-candidate set** (the dossier's `Candidate.node`s); prunes any
    `Verdict.mechanism`/`consistency` citation not in the allowed set; re-runs #03's `validate_dossier`
    and, on a `ValidationError` (a load-bearing claim lost all its citations) **or** when
    `Verdict.confidence` is below `config.get_abstain_below_confidence()` **or** when the result is
    empty/all-`None` (missing final ` ```json ` block), downgrades `Verdict.decision` to
    `Decision.abstain` with a populated `abstain_reason` and clears `needinfo_draft`. Reuses #03's
    `parse_and_validate` as the abstain-safe entry when handed raw text. Logs the dropped-claim count
    and abstain reason. **Never loads, never persists, never raises** into the caller.
- (No `crashclouseau/models.py` change.) Verdict/score persistence and the seed `Score` reads are
  owned by #11/#04; this unit resolves the needinfo author from `Candidate.author` (#03), not from a
  `models.py` join.
- (No `crashclouseau/config.py` change.) The principal tier is read via #02's `get_llm()`/
  `get_llm_role("principal")`; the confidence floor via #03's `get_abstain_below_confidence()`.
- (No `config/global.json` change.) The `agent.llm.principal` block is owned by #02; this unit reads
  it. Keep the principal default `model="opus"`.
- (No `requirements.txt` change.) There is **no** `anthropic` dependency; `claude-agent-sdk` is owned
  by #02 and `pydantic` by #01/#03.

## Interfaces
Inputs consumed (read-only, fields owned by #03):
- The assembled `Dossier` (folded into `CrashTriageResult` by #02) — `crash` (`CrashBrief`),
  `candidate`/candidates (`Candidate`, incl. `node`/`author`/on-stack-vs-off-stack), `call_path`
  (`CallPath`/`CallEdge` with searchfox citations), `hunks` (`DiffHunk`/`DiffLineCitation`),
  `data_flow` (`DataFlowHypothesis`), `skeptic` (`SkepticResult`), and **every `Citation` attached** —
  the union of these citations is the allowed-citation set the grounding validator keys on; the union
  of `Candidate.node`s is the allowed-candidate set.
- The principal's emitted `Verdict` (the terminal ` ```json ` block, parsed by #02) —
  `decision`/`confidence`/`mechanism`/`consistency`/`needinfo_draft`/`abstain_reason`.
- Config: `agent.llm.principal` (`model`/`effort`/`max_turns`, via #02's `get_llm()`) and
  `agent.abstain_below_confidence` (the strong-evidence floor, via #03).

Outputs produced:
- `build_principal_prompt()` — the principal system prompt string; consumed by #02's
  `run_crash_triage` as `ClaudeAgentOptions(system_prompt=...)`.
- `principal_options(...)` — the principal-specific `ClaudeAgentOptions` kwargs (delegation allowlist,
  `Task` in `allowed_tools`, `model`/`effort`/`max_turns`); consumed by #02.
- `validate_grounding(result, dossier)` — the grounded `CrashTriageResult` (its `Verdict` pruned to
  the allowed set, downgraded to `abstain` when a load-bearing claim empties or confidence is below
  the floor). This is the object #11 persists; this unit writes nothing itself.

Dossier/verdict fields this unit reads/writes:
- *Reads:* the full dossier evidence corpus (for the allowed-citation/allowed-candidate sets) and the
  principal's emitted `Verdict`.
- *Writes:* **none to the DB.** It returns a grounded `CrashTriageResult` whose `Verdict` it may have
  pruned/downgraded; #11 persists that via the #04 DAO (`Verdict.set`, `dossiers.status`).

Depends on: **#03** (`Dossier`/`Verdict`/`Citation` models, `parse_and_validate`/`validate_dossier`,
`get_abstain_below_confidence`), **#02** (`run_crash_triage`, `CrashTriageResult`, `roles.py`
`AgentDefinition`s, `get_llm()`/`get_llm_role`), and the config `agent.llm.principal` block (#02).
Feeds: **#02** (`run_crash_triage` wires in `build_principal_prompt`/`principal_options`/
`validate_grounding`), **#11** (persists the grounded `CrashTriageResult`/`Verdict`), **#12** (renders
+ records the needinfo draft from `Verdict.needinfo_draft`), and the offline-eval harness **#13**
(reads the persisted verdict).

## Implementation steps
1. Confirm #03's field names (`Dossier`/`Candidate`/`CallEdge`/`DiffHunk`/`DataFlowHypothesis`/
   `SkepticResult`/`Verdict` + `Citation` shape, `parse_and_validate`/`validate_dossier`,
   `get_abstain_below_confidence`) and #02's `run_crash_triage`/`CrashTriageResult` signature +
   `get_llm()`/`get_llm_role("principal")`; encode any mismatch as a thin local adapter rather than
   forking either contract.
2. Author `prompts/system.md`: the principal system prompt — the anti-hallucination contract (only
   cite citations already in the dossier; only name candidates the seniors surfaced; insufficient
   evidence → `abstain`), the strong-evidence-vs-abstain decision rule (incl. the off-stack
   "modified a call-graph function not on the crash stack" case and the cited-`mechanism`+
   `consistency`+needinfo-draft requirement for `strong-evidence`), the delegation flow
   (`crash-interpreter` → `call-graph-explorer` → `patch-scout` → `data-flow-tracer` → `skeptic`),
   and the single-final-` ```json ` `Verdict` instruction. Double any literal `{{ }}` (it is
   `str.format`-ed by #02).
3. Implement `PRINCIPAL_DELEGATES` and `build_principal_prompt()` (loads `prompts/system.md`).
4. Implement `principal_options(...)`: assemble the principal-specific option kwargs from
   `agent.llm.principal` (default `model="opus"`, options-level `effort`, `max_turns`), the delegation
   allowlist subset of `agents`, and `allowed_tools` = the MCP tool ids + `"Task"` (+ the actions tool
   ids when a `recorder` is passed). Set **no** `max_tokens`/`stream`/`thinking`/`temperature` and no
   manual `cache_control`.
5. Implement `validate_grounding(result, dossier)`: build the allowed-citation set (union of all
   dossier `Citation`s) and allowed-candidate set (dossier `Candidate.node`s); prune out-of-set
   citations from `Verdict.mechanism`/`consistency` and recompute the referenced culprit; re-run #03's
   `validate_dossier`; downgrade `decision` to `Decision.abstain` (populate `abstain_reason`, clear
   `needinfo_draft`) when a load-bearing claim empties, when `confidence <
   get_abstain_below_confidence()`, or when the result is empty/all-`None` (missing final block). Log
   each drop. Never raise.
6. Coordinate the wiring into #02: `run_crash_triage` assembles `ClaudeAgentOptions` using
   `principal_options(...)` + `build_principal_prompt()`, drives `ClaudeSDKClient`, parses the trailing
   ` ```json ` via #03's `parse_and_validate` into `CrashTriageResult`, then applies
   `validate_grounding(result, dossier)` before returning. (This unit provides the callables; #02 owns
   the loop.)
7. Tests (`tests/test_principal.py`, SDK/`run_crash_triage` mocked — no network/CLI): a fixture
   dossier with a known citation/candidate set + a fixture `CrashTriageResult`/`Verdict`; assert
   (a) a verdict citing an out-of-set citation has that citation dropped and, if load-bearing,
   downgrades to `abstain`; (b) an all-in-set chain survives with `decision=strong-evidence`;
   (c) `confidence` below `get_abstain_below_confidence()` downgrades to `abstain`; (d) an empty/all-
   `None` `CrashTriageResult` (missing final block) yields an `abstain` verdict, never an exception;
   (e) `principal_options(...)` includes `"Task"` + the `PRINCIPAL_DELEGATES` role set + `model="opus"`
   / options-level `effort` / `max_turns` from `agent.llm.principal`, and sets **no** `max_tokens`/
   `stream`; (f) `principal.py` imports no DB/persistence symbol (grep the module).

## Risks & open questions
- **Allowed-set provenance:** the grounding guarantee depends on #03's `Dossier` making the seniors'
  full citation corpus enumerable (so out-of-dossier citations are detectable). If citations are not
  reachable as a set from the parsed dossier, the anti-invention guarantee is unenforceable — pin the
  citation-enumeration path in step 1.
- **Best-effort hand-off (see #02/#03):** the principal may omit or malform its final ` ```json `
  block, leaving `CrashTriageResult`'s typed fields `None`. `validate_grounding` (and #03's
  `parse_and_validate`) MUST abstain, never raise; ingestion isolation (try/except around the run) is
  #11's, but this unit must not be the thing that throws.
- **Prompt-file ownership overlap with #02 step 7:** both #02 and this unit reference
  `prompts/system.md`. Single source: this unit authors the principal prompt **content**; #02 provides
  the loader/renderer (`str.format`, doubled braces) and does not fork a second copy. Coordinate so
  exactly one `prompts/system.md` exists.
- **Fable 5 refusal/retention:** if the config opts the principal onto Fable 5 for hardest cases, a
  `refusal` (`stop_reason == "refusal"`) maps to `abstain`; Fable 5 has thinking always-on
  (`disabled` 400s) and requires ≥30-day data retention. Tier selection + any server-side fallback to
  `claude-opus-4-8` are owned by #02; this unit only keeps `model="opus"` as the default.
- **Confidence floor calibration:** `agent.abstain_below_confidence` (#03) is a guess; it needs tuning
  against the offline historical corpus (#13) before the strong-evidence badge is trusted.
- **Cost/latency:** one Opus 4.8 principal session per scored crash at $5/$25 per 1M tok is the
  dominant cost; #11's `agent.enabled` gate + `max_turns`/`effort` (from `agent.llm.principal`) + the
  per-crash cost cap must keep principal sessions rare — there is no Batch API to discount the
  multi-turn agent loop, so offline eval (#13) re-runs are full-price too.
- **Needinfo draft:** it is an LLM-emitted `Verdict.needinfo_draft` field (author taken from
  `Candidate.author`, #03), recorded via the `actions` MCP server and rendered behind the human-confirm
  UI in #12 — this unit renders no template and resolves no author via a `models.py` join.
- **Downgraded-to-abstain audit:** should a downgraded verdict retain its dropped claims for
  debugging? Proposal: keep dropped claims in the log (+ an optional audit field on the payload #11
  persists), not in the user-facing needinfo draft.

## Acceptance criteria
- `validate_grounding(result, dossier)` returns a `CrashTriageResult` whose surviving
  `Verdict.mechanism`/`consistency` citations are all present in the dossier's senior-evidence citation
  set and whose referenced culprit is a dossier `Candidate.node` — verified by the out-of-set-drop
  test.
- Given a dossier with no sufficient/in-set evidence (or after pruning empties a load-bearing claim),
  the verdict is `decision=abstain` with a populated `abstain_reason` and no `needinfo_draft`.
- A `strong-evidence` verdict with `confidence` below `config.get_abstain_below_confidence()` is
  emitted as `abstain`.
- `principal_options(...)` assembles `ClaudeAgentOptions` kwargs with `model="opus"` (default),
  options-level `effort`, `max_turns` from `agent.llm.principal`, and `allowed_tools` including
  `"Task"` and the `PRINCIPAL_DELEGATES` role set — and sets **no** `max_tokens`/`stream`/`thinking`/
  `temperature` and no manual `cache_control`; confirmed by inspecting the kwargs in the mocked test.
- An empty/all-`None` `CrashTriageResult` (missing/malformed final ` ```json ` block) yields an
  abstaining verdict, not an exception.
- This unit loads and persists nothing: `principal.py` imports no `DossierStore`/`UUID.set_verdict`/
  `Score.get_for_uuid`/`models.commit`/DB symbol (verified by inspection/test); persistence is #11's
  via the #04 `Verdict`/`Dossier` DAO on `dossiers.status`.
- There is no `anthropic` import or `requirements.txt` line introduced by this unit, and no manual
  `cache_control` anywhere in it.
- `prompts/system.md` renders (via #02's `str.format`, literal braces doubled) into a principal system
  prompt string; `flake8` is clean on the new files.

Relevant paths: `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/agent/principal.py` (NEW), `/home/calixte/dev/mozilla/crash-clouseau/prompts/system.md` (NEW, content authored here / loaded by #02), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/agent/schema.py` (#03, imported), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/agent/triage.py` + `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/agent/result.py` (#02, imported), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/config.py` (read-only), `/home/calixte/dev/mozilla/crash-clouseau/config/global.json` (read-only `agent.llm.principal`), `/home/calixte/dev/mozilla/crash-clouseau/tests/test_principal.py` (NEW).
