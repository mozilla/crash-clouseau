# 02 — Agent substrate: vendored hackbot + Claude Agent SDK seam & tiering

> **SUBSTRATE DECISION (2026-07-02): adopt `mozilla/bugbug` "hackbot".** This unit was
> originally specced as a bespoke `llm_call()` seam over the raw `anthropic` **Messages** SDK
> (`messages.parse`/`output_format`). That is **superseded.** Clouseau now **vendors
> `libs/hackbot-runtime` + `libs/agent-tools`** and drives the **Claude Agent SDK**
> (`claude-agent-sdk`), which runs a multi-turn tool-use loop via the bundled Claude Code CLI.
> The "LLM seam" is therefore not a function we build — it is `ClaudeAgentOptions` +
> `ClaudeSDKClient`, assembled in Clouseau's own agent coroutine. See the verified API notes in
> memory `clouseau-hackbot-substrate-api` and the decision in `clouseau-agent-tooling-stack`.

## Objective
Stand up the agent substrate every LLM-using role runs on: **vendor hackbot-runtime +
agent-tools**, expose Clouseau's evidence tools (the #01 searchfox call-graph client, crash
reading, VCS) as **in-process MCP servers** via `agent_tools`' `@tool`/`build_sdk_server`, and
provide the **one crash-triage agent coroutine** (`run_crash_triage(...)`) that assembles
`ClaudeAgentOptions` (system prompt, MCP servers, subagent `AgentDefinition`s, allowed tools,
model/effort/max_turns) and drives `ClaudeSDKClient` to a typed result. It owns model **tiering**
(per-role `AgentDefinition(model=...)` + options-level `effort`), structured-output extraction
(best-effort trailing ` ```json ` block → Pydantic-validated handoff), usage accounting (from the
SDK `ResultMessage`), and the vendoring/auth wiring — nothing about the RQ worker (that's #11),
the dossier field rules (#03), the needinfo apply step (#12), or any individual role's domain
logic (#05–#10).

## Scope
**In scope**
- Vendoring `hackbot_runtime` + `agent_tools` into Clouseau (path/copy deps) and adding the
  `claude-agent-sdk` optional-extra deps to `requirements.txt`.
- The `clouseau/agent/` package: the crash-triage **agent coroutine** (`run_crash_triage`), the
  `AgentDefinition` factory per role, the `ClaudeAgentOptions` assembly, the `ClaudeSDKClient`
  query/`receive_response` drive loop (inside a hackbot `Reporter` for transcript logging), and
  the final-message parser that turns the principal's terminal `ResultMessage` into a typed
  `CrashTriageResult(HackbotAgentResult)`.
- Wrapping Clouseau's evidence clients as `agent_tools` `@tool` modules and exposing them via
  `build_sdk_server(name, ctx, TOOLS)` (searchfox call-graph from #01; crash reader; VCS).
- Per-role **model tiering** + `effort` resolved from a config `"agent"."llm"` block (config-driven,
  no code edit to re-tier).
- Usage/cost accounting from `ResultMessage.total_cost_usd` / `num_turns` (+ a `PRICES` table for
  estimation), and structured logging of each run.

**Out of scope (owned elsewhere)**
- The RQ job that calls `asyncio.run(run_crash_triage(...))`, enqueue at `update.py`, per-crash
  budget enforcement, DB persistence — **#11 orchestration-worker**.
- The dossier/citation Pydantic models + every-claim-has-a-citation validation — **#03**
  (applied to the parsed handoff here; defined there).
- The recorded-action **apply/replay** step and needinfo UI — **#12** (this unit only wires the
  `actions` MCP server so the agent can *record* a comment/needinfo).
- Each role's prompt content, tool subset choice, and evidence logic — **#05–#10** (this unit
  provides the `AgentDefinition` mechanism + tiering they plug into).
- The searchfox call-graph client itself — **#01** (this unit only `@tool`-wraps it).

## Externalities

| Name | Kind | Version / Endpoint / Command | Status | Purpose |
|---|---|---|---|---|
| `hackbot_runtime` | vendored-lib | `mozilla/bugbug` `libs/hackbot-runtime` (copied into `crashclouseau/vendor/hackbot_runtime/` or added as a path dep) | NEW | `HackbotContext`, `HackbotAgentResult`, `ActionsRecorder`, `actions_server_for`/`actions_to_tool_names`, the bugzilla recordable actions, `AgentError`, `Reporter`. **Do NOT use `run`/`run_async` as the RQ entry** (they `raise SystemExit` + `asyncio.run`). |
| `agent_tools` | vendored-lib | `mozilla/bugbug` `libs/agent-tools` | NEW | `@tool`/`ToolDefinition` registry (`registry.py`), `build_sdk_server(name, ctx, TOOLS)` (`claude_sdk.py`, needs the `claude-sdk` extra), and the read-tool modules (searchfox/mozilla_vcs/bugzilla) as templates. |
| `claude-agent-sdk` | python-lib | `claude-agent-sdk>=0.2` (uv.lock in bugbug resolves **0.2.105**; the manylinux_2_17_x86_64 wheel is ~74MB and **bundles the Claude Code CLI** — no Node install needed for read-only crash triage) | NEW | The agent loop: `ClaudeSDKClient`, `ClaudeAgentOptions`, `AgentDefinition`, `create_sdk_mcp_server`, `tool`, message/blocks types. Pin `>=0.2` (every bugbug pyproject says `>=0.1.30` but the API used — `receive_response`, `AgentDefinition`, `effort=`, `setting_sources=[]` — is 0.2.x). |
| `pydantic` | python-lib | `pydantic>=2` | NEW (added by #01) | `HackbotAgentResult`/`CrashTriageResult` + dossier models; validation of the parsed handoff. |
| `pydantic-settings` | python-lib | `pydantic-settings` (hackbot dep) | NEW | `HackbotContext`/`AgentInputs` are `BaseSettings`. (Clouseau passes inputs as **kwargs**, not env — see Risks — but the dep is still pulled in.) |
| `google-auth`, `requests` | python-lib | hackbot-runtime deps | NEW | Pulled in transitively (WIF/uploader). Inert on Heroku (no WIF, no signed-policy uploader). |
| Claude Haiku 4.5 | llm-model | `claude-haiku-4-5` — $1 / $5 per 1M (in/out), 200K ctx | NEW | **Senior** tier: crash-interpreter, patch-scout, skeptic. Set via `AgentDefinition(model="haiku")` (or the full id in config). |
| Claude Sonnet 5 | llm-model | `claude-sonnet-5` — $3 / $15 per 1M, 1M ctx | NEW | Default **navigator** (`call-graph-explorer`, Phase-0 finding below) and `data-flow-tracer`. `AgentDefinition(model="sonnet")`; supports `effort`. |
| Claude Opus 4.8 | llm-model | `claude-opus-4-8` — $5 / $25 per 1M, 1M ctx | NEW | Default **principal** (final verdict/abstain) and the optional judge; `model="opus"`, `effort` `low..max`. |
| Claude Fable 5 | llm-model | `claude-fable-5` — $10 / $50 per 1M, 1M ctx | NEW | Optional hardest-case principal; needs 30-day retention; opt-in. |
| `ANTHROPIC_API_KEY` | config | env var read **by the bundled Claude Code CLI directly** (agents never pass it into the SDK); put it in the dyno config / `~/.mozdata.ini` (see `clouseau-secrets-in-mozdata-ini`), never the tracked repo file | NEW | Credential. `hackbot_runtime` WIF (`anthropic_wif`) is inert unless `ANTHROPIC_FEDERATION_RULE_ID` is set — leave it **unset** on Heroku; never set both. |
| `crashclouseau/config.py` | internal-module | existing `_get_global()` + the `get_agent()`/`get_searchfox()` added by #01 | existing (extend) | Add `get_llm()` / `get_llm_role(role)` reading a new `agent.llm` block. |
| `config/global.json` | config | the `agent` block added by #01 | existing (extend) | Add `agent.llm` (per-role model/effort/max_turns, pricing, caps). |
| `crashclouseau/searchfox.py` | internal-module | the #01 adapter (`SearchfoxClient`) | existing | Wrapped here as `@tool` functions for the SDK; not modified. |

## Architecture — the two loops (critical)
hackbot has **two** loops and only the second is an LLM loop:

1. **Runtime loop** (`hackbot_runtime.run`/`run_async`): agent-neutral and thin — builds
   `HackbotContext`, `_configure_auth()` (WIF or API-key fallback), runs the `main(ctx)`
   coroutine, serializes the outcome into `summary.json` (`{status, error, findings, actions}`),
   `raise SystemExit`. **It never touches the Claude SDK and is unusable as an RQ job body.**
2. **Principal loop** (per-agent, in `agent.py`): assemble `ClaudeAgentOptions`, then
   `async with ClaudeSDKClient(options) as c: await c.query(prompt); async for m in
   c.receive_response(): reporter.message(m)`, capture the terminal `ResultMessage`, build the
   typed result. The SDK runs the multi-turn tool-use + subagent loop internally (spawning the
   bundled CLI subprocess); the agent just consumes the streamed messages.

**Clouseau writes loop #2 itself** (`run_crash_triage`) and #11 runs
`asyncio.run(run_crash_triage(...))` inside the RQ job. The vendored runtime is used for its
`HackbotContext`/`ActionsRecorder`/`HackbotAgentResult`/`Reporter` helpers, not its entry points.

## Deliverables

**Vendor the libs** — copy `hackbot_runtime` + `agent_tools` under `crashclouseau/vendor/` (or add
as path deps in `requirements.txt`), and add `claude-agent-sdk>=0.2`, `pydantic-settings`,
`google-auth`, `requests` to `requirements.txt` (dedupe with #01's `pydantic>=2`). Confirm the
Heroku slug resolves the linux-x86_64 `claude-agent-sdk` wheel and the bundled CLI is on `PATH`
inside the venv.

**Create `crashclouseau/agent/tools/`** — Clouseau's evidence tools as `agent_tools` `@tool`
modules (each `async def <tool>(ctx, ...)` with a docstring; first param is the ctx):
- `searchfox_cg.py` — `calls_from` / `calls_to` / `calls_between` / `define` / `lookup` / `search`
  over a `SearchfoxCtx(client=SearchfoxClient())` (the #01 adapter). `TOOLS = tools_in(__name__)`.
  (Distinct from bugbug's read-only `agent_tools/searchfox.py`, which has **no call-graph**.)
- `crash.py` — read the processed crash / stack via existing libmozdata + `inspector` (crash
  reading stays on libmozdata; no new CLI).
- Each exposed at runtime with `build_sdk_server("searchfox", SearchfoxCtx(...), searchfox_cg.TOOLS)`
  etc.; the model sees `mcp__searchfox__calls_from`-style ids listed in `allowed_tools`.

**Create `crashclouseau/agent/roles.py`** — the `AgentDefinition` factory per senior role
(`crash-interpreter`, `call-graph-explorer`, `patch-scout`, `data-flow-tracer`, `skeptic`), each
with a role prompt, a curated `tools=[...]` allowlist (mcp ids + built-ins Read/Grep/Glob/Bash),
and a per-role `model` from config. Roles are **authored by Clouseau** — hackbot ships only one
generic `investigator`, so there is nothing to copy but the mechanism.

**Create `crashclouseau/agent/triage.py`** — `async def run_crash_triage(*, crash, tools_cfg,
llm_cfg, recorder=None, extra=None) -> CrashTriageResult`:
- Build in-process MCP servers (`build_sdk_server`) for searchfox/crash (+ `actions_server_for`
  from #12 when needinfo recording is enabled).
- Render the principal system prompt (from `prompts/system.md`, `str.format()`-ed — **double any
  literal `{{ }}`**), assemble `ClaudeAgentOptions(system_prompt=..., mcp_servers={...},
  agents={role: AgentDefinition(...)}, allowed_tools=[...,'Task'], model=<principal>,
  max_turns=..., **({'effort':effort} if effort else {}), permission_mode='bypassPermissions',
  setting_sources=[])`.
- Drive `ClaudeSDKClient` (query + `receive_response`, inside a `Reporter`), capture the
  `ResultMessage`, and build `CrashTriageResult` from `result_msg.result` + `num_turns` +
  `total_cost_usd`, best-effort parsing the **last ` ```json ` block** into the typed handoff
  fields. `None`/`is_error` result → `raise AgentError`.

**Create `crashclouseau/agent/result.py`** — `class CrashTriageResult(HackbotAgentResult)` (the
`num_turns`/`total_cost_usd` base + Clouseau's verdict fields: culprit frame/node, off-stack
candidate(s), `confidence`, `actionable`/abstain, `regressor_node`, proposed needinfo text +
citations). This is the dossier hand-off; #03 defines its exact fields + citation validators.

**Modify `crashclouseau/config.py`** — add `get_llm()` → `get_agent().get("llm", {})` and
`get_llm_role(role)` (with documented defaults applied in `agent/roles.py`).

**Modify `config/global.json`** — add the `agent.llm` block (see Interfaces).

**Create `tests/test_agent.py`** — unit tests with `ClaudeSDKClient` **mocked** (no live calls,
no CLI): role→model/effort resolution from config, `ClaudeAgentOptions` assembly (allowed tools
include `Task` + the right `mcp__*` ids; `permission_mode`/`setting_sources` set), the
trailing-```json parser (valid block → typed fields; missing/malformed block → all-None + raw
text survives), and `CrashTriageResult` construction from a mocked `ResultMessage`. Uses
`unittest` (MPL header; `DATABASE_URL=sqlite://`) like `tests/test_searchfox.py`.

## Interfaces

**Inputs consumed**
- `crash` — the per-run crash payload (uuid/signature/stack/regression range) built from
  libmozdata/Socorro by the crash sub-plan; passed as a **kwarg** (not env).
- `llm_cfg` — the resolved `agent.llm` block (per-role model/effort/max_turns).
- `recorder` — an optional `ActionsRecorder` (from #12) when needinfo recording is on.

**Outputs produced**
- `CrashTriageResult` (a `HackbotAgentResult` subclass) — the typed verdict/dossier hand-off;
  `.model_dump()` is what #11 persists (mirrors hackbot's `summary.json['findings']`).
- Recorded actions live on the passed `recorder.actions` (→ #12), not returned here.
- Per-run cost/turns from the `ResultMessage` for the #11 usage accounting/budget.

**Depends on / feeds**
- Depends on: vendored `hackbot_runtime` + `agent_tools`, `claude-agent-sdk`, `config.py`,
  `crashclouseau/searchfox.py` (#01).
- Feeds: #11 (calls `run_crash_triage` in an RQ job), #05–#10 (their roles are `AgentDefinition`s +
  `@tool`s registered here), #12 (the `actions` server + recorded needinfo), #03 (validates the
  parsed handoff).

Proposed `agent.llm` config block (added under the existing `agent` object in `config/global.json`):
```json
"llm": {
  "principal": {"model": "opus",   "effort": "high", "max_turns": 40},
  "roles": {
    "crash-interpreter":   {"model": "haiku"},
    "call-graph-explorer": {"model": "sonnet", "effort": "high"},
    "patch-scout":         {"model": "haiku"},
    "data-flow-tracer":    {"model": "sonnet", "effort": "high"},
    "skeptic":             {"model": "haiku"}
  },
  "pricing": {
    "claude-haiku-4-5": {"in": 1.0, "out": 5.0},
    "claude-sonnet-5":  {"in": 3.0, "out": 15.0},
    "claude-opus-4-8":  {"in": 5.0, "out": 25.0},
    "claude-fable-5":   {"in": 10.0, "out": 50.0}
  },
  "max_cost_usd_per_crash": 2.0
}
```
> `model` values are the SDK short names (`haiku`/`sonnet`/`opus`) accepted by
> `AgentDefinition(model=...)`; use full ids (`claude-opus-4-8`) at the `ClaudeAgentOptions`
> `model=` level. **`effort` is options-level** (principal session), not a per-`AgentDefinition`
> field. A subagent with `model="inherit"` follows the principal — set an explicit tier per role.

> **Phase-0 finding — the navigator (`call-graph-explorer`) is `sonnet`, not `haiku`.**
> The spike (`spike/`, memory `clouseau-phase0-findings`) tiered the navigator on a 20-case
> corpus: off-stack recall **Sonnet 5 4/7 vs Haiku 1/7 vs default-Opus 1/7**. The binding factor
> is *exploration persistence* (Haiku/default-Opus quit early; Sonnet runs to fixpoint), not raw
> model rank; Opus did not beat Sonnet even with a persistence nudge + effort=high, and `xhigh`
> was ~minutes/call. So the navigator seat is `AgentDefinition(model="sonnet", effort via options)`;
> the other three seniors stay `haiku`. Keep the mechanical-BFS neighborhood as a floor (union of
> BFS ∪ navigator > either alone).

## Implementation steps
1. Vendor `hackbot_runtime` + `agent_tools` (copy under `crashclouseau/vendor/` or path deps).
   Add `claude-agent-sdk>=0.2`, `pydantic-settings`, `google-auth`, `requests` to
   `requirements.txt`; `uv pip install`; confirm import + that the bundled CLI resolves.
2. Add the `agent.llm` block to `config/global.json`; add `get_llm()`/`get_llm_role(role)` to
   `config.py`.
3. Write `crashclouseau/agent/tools/searchfox_cg.py` (+ `crash.py`) as `@tool` modules over the
   #01 client; smoke `build_sdk_server(...)` builds an MCP server.
4. Write `crashclouseau/agent/roles.py` (`AgentDefinition` factory per role, config-driven model).
5. Write `crashclouseau/agent/result.py` (`CrashTriageResult(HackbotAgentResult)`).
6. Write `crashclouseau/agent/triage.py` (`run_crash_triage`): options assembly, `ClaudeSDKClient`
   drive loop, trailing-```json parser, typed-result build; `AgentError` on empty/error result.
7. Write `prompts/system.md` (principal navigator prompt; double literal braces).
8. Write `tests/test_agent.py` with the SDK mocked (no network/CLI).
9. Manual live smoke (with `ANTHROPIC_API_KEY`): `asyncio.run(run_crash_triage(crash=<known
   regression>))` returns a `CrashTriageResult` with a non-empty verdict + `total_cost_usd>0`.

## Risks & open questions
- **The SDK spawns the Claude Code CLI as a subprocess.** Not a pure HTTP call: the worker dyno
  must allow subprocess spawn and have the wheel's CLI on `PATH` in the venv. The ~74MB wheel
  bundles it; verify the Heroku slug ships it (buildpack size). No Node needed for read-only
  triage (only bugbug's devtools agents need `npx`).
- **Structured output is best-effort** (trailing ` ```json ` parse, no `output_format`). If the
  model omits/malforms the block, all typed fields are `None` and only raw text survives → the
  handoff must **abstain** (route to #07/#10 abstain path), and the prompt must strongly instruct
  a final JSON block. #03's schema is validated *after* parse, not enforced by the API.
- **`permission_mode='bypassPermissions'` + `setting_sources=[]`** auto-approve every tool
  (including `Bash`) and stop the SDK loading dev `CLAUDE.md`/settings — required for headless RQ;
  keep them, and scope `allowed_tools` tightly (read-only roles omit Write/Edit).
- **Subagent tiering:** `model="inherit"` silently follows the principal; set an explicit
  `AgentDefinition(model=...)` per role or the Phase-0 Sonnet navigator won't take effect. `Task`
  must be in the principal's `allowed_tools` or delegation silently no-ops; children get no `Task`.
- **Vendoring vs upstream drift.** hackbot is not on PyPI as installable libs; vendoring pins a
  copy — record the bugbug commit and re-sync deliberately. `claude-agent-sdk` floor is `>=0.2`
  (the code uses 0.2.x APIs despite the `>=0.1.30` pin in bugbug).
- **Cost ceiling per crash.** `max_cost_usd_per_crash` is config; enforcement (abort/downgrade on
  overspend using `ResultMessage.total_cost_usd`) lives in #11.
- **Auth foot-gun:** never set `ANTHROPIC_FEDERATION_RULE_ID` alongside `ANTHROPIC_API_KEY`
  (`anthropic_wif.configure()` errors and the key shadows WIF). On Heroku set only the API key.

## Acceptance criteria
- `hackbot_runtime` + `agent_tools` import from the vendored path; `claude-agent-sdk>=0.2` installs
  on the target Python and its bundled CLI resolves on `PATH`.
- `from crashclouseau.agent.triage import run_crash_triage` imports without spawning the CLI at
  import time.
- `tests/test_agent.py` passes with `ClaudeSDKClient` mocked (no network/CLI, no API key):
  config→per-role model/effort resolution; `ClaudeAgentOptions` includes `Task` + the expected
  `mcp__searchfox__*` ids and sets `permission_mode`/`setting_sources`; the trailing-```json parser
  yields typed fields on a good block and an all-None+raw-text abstain on a missing/malformed block.
- `build_sdk_server("searchfox", SearchfoxCtx(...), searchfox_cg.TOOLS)` returns an MCP server
  config whose tool ids match the `allowed_tools` list (searchfox call-graph reachable by the agent).
- A live smoke run analyzes a known real regression and returns a `CrashTriageResult` with a
  populated verdict + citations and `total_cost_usd > 0`; changing a role's `model` in config
  changes the tier used with no code edit.
- No API key in any committed file; the key is read from env only.
- `flake8` clean; `#01`'s `crashclouseau/searchfox.py` is unchanged by this unit.
