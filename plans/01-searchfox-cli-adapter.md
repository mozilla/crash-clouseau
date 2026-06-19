# 01 — searchfox-cli adapter (call-graph tool layer)

## Objective
Provide a thin, well-typed Python wrapper around the external `searchfox-cli` binary that every call-graph-using role (Call-graph Explorer, Patch Scout, Data-flow Tracer, Skeptic) invokes via one stable seam. It builds and runs the tool's `--calls-from` / `--calls-to` / `--define` / `-q` (search) **flag-style** commands (and `--calls-between` **if and only if** step 1 confirms the flag exists) against a selected repo, parses the tool's LLM-friendly markdown into typed Pydantic results, and captures the permalink + symbol-id for every node/edge so downstream claims are citable and verifiable. It owns subprocess invocation, timeouts, retries, repo selection, and structured error handling — and nothing about LLMs or dossier assembly.

> **CLI shape (verified against github.com/padenot/searchfox-cli):** the tool uses **flags, not positional subcommands** — e.g. `searchfox-cli --calls-from 'mozilla::Foo::Bar' --depth 2 -R mozilla-central`. The builders must emit this flag form, not `searchfox-cli calls-from <symbol>`.

## Scope
**In scope**
- Locating and invoking the `searchfox-cli` binary (configurable path; installed via `cargo install searchfox-cli`), subprocess management, stdout/stderr capture, exit-code handling.
- Command builders for: `--calls-from`, `--calls-to`, `--define` (definition + full-function source extraction), and text/regex search (`-q`/`--query`). **`--calls-between` is provisional** — its existence as a flag is NOT yet confirmed (see Risks); the builder/method for it is gated on step-1 verification and removed/replaced (e.g. compose from `--calls-from` + `--calls-to`) if the flag does not exist.
- Parsing markdown output into typed results (`SymbolRef`, `CallEdge`, `CallGraph`, `Definition`, `SearchHit`) carrying permalink + symbol-id for citation.
- Repo selection via `-R/--repo` (`mozilla-central` / `mozilla-beta` / `mozilla-release` / `comm-central` / `mozilla-esr*` — the exact tokens searchfox-cli accepts) per call.
- Timeouts, bounded retries with backoff, a `SearchfoxError` taxonomy, and an in-process result cache.
- A small standalone CLI entry point for the Phase-0 spike and for manual debugging.

**Out of scope (owned by other sub-plans)**
- Any LLM call, prompt, model selection, or `llm_call(role, …)` abstraction (LLM-abstraction sub-plan).
- Dossier Pydantic schema, citation validation at the hand-off, and the dossier table (dossier-contract sub-plan).
- Role logic — which symbols to expand, neighborhood pruning, off-stack candidate selection (Call-graph Explorer sub-plan); diff fetch/parse (Patch Scout sub-plan); reading bodies for data flow (Data-flow Tracer sub-plan).
- Crash JSON decoding (Crash Interpreter sub-plan) and RQ wiring/enqueue from `update.py` (worker-integration sub-plan).
- Mapping a crash build node to a searchfox-indexed revision / revision-drift policy (consumed here only as an input parameter; the policy lives in the explorer/tracer sub-plans).

## Externalities

