# 06 — Senior #2: Call-graph Explorer

## Objective
Build the agent loop that, starting from each function on a crash's stack, drives the `searchfox-cli` adapter (`--calls-from` / `--calls-to` / `--calls-between` + `--define`) to construct a cited *neighborhood map* of callees, callers, and the values passed between frames — explicitly reaching off-stack functions that no longer appear on the stack but sit in the call graph. It applies bridge heuristics to recover the call edges searchfox misses (virtual dispatch, function pointers, IPC, cross-language FFI/WebIDL) and emits a typed, searchfox-cited `Neighborhood` dossier fragment that stays honest about revision drift and known call-graph holes. This is the productionized version of the Phase-0 spike (PLAN.md §9), which proved the premise that off-stack regressors are reachable.

## Scope
**In scope**
- A thin Python subprocess adapter around `searchfox-cli` that captures stdout markdown *and* the verifiable citation for every result. NOTE: searchfox-cli does **not** document emitting a searchfox permalink or an indexed revision; its `--define` output gives file path + line numbers (definition line marked `>>>`) and call-graph commands emit **mangled symbol ids**. The adapter therefore *constructs* a citation from `(repo, path, line, mangled-symbol, exact argv)` rather than parsing a permalink out of the markdown. A searchfox permalink can be synthesized from `repo` + `path` + `line` if desired, but this is adapter-built, not CLI-provided, and must be pinned to real fixtures.
- The Call-graph Explorer senior: an LLM-driven loop (Haiku 4.5 via the shared `llm_call(role,...)`) that, per stack frame function, decides which `searchfox-cli` queries to run, prunes the frontier, pulls bodies with `--define`, and assembles a `Neighborhood` of `CallEdge`s plus the off-stack candidate function set.
- Bridge heuristics for virtual/indirect/IPC/cross-language gaps, each emitting a typed `bridge_kind` + the search query that justified it (so the Skeptic can re-verify).
- Honest annotation of revision drift (crash build node vs. searchfox-indexed ~tip) and explicit `holes` entries where exploration hit a known blind spot.
- Pydantic schema for the `Neighborhood` fragment and validation that every `CallEdge` carries a citation.

**Out of scope (owned elsewhere)**
- The `searchfox-cli` binary itself (external dep, github.com/padenot/searchfox-cli) and its provisioning in the Docker image — flagged here, owned by the deployment/Dockerfile sub-plan.
- The `llm_call(role, ...)` abstraction, model-id config plumbing, prompt caching, Pydantic-structured-output wiring — owned by the **LLM-abstraction** sub-plan; this unit only *calls* `llm_call("call-graph-explorer", ...)`.
- The crash brief (failure class, crashing thread, inlines) — produced by **Senior #1 Crash Interpreter**; consumed here.
- Intersecting the neighborhood with the recent-patch window and fetching diffs — owned by **Senior #3 Patch Scout**.
- Reading function bodies to decide free/mutate/null (data flow) — owned by **Senior #4 Data-flow Tracer**; this unit supplies edges + bodies, not data-flow conclusions.
- Re-verifying each edge — owned by **Senior #5 Skeptic**.
- The top-level `Dossier` Pydantic model, the orchestrator job, and the new persistence table — owned by the **dossier-contract / worker-orchestration** sub-plan; this unit defines only the `Neighborhood` fragment that nests into it.
- DB seed reads (`CrashStack.get_by_uuid`, `Score`) — done by the orchestrator and handed in; this unit does not query Postgres.

## Externalities

