# Evidence-Agent Rebuild — Implementation Sub-Plans

This directory holds the 14 implementation sub-plans that decompose crash-clouseau's
evidence-agent rebuild (see `../PLAN.md`). The rebuild turns Clouseau from a
line-proximity scorer into a **tiered LLM engineering team**: a crew of cheap
"senior engineer" models (Haiku 4.5) drives `searchfox-cli` to explore the Firefox
call graph, read function bodies, gather candidate patches, and assemble a grounded
evidence **dossier** in which every claim cites a verifiable artifact (searchfox
permalink, diff hunk, faulting address); a strong "principal" (Opus 4.8, optionally
Fable 5) then performs the deep patch↔crash causal judgment or **abstains**. The
existing heuristic scorer is demoted from "the answer" to a cheap **candidate seed**.
The whole point is to surface the common off-stack culprit (a patch that modified a
function *in the call graph but not on the crash stack*) with evidence strong enough
to justify a developer needinfo.

> **Substrate decision (2026-07-02): adopt `mozilla/bugbug` "hackbot".** Clouseau does
> **not** build a bespoke `llm_call()` seam over the raw `anthropic` **Messages** SDK. It
> **vendors `libs/hackbot-runtime` (`hackbot_runtime`) + `libs/agent-tools` (`agent_tools`)**
> and builds on the **Claude Agent SDK** (`claude-agent-sdk>=0.2`): the **principal agent loop**
> (`ClaudeSDKClient` + `ClaudeAgentOptions`, assembled in Clouseau's own `run_crash_triage`
> coroutine), **`Task` subagents** (each role is an `AgentDefinition(model=…)` the SDK spawns and
> folds back), in-process **MCP tool servers** (`@tool`/`build_sdk_server`), and the
> **`ActionsRecorder`** — Bugzilla/needinfo actions are *recorded*, not executed. Model tiering is
> imperative Python (per-role `AgentDefinition(model=…)` + options-level `effort`), not a config
> file. Structured output is **best-effort** (parse the last trailing ` ```json ` block from the
> `ResultMessage`, then Pydantic-validate — there is no `messages.parse`/`output_format`). See
> `#02` for the seam and the verified API notes.

## Sub-plans

| File | Title | Objective | Effort | Key NEW externalities |
|---|---|---|---|---|
| `00-phase0-callgraph-spike.md` | Phase-0 Call-graph Explorer spike (go/no-go) | Prove searchfox-cli + corpus mining can reach off-stack culprits; decide go/no-go | M | searchfox-cli, libmozdata SuperSearch/ProcessedCrash, lando git2hg, Haiku 4.5, `--budget-queries` |
| `01-searchfox-cli-adapter.md` | searchfox-cli adapter (call-graph tool layer) | Python subprocess wrapper + markdown parsers for calls-from/-to/-between/define/get-file | M | searchfox-cli binary (Rust/`cargo install`), searchfox.org egress, repo/`-R` enum |
| `02-llm-abstraction-tiering.md` | Agent substrate: vendored hackbot + Claude Agent SDK seam & tiering | Vendor hackbot-runtime + agent-tools; drive the `claude-agent-sdk` principal loop + `Task` subagents; per-role model/effort tiering; usage accounting | M | `claude-agent-sdk>=0.2` (bundles Claude Code CLI), vendored `hackbot_runtime`+`agent_tools`, all 4 Claude models, `agent.llm` config, `tests/test_agent.py` |
| `03-dossier-schema-contract.md` | Dossier schema & strong-evidence contract | Pydantic dossier/verdict models + citation types + DB-JSON round-trip; validate the best-effort trailing-`json` handoff (no `output_format`) + abstain on uncited claims | M | `pydantic>=2`, `SearchfoxCitation`, JSONB envelope, every-claim-cites-an-artifact contract |
| `04-persistence-tables.md` | Persistence: dossier/verdict tables | New SQLAlchemy tables + migration for stored dossier + verdict | S | `dossier`/`verdict` tables, 2 PG enum types, `bin/migrate_dossier.py` |
| `05-crash-interpreter.md` | Senior #1: Crash Interpreter | Decode processed crash (reason/address/moz_crash/inlines/phc) into a `CrashBrief` | M | Haiku 4.5, libmozdata ProcessedCrash, `agent/schema.py` |
| `06-callgraph-explorer.md` | Senior #2: Call-graph Explorer | Drive searchfox-cli from frames to build a neighborhood map with citations | L | Sonnet 5 (Phase-0 navigator winner), searchfox-cli, lando git2hg, `parse_citation` fixtures |
| `07-patch-scout.md` | Senior #3: Patch Scout | Map neighborhood functions to candidate patches; tag/score them | M | Haiku 4.5, lando git2hg, `agent.patch_scout` config |
| `08-dataflow-tracer.md` | Senior #4: Data-flow Tracer | Read function bodies; argue arg freed/mutated/nulled along the path | L | Sonnet 5 / Opus 4.8, searchfox-cli `define`, libmozdata RawRevision |
| `09-skeptic-verifier.md` | Senior #5: Skeptic / verification pass | Re-verify every dossier edge/citation; flag drift/backout; prune unsupported | M | Haiku 4.5, searchfox-cli re-query, lando git2hg drift |
| `10-principal-verdict.md` | Principal: Claude verdict + abstain | Deep causal verdict over the distilled dossier; anti-hallucination allow-lists | M | Opus 4.8 (Fable 5 optional), `agent.llm.principal` config, options-level `effort` |
| `11-orchestration-worker.md` | Orchestration worker & seed seam | RQ job runs `asyncio.run(run_crash_triage(...))`; wire seed → dossier/verdict persistence + per-crash cost budget | M | `agent` RQ queue, `Dossier.set_status`/`Dossier.get_by_uuid` idempotency, subprocess-spawn (bundled CLI), Procfile change (ops) |
| `12-product-wiring-ui-needinfo.md` | Product wiring: UI evidence panel + needinfo apply | Render verdict/evidence panel; human-confirm + apply the recorded needinfo action (`bugzilla.update_bug`) | M | `agent.ui.*`/`agent.confidence.*` config, `ActionsRecorder` replay, `Verdict.get_by_uuid`, searchfox permalinks |
| `13-eval-harness.md` | Evaluation harness | Offline re-run over historical corpus; precision/abstain metrics | M | #02 `run_crash_triage` (bounded-concurrency re-run; no Batch API), libmozdata RawRevision, BMO `clouseau`-alias query |
| `14-patch-extraction.md` | Patch extraction upgrade (foundation) | Parse diff hunks + `@@` enclosing function + identifiers + cosmetic flag (no new deps); feeds `#07`/`#08` + the current scorer | M | parsepatch/RawRevision (existing), new `patch_file`/`patch_func` tables — **no new Python deps** |

Effort key: S ≈ ≤1 day, M ≈ 1–3 days, L ≈ 3–5 days.

## Build order & dependency graph

**Phase 0 is a hard go/no-go gate.** `#00` must run first; if searchfox-cli cannot
reach off-stack culprits at acceptable token cost, the rebuild does not proceed.

After the gate, four **foundations** are built (they share no dependencies on each
other and can proceed in parallel):

- `#01` searchfox-cli adapter — the tool layer every explorer/tracer/skeptic role drives.
- `#02` agent substrate — vendored hackbot + the `claude-agent-sdk` principal loop / `Task`
  subagents / MCP tool servers every model-calling role runs on (per-role model/effort tiering).
- `#03` dossier schema contract — the Pydantic types every role produces/consumes.
- `#14` patch extraction — hunk text + `@@` enclosing function + identifiers + cosmetic
  flag (no new deps); the single diff source for `#07`/`#08` and an independent
  improvement to the current scorer.

`#04` persistence depends on `#03` (it stores the dossier/verdict shapes). The five
**senior roles** (`#05`–`#09`) and the **principal** (`#10`) all sit on top of the
foundations — and `#07` (Patch Scout) and `#08` (Data-flow Tracer) additionally consume
`#14` (they no longer fetch/split diffs themselves). `#11` orchestration ties the roles into one job and connects them to
the seed seam and persistence. `#12` product wiring and `#13` eval are last (they
read persisted verdicts / replay the assembled pipeline).

```
                        #00 Phase-0 spike  (GO / NO-GO gate)
                                  |
        +-------------------------+-------------------------+
        |                         |                         |
   #01 searchfox          #02 llm abstraction        #03 dossier schema
     adapter                & tiering                  contract
        |                         |                         |
        |                         |                    #04 persistence
        |                         |                         |
        +-----------+-------------+----------+--------------+
                    |             |          |
        #14 patch-extraction (foundation: hunk text + @@ enclosing fn; no new deps)
        seniors (depend on #01, #02, #03):
          #05 crash-interpreter      -> #02, #03
          #06 call-graph-explorer    -> #01, #02, #03
          #07 patch-scout            -> #02, #03, #14 (consumes #06 neighborhood + #14 hunks)
          #08 data-flow-tracer       -> #01, #02, #03, #14 (consumes #06, #07, #14)
          #09 skeptic                -> #01, #02, #03 (re-verifies #06..#08)
        principal:
          #10 principal-verdict      -> #02, #03 (consumes #05..#09)
                    |
              #11 orchestration  -> #04, #05..#10  (+ seed seam in update.py)
                    |
        +-----------+-----------+
        |                       |
   #12 product wiring      #13 eval harness
   -> #04 (read verdict),  -> #02, #03, #04, full #11 pipeline
      #11 (live verdicts)
```

Critical path: `#00 → (#01 ∥ #02 ∥ #03) → #04 → #11 → #12/#13`, with the seniors and
principal landing in parallel between the foundations and orchestration.

## Consolidated externalities

The complete, de-duplicated list of everything new the rebuild introduces or reuses.

### NEW Python deps to add to `requirements.txt`
- `claude-agent-sdk` (`>=0.2`) — **the key new dep.** The Claude Agent SDK: the principal agent
  loop (`ClaudeSDKClient` + `ClaudeAgentOptions`), `Task` subagents (`AgentDefinition`), and
  in-process MCP tool servers. The manylinux_2_17_x86_64 wheel is **~74MB and bundles the Claude
  Code CLI** (no Node install needed for read-only triage); bugbug's `uv.lock` resolves **0.2.105**.
  Pin `>=0.2` (the API used — `receive_response`, `AgentDefinition`, options-level `effort`,
  `setting_sources=[]` — is 0.2.x despite bugbug's `>=0.1.30` pin) (#02).
- vendored `hackbot_runtime` + `agent_tools` — copied from `mozilla/bugbug`
  `libs/hackbot-runtime` + `libs/agent-tools` (not on PyPI as installable libs) under
  `crashclouseau/vendor/` (or path deps). Provide `HackbotContext`, `HackbotAgentResult`,
  `ActionsRecorder` + `actions_server_for`/`actions_to_tool_names` + the recordable bugzilla
  actions, `Reporter`, and the `@tool`/`build_sdk_server` registry (#02).
- `pydantic` (`>=2`) — dossier/verdict/citation models; validates the best-effort trailing-`json`
  handoff (there is **no** `messages.parse`/`output_format` in this path).
- `pydantic-settings` — hackbot's `HackbotContext`/`AgentInputs` are `BaseSettings` (transitive).
- `google-auth`, `requests` — hackbot-runtime deps (WIF/uploader), pulled in transitively and
  **inert on Heroku**.

Auth is the `ANTHROPIC_API_KEY` env var, read **by the bundled CLI directly** (never passed into
the SDK). No WIF on Heroku — leave `ANTHROPIC_FEDERATION_RULE_ID` unset; put the key in the dyno
config / `~/.mozdata.ini`, never the tracked repo file (#02).

(No raw `anthropic` **Messages** SDK — the agent loop is the bundled CLI driven via
`claude-agent-sdk`. `libmozdata>=0.2.12`, `parsepatch`, `requests`, `rq`, `redis`, `sqlalchemy`,
`flask*`, `jinja2` are already present and reused. **`#14` patch extraction adds no new deps** — it
parses diff hunks with the existing `parsepatch`/`RawRevision`/`requests`.)

### Claude models + tiers
Tiering is imperative, not a config file: per-role `AgentDefinition(model="haiku"|"sonnet"|"opus")`
+ options-level `effort` on the principal session (#02). A subagent left `model="inherit"` silently
follows the principal — set an explicit tier per role.
- `claude-haiku-4-5` — $1/$5 per 1M, 200K ctx — seniors: crash-interpreter (#05),
  patch-scout (#07), skeptic (#09). Plain tier (no `effort`).
- `claude-sonnet-5` — $3/$15 per 1M, 1M ctx — **call-graph-explorer (#06)** default
  (Phase-0 tier winner: off-stack recall 4/7 vs Haiku 1/7; persistence is the binding
  factor) and data-flow-tracer (#08) option.
- `claude-opus-4-8` — $5/$25 per 1M, 1M ctx — default principal (#10); also a
  data-flow-tracer (#08) option. Runs at options-level `effort` (`low..max`).
- `claude-fable-5` — $10/$50 per 1M, 1M ctx — optional principal for the hardest
  cases (requires the org to meet the 30-day data-retention policy, else 400s).
- Per-crash cost/turns come from the SDK `ResultMessage` (`total_cost_usd`/`num_turns`); the
  `agent.llm.max_cost_usd_per_crash` budget is enforced in #11.
- Prompt caching is handled by the Agent SDK / bundled CLI (not a manual `cache_control`). Offline
  eval re-runs (#13) drive `run_crash_triage` per case under bounded concurrency — the multi-turn
  agent loop has no Batch API, so re-runs are full-price (not a 50%-off async batch).

### New external CLIs / services
- **`searchfox-cli`** (github.com/padenot/searchfox-cli) — new external Rust CLI,
  invoked via subprocess; flag-based (`--calls-from`, `--calls-to`, `--calls-between`,
  `--define`, `--get-file`, `-R/--repo`, `-q`). Requires a Rust toolchain to
  `cargo install` at Heroku/Docker build time **and** outbound egress to
  searchfox.org at run time (deploy/buildpack ownership: #11 ops follow-up).
- **lando git2hg** (`https://lando.moz.tools/api/git2hg/firefox/{hash}`) — already
  used in `inspector.py`; reused to convert git hashes (searchfox / crash-stack
  frames) to hg nodes (#06, #07, #09, #12).
- **Bugzilla / BMO REST** (via `libmozdata.bugzilla`) — already used; reused by #13
  for the `clouseau`-alias historical corpus query.
- **Socorro SuperSearch / ProcessedCrash / RawRevision** (via `libmozdata`) —
  already used; reused by #00 (corpus mining), #05 (crash brief), #08/#13 (raw diff).

### Reused internal modules
- `crashclouseau/inspector.py` — `get_crash_data`, `inspect_stacktrace`,
  `get_path_node`, `git2hg` (#05, #06, #07, #08, #09, #12).
- `crashclouseau/worker.py` — RQ queues + `get_queue().enqueue_call(...)` (#11).
- `crashclouseau/update.py` — `put_report()` / `analyze_one_report` seed enqueue hook (#11).
- `crashclouseau/patch.py` + `parsepatch` — flat patch line numbers; raw hunk text
  via `libmozdata.hgmozilla.RawRevision` (#07, #08, #13).
- `crashclouseau/models.py` — `UUID`, `CrashStack`, `Changeset`, `Node`, `Score`,
  `HGAuthor` (seed, #04, #11, #12).
- `crashclouseau/config.py` — `./config/global.json` reader (all model-calling units).
- `crashclouseau/report_bug.py` + `templates/bug.txt` — needinfo/bug rendering. The agent only
  *records* the needinfo (`bugzilla.update_bug` via `ActionsRecorder`); #12 builds the human-confirm
  UI + replay that posts it via `libmozdata` (there is no hackbot apply step).
- `crashclouseau/html.py`, `templates/`, `static/clouseau.js` — UI panel (#12).
- New module namespace `crashclouseau/agent/` (`triage.py` = `run_crash_triage`, `roles.py` =
  per-role `AgentDefinition`s, `result.py` = `CrashTriageResult`, `tools/` = `@tool` MCP wrappers,
  plus the dossier `schema.py`) and vendored `crashclouseau/vendor/` (hackbot) —
  created by the foundations and roles.

### New config keys (in `config/global.json`, single `agent` block — must NOT diverge)
- `agent.llm` → `{principal:{model,effort?,max_turns?}, roles:{<role>:{model,effort?}}, pricing,
  max_cost_usd_per_crash}` for the principal + 5 seniors (#02, #05–#10). `model` values are the SDK
  short names (`haiku`/`sonnet`/`opus`); `effort` is options-level (principal session).
- `agent.patch_scout.{max_candidates, neighborhood_file_cap, diff_byte_cap, model, enabled}` (#07).
- `agent.{budget_queries, enabled, agent_version}` (#06, #11). There is no
  `per_role_timeout`: the in-loop bound is `max_turns` (owned by #02) and the whole-run bound
  is the RQ `job_timeout` (#11).
- `agent.ui.{show_abstain, high_confidence_label}` and
  `agent.confidence.needinfo_min` (#12).
- Readers: `get_agent()` / `get_llm()` / `get_llm_role(role)` / `get_patch_scout_cfg()` — names
  to be reconciled across #02, #07, #10, #12 so the nested `agent` shape is single-sourced.

### New DB tables
- `dossier` — one stored dossier JSON envelope per uuid (`schema_version` +
  verdict/confidence summary indexable keys), JSON/JSONB column (#03, #04).
- `verdict` — principal verdict (verdict label, `confidence`, culprit node/bug/author),
  read by `Verdict.get_by_uuid` (#04, #10, #12).
- 2 new PG enum types created by `bin/migrate_dossier.py` (verdict label, confidence)
  — no Alembic in repo, applied via `heroku run` one-off migration (#04).
- Run-state lives on the `dossier.status` column (`AGENT_STATUS_TYPE`, set via
  `Dossier.set_status`); orchestration idempotency is `Dossier.get_by_uuid` — there is no
  separate `UUID` agent-state column (#04, #11).
- `patch_file` / `patch_func` — the persisted *derived* patch index (enclosing functions,
  touched identifiers, cosmetic flag, churn, file metadata) keyed by `(changesetid, fileid)`,
  FK→`Changeset`/`File` `ON DELETE CASCADE` (rides the existing 30-day clean). Hunk *text*
  is not persisted — fetched lazily and cached per run (#14).

## Open risks

- **`@@` enclosing-function context unverified.** `#14`'s "function for free" win
  assumes hg `raw-rev` emits the function-context suffix on `@@` hunk headers (git-format
  / `showfunc` diffs do; plain hg diffs may not), and the suffix is an `xfuncname`
  heuristic — weaker for Rust/JS, occasionally an outer function. `#14` step 1 measures
  presence/accuracy per language on real changesets before the Patch Scout relies on it;
  where absent, `#07` falls back to the searchfox line-range intersection (#14, #07).
- **searchfox-cli surface unverified end-to-end.** The binary is not installed in
  this environment: exact flag spelling, the existence/semantics of `--calls-between`
  (function- vs class/namespace-scoped), the `-R/--repo` enum, and the **byte-for-byte
  markdown output** (permalinks, symbol-ids, edge tree) were only read from the README.
  All parsers must be pinned to real Phase-0 fixtures before being trusted (#00, #01,
  #06, #09).
- **Go/no-go is fragile.** Whether `--calls-from`/`--calls-to` alone give useful
  function-level reach, plus a guessed `--budget-queries=40` and uncertain token
  spend over large markdown, drive the #00 decision (#00, #06).
- **Revision drift.** searchfox indexes ~tip of mozilla-firefox/firefox with no
  per-revision pinning, and may not even expose its indexed rev — so any edge for an
  older crash build node is unverifiable and must be handled by an abstain/drift
  policy. Vendored/3rd-party frames yield no hg node and cannot anchor drift (#01,
  #06, #09).
- **Corpus availability.** Socorro retention may not keep `json_dump` for nightly
  UUIDs old enough to match frozen signatures, and resolving a `regressed_by` bug to
  its landed nightly hg node(s) (multi-commit / backed-out-and-relanded) has no single
  libmozdata call — both can shrink the corpus below the ≥6 target (#00, #13).
- **libmozdata shape unverified.** Not importable in this checkout; SuperSearch param
  names and ProcessedCrash/RawRevision return shapes (esp. per-frame `inlines`, which
  has no in-repo precedent) and `RawRevision.get_url` convention were read from a
  source copy and must be confirmed against the installed `>=0.2.12` release (#00,
  #05, #08, #13).
- **Agent SDK / bundled CLI on the slug.** `claude-agent-sdk>=0.2` must install on Heroku
  Python 3.14 and its **bundled Claude Code CLI** must resolve on `PATH` inside the venv — the
  SDK spawns it as a subprocess, so the worker dyno must allow subprocess spawn and the ~74MB
  wheel must ship in the slug (buildpack/slug size). The API used (`receive_response`,
  `AgentDefinition`, options-level `effort`, `setting_sources=[]`) is 0.2.x despite bugbug's
  `>=0.1.30` pin, and must be confirmed on the resolved version (#02, #03, #10).
- **Structured output is best-effort.** There is **no** `messages.parse`/`output_format` in this
  path: the typed handoff is parsed from the **last trailing ` ```json ` block** of the principal's
  `ResultMessage` and then Pydantic-validated by #03. If the model omits/malforms the block, the
  typed fields are `None` and the run must **abstain** — the anti-hallucination contract is enforced
  by validating the parsed handoff + abstaining on an uncited claim, not by a strict server-side
  schema (#02, #03, #10).
- **Vendoring vs upstream drift.** hackbot ships no PyPI-installable libs, so `hackbot_runtime` +
  `agent_tools` are vendored — record the bugbug commit and re-sync deliberately (#02).
- **Account-level model access.** `claude-fable-5` (and the 30-day data-retention
  requirement) and `claude-opus-4-8` enablement on the org API key cannot be confirmed
  from the repo; if missing, principal calls 400 (#03, #10, #11).
- **Unowned schema seams.** The `crashclouseau/agent/` modules (`schema.py`,
  `llm.py`, `searchfox.py`) and the exact dossier field names are
  consumed by many units before they exist; multiple sub-plans risk writing the same
  file or assuming divergent field names — needs a written contract before the seniors
  code (#03, #05–#10).
- **Confidence type unreconciled.** Numeric 0–100 vs categorical high/medium/low drives
  the `verdict.confidence` column type and the #12 needinfo ordinal gate; changing a
  column type post-migration is costly without Alembic (#04, #10, #12).
- **Persistence / migration.** Unique-per-uuid rows foreclose re-run history the eval
  harness may need; the Heroku one-off migration is unverified against live DB state
  (orphaned enum types may need manual `DROP TYPE`) (#04, #13).
- **Wiring not enforceable in-unit.** The RQ job runs `asyncio.run(run_crash_triage(...))`, so the
  worker dyno must allow **subprocess spawn** (the SDK spawns the bundled CLI) and per-crash bounds
  are `max_turns` + the cost budget (RQ `job_timeout` only bounds the whole job). The enqueue site
  (`put_report` vs `analyze_one_report`) and a Procfile change to run a worker on the `agent` queue
  are ops follow-ups — without the Procfile change jobs enqueue but never run (#02, #11).
- **Eval ↔ Batch mismatch (needs human verification).** A multi-step senior+principal
  agent loop may not fit a single non-interactive Batch request without redesign (#13).
- **Product policy / sign-off (human decisions, not bugs).** Whether a backed-out
  patch is ever kept (#09), and product-owner acceptance that the needinfo is a
  **human-confirmed apply** of the agent's *recorded* action (never auto-posted) (#12).