| Name | Kind | Version / Endpoint / Command | Status | Purpose |
|---|---|---|---|---|
| `searchfox-cli` | CLI | Rust binary installed via `cargo install searchfox-cli` (github.com/padenot/searchfox-cli); path resolved from `$SEARCHFOX_CLI` or config `agent.searchfox.bin` (default `searchfox-cli` on `PATH`); **flag-style** invocation: `--calls-from`, `--calls-to`, `--define`, `-q/--query`; repo via `-R/--repo`; depth via `--depth` (default 1); markdown output | NEW | The call-graph / source index engine this whole unit wraps. |
| `pydantic` | python-lib | `pydantic>=2` (add to requirements.txt; not currently present) | NEW | Typed result models (`SymbolRef`, `CallEdge`, `CallGraph`, `Definition`, `SearchHit`). First introduced by this or the dossier sub-plan; pin `>=2` for `model_validate`. |
| `subprocess` (stdlib) | python-lib | stdlib (`subprocess.run`, `TimeoutExpired`, `CalledProcessError`) | existing | Invoke the binary, capture stdout/stderr, enforce per-call timeout. |
| `shutil` (stdlib) | python-lib | stdlib (`shutil.which`) | existing | Resolve/verify the binary path at startup. |
| `re` (stdlib) | python-lib | stdlib | existing | Parse the markdown output (symbol headers, permalink links, indented edge tree). |
| `time` (stdlib) | python-lib | stdlib (`time.monotonic`, `time.sleep`) | existing | Retry backoff and per-call latency metrics. |
| `functools` (stdlib) | python-lib | stdlib (`lru_cache` / manual dict) | existing | In-process cache keyed by `(repo, command, args)` for repeated queries within one worker job. |
| `crashclouseau/config.py` | internal-module | `_get_global()` (verified present); add `get_agent()` / `get_searchfox()` reading the new `agent.searchfox` block in `./config/global.json` | existing (extend) | Source of bin path, default repo, depth caps, timeout, retry counts. |
| `crashclouseau/logger.py` | internal-module | module-level `logger = logging.getLogger()` (verified present) | existing | Structured warning/error logging for failed invocations and retries (mirror `inspector.py` logging style). |
| `crashclouseau/utils.py` | internal-module | `short_rev(rev)` (verified present — truncates to 12 chars) | existing | Normalize any revision/branch identifier passed as an input parameter to 12-char form for consistency with the rest of the pipeline (used only for bookkeeping/labeling; **not** passed to searchfox-cli, which has no per-rev flag). |
| `config/global.json` | config | new top-level `"agent": {"searchfox": {...}}` block | NEW | Holds `bin`, `default_repo`, `max_depth`, `timeout_secs`, `retries`, `retry_backoff_secs`, `cache_enabled`. (No `default_branch` — searchfox-cli has no branch/rev selector beyond `-R/--repo`.) |
| `SEARCHFOX_CLI` (env) | config | env var, overrides config `bin` | NEW | Heroku/Docker override of the binary path (mirrors the `REDIS_URL` os.getenv precedence pattern in `worker.py`). |
| `libmozdata` | python-lib | `libmozdata>=0.2.12` (verified present in requirements.txt) | existing | Not called directly here, but the searchfox repo selection must be consistent with channel→repo conventions used elsewhere; documented as an input contract, not a dependency edge. |
| `anthropic` | python-lib | n/a | NEW (other unit) | **Explicitly not used by this unit** — listed to mark the boundary; no model id, no price tier touched here. (Per the Claude API skill, the seniors map to `claude-haiku-4-5` and the principal to `claude-opus-4-8`, but those choices live entirely in the LLM-abstraction sub-plan.) |

Note on revision drift: this adapter does **not** call Lando `git2hg` (that lives in `inspector.py` as `LANDO_GIT2HG`/`git2hg()`). searchfox indexes **only ~tip** of `github.com/mozilla-firefox/firefox` (post hg→git migration); searchfox-cli exposes **no per-revision query flag**. The adapter therefore accepts a repo (and an advisory `rev`/branch label used only for bookkeeping) as input and records the repo it actually queried, plus the fact that the query is against tip, in each result so callers can do the drift bookkeeping.

## Deliverables

**Create `crashclouseau/searchfox.py`** — the adapter module:
- `class Repo(str, Enum)` — members whose **string values are the exact `-R/--repo` tokens searchfox-cli accepts** (verified set: `mozilla-central`, `mozilla-beta`, `mozilla-release`, `comm-central`, and the `mozilla-esr*` variants present at step 1). **There is no `autoland` tree on searchfox** — do not include it. (If a future need for autoland arises, it is a separate index and out of scope here.)
- Pydantic result types:
  - `class SymbolRef(BaseModel)` — `symbol_id: str`, `pretty: str` (display name), `file: str | None`, `line: int | None`, `permalink: str | None`, `repo: str`, `rev: str | None` (advisory label only).
  - `class CallEdge(BaseModel)` — `caller: SymbolRef`, `callee: SymbolRef`, `depth: int`, `permalink: str | None`.
  - `class CallGraph(BaseModel)` — `root: SymbolRef`, `direction: Literal["from","to","between"]`, `depth: int`, `edges: list[CallEdge]`, `repo: str`, `queried_tip: bool` (always True given no per-rev support), `rev_label: str | None`, `raw_markdown: str`.
  - `class Definition(BaseModel)` — `symbol: SymbolRef`, `source: str` (full function body), `permalink: str | None`, `start_line: int | None`, `end_line: int | None`.
  - `class SearchHit(BaseModel)` — `symbol: SymbolRef | None`, `file: str`, `line: int`, `text: str`, `permalink: str | None`.
