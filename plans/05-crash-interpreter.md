# 05 — Senior #1: Crash Interpreter

## Objective
Build a cheap-LLM "senior engineer" agent that converts the raw Socorro processed crash for a UUID into a normalized, fully-grounded **crash brief** — the leading section of the evidence dossier. It decodes the crash signals the current pipeline discards (`reason`, `crash_info.address`, `moz_crash_reason`, per-frame `inlines`/`trust`, `phc_*`, `async_shutdown_timeout`), classifies the failure (UAF-from-poison / null-deref / assertion / OOB / shutdownhang), picks the thread that truly matters, and expands inlines into the stack. Everything downstream (Call-graph Explorer, Patch Scout, Data-flow Tracer, Skeptic, Principal) narrows from this brief, so every field it emits must cite a verifiable artifact (a thread/frame index in `json_dump`, a raw crash field name, or a decoded poison constant).

## Scope
**In scope**
- Fetch the processed crash via the existing `inspector.get_crash_data(uuid)` seam (no new Socorro plumbing).
- Decode the discarded crash fields into typed values: faulting `address` + poison-pattern recognition, `moz_crash_reason`/`reason` parsing, `phc_kind`/`phc_alloc_stack`/`phc_free_stack`, `async_shutdown_timeout`.
- Failure classification (UAF / null-deref / assertion / OOB / shutdownhang / unknown) as a constrained enum, with the evidence each class rests on.
- "Which thread matters" selection: default to `json_dump.crash_info.crashing_thread`, but surface shutdown-hang / main-thread overrides keyed off `async_shutdown_timeout` and thread names.
- Inline expansion: flatten each frame's `inlines` array into pseudo-frames so the call site of an inlined callee is visible, preserving `trust`.
- One cheap-LLM call (`role="crash_interpreter"`) that *normalizes/labels* the pre-extracted facts into the `CrashBrief` Pydantic model; the deterministic Python extractor produces the raw evidence the model is constrained to.
- Emit the `crash_brief` portion of the dossier as a validated Pydantic object.

**Out of scope (owned elsewhere)**
- The `llm_call(role, ...)` abstraction, model-per-role config, prompt caching, the `anthropic` client, and `messages.parse`/structured-output wiring — owned by the **LLM-abstraction sub-plan**. This unit *calls* `llm_call`, it does not build it.
- The top-level `Dossier` Pydantic container, the dossier persistence table, and schema-validation-at-handoff policy — owned by the **Dossier-contract sub-plan** (this unit defines only the nested `CrashBrief` model and conforms to that container).
- `searchfox-cli` invocation / call-graph neighborhood — **Call-graph Explorer** sub-plan.
- Candidate-patch intersection and diffs — **Patch Scout** sub-plan.
- The RQ worker job that orchestrates the seniors and the `update.py` enqueue hook — owned by the **agent-orchestration/worker sub-plan** (this unit exposes a pure `build_crash_brief(uuid)` entry point it can call).
- Reading DB seeds (`CrashStack.get_by_uuid`, `Score`) — orchestration sub-plan; this unit takes only the `uuid` (and optionally the already-fetched processed-crash dict).

## Externalities