| Name | Kind | Version / Endpoint / Command | Status | Purpose |
|---|---|---|---|---|
| `searchfox-cli` | CLI | **Flag-based** invocation (no subcommands): `searchfox-cli --calls-from <SYMBOL> --depth N`, `--calls-to <SYMBOL> --depth N`, `--calls-between <SOURCE,TARGET>` (single comma-separated arg), `--define <SYMBOL>` (full-function source, line numbers, definition line marked `>>>`), `-q/--query` + `-r/--regexp` (+ `-p/--path`) for search/bridge heuristics; repo via `-R/--repo <REPO>` (default `mozilla-central`); language filters `--cpp`/`--js`/`--webidl`/`--c`; LLM-friendly markdown output; call-graph emits **mangled symbol ids** | NEW | Drives call-graph exploration and function-body extraction; emits LLM-friendly markdown plus mangled symbol ids + path/line used to build citations |
| `anthropic` | python-lib | `anthropic` (NEW in requirements.txt; pinned by LLM-abstraction sub-plan) | NEW | SDK used indirectly via `llm_call`; not imported directly here |
| `claude-haiku-4-5` | llm-model | id `claude-haiku-4-5`, $1 / $5 per 1M tok (input/output), 200K ctx, cheap "senior" tier | NEW | The reasoning engine for this senior; plain calls only — no `effort` (Haiku 4.5 errors on `effort`), no extended thinking |
| `pydantic` | python-lib | `pydantic` (NEW; pinned by LLM-abstraction sub-plan) | NEW | `Neighborhood`, `CallEdge`, `NeighborFunction`, `Hole` models; JSON-schema-strict for structured output (`additionalProperties:false`, no `minLength`/`maxLength`/`minimum`/`maximum`/recursive) |
| LLM-abstraction module | internal-module | `crashclouseau/agent/llm.py` → `llm_call(role, system, prompt, output_format=..., cache_prefix=...)` (structured output via `client.messages.parse(..., output_format=PydanticModel)` → `response.parsed_output`) | NEW | Model-per-role indirection; this unit calls it with `role="call-graph-explorer"`; prompt caching (`cache_control` ephemeral) on the shared crash-brief prefix is applied there (min cacheable prefix on Haiku 4.5 = 4096 tokens) |
| Crash Interpreter fragment | internal-module | `crashclouseau/agent/crash_interpreter.py` → `CrashBrief` Pydantic model | NEW | Input: failure class, crashing thread, normalized stack with inlines expanded; drives which frames to explore first |
| Dossier contract | internal-module | `crashclouseau/agent/dossier.py` → `Dossier`, nests `Neighborhood` | NEW | Output container; this unit produces the `neighborhood` field |
| `inspector.get_crash_data` | internal-module | `crashclouseau/inspector.py:get_crash_data(uuid)` (`:58`) | existing | Source of the processed crash (frame `file` uri, `function`, `line`, `module`); the build node for drift comparison comes from frame uris via `get_path_node` |
| `inspector.get_path_node` / `inspector.git2hg` | internal-module | `crashclouseau/inspector.py:get_path_node` (`:125`), `git2hg` (`:31`); constants `HG_PAT` (`:13`), `GIT_PAT` (`:17`), `LANDO_GIT2HG` (`:21`) | existing | Parse frame uris (hg: or git:) to `(path, hg-node)`; git hashes are converted to hg revs via lando; the per-frame `node` is the build revision used to flag drift vs. searchfox tip |
| Lando git2hg | REST-API | `https://lando.moz.tools/api/git2hg/firefox/{git_hash}` (via `inspector.git2hg`, cached; returns `{"hg_hash": ...}`, 404 = non-Firefox/vendored source) | existing | Maps the build's git node ↔ hg rev so drift can be expressed in the units the rest of the pipeline uses; not called directly here, reused through `inspector.get_path_node` |
| `CrashStack` rows (handed in) | data-source | `crashclouseau/models.py:1176`; columns `uuidid`, `java`, `stackpos`, `original`, `module`, `filename`, `function`, `line`, `node`, `internal`; read via `CrashStack.get_by_uuid(uuid)` (`:1264`) by the orchestrator | existing | The stack-frame functions that seed exploration; `node` is the per-frame build rev for drift; `internal=True` marks Firefox-owned frames worth exploring. NOTE: `get_by_uuid` returns dicts keyed `stackpos`/`filename`/`function`/`line`/`node`/`original`/`internal`/`url`/`changesets` (no `module` in the returned dict — `module` is a column on the model, not in the `get_by_uuid` projection) |
| `Node` (handed in) | data-source | `crashclouseau/models.py:127`; columns `id`, `channel`, `node`, `pushdate`, `backedout`, `merge`, `bug`, `hgauthor` | existing | Channel → searchfox `--repo` selection (nightly→`mozilla-central`); `pushdate` for drift magnitude reporting |
| `logger` | internal-module | `crashclouseau/logger.py:logger` | existing | Structured logging of queries, holes, subprocess failures |
| agent config block | config | `./config/global.json` new `"agent"` key: `searchfox_cli` (binary path), `searchfox_repo` per channel, `max_explore_depth`, `max_frames_explored`, `max_searchfox_calls_per_crash`, `subprocess_timeout_s`, `define_max_bodies` | NEW | Tunable exploration budget + binary location; read via new `config.get_agent()` accessor |
| `config.get_agent` | internal-module | `crashclouseau/config.py` new accessor returning `_get_global()["agent"]` | NEW | Exposes the agent block (mirrors existing `get_ndays`/`get_max_score` accessors) |
| RQ worker | internal-module | `crashclouseau/worker.py` queues `["high","default","low"]`; `get_queue(name="low")` returns a `Queue`; enqueue via `queue.enqueue_call(func=..., args=(...), result_ttl=0)`. Explorer runs inside the agent job enqueued from `update.py:put_report` (`:54`) / `analyze_one_report` (`:101`) | existing | The explorer runs synchronously inside the agent RQ job; this unit adds no new queue |