- Error taxonomy:
  - `class SearchfoxError(Exception)` (base), `SearchfoxNotFound` (binary missing), `SearchfoxTimeout`, `SearchfoxInvocationError` (nonzero exit, carries stderr+cmd), `SearchfoxParseError` (carries raw markdown), `SearchfoxNoResult` (valid run, empty result — distinct from error, used for the abstain path).
- `class SearchfoxClient`:
  - `__init__(self, bin=None, default_repo=None, timeout=None, retries=None, backoff=None, cache=True)` — resolves bin via env→config→`shutil.which`; raises `SearchfoxNotFound` if unresolved.
  - private `_run(self, args: list[str], repo, rev_label=None) -> str` — builds argv in **flag form** (`[bin, "--calls-from", symbol, "--depth", str(depth), "-R", repo.value, ...]`), runs `subprocess.run(..., capture_output=True, text=True, timeout=…)`, retries on timeout/transient nonzero with backoff, raises typed errors; returns stdout markdown. Caches on `(repo, tuple(args))`.
  - `calls_from(self, symbol, *, repo=None, depth=1) -> CallGraph`
  - `calls_to(self, symbol, *, repo=None, depth=1) -> CallGraph`
  - `calls_between(self, src, dst, *, repo=None, depth=…) -> CallGraph` — **only if step 1 confirms a `--calls-between` flag.** If it does not exist, omit this method and have callers (Skeptic/Explorer) compose a path from `calls_from`/`calls_to`; record this decision in the module docstring.
  - `define(self, symbol, *, repo=None) -> Definition`
  - `lookup(self, name_or_symbol, *, repo=None) -> list[SymbolRef]` (symbol/definition lookup — built on `--define` and/or `-q`, per step 1)
  - `search(self, query, *, regex=False, repo=None, limit=…) -> list[SearchHit]` (uses `-q`/`--query`)
  - `clear_cache(self)`.
  - **Note:** no `rev`/`branch` parameter is plumbed to searchfox-cli (no such flag exists). A `rev_label` may be accepted purely to stamp results for caller-side drift bookkeeping.
- Module-level parsers (pure functions, unit-testable without the binary): `_parse_call_graph(md, direction, depth, repo, rev_label) -> CallGraph`, `_parse_definition(md, repo, rev_label) -> Definition`, `_parse_search(md, repo, rev_label) -> list[SearchHit]`, `_parse_symbol_header(line) -> SymbolRef`, `_extract_permalink(line) -> str | None`.
- `if __name__ == "__main__":` thin argparse CLI (`python -m crashclouseau.searchfox calls-from <symbol> --repo … --depth …`) that maps its own positional verbs to the binary's **flag** invocation and prints parsed JSON — used by the Phase-0 spike and manual debugging.

**Modify `crashclouseau/config.py`**:
- Add `get_agent()` returning `_get_global().get("agent", {})` and `get_searchfox()` returning `get_agent().get("searchfox", {})`, with documented defaults applied in `searchfox.py` (config stays declarative).

**Modify `config/global.json`**:
- Add the `"agent": {"searchfox": { "bin": "searchfox-cli", "default_repo": "mozilla-central", "max_depth": 4, "timeout_secs": 60, "retries": 2, "retry_backoff_secs": 1.5, "cache_enabled": true }}` block. (No `default_branch` key — searchfox-cli has no branch selector.)

