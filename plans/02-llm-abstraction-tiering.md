# 02 — LLM abstraction & model tiering

## Objective

Build a single `llm_call(role, messages, schema=...)` abstraction over the `anthropic` Python SDK that selects the model, effort, thinking, and provider per role from a config-driven `"llm"` block. It centralizes structured output (via `messages.parse` + Pydantic), prompt-caching of the shared crash/diff prefix, retry/backoff, and token/cost accounting so every agent role (workers, tracer, principal) calls the LLM through one verifiable, swappable seam. This unit owns only the LLM seam; it does not implement the agents, dossier schema, or searchfox driving.

## Scope

**In scope**
- New `crashclouseau/llm/` package: the `llm_call(role, ...)` entry point, an `LLMClient` wrapping `anthropic.Anthropic()`, per-role model/effort resolution from config, structured-output dispatch via `client.messages.parse(output_format=PydanticModel)`, prompt-cache breakpoint placement on a shared prefix, retry/backoff, and a `UsageAccumulator` for token/cost accounting.
- New `"llm"` block in `config/global.json` + accessor functions in `crashclouseau/config.py`.
- New deps `anthropic` and `pydantic` added to `requirements.txt`.
- A small set of base Pydantic models that are pure infrastructure (envelope/usage), NOT the dossier.
- Batch-mode helper (`llm_batch(...)`) wrapping `client.messages.batches.*` for the OFFLINE eval re-runs only.

