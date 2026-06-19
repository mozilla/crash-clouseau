# 14 — Patch extraction upgrade (hunks, enclosing function, identifiers, cosmetic flags)

## Objective
Today `crashclouseau/patch.py:parse()` runs `parsepatch` in FLAT mode and keeps only
**line numbers** (`added_lines`/`deleted_lines`/`touched_lines` on `Changeset`). This
unit makes patch extraction first-class: parse each candidate changeset's **raw
unified diff once** and capture, per file/hunk, the **added/deleted text**, the
**enclosing function from the `@@` hunk header** (a per-hunk function name *for free*,
with no symbol index), a **touched-identifier set**, a **cosmetic/whitespace-only
flag**, churn counts, and file-level metadata (rename/copy/new/deleted/binary). It is a
shared foundation: the Patch Scout (`#07`) and Data-flow Tracer (`#08`) consume it
instead of each re-fetching and re-splitting diffs, and it independently improves the
*current* line-proximity scorer (cosmetic down-weight, deleted-guard detection).

## Scope
**In scope**
- A single canonical hunk parser: fetch a changeset's raw unified diff once, split into
  per-file/per-hunk structures with old/new start lines, the `@@` enclosing-function
  context, and added/deleted line text.
- Cheap derived extractions over the hunks (no LLM, no searchfox): enclosing-function
  set per file, touched-identifier set, cosmetic-only flag, per-file churn counts,
  file-level metadata (rename/copy/new/deleted/binary/mode-only).
- A persisted, *small* derived index (functions / identifiers / cosmetic flags / churn)
  keyed by changeset+file, plus a lazily-fetched-and-cached raw-hunk-text accessor for
  when an agent actually needs the bytes.
- Verify on **real** hg `raw-rev` output whether the `@@` header carries the
  function-context suffix; provide the regex extractor and a graceful no-context path.
- A clean Python API the seniors and the current scorer call.

**Out of scope (owned elsewhere)**
- Mapping enclosing functions to the searchfox **call-graph neighborhood** and the
  off-stack candidate union — **Patch Scout (`#07`)** (this unit only hands it the
  function/identifier index).
- Reading function *bodies* / data-flow reasoning — **Data-flow Tracer (`#08`)**
  (this unit provides the diff hunks + cites; bodies come from `searchfox define`).
- Semantic *classification* of a change into a crash-mechanism class (UAF/null/…) via
  LLM — that is a senior's job; this unit emits only cheap regex `change_tags` as a
  pre-filter, never the verdict.
- The dossier Pydantic types, the `llm_call` abstraction, persistence of the dossier
  itself — their own sub-plans.

## Externalities