**Modify `requirements.txt`**:
- Add `pydantic>=2` (verified not currently present; only if no earlier-merged sub-plan already added it — coordinate so it is added once).

**Create `tests/test_searchfox.py`**:
- Parser tests against checked-in markdown fixtures (`tests/searchfox/*.md`) covering each command, empty results, and malformed output.
- `SearchfoxClient` tests with `subprocess.run` monkeypatched (fake binary) to assert **flag-form** argv construction (e.g. `--calls-from`, `-R`, `--depth`), timeout→`SearchfoxTimeout`, nonzero exit→`SearchfoxInvocationError` + retry behavior, and cache hits.

**Create `tests/searchfox/` fixtures** — captured real markdown for `--calls-from`/`--calls-to`/`--define`/`-q` (and `--calls-between` only if it exists), plus one empty and one malformed sample.

## Interfaces

**Inputs consumed**
- A symbol identifier or function name (string) from the Call-graph Explorer, plus `repo` and `depth`.
- For `calls_between` (if available): source and destination symbols (e.g. an on-stack frame function and an off-stack candidate).
- For `define`: a symbol id (typically obtained from a prior `lookup`/call-graph result) to fetch the full body for the Data-flow Tracer.
- Config: `agent.searchfox.*` from `config/global.json` and the `SEARCHFOX_CLI` env override.
- An advisory `rev_label` for bookkeeping only (never forwarded to the binary).

**Outputs produced**
- Typed result objects (above), each populated with `symbol_id` and `permalink` so the dossier's citation fields can be filled directly.
- Dossier fields this unit **feeds**: each call-path edge's searchfox **permalink** and **symbol-id**; each `define` result's **permalink + start/end line** (when present) for hunk-to-body correlation; `repo`/`queried_tip`/`rev_label` for the revision-drift note. This unit does **not** itself read or write the dossier object or DB rows.
- A clean `SearchfoxNoResult` / `SearchfoxError` distinction so callers can route a missing edge to the **abstain** path (PLAN §7) rather than fabricate one.

**Depends on**
- `config.py` (extended) for settings; `logger.py`, `utils.short_rev`. No dependency on the LLM, dossier, or worker sub-plans.

**Feeds**
- Call-graph Explorer (neighborhood build, off-stack surfacing), Data-flow Tracer (`define` bodies), Skeptic (re-running `--calls-to`/`--calls-from` to independently confirm a claimed edge exists at tip), and the Phase-0 spike script.