| Name | Kind | Version / Endpoint / Command | Status | Purpose |
|---|---|---|---|---|
| `libmozdata.socorro.ProcessedCrash.get_processed(uuid)` | python-lib | `libmozdata>=0.2.12` (in requirements.txt) | existing | Underlying fetch used by `inspector.get_crash_data`; returns `data[uuid]` dict supplying `json_dump`, `reason`, `moz_crash_reason`, `async_shutdown_timeout`, `phc_*`. |
| `crashclouseau.inspector` | internal-module | `get_crash_data(uuid)` → `data[uuid]` | existing | The exact reuse seam mandated by the task; returns the processed-crash dict. |
| `crashclouseau.inspector` | internal-module | `get_path_node(uri)` → `(filename, node)`; `HG_PAT`, `GIT_PAT`, `git2hg(git_hash)` | existing | Reused to resolve per-frame `file` URIs to `(filename, hg-node)`; same hg/git handling as scored frames. NOTE: `get_path_node` returns only `(name, node)` — it does **not** itself compute an `internal` flag. The extractor derives `internal = (node != "")`, exactly as `inspector.inspect_stacktrace` does (line `"internal": node != ""`). |
| `crashclouseau.utils` | internal-module | `short_rev(rev)`, `get_build_date(bid)`, `hash(s)` | existing | Node normalization / build-date parsing reused inside the extractor (all three confirmed present). |
| `crashclouseau.config` | config | `_get_global()` reads `./config/global.json` | existing | Adds `get_agent_*` readers for a new `"agent"` block (poison constants list, model role default). Mirrors existing reader style (`get_ndays()` etc.). |
| `crashclouseau.logger.logger` | internal-module | `logger.info/warning` (root `logging.getLogger()`) | existing | Logging, matching house style. |
| `pydantic` | python-lib | `pydantic>=2.7` (pin in requirements.txt) | NEW | Typed `CrashBrief` / `CrashFrame` models; structured-output target for `messages.parse`. Shared with dossier sub-plan; this unit only *uses* it. |
| `anthropic` | python-lib | `anthropic>=0.40` (pin in requirements.txt) | NEW | SDK used by the LLM-abstraction layer; this unit depends on it transitively via `llm_call`, does not import it directly. |
| `crashclouseau.agent.llm` (`llm_call`) | internal-module | `llm_call(role="crash_interpreter", system=..., user=..., output_format=CrashBrief)` | NEW (other sub-plan) | The single LLM abstraction. It wraps `client.messages.parse(..., output_format=CrashBrief)` and returns `response.parsed_output` (a validated `CrashBrief`); this unit consumes it. |
| Claude Haiku 4.5 | llm-model | model id `claude-haiku-4-5`, price tier $1/$5 per 1M tok, 200K ctx | NEW | The cheap "senior" tier for this role. Plain call: **no `output_config.effort` (Haiku 4.5 errors on `effort`), no extended thinking** (Haiku 4.5 has no thinking). Configured via role→model map, not hard-coded here. |
| Prompt caching | config | `cache_control={"type":"ephemeral"}` on shared prefix (Haiku 4.5 min cacheable prefix = 4096 tok) | NEW | The crash brief's large extracted-facts prefix is cacheable for reuse by sibling seniors; volatile content kept last. Applied by the LLM layer per role. Below 4096 tok the prefix silently won't cache on Haiku. |
| `config/global.json` → `"agent"` block | config | keys: `"agent.models.crash_interpreter"`, `"agent.poison_patterns"`, `"agent.max_frames"` | NEW | Model-per-role + tunable poison-address constants and frame cap (default reuse the `inspect_stacktrace` hard-coded `max_frames = 50`). |
| Socorro processed-crash fields | data-source | `json_dump.threads[N].frames[*].{inlines,trust,file,function,line,module}`, `json_dump.crash_info.{address,crashing_thread}`, top-level `reason`, `moz_crash_reason`, `async_shutdown_timeout`, `phc_kind`, `phc_alloc_stack`, `phc_free_stack`, `java_stack_trace` | existing (fields currently discarded; `crash_info.crashing_thread` is the only one used today) | The raw material decoded into the brief. |
| jemalloc/PHC poison constants | data-source | `0xe5e5e5e5` (freed), `0xe4e4e4e4`/`0xe5..` family, `0x4b4b4b4b`/`0xe1...` jemalloc fill, null `0x0`, small-offset-of-poison | NEW (constants table in config) | Address → failure-class heuristic input; the LLM only *labels*, the constants are deterministic. |

No new REST endpoints are introduced by this unit. (The git→hg lando endpoint `https://lando.moz.tools/api/git2hg/firefox/{hash}` is reached only transitively through the reused `inspector.get_path_node` → `inspector.git2hg`, with `inspector._GIT2HG_CACHE` deduplicating lookups.)

## Deliverables

**New files**

