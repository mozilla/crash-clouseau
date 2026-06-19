# 07 — Senior #3: Patch Scout

## Objective
The Patch Scout intersects the call-graph neighborhood (produced by the Call-graph Explorer, unit 06) with the recent-patch window, extending beyond `Changeset.find` — which only sees patches touching files *on the crash stack* — to surface patches touching *any* function in the neighborhood, including off-stack callees/callers. For each resulting candidate changeset it fetches the raw unified diff via `libmozdata.hgmozilla.RawRevision`, identifies which modified functions overlap the neighborhood, and writes a one-line, fully-cited "what changed semantically" note per candidate. Its output is the candidate list (each carrying node/bug/author + modified-function + diff-line citations) handed to the Data-flow Tracer and ultimately the principal.

## Scope
**In scope**
- Read seeds for a UUID from Postgres: `CrashStack.get_by_uuid`, `Score`, `Changeset`, `Node` (on-stack changesets already windowed by `Changeset.find` / `get_scores`).
- Consume the neighborhood map (functions + their files, with searchfox citations) from unit 06's output.
- Off-stack expansion: given neighborhood functions/files, query the recent-patch window for changesets that touched any of those files (reusing `Changeset.find`'s window query semantics over the neighborhood file set, not just the stack file set).
- Obtain each candidate's hunk text, **`@@` enclosing-function names**, touched-identifier set, and cosmetic flag from the **patch-extraction foundation (`#14`)** — this unit no longer fetches or splits diffs itself.
- Match candidate → modified functions **primarily by the `@@` enclosing-function name** (from `#14`, symbol-index-free and drift-robust), corroborated by intersecting changed line numbers with the neighborhood functions' line ranges (`define`, unit 06). Use `#14`'s touched-identifier set ∩ neighborhood symbols as a cheap pre-filter to order which candidates the LLM reads first.
- A single Haiku 4.5 `llm_call(role="patch_scout", ...)` per candidate (or batched per UUID) that, given the diff hunks + neighborhood function list, emits a structured, quote-only `PatchCandidate` (one-line semantic summary + per-claim citations).
- Populate the dossier's `candidates: list[PatchCandidate]` field.

**Out of scope (owned elsewhere)**
- The `llm_call(role, ...)` abstraction, anthropic client, model-per-role config, prompt caching of the shared prefix — **LLM-abstraction sub-plan**.
- The Pydantic dossier types (`Dossier`, `CrashBrief`, `PatchCandidate`, citation types) — **dossier-contract sub-plan**; this unit imports and fills them.
- The `searchfox-cli` Python adapter (subprocess wrapper, permalink/symbol-id capture, `define`/`calls-*` commands) and the neighborhood map structure — **Call-graph Explorer (unit 06)**.
- Crash brief / discarded-crash-field extraction — **Crash Interpreter (unit 05)**.
- Verifying that each cited edge/line actually holds — **Skeptic**; this unit only *cites*, it does not re-verify.
- Final causal verdict, abstain, needinfo draft — **Principal** + **report_bug.py** units.
- The RQ enqueue hook in `update.py` and the dossier-persistence table — **orchestration / persistence** sub-plans (this unit is a function called by the orchestrator, not the orchestrator).

## Externalities

| Name | Kind | Version / Endpoint / Command | Status | Purpose |
|---|---|---|---|---|
| `libmozdata.hgmozilla.RawRevision` | python-lib | `libmozdata>=0.2.12`; `RawRevision.get_url(channel)` → `<repo_url>/raw-rev` (the **base** URL, no node). The full raw-diff URL is `RawRevision.get_url(channel) + "/" + node` | existing | Build the per-channel raw-diff base URL; fetch full diff text for a candidate node by appending `"/" + node` |
| `parsepatch.patch.Patch` | python-lib | `parsepatch>=0.1.3`; `Patch.parse_changeset(base_url, chgset, chunk_size=, file_filter=, skip_comments=True, add_lines_for_new=True)` — internally fetches `'{}/{}'.format(base_url, chgset)` | existing | FLAT-mode parse to get added/deleted/touched **line numbers** per file (cheap, structured) used to map lines→functions; reused via `crashclouseau/patch.py:parse()`. `file_filter` is called with each **filename string** |
| Patch extraction (`#14`) | internal-module | `crashclouseau/agent/patch_extract.py`: `extract(node, channel)` → hunks + `enclosing_functions()` + `touched_identifiers()` + `is_cosmetic()` + `change_tags()` + raw-text accessor | NEW (foundation) | **The single source of diff hunks + the `@@` enclosing-function join key.** Replaces this unit's own diff fetch/split; the `@@` function name is the primary, symbol-index-free match to the neighborhood, and the identifier set is the cheap candidate pre-filter |
| `requests` | python-lib | `requests>=2.34.2` (pinned in requirements.txt) | existing | Fetch the raw unified-diff **text** (hunk bodies) from `RawRevision.get_url(channel) + "/" + node` — parsepatch FLAT gives only line numbers, not hunk text |
| `anthropic` | python-lib | `anthropic` (pin a current floor, e.g. `>=0.40`) | NEW | Python SDK used *only* via the shared `llm_call()` abstraction; not imported directly here |
| `pydantic` | python-lib | `pydantic>=2` | NEW | `PatchCandidate` / citation models defined by dossier unit; imported here to construct/validate |
| Claude Haiku 4.5 | llm-model | model id `claude-haiku-4-5`; $1 / $5 per 1M tok (input/output); 200K ctx | NEW | The "senior" worker model for Patch Scout. Plain call: **no** `effort` (Haiku 4.5 errors on `effort`), **no** extended thinking. Structured output via `messages.parse(model="claude-haiku-4-5", messages=, output_format=PatchScoutResult)` → `response.parsed_output`, or `output_config={"format":{"type":"json_schema","schema":{...,"additionalProperties":false}}}`. Min cacheable prefix on Haiku 4.5 = 4096 tokens |
| searchfox-cli `define` | CLI | `searchfox-cli define <symbol>` (repo selected by adapter) | NEW (via unit 06) | Source of function bodies + **line ranges** used to map diff hunk line numbers to neighborhood functions. Consumed indirectly: this unit reads the neighborhood map unit 06 already built; only re-invokes `define` if a candidate touches a file/function not yet in the map. Exact CLI flag/arg syntax is owned/validated by unit 06 |
| searchfox-cli `calls-from`/`calls-to`/`calls-between` | CLI | same adapter (depth-bounded) | NEW (via unit 06) | Not invoked directly here; the neighborhood (set of functions+files+citations) it produced is the join key for off-stack expansion. Call-graph LIMITS (honest): misses virtual/indirect/function-pointer/template/macro and cross-language (JS↔C++↔Rust) edges, and indexes ~tip not an arbitrary crash build node (revision drift) — these bound recall, not correctness |
| `crashclouseau.models.Changeset` | internal-module | `Changeset.find(filenames, mindate, maxdate, channel)` (models.py:338) — already filters `Node.merge.is_(False)`, returns `{filename: [node, ...]}` or `None`; `Changeset.get_scores(filename, line, chgsets, csid)` (:363); columns `added_lines/deleted_lines/touched_lines/isnew/analyzed` | existing | `find` reused/extended over the **neighborhood file set** to enumerate window candidates beyond on-stack; `get_scores` gives the seed scores already computed |
| `crashclouseau.models.CrashStack` | internal-module | `CrashStack.get_by_uuid(uuid)` (:1264) returns a **tuple** `(res, uuid_info)` where `res = {"frames": [...]}`, each frame carries `filename/function/line/node/stackpos/changesets(OrderedDict)`; columns `filename/function/line/stackpos/node/uuidid` | existing | Read the scored stack (seed) for the UUID |
| `crashclouseau.models.Node` | internal-module | columns `node(String(12))/channel/backedout/merge/bug/pushdate/hgauthor`; `Node.get_ids(revs, channel)` returns `{node: id}` | existing | Per-candidate metadata: bug id, author, pushdate, backed-out flag, merge filter |
| `crashclouseau.models.Score` | internal-module | `Score` (:1140); `(changesetid, crashstackid, score)` | existing | Seed scores to order which candidates the scout reads first (hot-file/seed prioritization) |
| `crashclouseau.models.HGAuthor` | internal-module | `HGAuthor` (:550); columns `email/real/nick/bucketid` | existing | Resolve `Node.hgauthor` (an FK id) → display author for the candidate (later used for needinfo display) |
| `crashclouseau.models.File` | internal-module | `File.get_id`/`File.get_ids`, joined in `Changeset.find` | existing | Filename↔fileid joins for the window query |
| `crashclouseau.utils` | internal-module | `is_interesting_file(filename)`, `short_rev`, `get_file_url`, `get_extension` | existing | `is_interesting_file` as `file_filter` for diff parse; `short_rev` to normalize nodes; `get_file_url` for source citations |
| `crashclouseau.patch.parse` | internal-module | `parse(chgset, channel="nightly", chunk_size=1000000)` → `Patch.parse_changeset(...)` result (FLAT) | existing | Reused to get FLAT line-number map per candidate; this unit adds raw-text fetch alongside |
| `crashclouseau.config` | internal-module | `config.get_ndays()` (backward_lookup_ndays=3), `config.get_ndays_of_data()` (max_ndays=30), `config.get_channels()` | existing | Window bounds (`mindate=push - backward_lookup_ndays`, capped by `max_ndays`); add an `agent.patch_scout` block + readers |
| `crashclouseau.logger.logger` | internal-module | `from .logger import logger` | existing | Logging |
| `crashclouseau.worker` | internal-module | `get_queue(name)` (default `"low"`), `queue.enqueue_call(func=, args=, result_ttl=0)`, queues `high/default/low` | existing | Patch Scout runs inside the agent RQ job; not enqueued standalone (orchestrator owns chaining) |
| lando git2hg | REST-API | `https://lando.moz.tools/api/git2hg/firefox/{hash}` (via `inspector.git2hg`) — returns `""` for vendored/non-Firefox hashes (caches misses) | existing | Only if the neighborhood map carries git hashes (searchfox indexes the git repo `mozilla-firefox/firefox`) needing conversion to hg revs to match `Node.node`; reuse `inspector.git2hg`, do not re-implement |
| hg raw-rev endpoint | data-source | `RawRevision.get_url(channel)` (= `<repo>/raw-rev`) `+ "/" + node` (hg.mozilla.org raw-rev) | existing | The diff text source itself |
| `config/global.json` → `agent.patch_scout` | config | new keys: `max_candidates`, `neighborhood_file_cap`, `model` (`"claude-haiku-4-5"`), `diff_byte_cap`, `enabled` | NEW | Bound off-stack expansion (cap candidates/files), pick the model per role, kill-switch |

## Deliverables

- **`crashclouseau/agent/__init__.py`** — new package marker for the evidence agent (created by whichever unit lands first; this unit creates it if absent).
- **`crashclouseau/agent/patch_scout.py`** — the unit's core. Adds:
  - `expand_candidates(uuid, neighborhood, seed_changesets, channel, mindate, maxdate) -> list[CandidateRef]` — union of seed (on-stack) changesets and off-stack changesets found by running `Changeset.find` over `neighborhood.files` (capped by `agent.patch_scout.neighborhood_file_cap`), de-duplicated by `(node, fileset)`, filtering backed-out via `Node` (merge is already excluded by `Changeset.find`), ordered by max seed `Score` then pushdate.
  - `extraction(node, channel)` — delegate to `agent.patch_extract.extract(node, channel)` (`#14`) for hunk text, `@@` enclosing-function names, touched-identifier set, cosmetic flag, and churn. **This unit no longer fetches or splits diffs itself** (closes the FLAT-vs-raw-text question — see Risks).
  - `modified_functions(extraction, neighborhood) -> list[FuncHit]` — match the patch's `@@` enclosing-function names (from `#14`) to neighborhood functions **by name** (symbol-index-free, drift-robust), corroborated by line-range intersection with unit-06 `define` ranges; each `FuncHit` carries `{function, filename, searchfox_citation, diff_line_cites:[...]}`.
  - `scout_candidate(candidate_ref, crash_brief, llm_call) -> PatchCandidate` — assembles the per-candidate prompt (crash brief as cached prefix + the candidate's hunks + the matched neighborhood functions as the volatile suffix), calls `llm_call(role="patch_scout", ..., output_format=PatchScoutResult)`, and folds the validated result into the dossier's `PatchCandidate` with all citations attached.
  - `run(uuid, dossier, neighborhood, llm_call) -> Dossier` — top-level entry the orchestrator calls; iterates candidates (respecting `max_candidates`), populates `dossier.candidates`.
  - Local Pydantic `PatchScoutResult` (LLM output schema: `node`, `modified_functions: list[str]`, `semantic_summary: str` one line, `change_tags: list[str]` from a closed enum e.g. `free|deref|null|bounds|lock|alloc|other`, `citations: list[Citation]`) — `additionalProperties:false`, no `minLength`/`maxLength`/`minimum`/`maximum`, strict-/structured-output-compatible.
- **`crashclouseau/agent/prompts/patch_scout.md`** — the quote-only system/role prompt (instructs: summarize ONLY from the provided diff text; every claim must cite a diff line or searchfox symbol; one line per candidate; emit JSON only).
- **`crashclouseau/config.py`** — add `get_agent()` / `get_patch_scout_cfg()` readers for the new `agent.patch_scout` block (mirror the existing `_get_global()` accessor style).
- **`config/global.json`** — add the `agent.patch_scout` block (see config externality).
- **`requirements.txt`** — add `anthropic` and `pydantic>=2` floors (shared with other units; add once).
- **`tests/test_patch_scout.py`** — unit tests with a recorded raw diff + a synthetic neighborhood (no live LLM/network).

## Interfaces

**Inputs consumed**
- `uuid` (str) and `channel` (from `UUID`/crash data) — the crash under analysis.
- **DB seeds (read-only):** `CrashStack.get_by_uuid(uuid)` → `(res, uuid_info)` with `res["frames"]` (each frame's `changesets` OrderedDict are the per-frame seed changesets); `Changeset` rows (`added_lines/deleted_lines/touched_lines/isnew`); `Node` (`node/bug/hgauthor/pushdate/backedout/merge`); `Score` (seed scores); `HGAuthor` (display author).
- **Window bounds:** `mindate = candidate_pushdate_window_start`, `maxdate`, derived from build pushdate and `config.get_ndays()` / `get_ndays_of_data()` — same bounds `Changeset.find` already uses on-stack.
- **Neighborhood map** (from unit 06): set of `{function, filename, line_range, searchfox_symbol_id, searchfox_permalink}` for on- and off-stack functions. This is the join key for off-stack expansion.
- **Crash brief** (dossier field, from unit 05): used as the cached prompt prefix.
- **`llm_call`** (from LLM-abstraction unit): `llm_call(role="patch_scout", system=, messages=, output_format=)`.

**Outputs produced**
- Writes dossier field **`candidates: list[PatchCandidate]`**, each with: `node`, `bug`, `author` (display), `pushdate`, `on_stack: bool`, `seed_score: int|None`, `modified_functions: list[FuncHit]` (with searchfox + diff-line citations), `semantic_summary` (one line), `change_tags`, and a per-claim citation list. Off-stack candidates are flagged `on_stack=False` — the central new signal.
- Does **not** write any DB table (read-only on the seed schema); the dossier is persisted by the persistence unit.

**Depends on / feeds**
- Depends on: patch-extraction foundation (`#14`, hunk text + `@@` enclosing functions + identifiers + cosmetic flag), unit 06 (neighborhood map), unit 05 (crash brief), dossier-contract unit (`PatchCandidate`/citation types), LLM-abstraction unit (`llm_call`, model config), config unit.
- Feeds: Data-flow Tracer (consumes each `PatchCandidate` + its `FuncHit`s to pick `(patch, frame)` pairs), Skeptic (re-verifies the cited diff lines / edges), Principal (final verdict over surviving candidates).

## Implementation steps

1. Create `crashclouseau/agent/__init__.py` (if not already created by an earlier unit) and `crashclouseau/agent/patch_scout.py`.
2. Add the `agent.patch_scout` block to `config/global.json` and the `get_agent()` / `get_patch_scout_cfg()` readers in `config.py` (mirror the existing `_get_global()` accessor style).
3. Add `anthropic` and `pydantic>=2` to `requirements.txt` (skip if a sibling unit already added them).
4. Implement `expand_candidates`:
   - Collect seed (on-stack) changeset nodes from `CrashStack.get_by_uuid` (unpack the `(res, uuid_info)` tuple) + each frame's `changesets`.
   - Build the neighborhood file set from the unit-06 map; cap at `neighborhood_file_cap`; pass it to `Changeset.find(neighborhood_files, mindate, maxdate, channel)` (handle the `None` return when the file set is empty) to get window candidates touching off-stack files. Note `Changeset.find` already excludes merge changesets.
   - Union with seeds; filter out backed-out via `Node`; tag each as `on_stack` or off-stack; resolve `bug`/`author` via `Node`+`HGAuthor` (the `Node.hgauthor` column is an FK id into `hgauthors`); order by max `Score` then pushdate; truncate to `max_candidates`.
5. Implement `fetch_diff`: call `crashclouseau.patch.parse(node, channel)` for FLAT line numbers; `requests.get(RawRevision.get_url(channel) + "/" + node, timeout=…)` for raw text (mind the `/` separator — `get_url` returns only `<repo>/raw-rev`); cache per node within a run; truncate body to `diff_byte_cap`; on network error log and degrade to FLAT-only (record a `diff_unavailable` flag so the Skeptic/principal can abstain).
6. Implement `split_hunks` (unified-diff parser: `@@ -a,b +c,d @@ func_context` headers → `Hunk` with new/old start lines and body text) and `modified_functions` (intersect FLAT/hunk line numbers with neighborhood function line ranges; fall back to hunk-header function context; attach searchfox citation from the map; if a touched file/function is absent from the map, optionally re-invoke unit-06's `define` adapter once, bounded by config).
7. Implement `scout_candidate`: build messages with crash brief as the `cache_control:{type:"ephemeral"}` shared prefix (must reach the 4096-token Haiku minimum to actually cache; below that it silently won't cache, which is acceptable) and the candidate-specific hunks+functions as the volatile suffix (kept last); call `llm_call(role="patch_scout", output_format=PatchScoutResult)` (plain — no `effort`, no thinking); validate; reject any returned claim lacking a citation (drop it, log a warning) before constructing `PatchCandidate`.
8. Implement `run`: orchestrate steps 4–7 over the candidate list, fill `dossier.candidates`, return the dossier. Consider one batched LLM call per UUID over all candidates to amortize the cached prefix, falling back to per-candidate on schema-validation failure.
9. Write `crashclouseau/agent/prompts/patch_scout.md` (quote-only, JSON-only, one-line summary, closed `change_tags` enum, cite-or-omit rule).
10. Write `tests/test_patch_scout.py`: a recorded raw diff fixture + synthetic neighborhood → assert `expand_candidates` surfaces an off-stack node not in seeds, `modified_functions` maps a hunk to the right function with a citation, and `scout_candidate` drops uncited claims (mock `llm_call`).

## Risks & open questions

- **Revision drift (§7).** Neighborhood line ranges come from searchfox at ~tip (the git repo), but the candidate diff and the crash build node predate tip. Mapping diff line numbers → neighborhood function ranges can be off. Mitigation: prefer the diff's own hunk-header function context as the primary function identifier and treat the line-range intersection as corroborating, not authoritative; flag low-confidence maps for the Skeptic.
- **git↔hg hash mismatch.** Searchfox/neighborhood may carry git hashes while `Node.node` is hg (a 12-char short rev). Reuse `inspector.git2hg`; vendored/non-Firefox files return `""` (no Node) and must be silently skipped, not treated as candidates. Normalize hg revs through `utils.short_rev` before comparing to `Node.node`.
- **Off-stack candidate explosion.** A hot neighborhood file (e.g. `nsTArray`-class) can match dozens of window changesets and blow the budget. Mitigation: `neighborhood_file_cap` + `max_candidates`, seed-score ordering, and the hot-file/IDF dampener (PLAN §10) to prioritize.
- **FLAT vs raw-text divergence — now owned by `#14`.** Hunk parsing (raw-text-primary, with FLAT line numbers derived/cross-checked) is centralized in the patch-extraction foundation, so the Patch Scout consumes one canonical `extract(node, channel)` instead of reconciling two diff sources here.
- **Diff size vs context/cache.** Large diffs can exceed token budget or push volatile content past the cached-prefix boundary. `diff_byte_cap` + keeping the crash brief (stable) as the cached prefix and hunks last.
- **Searchfox call-graph holes (§7).** If the true off-stack culprit is reached only via a virtual/IPC/cross-language edge invisible to unit 06, it never enters the neighborhood and the scout cannot surface it → downstream abstain. This bounds recall, not correctness.
- Open: should off-stack candidates with **no** seed `Score` (purely neighborhood-derived) be persisted, or only fed forward in-memory? (Persistence unit decides; this unit produces them regardless.)

## Acceptance criteria

- `crashclouseau/agent/patch_scout.py` imports cleanly and `tests/test_patch_scout.py` passes with no live network/LLM (recorded diff + mocked `llm_call`).
- For a crash whose true regressor file is **off-stack**, `expand_candidates` returns that changeset (flagged `on_stack=False`) that `Changeset.find(stack_files, …)` alone does **not** return — demonstrated by a test asserting the off-stack node is present only when the neighborhood file set is passed.
- `fetch_diff` returns both FLAT line numbers (via `crashclouseau.patch.parse`) and raw hunk text (via `RawRevision.get_url(channel) + "/" + node`), and degrades gracefully (sets `diff_unavailable`) on network failure rather than raising.
- `modified_functions` maps at least one hunk to a neighborhood function and attaches a searchfox citation + diff-line cite for it.
- Every `PatchCandidate` in `dossier.candidates` validates against the dossier Pydantic schema, carries a one-line `semantic_summary`, and contains **no** claim without a citation (uncited claims are dropped, with a logged warning).
- The Patch Scout LLM call uses `claude-haiku-4-5` with **no** `effort`/thinking params (Haiku 4.5 errors on `effort`), driven by `agent.patch_scout.model` config (swappable without code change).
- Candidate count is bounded by `agent.patch_scout.max_candidates` and neighborhood files by `neighborhood_file_cap` (verified by a test with an oversized neighborhood).