## Deliverables

- **`crashclouseau/agent/__init__.py`** (NEW) — package marker for the agent subsystem.
- **`crashclouseau/agent/searchfox.py`** (NEW) — `searchfox-cli` adapter.
  - `class SearchfoxResult` (dataclass): `markdown: str`, `symbol_id: str | None` (mangled symbol from CLI output), `path: str | None`, `line: int | None`, `permalink: str | None` (adapter-constructed from repo/path/line, may be None), `revision: str | None` (adapter-supplied drift context, not CLI-provided), `command: list[str]`, `repo: str`.
  - `class SearchfoxAdapter`: `__init__(self, repo, binary=None, timeout=None)`; methods `calls_from(symbol, depth=1)`, `calls_to(symbol, depth=1)`, `calls_between(source, target)`, `define(symbol)`, `search(pattern, regex=False, path=None)` — each shells out via `subprocess.run([binary, "--calls-from", symbol, "--depth", str(depth), "--repo", repo, ...], capture_output=True, text=True, timeout=...)` (flag-based argv; `calls_between` passes a single `"<source>,<target>"` arg to `--calls-between`; `search` uses `-q`/`--query` plus `-r`/`--regexp`), returns `SearchfoxResult`, raises `SearchfoxError` on non-zero exit/timeout.
  - `class SearchfoxError(Exception)`; `parse_citation(markdown) -> (symbol_id, path, line)` (extracts the mangled symbol id + path + line from the CLI markdown so callers never assert un-cited; pinned to real fixtures).
  - `repo_for_channel(channel) -> str` (nightly→`mozilla-central`, etc., config-driven).
- **`crashclouseau/agent/neighborhood.py`** (NEW) — Pydantic schema + the explorer loop.
  - Pydantic models: `Citation` (`symbol_id`, `path`, `line`, `permalink`, `revision`, `command`), `CallEdge` (`caller`, `callee`, `direction` ∈ {`calls-from`,`calls-to`,`calls-between`}, `bridge_kind` ∈ {`direct`,`virtual`,`function-pointer`,`ipc`,`cross-language`,`macro`,`template`}, `citation`, `passed_args: list[str]`), `NeighborFunction` (`symbol`, `file`, `on_stack: bool`, `stack_pos: int | None`, `body_excerpt: str | None`, `citation`), `Hole` (`at_symbol`, `kind`, `note`, `attempted_query`), `Neighborhood` (`frames_explored: list[str]`, `edges: list[CallEdge]`, `functions: list[NeighborFunction]`, `off_stack_candidates: list[str]`, `holes: list[Hole]`, `revision_drift: RevisionDrift`). `RevisionDrift` (`build_node`, `indexed_repo`, `note`). All models `model_config = ConfigDict(extra="forbid")` (maps to `additionalProperties:false`); no recursive fields; no `minLength`/`maxLength`/numeric constraints (unsupported by strict structured output — SDK strips them client-side, but keep schemas clean for Haiku 4.5).
  - `class CallGraphExplorer`: `__init__(self, adapter, crash_brief, frames, channel)`; `explore() -> Neighborhood` — the agent loop.
  - `validate_neighborhood(n: Neighborhood) -> None` — rejects any `CallEdge`/`NeighborFunction` whose `citation` lacks the CLI-verifiable anchor (empty `symbol_id` AND empty `path`); the anti-hallucination gate from PLAN.md §6.2. (Permalink alone cannot be the gate, since it is adapter-synthesized and may be absent.)
  - `_bridge_virtual`, `_bridge_ipc`, `_bridge_cross_language` helpers (each runs a `search`/`calls-*` recovery query and records a `Hole` if it still fails).