- `crashclouseau/agent/__init__.py` — package marker for the evidence-agent code (shared with sibling sub-plans; create if absent).
- `crashclouseau/agent/schema.py` *(coordinate with dossier sub-plan; this unit owns the crash-brief part)*
  - `class CrashFrame(BaseModel)`: `stackpos:int`, `function:str`, `filename:str`, `line:int`, `module:str`, `node:str`, `internal:bool`, `trust:str|None`, `inlined:bool`, `inline_of_stackpos:int|None`, `original:str|None`. JSON-schema-safe (no `minLength`/`maximum`/recursion; `additionalProperties:false`).
  - `class FailureClass(str, Enum)`: `UAF_POISON`, `NULL_DEREF`, `ASSERTION`, `OOB`, `SHUTDOWNHANG`, `UNKNOWN`.
  - `class CrashSignals(BaseModel)`: decoded `reason`, `address:str|None`, `address_decoded:str|None` (e.g. `"jemalloc freed-poison 0xe5e5e5e5"`), `moz_crash_reason:str|None`, `phc_kind:str|None`, `phc_has_alloc_stack:bool`, `phc_has_free_stack:bool`, `async_shutdown_timeout:str|None`.
  - `class CrashBrief(BaseModel)`: `uuid:str`, `failure_class:FailureClass`, `failure_rationale:str` (must reference the cited signal field), `crashing_thread:int`, `crashing_thread_name:str|None`, `thread_override_reason:str|None`, `signals:CrashSignals`, `frames:list[CrashFrame]`, `evidence:list[str]` (artifact citations: field names / `threads[N].frames[M]` indices).