## Implementation steps
1. **Pin the searchfox-cli interface (CRITICAL — the rest depends on this).** Install via `cargo install searchfox-cli`, run `searchfox-cli --help`; capture the **exact flag names** for `--calls-from`/`--calls-to`/`--define`/search/`--depth`/`-R/--repo`, the **exact accepted repo tokens**, and **whether `--calls-between` exists at all**. Confirm markdown output shape and that there is no per-rev flag. Save representative outputs into `tests/searchfox/` as fixtures. Update builders + the `Repo` enum + (if needed) drop the `calls_between` method to match reality.
2. Add the `agent.searchfox` block to `config/global.json`; add `get_agent()`/`get_searchfox()` to `config.py`.
3. Add `pydantic>=2` to `requirements.txt` (skip if already present from a sibling sub-plan).
4. Create `crashclouseau/searchfox.py`: define `Repo` enum (verified tokens only) and the five Pydantic result models + the `SearchfoxError` taxonomy.
5. Implement `SearchfoxClient.__init__` with env→config→`shutil.which` bin resolution and `SearchfoxNotFound`.
6. Implement `_run`: **flag-form** argv assembly, `subprocess.run(timeout=…, capture_output=True, text=True)`, retries with backoff on `TimeoutExpired`/transient nonzero exit, typed-error raising, latency logging, and the `(repo, args)` cache.
7. Implement the parsers as pure functions; write them against the fixtures first (parse-then-invoke order keeps them unit-testable without the binary).
8. Implement the public command methods, each applying config defaults and clamping `depth` to `max_depth`.
9. Ensure every parsed `SymbolRef`/edge/definition carries a permalink and symbol-id when present, and set `repo`/`queried_tip`/`rev_label` from the actual invocation.
10. Add the `__main__` argparse CLI emitting JSON (mapping its verbs to the binary's flags).
11. Write `tests/test_searchfox.py` (parsers against fixtures; client with monkeypatched `subprocess.run`).
12. Smoke-run the CLI against a real symbol on `mozilla-central` (e.g. a known on-stack function) before the explorer sub-plan builds on it.

## Risks & open questions
- **`--calls-between` may not exist.** The verified flag set is `--calls-from`, `--calls-to`, `--define`, `-q`. `calls-between` appears in some prose descriptions but was **not** confirmed as a CLI flag. Step 1 must resolve this; if absent, the adapter does not expose `calls_between` and callers compose a path from `calls_from`/`calls_to`.
- **Repo tokens / no autoland.** searchfox trees are `mozilla-central`, `mozilla-beta`, `mozilla-release`, `comm-central`, `mozilla-esr*` — **there is no autoland tree**. The original assumption of an `autoland` repo and bare `beta`/`release` tokens was wrong; the `Repo` enum values must be the exact `-R` tokens.
- **No per-revision query — tip only (PLAN §7).** searchfox-cli has no `--rev` flag; it queries ~tip of the git repo. The adapter cannot pin a crash build node; `queried_tip=True`/`rev_label` must record this so callers don't over-trust an edge that may not exist at the crash build node.
- **Markdown format drift.** searchfox-cli is built for Claude Code and may change its output; parsers must fail loudly with `SearchfoxParseError` carrying the raw markdown rather than silently producing empty graphs. Pin/record the tested CLI (crate) version.
- **Call-graph completeness limits.** The call graph misses virtual/indirect/function-pointer/template/macro and cross-language (JS↔C++↔Rust) edges; it gives EDGES, not data flow. Surfacing/relying on these limits is the Explorer/Tracer's job; this adapter just reports what the tool returned.
- **Permalink/symbol-id availability per command.** Some commands (e.g. search) may not emit a symbol-id for every hit; result models make these `Optional`, and callers route missing-citation results to abstain.
- **Binary provisioning on Heroku.** searchfox-cli must be installed in the deploy image (`cargo install searchfox-cli` in the Dockerfile/buildpack) and needs network access to searchfox.org at runtime — out of this unit's code but a hard runtime prerequisite; flag to the worker-integration/deploy owner.
- **Cache staleness.** In-process cache is per-job and fine for a single crash analysis; do not persist it across jobs (index tracks tip and can move).

## Acceptance criteria
- `from crashclouseau.searchfox import SearchfoxClient` imports with no LLM/dossier deps pulled in.
- `python -m crashclouseau.searchfox calls-from <symbol> --repo mozilla-central --depth 2` (which internally invokes `searchfox-cli --calls-from <symbol> --depth 2 -R mozilla-central`) prints valid JSON with a non-empty `edges` list, each edge carrying `symbol_id` and `permalink`, for a known real function.
- `define <symbol>` returns a `Definition` whose `source` is the full function body and whose `permalink`/`start_line`/`end_line` are populated when the tool provides them.
- If `--calls-between` exists: `calls-between <src> <dst> --depth N` returns a `CallGraph`; for a pair with no path it raises/returns `SearchfoxNoResult` (not a fabricated edge). If it does not exist, this criterion is replaced by an equivalent compose-from-`calls_from`/`calls_to` path check.
- Error paths verified: missing binary → `SearchfoxNotFound`; timeout → `SearchfoxTimeout` after configured retries; nonzero exit → `SearchfoxInvocationError` with stderr+cmd; malformed markdown → `SearchfoxParseError` with raw output attached.
- `pytest tests/test_searchfox.py` passes: all parser-fixture tests and all monkeypatched-`subprocess` client tests (flag-form argv, timeout, nonzero+retry, cache) green; no real binary required in CI.
- `flake8` clean (repo uses `.flake8`); no change to existing pipeline behavior (no edits to `inspector.py`/`update.py`/`models.py` in this unit).
- Config defaults documented in `config/global.json` and overridable via `SEARCHFOX_CLI` / `agent.searchfox.*`.