- **`crashclouseau/agent/prompts.py`** (NEW or shared) — `CALL_GRAPH_EXPLORER_SYSTEM` and the per-iteration tool-decision prompt template (quote-only instruction; "the map may be incomplete — record a hole, never invent an edge").
- **`crashclouseau/config.py`** (MODIFY) — add `get_agent()` returning `_get_global()["agent"]`.
- **`config/global.json`** (MODIFY) — add the `"agent"` block (keys per Externalities).
- **`tests/test_agent_searchfox.py`** (NEW) — adapter tests with `searchfox-cli` mocked via `subprocess` (fixture markdown captured from real runs in the Phase-0 spike); assert the **flag-based** argv is built correctly.
- **`tests/test_agent_neighborhood.py`** (NEW) — explorer-loop tests with the adapter + `llm_call` stubbed: asserts off-stack candidate appears, citation-validation rejects un-cited edges, holes recorded on bridge failure.

## Interfaces

**Inputs consumed**
- `CrashBrief` (from Senior #1): failure class, crashing-thread index, normalized frames with inlines expanded — used to *order* exploration (start from frame 0 and frames flagged relevant to the failure mechanism).
- Stack frames handed in by the orchestrator from `CrashStack.get_by_uuid(uuid)` (`models.py:1264`): per returned-dict fields `function`, `filename`, `line`, `node` (build rev), `stackpos`, `internal`. Only `internal=True` frames are explored (non-Firefox frames have no searchfox symbol).
- `channel` + `Node` (for `--repo` selection and drift reporting).
- Agent config via `config.get_agent()` (budgets, binary path).

**Outputs produced**
- A validated `Neighborhood` Pydantic object written into the `Dossier.neighborhood` field. Specifically it *writes*: `edges`, `functions`, `off_stack_candidates`, `holes`, `revision_drift`, `frames_explored`. Every edge/function carries a `Citation` anchored to the CLI-verifiable `symbol_id` + `path` (+ `line`); `permalink`/`revision` are best-effort adapter-supplied context.

**Depends on**
- LLM-abstraction sub-plan (`llm_call`, Pydantic structured-output, caching, model config).
- Crash Interpreter sub-plan (`CrashBrief`).
- Dossier-contract sub-plan (`Dossier` container, `Neighborhood` nesting point).
- Deployment sub-plan (`searchfox-cli` binary present in the worker image).

**Feeds**
- Patch Scout (Senior #3) reads `off_stack_candidates` + `functions[].file` to intersect with the recent-patch window — the off-stack expansion that PLAN.md §1.1 / §4 calls the whole point.
- Data-flow Tracer (Senior #4) reads `edges` + `functions[].body_excerpt` for path bodies.
- Skeptic (Senior #5) re-verifies every `CallEdge.citation` and each `Hole.attempted_query`.

## Implementation steps
1. Add the `"agent"` block to `config/global.json` and `config.get_agent()` in `config.py` (mirror `get_ndays`); include `searchfox_cli` path, per-channel `searchfox_repo`, and all budget keys.
2. Create `crashclouseau/agent/__init__.py`.
3. Implement `crashclouseau/agent/searchfox.py`: `SearchfoxAdapter` shelling to `searchfox-cli` with `subprocess.run`, building **flag-based** argv (`--calls-from`/`--calls-to`/`--calls-between`/`--define`/`-q`/`-r`/`-R`/`--depth`), `timeout` from config, `capture_output=True, text=True`; raise `SearchfoxError` on `CalledProcessError`/`TimeoutExpired`; log the exact argv via `logger`. Implement `parse_citation` to pull the mangled symbol id + path + line out of the markdown; optionally synthesize a searchfox permalink from `repo`+`path`+`line`; `repo_for_channel` from config.
4. Implement `calls_from`/`calls_to`/`calls_between`/`define`/`search`, each returning a `SearchfoxResult` (never raw strings — citation anchor is mandatory at this boundary). `calls_between(source, target)` joins into one `"source,target"` arg.
5. Define the Pydantic models in `neighborhood.py` with `extra="forbid"`; keep them flat (no recursion) so the JSON-schema-strict structured output is valid for Haiku 4.5; write `validate_neighborhood`.
6. Write the explorer loop `CallGraphExplorer.explore()`:
   a. Select frames to explore: `internal=True`, ordered by `CrashBrief` relevance then `stackpos`, capped at `max_frames_explored`.
   b. For each frame function: `define` it (capture body + citation), then `calls_from` (depth from config) to find callees that could produce-and-return bad state, and `calls_to` to find callers that may pass bad data; record each as a `CallEdge` with `bridge_kind="direct"` and its citation.
   c. Run `calls_between(frame_func_class, lower_frame_func_class)` for adjacent stack frames to confirm/expand the on-stack path and surface intermediate off-stack hops. NOTE: `--calls-between` operates on **class/namespace** scopes, not arbitrary function symbols — pass the enclosing class/namespace of each frame, and fall back gracefully (record a hole, not a crash) when a frame has no class scope.
   d. Use `llm_call("call-graph-explorer", ...)` with the structured `Neighborhood`-fragment `output_format` to decide which frontier symbols are worth expanding next (prune noise like container/`nsTArray` helpers per PLAN.md §10) and to label `passed_args` from the bodies — quote-only, never invent.
   e. Enforce `max_searchfox_calls_per_crash`; stop expanding when budget hit and record the unexpanded frontier as informational (not a hole).
7. Bridge heuristics: when `calls_from`/`calls_to` returns nothing for a frame whose body shows a virtual call / IPC send / FFI boundary, run `_bridge_virtual` (search implementors of the interface symbol via `-q`/`--id`), `_bridge_ipc` (search the message name), `_bridge_cross_language` (search the WebIDL/XPIDL/`extern "C"` symbol, using `--webidl`/`--js` filters where helpful). On success add a `CallEdge` with the matching `bridge_kind` + citation; on failure append a `Hole`.
8. Populate `revision_drift`: `build_node` = frame `node` (hg rev from `inspector.get_path_node`), `indexed_repo` = the searchfox `--repo`, `note` explaining searchfox indexes ~tip, not the build node (PLAN.md §7). The indexed-revision value is **not** emitted by searchfox-cli, so `revision` on citations stays best-effort/None and drift is reported qualitatively.
9. Mark `on_stack`/`off_stack_candidates`: any explored callee/caller whose symbol is not among the input frame functions goes into `off_stack_candidates`.
10. Call `validate_neighborhood` before returning; on validation failure, drop the offending edge/function (do not raise) and log it — degrade gracefully, never block the dossier.
11. Tests: `tests/test_agent_searchfox.py` (subprocess mocked, asserts flag-based argv + citation parsing + `SearchfoxError` on timeout/non-zero); `tests/test_agent_neighborhood.py` (adapter + `llm_call` stubbed; asserts an off-stack callee surfaces in `off_stack_candidates`, un-cited edge is rejected, bridge failure records a `Hole`).
12. Run `flake8` (repo `.flake8`) and the test suite.

## Risks & open questions
- **searchfox-cli markdown format / citation extraction.** searchfox-cli does **not** document emitting a searchfox permalink or an indexed revision — it emits mangled symbol ids and (for `--define`) path + line numbers with the definition line marked `>>>`. `parse_citation` must therefore anchor on the mangled symbol + path + line, and any permalink is adapter-synthesized from `repo`+`path`+`line`. Mitigation: capture real fixtures during the Phase-0 spike and pin the parser to them; fail closed (no symbol_id and no path → edge dropped by `validate_neighborhood`).
- **Revision drift is real and unfixable by this unit.** searchfox indexes ~tip (now the git repo, github.com/mozilla-firefox/firefox), not the crash build node. A symbol may be absent/renamed at the indexed rev, and the CLI does not return the rev it indexed. We *report* drift qualitatively (`revision_drift`, `holes`) rather than resolve it; the principal abstains on stretched chains (PLAN.md §7).
- **Call-graph holes by design.** Virtual/function-pointer/IPC/template/macro/cross-language edges are invisible to `--calls-to`/`--calls-from`. `--calls-between` only covers class/namespace-scoped direct calls. Bridge heuristics are best-effort and can over-match (e.g. many implementors of a virtual method); the Skeptic must confirm and the abstain path absorbs the rest. Open question: do we cap bridge-implementor fan-out, and at what number?
- **Budget vs. recall.** `max_searchfox_calls_per_crash` / `max_explore_depth` trade off off-stack recall (PLAN.md §8 metric 1) against latency/cost. `--depth` defaults to 1 in the CLI. Initial values are guesses to be tuned in Phase-4 eval. Open question: depth default — start at 1 (callees/callers only) or 2?
- **Haiku variance.** A cheap model driving exploration may pick poor frontier symbols. Mitigation: quote-only prompting + structured output + the grounding gate; the model navigates, it does not assert (PLAN.md §6). Haiku 4.5 calls must be plain — no `effort`, no extended thinking.
- **Binary provisioning.** `searchfox-cli` must exist in the Heroku/Docker worker image; absence yields `SearchfoxError`. Owned by deployment, but this unit must surface a clear log + abstain-friendly empty `Neighborhood` rather than crash the job.
- **Open: should `--calls-between` use the on-stack adjacent pair's class scopes or also brief-implied target classes?** Leaning adjacent-pairs first, brief-driven targets as a stretch.

## Acceptance criteria
- `crashclouseau/agent/searchfox.py` and `crashclouseau/agent/neighborhood.py` exist; `from crashclouseau.agent.neighborhood import CallGraphExplorer, Neighborhood, validate_neighborhood` imports cleanly.
- `config.get_agent()` returns the new block and `config/global.json` parses (existing `_get_global()` JSON load succeeds).
- Given a recorded Phase-0 off-stack regression fixture (stack frames + crash brief + mocked `searchfox-cli` output), `CallGraphExplorer.explore()` returns a `Neighborhood` whose `off_stack_candidates` contains the known true off-stack regressor function (the §8 metric-1 sanity check at unit scale).
- Every `CallEdge` and `NeighborFunction` in the returned `Neighborhood` has a non-empty `citation` anchor (`symbol_id` and/or `path`+`line`); `validate_neighborhood` raises/drops on a synthetically un-cited edge.
- The adapter builds **flag-based** argv (`--calls-from`, `--define`, `--depth`, `-R`, comma-joined `--calls-between`) — verified by `test_agent_searchfox.py`.
- A frame with a simulated virtual/IPC/FFI call and empty `calls-from` produces either a bridge `CallEdge` (with the correct `bridge_kind`) or a recorded `Hole` with `attempted_query` — never a silently dropped frame.
- `revision_drift` is populated with the build node and the indexed repo on every run (`revision` may be None — the CLI does not provide it).
- `SearchfoxAdapter` raises `SearchfoxError` (not an uncaught exception) on non-zero exit and on `subprocess` timeout; a missing binary degrades to a logged error + empty `Neighborhood`, not a worker crash.
- `tests/test_agent_searchfox.py` and `tests/test_agent_neighborhood.py` pass; `flake8` is clean.

Relevant files: `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/agent/searchfox.py` (NEW), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/agent/neighborhood.py` (NEW), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/agent/prompts.py` (NEW), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/config.py` (MODIFY), `/home/calixte/dev/mozilla/crash-clouseau/config/global.json` (MODIFY), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/inspector.py` (reused: get_crash_data:58, get_path_node:125, git2hg:31), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/models.py` (CrashStack:1176, get_by_uuid:1264, Node:127 — read by orchestrator), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/worker.py` (get_queue, enqueue_call), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/update.py` (put_report:54, analyze_one_report:101), `/home/calixte/dev/mozilla/crash-clouseau/PLAN.md` (§2,§3,§7,§9).