- `crashclouseau/agent/crash_interpreter.py`
  - `extract_crash_facts(data: dict, uuid: str) -> dict` — pure, deterministic: pulls all discarded fields, decodes the faulting address against the poison table (`decode_address`), runs `inspector.get_path_node` per frame, expands `inlines` into `CrashFrame`s, computes a *candidate* `FailureClass` and thread. No LLM.
  - `decode_address(address: str|None, signals: dict) -> tuple[str|None, FailureClass|None]` — poison-pattern matcher driven by `config` constants.
  - `select_thread(dump: dict, signals: dict) -> tuple[int, str|None, str|None]` — returns `(thread_index, thread_name, override_reason)`; default `crash_info.crashing_thread`, shutdown-hang override on `async_shutdown_timeout`.
  - `expand_inlines(frames: list, build_node: str) -> list[CrashFrame]` — flatten `inlines` preserving `trust` and call order; for each frame call `inspector.get_path_node(frame.get("file"))` → `(filename, node)` and set `internal = (node != "")` locally (the extractor computes `internal`, not `get_path_node`).
  - `build_crash_brief(uuid: str, data: dict | None = None) -> CrashBrief` — the public entry point: calls `extract_crash_facts`, then one `llm_call(role="crash_interpreter", output_format=CrashBrief, ...)` to normalize/label, then re-asserts the deterministic facts (thread index, decoded address, frame list) over whatever the model returned (the model may not override grounded facts). Raises a typed `CrashInterpretError` when `json_dump` is absent (mirrors the `"json_dump" not in data` guard inside `inspector.get_crash_info`, which returns `None` and is what `update.put_report`'s `if res is None: return` keys off).
- `crashclouseau/agent/prompts/crash_interpreter.md` — the senior's system prompt: "quote the index, never assert; classify only from the provided decoded signals; output the schema." Loaded as the cacheable shared prefix.
- `tests/test_crash_interpreter.py` — unit tests over recorded fixtures (see Acceptance).
- `tests/fixtures/crashes/*.json` — saved processed-crash payloads (UAF-poison, null-deref, MOZ_CRASH assertion, OOB, shutdownhang) for offline tests; no network.

**Modified files**

- `crashclouseau/config.py` — add `get_agent_model(role)`, `get_agent_poison_patterns()`, `get_agent_max_frames()` reading the new `"agent"` block (same `_get_global()[...]` pattern as `get_ndays`, `get_max_score`, etc.).
- `config/global.json` — add the `"agent"` block (models map, `poison_patterns`, `max_frames`).
- `requirements.txt` — add `pydantic>=2.7` and `anthropic>=0.40` (shared adds; declare here, owned by LLM sub-plan).

This unit does **not** modify `inspector.py`, `update.py`, or `worker.py` (the enqueue hook is the orchestration sub-plan's edit).

## Interfaces

**Inputs consumed**
- `uuid: str` (the only required input from the orchestrator).
- Processed crash dict from `inspector.get_crash_data(uuid)` (orchestrator may pass it in via the `data` arg to avoid a double fetch). Fields read: `json_dump.threads`, `json_dump.crash_info.{crashing_thread,address}`, per-frame `file`/`function`/`line`/`module`/`inlines`/`trust`, top-level `reason`/`moz_crash_reason`/`async_shutdown_timeout`/`phc_kind`/`phc_alloc_stack`/`phc_free_stack`/`java_stack_trace`.
- Config: model id for `role="crash_interpreter"`, poison-pattern table, frame cap.

**Outputs produced**
- A validated `CrashBrief` object — the `dossier.crash_brief` field. Writes/owns these dossier fields: `failure_class`, `failure_rationale`, `crashing_thread` (+ name/override), `signals.*`, `frames[*]` (including inlined pseudo-frames + `trust`), `evidence[*]` citations.
- Reads no dossier field (it is the first producer).

**Dependency edges**
- **Depends on:** LLM-abstraction sub-plan (`llm_call`), Dossier-contract sub-plan (`Dossier`/validation policy and the `agent/schema.py` module location), config sub-plan.
- **Feeds:** Call-graph Explorer (uses `frames[*].function`/`filename`/`node` and `crashing_thread` to seed its `searchfox-cli --calls-from` / `--calls-to` queries — note searchfox-cli exposes these as **flags** `--calls-from`/`--calls-to`/`--calls-between`/`--define`, not subcommands; that unit owns the invocation), Data-flow Tracer (uses `failure_class` + `signals.address_decoded` + `moz_crash_reason` to frame the "is the freed/nulled value the one the crash site touches" question), and the Principal (the brief is the top of every dossier). Skeptic re-checks the `evidence[*]` frame/field citations.

## Implementation steps

1. Add the `"agent"` block to `config/global.json` (models map with `"crash_interpreter":"claude-haiku-4-5"`, `poison_patterns`, `max_frames`: 50) and the three reader functions in `config.py` (using the existing `_get_global()[...]` accessor pattern).
2. Add `pydantic>=2.7` and `anthropic>=0.40` to `requirements.txt`.
3. Create `crashclouseau/agent/__init__.py` and `crashclouseau/agent/schema.py` with `CrashFrame`, `FailureClass`, `CrashSignals`, `CrashBrief` (JSON-schema-safe: `additionalProperties:false`, no unsupported keywords) — coordinate the module path with the dossier sub-plan.
4. Implement `decode_address`: match the faulting `crash_info.address` against the config poison table (freed-poison `0xe5e5e5e5`, jemalloc fill, near-null small offsets → null-deref); return `(human_string, candidate_class)`.
5. Implement `select_thread`: default to `crash_info.crashing_thread`; if `async_shutdown_timeout` present → `SHUTDOWNHANG` candidate and prefer the main/shutdown thread, recording `thread_override_reason`. Capture the thread name when Socorro provides one.
6. Implement `expand_inlines`: for the selected thread's frames (capped at `max_frames`), reuse `inspector.get_path_node(frame.get("file"))` for `(filename, node)`, compute `internal = (node != "")` in the extractor (do not expect `get_path_node` to return it), then flatten each `inlines` entry into an ordered pseudo-`CrashFrame` carrying `inlined=True`, `inline_of_stackpos`, and the parent's `trust`.
7. Implement `extract_crash_facts`: assemble `CrashSignals` (decoded), the candidate `FailureClass` (from address + `moz_crash_reason` + `reason` + `phc_*`), the selected thread, and the expanded frame list, plus the deterministic `evidence` citations (e.g. `"crash_info.address=0xe5e5e5e5"`, `"threads[N].frames[0]"`). Raise `CrashInterpretError` if `json_dump` missing.
8. Write `crashclouseau/agent/prompts/crash_interpreter.md` (quote-only, classify-from-decoded-signals-only, emit-schema). Mark it as the cacheable prefix for the LLM layer.
9. Implement `build_crash_brief`: fetch via `inspector.get_crash_data` if `data is None`; run `extract_crash_facts`; call `llm_call(role="crash_interpreter", output_format=CrashBrief, system=<prompt+facts prefix>, user=<normalize request>)` (the LLM layer returns the validated `CrashBrief` from `messages.parse(...).parsed_output`); then **overwrite** the model's `crashing_thread`, `signals`, and `frames` with the deterministic facts (model may only fill `failure_class`/`failure_rationale`/labels and is reconciled if it contradicts a decoded signal — prefer deterministic class when they disagree, log the disagreement).
10. Record fixtures: pull 5 real nightly UUIDs (one per failure class) once, save the processed-crash JSON under `tests/fixtures/crashes/`.
11. Write `tests/test_crash_interpreter.py`: deterministic path (`extract_crash_facts`/`decode_address`/`expand_inlines`/`select_thread`) with the LLM mocked; assert classification, thread selection, inline expansion order, and that every `evidence` string points at a field that exists in the fixture.

## Risks & open questions
- **Module-path collision with the dossier sub-plan.** Both touch `crashclouseau/agent/schema.py`; needs a one-line ownership agreement (this unit owns `CrashFrame`/`FailureClass`/`CrashSignals`/`CrashBrief`, dossier sub-plan owns the `Dossier` container). Resolve before coding step 3.
- **Poison-constant accuracy.** The freed-poison `0xe5e5e5e5` is well-known, but jemalloc fill patterns and "small offset of a poison value" (struct-field deref of a freed object) vary by platform/allocator config. Keeping the table in config makes it tunable; mis-tagging is contained because the LLM gets the decoded string as *evidence*, not as truth, and the Tracer/Skeptic re-verify.
- **Inline schema shape.** Socorro's per-frame `inlines` field shape (keys, presence of `function`/`file`/`line`) should be confirmed against the fixtures before finalizing `expand_inlines`; field may be absent on many frames. (The existing `inspect_stacktrace` reads only `file`/`function`/`line`/`module` per frame and never touches `inlines`, so there is no in-repo precedent to copy — confirm purely from the recorded fixtures.)
- **Thread-override heuristic.** "Which thread truly matters" for shutdownhang vs. a crashing-thread that is a watchdog is genuinely ambiguous; v1 only overrides on `async_shutdown_timeout` and records the rationale, leaving harder cases to the default crashing thread (abstain-friendly).
- **Haiku constraints.** Must not pass `output_config.effort` or thinking to `claude-haiku-4-5` (Haiku 4.5 errors on `effort` and has no extended thinking; the LLM layer enforces this per role) — open dependency on that sub-plan landing the role guard.
- **Open:** does the `data` arg get reused from the orchestrator's seed read, or is a second `get_crash_data` acceptable? (Latency-cheap, but a double Socorro hit; prefer pass-through.)

## Acceptance criteria
- `build_crash_brief(uuid)` returns a `CrashBrief` that validates against the Pydantic schema for all five fixture classes, with the LLM mocked and with a live `llm_call` smoke test on one nightly UUID.
- For the UAF fixture: `failure_class == UAF_POISON`, `signals.address_decoded` names the poison constant, and `evidence` cites `crash_info.address`.
- For the assertion fixture: `failure_class == ASSERTION` and `signals.moz_crash_reason` is populated and cited.
- For the shutdownhang fixture: `signals.async_shutdown_timeout` populated, `failure_class == SHUTDOWNHANG`, and `thread_override_reason` set.
- Inlined frames appear in the `frames` list in call order with `inlined=True` and a valid `inline_of_stackpos`, and non-inlined frames carry the same `(filename,node,internal)` that `inspector.inspect_stacktrace` would produce for the same data (parity test against the existing extractor on a shared fixture — including the `internal = (node != "")` derivation and the `max_frames = 50` cap, both of which `inspect_stacktrace` applies).
- Every string in `CrashBrief.evidence` resolves to a real path/field in the source crash dict (test asserts this — the grounding rule).
- `CrashInterpretError` is raised (not an unhandled exception) when `json_dump` is absent, so the orchestration worker can skip cleanly like `update.put_report` does today (`if res is None: return`).
- `pytest tests/test_crash_interpreter.py` passes with no network access (fixtures only); `pip install -r requirements.txt` resolves with the new `pydantic`/`anthropic` pins.

Key file paths: `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/agent/crash_interpreter.py`, `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/agent/schema.py`, `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/agent/prompts/crash_interpreter.md`, `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/config.py`, `/home/calixte/dev/mozilla/crash-clouseau/config/global.json`, `/home/calixte/dev/mozilla/crash-clouseau/requirements.txt`, `/home/calixte/dev/mozilla/crash-clouseau/tests/test_crash_interpreter.py`.