| Name | Kind | Version / Endpoint / Command | Status | Purpose |
|---|---|---|---|---|
| `libmozdata.hgmozilla.RawRevision` | python-lib | `libmozdata>=0.2.12`; full raw-diff URL = `RawRevision.get_url(channel) + "/" + node` (note: `get_url` returns only the base `<repo>/raw-rev`) | existing | Fetch the raw unified-diff **text** for a node (the single source of hunk bodies + `@@` headers) |
| `parsepatch.patch.Patch` | python-lib | `parsepatch>=0.1.3` (in requirements.txt); FLAT `Patch.parse_changeset(base_url, chgset, file_filter=, skip_comments=True)` | existing | Keep for back-compat line numbers / `Changeset.add_analyzis`; investigate whether parsepatch exposes a hunk-text mode — if not, parse hunks from the raw diff here (single source of truth) |
| `requests` | python-lib | `requests>=2.34.2` (in requirements.txt) | existing | HTTP GET the raw diff text from the raw-rev URL; timeout + degrade on failure |
| **NO new Python deps** | python-lib | — | — | This unit needs nothing beyond what's already installed — a deliberate property (it also helps the current scorer with zero new deps) |
| `crashclouseau.patch.parse` | internal-module | `parse(chgset, channel, chunk_size)` → FLAT result | existing | Reused for line numbers; this unit adds the hunk/text/derived layer alongside (or derives the FLAT lists from the hunks) |
| `crashclouseau.models.Changeset` | internal-module | `add_analyzis(data, nodeid, channel)` (~:320); columns `added_lines/deleted_lines/touched_lines/isnew/analyzed`; `to_analyze()` | existing | The existing per-changeset analysis store; extend it to also persist the derived patch index (see Deliverables) |
| `crashclouseau.models.File` | internal-module | `File.get_id`/`get_ids` (filename↔id) | existing | Key the derived index by `(changesetid, fileid)` |
| `crashclouseau.utils` | internal-module | `is_interesting_file(filename)`, `get_extension(filename)`, `short_rev` | existing | `file_filter` for the diff; extension→language for the identifier tokenizer's keyword set |
| `crashclouseau.config` | internal-module | `_get_global()` reader of `config/global.json`; existing `get_ndays`/`get_max_score` style | existing | Add `agent.patch_extraction` readers |
| `crashclouseau.logger.logger` | internal-module | `from .logger import logger` | existing | Logging / degrade-on-fetch-failure |
| new DB: `patch_file` / `patch_func` | data-source | new SQLAlchemy table(s), keyed by `(changesetid, fileid)`; FK→`Changeset`/`File` with `ON DELETE CASCADE` so the existing 30-day `Node.clean`/`UUID.clean` cascade purges them | NEW | Persist the *small* derived index: enclosing-function names, touched-identifier set, cosmetic flag, churn counts, file metadata. **Hunk text is NOT persisted** — fetched lazily and cached per run |
| `config/global.json` → `agent.patch_extraction` | config | new keys: `diff_byte_cap`, `min_identifier_len`, `enabled`, `persist_index` | NEW | Bound diff size, tune the identifier tokenizer, kill-switch |

## Deliverables

**Create — `crashclouseau/agent/patch_extract.py`** (the `crashclouseau/agent/` package is created by whichever foundation lands first):
- `fetch_raw_diff(node, channel) -> str | None` — `requests.get(RawRevision.get_url(channel) + "/" + node, timeout=…)`; truncate to `diff_byte_cap`; return `None` (logged) on failure so callers degrade rather than raise. Cache per `(node, channel)` for the lifetime of a run.
- `parse_hunks(raw_diff_text) -> list[FileDiff]` — unified-diff parser producing, per file: `FileDiff(filename, old_path, status, is_binary, mode_only, hunks=[Hunk(old_start, new_start, enclosing_function, added_lines:[(lineno,text)], deleted_lines:[(lineno,text)])])`. `status ∈ {modified, added, deleted, renamed, copied}` from the `diff --git` / `rename from` / `copy from` / `new file` / `deleted file mode` markers; `enclosing_function` from the `@@ ... @@ <ctx>` suffix via `^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@(?: (.*))?$` (empty string when hg emits no context).
- `enclosing_functions(file_diffs) -> dict[str, list[str]]` — per file, the de-duplicated enclosing-function names touched. **The primary, symbol-index-free join key to the call-graph neighborhood (`#07`).**
- `touched_identifiers(file_diffs, lang) -> set[str]` — tokenize `[A-Za-z_]\w*` from added+deleted text, drop the language's keywords and identifiers shorter than `min_identifier_len`. Cheap candidate pre-filter (`#07`) and a corroborating signal for assertion/symbol matches.
- `is_cosmetic(hunk) -> bool` and `file_is_cosmetic(file_diff) -> bool` — true when every changed line is whitespace/brace-only after normalization (comments are already dropped by `skip_comments=True` upstream, so the residual is reflows/reindents). Treat renames/mode-only as down-weight, not exclude.
- `change_tags(file_diffs) -> set[str]` — cheap regex pre-tags from a **closed** set `{free, deref, bounds, lock, null_check, alloc, assert, other}` (e.g. `free`/`delete`/`Release`/`RefPtr`/`Drop`; `if (!x)`/`MOZ_ASSERT`; `[`/`length`/`size`). A pre-filter/feature, never a verdict.
- `churn(file_diffs) -> dict` — per-file added/deleted counts, hunk count, files-touched total (L-SZZ feature; also cheap from FLAT).
- `extract(node, channel) -> PatchExtraction` — the top-level: fetch → parse → derive; returns a dataclass with all of the above. Pure/cacheable; no DB write.
- `persist(changesetid, node, channel)` — compute `extract(...)`'s *derived index only* (functions/identifiers/cosmetic/churn/metadata — NOT the hunk text) and upsert into `patch_file`/`patch_func`. Hooked into `Changeset.add_analyzis` flow (or called right after it) so the index is built when patches are analyzed.