**Out of scope (owned by other sub-plans)**
- The dossier Pydantic schema and grounding/citation invariants (owned by the dossier/contract sub-plan).
- The senior/tracer/principal agent role *logic* and their prompts (each role's sub-plan calls `llm_call` with its own role string + schema).
- `searchfox-cli` subprocess driving and the call-graph exploration (owned by the call-graph/tooling sub-plan).
- The RQ job that hooks into `update.put_report` / `analyze_one_report` to launch the agent pipeline (owned by the orchestration/enqueue sub-plan; this unit only provides the callable seam it will use).
- Crash-data enrichment (`inspector.py` extra fields) and patch raw-diff fetching (owned by the evidence-assembly sub-plan; this unit only *receives* assembled text).

## Externalities

| Name | Kind | Version / Endpoint / Command | Status | Purpose |
|---|---|---|---|---|
| `anthropic` | python-lib | `anthropic` — pin a floor at the exact installed version after `pip install` (current PyPI release is `0.109.2`, 2026-06-15; requires Python ≥3.9). Do NOT pin `>=0.69` — that floor predates `messages.parse` / `output_config.effort` / server-side `fallbacks` and would not provide the APIs this unit calls. | NEW | The official SDK; `client = anthropic.Anthropic()` resolving `ANTHROPIC_API_KEY` from env. Provides `messages.create`, `messages.parse`, `messages.stream`, `messages.batches.*`, typed exceptions. |
| `pydantic` | python-lib | `pydantic>=2` (v2 required for `messages.parse` / `output_format`) | NEW | Schema models for structured output validation; `response.parsed_output` is a validated instance. |
| Claude Haiku 4.5 | llm-model | `claude-haiku-4-5` — $1 / $5 per 1M (in/out), 200K ctx | NEW | WORKER ("senior") tier: crash-interpreter, patch-scout, skeptic. Plain calls — NO `effort`, NO extended thinking (both error / unsupported on Haiku). |
| Claude Sonnet 5 | llm-model | `claude-sonnet-5` — $3 / $15 per 1M, 1M ctx | NEW | Mid tier; default for **`call-graph-explorer`** (see Phase-0 finding below) and `data-flow-tracer` (config-selectable up to Opus 4.8). Supports adaptive thinking + `effort` (`low`..`max`). Min cacheable prefix 2048 tokens (verify for Sonnet 5 at implement time). |
| Claude Opus 4.8 | llm-model | `claude-opus-4-8` — $5 / $25 per 1M, 1M ctx | NEW | Default PRINCIPAL tier (final causal verdict / abstain). Adaptive thinking + `effort` (`low`/`medium`/`high`/`xhigh`/`max`). Min cacheable prefix 4096 tokens. |
| Claude Fable 5 | llm-model | `claude-fable-5` — $10 / $50 per 1M, 1M ctx | NEW | Optional hardest-case principal/tracer. Thinking always on (omit `thinking`); explicit `{type:"disabled"}` 400s; requires 30-day data retention (ZDR orgs 400 on every request); `refusal` stop reason — default-enable server-side `fallbacks` to `claude-opus-4-8` (beta header `server-side-fallback-2026-06-01`). Min cacheable prefix 2048. |
| `messages.parse` | REST-API | `POST /v1/messages` via `client.messages.parse(model=..., messages=..., output_format=PydanticModel)` → `response.parsed_output` | NEW | Structured-output path; validates against the Pydantic schema. NOTE: `output_format=` is the correct kwarg for `messages.parse(...)`; only the top-level `output_format` parameter on `messages.create(...)` is deprecated (replaced there by `output_config.format`). Schema must use `additionalProperties:false` + `required`; no `minLength`/`maxLength`/`minimum`/`maximum`/`multipleOf`/recursive (the Python SDK strips unsupported constraints and validates them client-side). |
| `messages.create` | REST-API | `POST /v1/messages` via `client.messages.create(...)` | NEW | Plain text / non-schema calls and the `stream=True` / `messages.stream` path for large `max_tokens`. |
| `messages.batches` | REST-API | `client.messages.batches.create/retrieve/results` (Python wraps requests as `Request(custom_id=..., params=MessageCreateParamsNonStreaming(...))`) | NEW | 50% cheaper async (<=24h) — OFFLINE eval re-runs over historical corpus only; key results by `custom_id` (results arrive in any order). NOTE: server-side `fallbacks` is rejected on the Batches API, so Fable batch items cannot use server-side refusal fallback. |
| prompt caching | config | `cache_control={"type":"ephemeral"}` on shared-prefix block | NEW | Cache the crash brief + candidate diffs reused across senior calls; reads ~0.1x, writes ~1.25x (5-min TTL). Verify via `usage.cache_read_input_tokens`. Max 4 breakpoints/request; 20-block lookback window. |
| `crashclouseau/config.py` | internal-module | existing `_get_global()` / `_get_local()` JSON loaders | existing | Add `get_llm_config()` / `get_llm_role(role)` / `get_llm_pricing()` / `get_llm_provider()` reading the new `"llm"` block; keep secrets (API key) in env, not config. |
| `config/global.json` | data-source | `./config/global.json` (loaded by `config._get_global`) | existing→NEW key | Add the `"llm"` object (per-role model/effort/max_tokens, caching toggles, pricing table, retry params). |
| `crashclouseau/logger.py` | internal-module | `from .logger import logger` | existing | Structured logging of each call (role, model, tokens, cost, latency, `_request_id` on failure). |
| `crashclouseau/worker.py` | internal-module | RQ `get_queue(name="low")` returns one of the cached queues `["high","default","low"]` (all with `default_timeout=6000`); enqueue via `queue.enqueue_call(func=..., result_ttl=0)` | existing | Not modified here; the orchestration sub-plan enqueues agent jobs that call `llm_call`. Listed because cost/latency budget interacts with RQ `default_timeout=6000`s. |
| `ANTHROPIC_API_KEY` | config | env var (SDK also accepts `ANTHROPIC_AUTH_TOKEN` or an `ant auth login` profile) | NEW | Credential resolved by `anthropic.Anthropic()`. Add to Heroku config vars; never commit. |
| `requirements.txt` | data-source | repo root | existing→modified | Add `anthropic` (floor = exact installed version) and `pydantic>=2` floors alongside existing pins. |
| Python runtime | config | Python 3.14 (Heroku, per `runtime.txt`) / 3.9+ for `anthropic`, 3.10+ for the `match/case` batch examples | existing | `messages.batches` examples use `match/case` (3.10+); fine on 3.14. |

## Deliverables

Exact files to create/modify:

- **`crashclouseau/llm/__init__.py`** (NEW) — re-exports `llm_call`, `llm_batch`, `UsageAccumulator`, `LLMError`.
- **`crashclouseau/llm/client.py`** (NEW)
  - `class LLMClient` — holds a module-level singleton `anthropic.Anthropic()` (and optional per-role provider clients); method `call(role, messages, *, schema=None, system=None, cache_prefix=None, max_tokens=None, stream=None) -> LLMResult`.
  - `def llm_call(role, messages, *, schema=None, system=None, cache_prefix=None, max_tokens=None) -> LLMResult` — module-level convenience that uses the singleton.
  - `def llm_batch(role, requests: list[BatchItem]) -> dict[str, LLMResult]` — wraps `messages.batches.*`, keyed by `custom_id`; for offline eval only.
  - Internal `_build_request(role, ...)` that resolves model/effort/thinking from config and assembles kwargs (handles the Haiku-no-effort / Fable-no-thinking / Opus-adaptive cases).
  - `_dispatch(...)` choosing `messages.parse` (schema given) vs `messages.stream`+`get_final_message` (large `max_tokens`) vs `messages.create`.
- **`crashclouseau/llm/config.py`** (NEW) — `RoleSpec` dataclass (model, effort, thinking, max_tokens, provider) and `resolve_role(role) -> RoleSpec` reading `config.get_llm_config()`, plus `default_max_tokens_for(model)`.
- **`crashclouseau/llm/pricing.py`** (NEW) — `PRICES` table (per-model in/out + cache read/write multipliers from the config pricing block) and `cost_of(usage, model) -> float` handling `input_tokens` + `cache_creation_input_tokens`(×1.25) + `cache_read_input_tokens`(×0.1) + `output_tokens`.
- **`crashclouseau/llm/usage.py`** (NEW) — `class UsageAccumulator` (thread-safe add of per-call usage, totals by role/model, total cost) and `LLMResult` dataclass (`parsed` Pydantic instance or `None`, `text`, `usage`, `cost`, `model`, `role`, `request_id`, `stop_reason`).
- **`crashclouseau/llm/retry.py`** (NEW) — `call_with_retry(fn, *, role)` wrapping the SDK call: rely on SDK `max_retries` for 429/5xx, add a thin outer guard for `RateLimitError`/`InternalServerError`/`APIConnectionError` honoring `retry-after`; map non-retryable 4xx (`BadRequestError`) and refusals to `LLMError`. (The SDK already retries 408/409/429/5xx + connection errors with default `max_retries=2`; this layer adds role-aware backoff caps + logging.)
- **`crashclouseau/llm/errors.py`** (NEW) — `class LLMError(Exception)` and `class LLMRefusal(LLMError)` (carries `stop_details`).
- **`crashclouseau/config.py`** (MODIFY) — add `get_llm_config()`, `get_llm_role(role)`, `get_llm_pricing()`, `get_llm_provider()`.
- **`config/global.json`** (MODIFY) — add the `"llm"` block (see Interfaces).
- **`requirements.txt`** (MODIFY) — add `anthropic` (floor = installed version) + `pydantic>=2`.
- **`tests/test_llm.py`** (NEW) — unit tests with the SDK mocked (no live calls): role→model resolution, schema dispatch, cost math, retry classification, cache-prefix placement, Haiku-no-effort guard. (NOTE: the repo currently has no `tests/` dir or test runner configured; this introduces one — confirm a runner with the maintainer.)

## Interfaces

**Inputs consumed**
- `role: str` — one of `crash-interpreter`, `call-graph-explorer`, `patch-scout`, `skeptic`, `data-flow-tracer`, `principal` (string keys matching the config `"llm"."roles"` map; unknown role → `LLMError`).
- `messages: list[dict]` — Anthropic message list (caller-built); first role `user`.
- `schema: type[pydantic.BaseModel] | None` — when given, dispatched via `messages.parse`; the SDK returns the validated instance on `response.parsed_output`, which the unit stores as `LLMResult.parsed`.
- `system: str | list[block] | None`, `cache_prefix: str | None` — `cache_prefix` is the large shared crash-brief + candidate-diffs text; placed as a `system`/leading-message block carrying `cache_control={"type":"ephemeral"}`, with volatile per-call content kept last (caching prefix-match invariant — render order is `tools` → `system` → `messages`).
- Config: the `"llm"` block (model per role, effort, max_tokens, provider, pricing, retry caps, caching on/off).

**Outputs produced**
- `LLMResult` — `parsed` (Pydantic instance or `None`, sourced from `response.parsed_output`), `text`, `usage` (raw SDK usage), `cost` (float USD), `model`, `role`, `request_id` (from `response._request_id`), `stop_reason`. Refusal → raises `LLMRefusal` (so callers/abstain logic treat it as first-class).
- Per-call accounting fed into a `UsageAccumulator` the orchestration layer can total per crash.

**Dossier fields read/written:** This unit reads/writes NO dossier fields directly. It is the transport: the dossier sub-plan defines the Pydantic schemas, and each role passes its schema into `llm_call`; the validated `LLMResult.parsed` is what the role then stores into the dossier. The grounding/citation invariant ("a field lacking its citation is invalid") is enforced in the dossier schema's validators, not here — this unit only guarantees the output validates against whatever schema it is handed.

**Depends on / feeds**
- Depends on: nothing in the new system (foundation unit); only existing `config.py` + `logger.py`.
- Feeds: every agent-role sub-plan (seniors, tracer, principal) calls `llm_call`; the orchestration sub-plan owns the `UsageAccumulator` lifecycle per crash and the RQ enqueue at `update.put_report` (which ends by calling `CrashStack.put_frames(...)` then `UUID.set_analyzed(uuid, useless)`; `useless=False` means frames were stored/scored).

Proposed `"llm"` config block (added to `config/global.json`):
```json
"llm": {
  "provider": "anthropic",
  "caching": true,
  "roles": {
    "crash-interpreter": {"model": "claude-haiku-4-5", "max_tokens": 4096},
    "call-graph-explorer": {"model": "claude-sonnet-5", "effort": "high", "max_tokens": 8192},
    "patch-scout": {"model": "claude-haiku-4-5", "max_tokens": 4096},
    "skeptic": {"model": "claude-haiku-4-5", "max_tokens": 4096},
    "data-flow-tracer": {"model": "claude-sonnet-5", "effort": "high", "max_tokens": 16000},
    "principal": {"model": "claude-opus-4-8", "effort": "high", "max_tokens": 16000, "fallbacks": []}
  },
  "pricing": {
    "claude-haiku-4-5": {"in": 1.0, "out": 5.0},
    "claude-sonnet-5": {"in": 3.0, "out": 15.0},
    "claude-opus-4-8": {"in": 5.0, "out": 25.0},
    "claude-fable-5": {"in": 10.0, "out": 50.0}
  },
  "cache_read_mult": 0.1,
  "cache_write_mult": 1.25,
  "retry": {"max_attempts": 5, "max_delay_s": 60}
}
```

> **Phase-0 finding — `call-graph-explorer` defaults to `claude-sonnet-5`, not Haiku.**
> The spike (`spike/`, memory `clouseau-phase0-findings`) tiered the navigator on a
> 20-case corpus: off-stack recall was **Sonnet 5 4/7 vs Haiku 1/7 vs Opus-default 1/7**.
> The binding factor is *exploration persistence*, not raw model rank — Haiku (and
> default Opus) quit early (`no-proposals`); Sonnet explores to `fixpoint`. Opus did not
> beat Sonnet even with a persistence directive + effort=high (2/7), and `xhigh` was
> ~minutes/call. So the navigator seat is Sonnet 5 at effort=high; the other three
> seniors (crash-interpreter, patch-scout, skeptic) stay Haiku (cheap, no persistence
> demand). Cost was ~$0.20/case for the Sonnet navigator. Keep the mechanical-BFS
> neighborhood as a floor/fallback (union of BFS ∪ navigator > either alone).

## Implementation steps

1. Add `anthropic` and `pydantic>=2` to `requirements.txt`; `pip install`, then set the `anthropic` floor to the exact installed version (current PyPI is `0.109.2` — pin `>=` that, not `>=0.69`).
2. Add the `"llm"` block above to `config/global.json`; add `get_llm_config()`, `get_llm_role(role)`, `get_llm_pricing()`, `get_llm_provider()` to `crashclouseau/config.py` (mirroring the existing `_get_global()` accessor pattern).
3. Create `crashclouseau/llm/errors.py` (`LLMError`, `LLMRefusal`) and `crashclouseau/llm/config.py` (`RoleSpec`, `resolve_role`, `default_max_tokens_for`).
4. Create `crashclouseau/llm/pricing.py` with `cost_of(usage, model)` covering `input_tokens`, `cache_creation_input_tokens` (×1.25), `cache_read_input_tokens` (×0.1), `output_tokens`, reading rates from config.
5. Create `crashclouseau/llm/usage.py` with `LLMResult` and a thread-safe `UsageAccumulator`.
6. Create `crashclouseau/llm/client.py`:
   - Lazy module-level `anthropic.Anthropic()` singleton.
   - `_build_request(role, ...)`: resolve `RoleSpec`; set `model`, `max_tokens`; if model is Haiku → omit `effort`/`thinking`; if Sonnet/Opus → `thinking={"type":"adaptive"}` + `output_config={"effort": spec.effort}` when `effort` set; if Fable → omit `thinking`, and when `fallbacks` configured use `client.beta.messages.*` with `betas=["server-side-fallback-2026-06-01"]` + `fallbacks=[{"model":"claude-opus-4-8"}]` (server-side fallbacks live on the beta messages endpoint, not plain `messages.create`).
   - Cache-prefix placement: when `caching` true and `cache_prefix` given, render it as the first stable block with `cache_control={"type":"ephemeral"}`; ensure it clears the per-model min-prefix size (4096 Opus/Haiku, 2048 Sonnet/Fable) else skip the marker (a sub-min prefix silently won't cache); keep volatile content after it.
   - `_dispatch`: `schema` → `messages.parse(output_format=schema)` (read `.parsed_output`); else if `max_tokens` large (>~16000) → `messages.stream(...).get_final_message()`; else `messages.create`.
   - Always check `stop_reason == "refusal"` before reading content → raise `LLMRefusal(stop_details=...)` (note `stop_details` is `None` for every non-refusal stop reason — only read it under the refusal branch).
   - Build `LLMResult` (compute cost via `pricing.cost_of`, set `request_id` from `response._request_id`), `acc.add(result)` if an accumulator is passed, log one structured line.
7. Create `crashclouseau/llm/retry.py` wrapping `_dispatch` with role-aware backoff over `RateLimitError`/`InternalServerError`/`APIConnectionError` (honor `retry-after`), re-raising 4xx/`BadRequestError` as `LLMError`.
8. Add `crashclouseau/llm/__init__.py` re-exporting the public surface.
9. Add `llm_batch(role, requests)` to `client.py` using `from anthropic.types.message_create_params import MessageCreateParamsNonStreaming` + `from anthropic.types.messages.batch_create_params import Request`, polling `batches.retrieve` until `processing_status=="ended"`, collecting `batches.results` keyed by `custom_id` (results unordered); document it as offline-only (latency). NOTE: server-side `fallbacks` is rejected on the Batches API — do not pass it on Fable batch items.
10. Write `tests/test_llm.py` mocking `anthropic.Anthropic` (no network): assert role→model/effort/thinking kwargs (especially Haiku omits effort, Fable omits thinking + uses the beta fallbacks path), schema path calls `messages.parse`, cost math against known usage, refusal → `LLMRefusal`, retry classification, and that a sub-min-size prefix is not marked for caching.

## Risks & open questions

- **SDK version drift / exact method names.** `messages.parse(output_format=...)` → `.parsed_output` and `output_config.effort` are current per the claude-api skill, but the installed `anthropic` build (latest PyPI 0.109.2) must be verified — write code, run the mocked tests, and fix against real import/signature errors rather than assuming.
- **Pydantic schema constraints leak from the dossier sub-plan.** The dossier models must avoid `minLength`/`maxLength`/`minimum`/`maximum`/`multipleOf`/recursive schemas and set `additionalProperties:false`. The Python SDK strips unsupported constraints and validates them client-side, but recursive schemas and a missing `additionalProperties:false` still 400 server-side; this unit should fail loudly (`LLMError`) so the dossier sub-plan fixes the schema — open question whether to add a schema-lint helper here.
- **Fable 5 data-retention + refusal.** Fable requires 30-day retention (else every request 400s) and can refuse benign security-adjacent crash analysis; default server-side fallbacks to Opus 4.8 (beta header `server-side-fallback-2026-06-01`, on the `client.beta.messages.*` endpoint) mitigate. Keep Fable opt-in via config; default principal stays Opus 4.8.
- **Cost ceiling per crash.** Many senior calls × Haiku + caching should stay cheap, but the orchestration layer needs the `UsageAccumulator` total to enforce a per-crash budget; the budget *policy* is out of scope here — open question where the cap lives (config vs orchestration).
- **RQ timeout interaction.** Principal calls at high effort can run minutes; `messages.stream`+`get_final_message` avoids HTTP timeouts (the SDK refuses non-streaming requests it estimates exceed ~10 min), but the RQ `default_timeout=6000`s (100 min) is the real ceiling — confirm streaming is used for any `max_tokens` > ~16000.
- **Free/non-Anthropic provider swap.** The `provider` config key is reserved for swapping seniors to a non-Anthropic model later; only Anthropic is implemented now — the `LLMClient` indirection is the seam, but a real second provider is unbuilt.
- **No test harness exists yet.** The repo has no `tests/` directory or configured runner; `tests/test_llm.py` introduces one — confirm the runner/CI convention with the maintainer.

## Acceptance criteria

- `from crashclouseau.llm import llm_call, llm_batch, UsageAccumulator, LLMError` imports cleanly with the new deps installed.
- `tests/test_llm.py` passes with the SDK fully mocked (no network, no API key needed): role resolution, per-model kwarg shaping (Haiku no-effort, Opus adaptive+effort, Fable no-thinking + beta server-side fallbacks), schema dispatch via `messages.parse`, cost math, refusal→`LLMRefusal`, retry classification, sub-min cache-prefix not marked.
- A live smoke test (run manually with `ANTHROPIC_API_KEY` set) shows: `llm_call("crash-interpreter", [...], schema=SomeModel)` returns an `LLMResult` whose `.parsed` is a validated instance (from `response.parsed_output`), `.cost` > 0, `.model == "claude-haiku-4-5"`, and a second call with the same `cache_prefix` reports `usage.cache_read_input_tokens > 0`.
- `config.get_llm_role("principal").model == "claude-opus-4-8"` and changing the config model string changes the model used with no code edit (config-driven tiering verified).
- A refusal-shaped response (mocked `stop_reason="refusal"`) raises `LLMRefusal` and is never read as `.content[0]`.
- `cost_of` matches a hand-computed value for a usage object mixing cached + uncached + output tokens.
- No secret/API key is present in `config/global.json` or any committed file; the key is read from the environment only.