**Modify**
- `crashclouseau/models.py` — add `PatchFile` / `PatchFunc` model(s) keyed by `(changesetid, fileid)`, FK→`Changeset`/`File` with `ondelete="CASCADE"`; helper `PatchFile.get_for_node(node, channel)`. (Migration runs with the existing `bin/migrate_*` one-off pattern — no Alembic in repo.)
- `crashclouseau/config.py` — add `get_patch_extraction_cfg()` (mirror `_get_global()` accessors).
- `config/global.json` — add the `agent.patch_extraction` block.
- *(optional, current-scorer win)* `crashclouseau/models.py:get_scores` / `utils` — consume `file_is_cosmetic` to down-weight cosmetic touches and `change_tags`/deleted-guard to lightly boost lifetime/guard changes. Gated behind the eval harness (`#13`); ships independently of the agent.
- `tests/agent/test_patch_extract.py` — recorded raw-diff fixtures (a real Firefox changeset with `@@` function context; a rename; a cosmetic reindent; a deletion of a null check) → assert enclosing-function extraction, identifier set, cosmetic flag, and tags. No live network.

## Interfaces
**Inputs:** a changeset `node` + `channel` (from the candidate set / `Changeset`); the file extension (→ language, for the tokenizer).
**Outputs:**
- In-memory `PatchExtraction` (hunks + derived signals) for an agent run.
- Persisted derived index (`patch_file`/`patch_func`) for cheap cross-call reuse.
- Lazily-fetched, run-cached raw hunk text on demand.

**Depends on:** nothing in the agent stack — only existing `libmozdata`/`parsepatch`/`models`/`config`. It is a **foundation** that can be built in parallel with `#01`/`#02`/`#03`, before the seniors.
**Feeds:**
- **`#07` Patch Scout** — uses `enclosing_functions()` as the primary neighborhood join key (replacing the searchfox line-range intersection as the *primary* function matcher), `touched_identifiers()` as a pre-filter, and `change_tags()`/cosmetic as candidate features. This **resolves `#07`'s open "FLAT-vs-raw-text divergence" question** — `#07` stops fetching/splitting diffs itself.
- **`#08` Data-flow Tracer** — `_anchor_diff` reads hunk text + cites from this unit instead of re-fetching `RawRevision`. **Resolves `#08`'s open question** "whether Patch Scout already attaches raw hunk text."
- **The current line-proximity scorer** — cosmetic down-weight + deleted-guard, no agent machinery required.

## Implementation steps
1. **Verify the `@@` function context first.** Pull the raw diff for 3–5 real Firefox changesets (C/C++, Rust, JS) via `RawRevision.get_url("nightly") + "/" + node` and check whether the `@@` headers carry the enclosing-decl suffix (hg only emits it for git-format/`showfunc` diffs). This gates the whole "function for free" claim — if absent for a language, that language falls back to no-context and relies on searchfox line ranges in `#07`. Record findings as fixtures.
2. Create `crashclouseau/agent/patch_extract.py`; implement `fetch_raw_diff` (single GET, `diff_byte_cap`, degrade-not-raise, per-run cache).
3. Implement `parse_hunks` + the `FileDiff`/`Hunk` dataclasses, including the `diff --git`/rename/copy/new/deleted/binary/mode-only markers and the `@@`-context regex.
4. Implement the cheap derived extractors: `enclosing_functions`, `touched_identifiers` (per-language keyword sets keyed on `utils.get_extension`), `is_cosmetic`/`file_is_cosmetic`, `change_tags` (closed enum), `churn`.
5. Add the `PatchFile`/`PatchFunc` model(s) + `get_for_node`; write `persist()` and hook it into the existing patch-analysis path (`Changeset.add_analyzis`), persisting **only the derived index**.
6. Add the `agent.patch_extraction` config block + `get_patch_extraction_cfg()`.
7. Wire consumers: update `#07` to import `enclosing_functions`/`touched_identifiers`/raw-text accessor (drop its `fetch_diff`/`split_hunks`); update `#08`'s `_anchor_diff` to read from here.
8. *(optional)* Wire the current scorer: down-weight cosmetic touches in `get_scores`, behind a config flag, validated on `#13`.
9. Tests with recorded fixtures; `flake8`/`black`; no import-time DB/network.

## Risks & open questions
- **`@@` function context is a heuristic, not a parser.** It comes from the diff tool's `xfuncname` regex: can be blank, name an *outer* function for nested/lambda code, and is weaker for Rust/JS than C/C++. Treat it as a strong hint *verified by searchfox* in `#08`, never as ground truth. Step 1 measures how often it's present/right per language.
- **hg may not emit function context at all.** If mozilla's `raw-rev` produces plain (non-git, no `showfunc`) diffs, the suffix is absent and the "function for free" win evaporates for everyone — then `#07` must rely on the searchfox line-range intersection as primary. Step 1 is the go/no-go for this specific claim (independent of the `#00` call-graph go/no-go).
- **Revision drift.** Enclosing-function names and line numbers come from the *patch* (build-accurate), but matching them to searchfox neighborhood functions indexed at ~tip can still mismatch on heavily-churned files. Function *name* match is far more drift-robust than line-range match — which is exactly why this unit elevates the `@@` name to primary.
- **Persistence size.** Persisting full hunk text per changeset is heavy and short-lived (30-day clean); persisting only the small derived index + lazily fetching text is the chosen tradeoff. Open: is the derived index even worth persisting vs recomputed per run? (Cheap to recompute; persist mainly to share across the agent and the current scorer.)
- **Identifier-tokenizer noise.** Common identifiers (`i`, `mFoo`, `index`) add noise; `min_identifier_len` + keyword filtering mitigate, but the identifier-intersection is a *prioritizer*, not a gate.
- **parsepatch hunk mode unconfirmed.** Whether `parsepatch` can emit hunk text directly (avoiding a second fetch) is unverified; default to parsing the raw diff ourselves, treat parsepatch-hunk-mode as an optimization to confirm later.

## Acceptance criteria
- For a recorded Firefox C/C++ changeset whose `@@` headers carry function context, `enclosing_functions()` returns the correct touched-function name(s) with no symbol index / searchfox call.
- `parse_hunks` correctly classifies a rename, a new file, a binary change, and a normal modification from real fixtures.
- `file_is_cosmetic` returns true for a pure reindent fixture and false for a one-line logic change; a deleted `if (!ptr) return;` is surfaced in `change_tags` as `null_check`/`deref`.
- `touched_identifiers` returns a sane set (keywords + sub-`min_identifier_len` tokens excluded) for a fixture.
- `extract(node, channel)` degrades to a populated-but-text-empty result (logged) when the raw-diff fetch fails, and never raises.
- `#07` and `#08`, after wiring, contain **no** independent raw-diff fetch/split logic — both go through this unit (their prior open questions are closed).
- Tests pass with no live network; `flake8`/`black` clean.
