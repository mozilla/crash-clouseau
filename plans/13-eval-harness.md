# 13 — Evaluation harness

## Objective
Build an offline measurement harness that scores the evidence-agent pipeline against a frozen, human-confirmed corpus of `clouseau`-aliased BMO bugs (their `regressed_by` + `cf_crash_signature`), computing three gating metrics: off-stack call-graph recall vs. today's stack-only recall, evidence correctness for strong-evidence verdicts (did the cited call path/diff line hold, was the named changeset the real regressor), and abstain calibration. Because the Postgres DB hard-deletes anything older than `max_ndays` (30), the corpus must be mined and frozen to disk at harvest time, never re-derived live. Re-runs over the corpus drive the assembled agent (#02's `run_crash_triage`) once per case under bounded concurrency, and these metrics become the gate that every model-tier or threshold change must clear before merge.

> **Substrate (adopt hackbot, see #02):** this harness does **not** call the raw `anthropic` Batch/Messages API. Corpus re-runs drive the assembled agent through #02's `run_crash_triage` (one `asyncio.run` per case, bounded-concurrency `asyncio.gather` across the corpus); each sweep is just an `agent.llm` model-tier override (plus the `eval` block's `confidence_thresholds`), and per-case cost/turns come from the `CrashTriageResult`/`ResultMessage` (`total_cost_usd`) — there is **no** 50%-off Batch discount, **no** `messages.parse`/`output_format`, and **no** `custom_id` remap (each result is associated to its case by construction). The object it deserializes + scores is `CrashTriageResult.model_dump()` (the dossier/verdict), validated per #03.

## Scope
**In scope**
- Mining `clouseau`-aliased bugs from BMO and freezing a self-contained corpus (crash UUIDs, processed-crash JSON, ground-truth regressor node/bug/author, signatures, seed snapshot) to disk.
- A label-derivation step: for each corpus case, mark whether the true regressor function was on-stack or off-stack (the denominator that makes off-stack recall meaningful).
- An async corpus re-runner that drives the frozen corpus through the assembled agent (#02's `run_crash_triage`) once per case under a bounded-concurrency `asyncio.gather`, collecting each case's `CrashTriageResult` directly (no `custom_id` remap — each result is associated to its case by construction).
- Metric computation (off-stack recall, evidence correctness/precision, abstain calibration) and a comparison-vs-baseline report.
- A CLI to run the full loop and a stable JSON metrics artifact that CI / a tuning loop can diff.

**Out of scope (owned elsewhere)**
- The dossier Pydantic schema and the agent substrate (`run_crash_triage` / `AgentDefinition` tiering, #02) — this unit *imports and reuses* them.
- The searchfox-cli adapter and the call-graph Explorer / Patch Scout / Data-flow Tracer / Skeptic seniors and the Principal verdict logic — this unit *invokes the assembled pipeline*, it does not implement agents.
- The dossier+verdict persistence table and UI panel (product-wiring sub-plan) — the harness reads the dossier object/fields, it does not define the table schema.
- Live per-crash enqueue from `update.py` (ingestion sub-plan).
- Needinfo drafting / `report_bug.py` changes.

## Externalities

| Name | Kind | Version / Endpoint / Command | Status | Purpose |
|---|---|---|---|---|
| #02 agent substrate | internal-module | `crashclouseau/agent/triage.py` (`run_crash_triage`) + `claude-agent-sdk` (owned by #02, **not** re-added here) | NEW | The harness imports and drives the assembled agent once per corpus case (`asyncio.run(run_crash_triage(...))`) under bounded concurrency; there is no raw `anthropic`/Batch/`messages.parse` client in this path |
| `pydantic` | python-lib | `pydantic>=2` (NEW dep shared with dossier sub-plan) | NEW | Typed dossier/verdict objects the harness deserializes and validates; typed corpus-record and metrics models |
| `claude-opus-4-8` | llm-model | id `claude-opus-4-8`; $5/$25 per 1M (input/output); 1M ctx; default PRINCIPAL | existing (model)/NEW (use) | Principal verdict model the harness exercises via `run_crash_triage`; `effort` is options-level (#02) and config-driven, not set here |
| `claude-haiku-4-5` | llm-model | id `claude-haiku-4-5`; $1/$5 per 1M; 200K ctx; senior tier | existing/NEW | Senior-role model exercised via `run_crash_triage`; rejects `effort`, no extended thinking |
| `claude-sonnet-5` | llm-model | id `claude-sonnet-5`; $3/$15 per 1M; 1M ctx; data-flow-tracer mid tier | existing/NEW | Mid-tier model that may appear in a tuning sweep |
| `claude-fable-5` | llm-model | id `claude-fable-5`; $10/$50 per 1M; 1M ctx | existing/NEW | Optional hardest-case principal in a sweep config |
| `libmozdata.bugzilla.Bugzilla` | python-lib / internal-module | `libmozdata>=0.2.12`; `Bugzilla(params, bughandler=, bugdata=).get_data()` (pattern from `buginfo.py`); `Bugzilla(bugids=, include_fields=, bughandler=, bugdata=)` for id-based fetch | existing | Mine bugs carrying the `clouseau` alias in `blocked`; fetch `regressed_by`, `cf_crash_signature`, `assigned_to`, product/component |
| `libmozdata.socorro.ProcessedCrash` | internal-module | `socorro.ProcessedCrash.get_processed(uuid)` (wrapped by `inspector.get_crash_data(uuid)`, which returns `data[uuid]`) | existing | Fetch + freeze the processed-crash JSON per corpus UUID so re-runs need no live Socorro |
| `libmozdata.socorro.SuperSearch` | REST-API | `https://crash-stats.mozilla.org/api/SuperSearch/` (filter by `signature`=`=<sig>` + `release_channel`=`nightly`); same endpoint `buginfo.py` hits | existing | Resolve representative nightly crash UUID(s) for a corpus signature when the bug doesn't carry one |
| `libmozdata.hgmozilla.RawRevision` | internal-module / REST-API | `RawRevision.get_url(channel)` returns a **base URL template** (NOT the diff); the raw unified diff is fetched from that URL with the changeset node (e.g. `requests.get(url, params={"node": rev})` / `url%rev` per libmozdata). `patch.py` only uses it via `Patch.parse_changeset(url, chgset, …)` which returns FLAT line *numbers*, not hunk text. | existing | Fetch the regressor changeset's raw diff text to confirm the cited diff line actually exists (evidence-correctness check) — the harness must fetch+parse the raw diff itself; `patch.parse` does not yield hunk text |
| Bugzilla REST | REST-API | `https://bugzilla.mozilla.org/rest/bug` (`include_fields`, `regressed_by`, `cf_crash_signature`, `blocked`, `assigned_to`) | existing | Source of the ground-truth corpus and labels |
| Lando git2hg | REST-API | `https://lando.moz.tools/api/git2hg/firefox/{hash}`; JSON `{"hg_hash": ...}`; 404 ⇒ non-Firefox/vendored commit. Accessed via `inspector.git2hg(git_hash) -> hg_hash str` (returns `""` on 404/error) | existing | Normalize git regressor hashes from BMO `regressed_by` to hg revs for comparison with frames/Nodes |
| `crashclouseau.inspector` | internal-module | `get_crash_data(uuid)`, `inspect_stacktrace(data, build_node)`, `get_path_node(uri)`, `git2hg(git_hash)` | existing | Re-derive stack frames from frozen crash JSON to compute the on-stack/off-stack label |
| `crashclouseau.models` | internal-module | `CrashStack.get_by_uuid` (L1264; returns `(res, uuid_info)`), `Score` (L1140), `Changeset.find(filenames, mindate, maxdate, channel)` (L338), `Changeset.get_scores(filename, line, chgsets, csid)` (L362), `Node.get_id`/`get_ids`/`get_bugid` (L198/L187/L169) — NOTE `Node.hgauthor` is an int FK to the `HGAuthor` table, resolve the author string via a join, not a column read; `UUID.get_info(uuid)`/`get_bid_chan_by_uuid(uuid)`, `Signature.get_reports`, `commit()` | existing | Read the seed snapshot (baseline stack-only candidate set) and resolve nodes/bugs/authors |
| `crashclouseau.config` | internal-module / config | `_get_global()`, `get_ndays_of_data()` (=30); reads `./config/global.json` | existing | Read retention window (corpus must be mined before deletion); host the new `eval` config block |
| `config/global.json` | config | new `"eval"` block: `corpus_dir`, `max_concurrency`, `per_case_timeout_s`, `max_cost_usd_per_case`, `sweep` (role→model overrides), `confidence_thresholds` | NEW | Pin corpus path, re-run concurrency/timeout/cost caps, and the model/threshold knobs each sweep varies |
| `crashclouseau.worker` | internal-module | `get_queue(name="low")` returns `Queue(name, connection=conn, default_timeout=6000)`; enqueue via `queue.enqueue_call(func=…, args=(…), result_ttl=0)` | existing | Not used for the live path here; referenced only so an optional re-run can enqueue the same `run_crash_triage` agent job instead of driving it in-process |
| searchfox-cli | CLI | flag-style options `--calls-from` / `--calls-to` / `--calls-between` (with `--depth`, default 1), `--define`; selects repo (`mozilla-central`/`beta`/`release`/`esr*`/`comm-central` — **no `autoland`**); LLM-friendly markdown out | NEW (ext dep) | Invoked indirectly through the call-graph Explorer during a re-run; the harness does not call it directly but records its citations from the dossier for the correctness check |
| `python-dateutil` | python-lib | `python-dateutil>=2.9.0` (`relativedelta`) | existing | Harvest-window date math, mirroring `models.Node.clean` |
| `requests` | python-lib | `requests>=2.34.2` | existing | Lando + raw-diff fetches during label derivation and correctness checks |

## Deliverables

- **`crashclouseau/eval/__init__.py`** — package marker.
- **`crashclouseau/eval/corpus.py`** — corpus mining + freezing.
  - `mine_clouseau_bugs(start_date, end_date) -> list[dict]`: BMO query (via `Bugzilla(params, bughandler=, bugdata=).get_data()`) for bugs whose `blocked` carries the `clouseau` alias (the value `report_bug.improve` writes as `blocked='clouseau,{bugid}'`, L49); pull `regressed_by`, `cf_crash_signature`, `assigned_to`, product/component.
  - `resolve_uuids(signature, channel="nightly") -> list[str]`: SuperSearch (`signature="="+sig`, `release_channel="nightly"`) for representative nightly UUID(s).
  - `freeze(records, corpus_dir)`: write per-case `processed_crash.json` (from `inspector.get_crash_data`, which returns `data[uuid]`), `regressor` (hg node via `inspector.git2hg`, bug, hgauthor resolved via the `HGAuthor` join), `signature`, and a `seed_snapshot.json` (baseline candidate set from `CrashStack.get_by_uuid` + `Score`) to `corpus_dir`. Emits a `manifest.json` with corpus hash + harvest timestamp (immutability/freeze marker).
  - `CorpusCase` (Pydantic): `uuid`, `signature`, `regressor_node`, `regressor_bug`, `regressor_author`, `crash_json_path`, `seed_nodes`, `on_stack_label` (filled by labeler).
- **`crashclouseau/eval/labels.py`** — `derive_onstack_label(case) -> bool`: re-derive frames via `inspector.inspect_stacktrace(data, build_node)` on the frozen crash JSON (pass the frozen build node so the node-match guard holds), map the regressor changeset's touched files/functions, and decide whether the true regressor function appears on the stack (off-stack = `False`). Produces the denominator for off-stack recall.
- **`crashclouseau/eval/runner.py`** — `rerun_corpus(cases, sweep_config) -> dict[uuid -> CrashTriageResult]`: drives #02's `run_crash_triage` once per case under a bounded-concurrency `asyncio.gather` (semaphore = `eval.max_concurrency`, honoring `per_case_timeout_s`/`max_cost_usd_per_case`), passing `tools_cfg=config.get_agent()` (searchfox tree/repo selection, exactly as #11 does), `recorder=None` (eval records no actions), and the sweep's role→model overrides as the `agent.llm` `llm_cfg`. Each case's result is associated to its `uuid` by construction (no `custom_id` remap). Reuses the assembled agent so the only thing that changes between sweeps is config. (Note: the SDK/bundled-CLI path has **no Batch API**, so re-runs are full-price and paced by local concurrency, not a 50%-off async batch — see Risks.)
- **`crashclouseau/eval/metrics.py`**:
  - `offstack_recall(cases, results) -> dict`: among off-stack-labeled cases, fraction whose agent neighborhood reached the regressor function; plus `stackonly_recall` baseline from `seed_snapshot` (today's `Changeset.find(filenames, mindate, maxdate, channel)` over on-stack files only).
  - `evidence_correctness(cases, results) -> dict`: among strong-evidence verdicts, fraction where (a) named changeset == `regressor_node`, (b) each cited diff line exists in the regressor changeset's raw diff (fetched from the `RawRevision` URL + node, then parsed for line presence — `patch.parse` returns only line numbers, so the harness fetches/parses the raw diff text itself), (c) each cited call edge is present in the dossier citations. Precision of confident verdicts.
  - `abstain_calibration(cases, results) -> dict`: confusion matrix over {abstain, strong} × {findable, unfindable} using the on-stack/off-stack label and seed reachability as the findability proxy.
  - `Metrics` (Pydantic) + `compare_to_baseline(metrics, baseline_path)`.
- **`crashclouseau/eval/run.py`** — CLI entry: `python -m crashclouseau.eval.run mine|label|rerun|score|all [--corpus-dir ...] [--sweep ...] [--baseline ...] [--out metrics.json]`. `all` chains mine→label→rerun→score and writes the stable `metrics.json` artifact.
- **`config/global.json`** — add the `"eval"` block (keys listed in Externalities).
- **`requirements.txt`** — no new dep: `pydantic>=2` is already present (#01) and the `claude-agent-sdk` dep is owned by #02. Do **not** add `anthropic`.
- **`tests/test_eval_metrics.py`** — unit tests for the three metric functions on synthetic dossiers/labels (no network).

## Interfaces

**Inputs consumed**
- BMO: `clouseau`-aliased bugs → `regressed_by`, `cf_crash_signature`, `assigned_to`.
- Frozen processed-crash JSON (from `inspector.get_crash_data`, i.e. `socorro.ProcessedCrash.get_processed(uuid)[uuid]`) — must be captured within the 30-day `max_ndays` retention window.
- Seed snapshot read from DB at harvest time: `CrashStack.get_by_uuid(uuid)` (returns `(res, uuid_info)`; `res["frames"][i]["changesets"][node] = {score, backedout, pushdate, bugid}`) and the on-stack-only candidate set implied by `Changeset.find`.
- The dossier+verdict object produced by the agent (read fields): `verdict`/`confidence` (strong vs abstain), `culprit.node`/`culprit.bug`/`culprit.author`, `call_path[]` edges each with its searchfox citation, `diff_hunks[]` with line cites, the per-claim skeptic pass/fail. The harness **reads** these dossier fields; it never writes them.

**Outputs produced**
- `corpus/manifest.json` + per-case frozen artifacts (the immutable corpus).
- `metrics.json`: `{offstack_recall, stackonly_recall, evidence_precision, abstain_calibration, n_cases, n_offstack, corpus_hash, sweep_config}` — the stable artifact a tuning loop / CI diffs.
- A human-readable comparison-vs-baseline summary returned by `compare_to_baseline`.

**Depends on / feeds**
- **Depends on:** #02 agent substrate (`run_crash_triage`, `agent.llm` model config, `CrashTriageResult`), dossier-schema sub-plan (Pydantic dossier/verdict types), the assembled agent pipeline (all senior roles + Principal + Skeptic), product-wiring sub-plan (the dossier object shape it deserializes).
- **Feeds:** every tuning change — model tiering, confidence/abstain thresholds, prompt/caching changes — gates on these metrics. This is the Phase-4 deliverable in PLAN.md §9 and operationalizes the §8 metric definitions.

## Implementation steps
1. Add the `"eval"` block to `config/global.json` and a `config.get_eval()` reader in `crashclouseau/config.py` (mirror `get_threshold`/`get_ndays_of_data`); no new dep (`pydantic>=2` is present from #01, the `claude-agent-sdk` dep is owned by #02, and there is no `anthropic` client here).
2. Create `crashclouseau/eval/` package; define `CorpusCase`, `Metrics`, and the sweep-config Pydantic models.
3. Implement `corpus.mine_clouseau_bugs` using the `Bugzilla(params, bughandler=, bugdata=).get_data()` pattern from `buginfo.py`, filtering `blocked` on the `clouseau` alias and requesting `regressed_by` + `cf_crash_signature` (+ `assigned_to`, product/component) via `include_fields`.
4. Implement `corpus.resolve_uuids` (SuperSearch, nightly) and `corpus.freeze` — write processed-crash JSON, regressor (git→hg via `inspector.git2hg`, author string via the `HGAuthor` join), signature, seed snapshot (`CrashStack.get_by_uuid`), and `manifest.json` with a content hash + harvest timestamp.
5. Run the miner **now / on a schedule** so the corpus is frozen before `Node.clean`/`UUID.clean` delete data past `max_ndays` (30); document that re-mining old cases is impossible.
6. Implement `labels.derive_onstack_label` reusing `inspector.inspect_stacktrace(data, build_node)` on the frozen JSON to classify on-stack vs off-stack regressor.
7. Implement `runner.rerun_corpus`: drive `asyncio.run(run_crash_triage(...))` per case under a bounded-concurrency `asyncio.gather` (semaphore = `eval.max_concurrency`), passing `tools_cfg=config.get_agent()` + `recorder=None` (as #11, minus action recording) and the sweep's role→model overrides as `llm_cfg`; enforce `per_case_timeout_s`/`max_cost_usd_per_case`; associate each `CrashTriageResult` to its case `uuid` directly (no `custom_id`). Prompt caching across senior calls is SDK/CLI-managed (#02).
8. Implement `metrics.offstack_recall` (+ `stackonly_recall` baseline from the seed snapshot), `evidence_correctness` (changeset match, diff-line existence via the raw `RawRevision` diff fetched by node, citation presence), and `abstain_calibration`.
9. Implement `run.py` CLI (`mine|label|rerun|score|all`) writing the stable `metrics.json`; add `compare_to_baseline`.
10. Write `tests/test_eval_metrics.py` with synthetic dossiers covering: off-stack hit/miss, strong-evidence correct/incorrect changeset, fabricated diff line, abstain on unfindable vs findable.
11. Establish a baseline `metrics.json` from the current pipeline and commit it as the gate reference.

## Risks & open questions
- **Corpus is perishable.** The 30-day retention (`max_ndays`) means the seed snapshot and processed crashes for older `clouseau` bugs may already be gone; mining must run continuously to accumulate a meaningful corpus, and historical cases cannot be reconstructed. Open: do we add a cron/RQ schedule for the miner, or run it manually?
- **Small / imbalanced corpus.** Nightly `clouseau` bugs are rare; the off-stack subset (the denominator that matters most) may be tiny, making recall noisy. Open: minimum N before metrics gate a merge?
- **Ground-truth quality.** `regressed_by` is human-set and sometimes wrong, multi-commit, or points at a merge (note `Changeset.find` already excludes `Node.merge` rows); a bug may carry several signatures. Open: how to handle multi-regressor / multi-signature cases — count partial credit?
- **Revision drift.** Searchfox indexes ~tip while the crash is an older nightly build node (PLAN.md §7); the regressor function may have moved/renamed, so on-stack labeling and diff-line existence checks need fuzzy file/line mapping, not exact match.
- **git2hg gaps.** `inspector.git2hg` returns `""` for non-Firefox/vendored commits (404) and on transient errors; a `regressed_by` hash that doesn't resolve to an hg rev must be handled (skip vs. retry), or the case is silently dropped from comparisons.
- **Findability proxy is circular.** Abstain calibration needs a "was it genuinely unfindable" oracle; using seed reachability as the proxy risks rewarding the agent for the seed's blind spots. Open: define findability independently of the agent.
- **Re-run determinism & cost.** LLM outputs vary run-to-run; metrics may need multi-seed averaging or a fixed low `effort` (options-level, #02) to be stable enough to gate on. And because the SDK/bundled-CLI path has **no Batch API**, corpus re-runs are full-price and paced by `eval.max_concurrency` (each case spawns a Claude Code CLI subprocess), not a 50%-off async batch — budget sweeps accordingly and cap per-case spend via `max_cost_usd_per_case`. (`effort` is settable on Opus 4.8 / Sonnet 5 / Fable 5; Haiku 4.5 rejects `effort` and has no extended thinking.)

## Acceptance criteria
- `python -m crashclouseau.eval.run all` mines, freezes, labels, re-runs the corpus via `run_crash_triage`, collects, and writes a `metrics.json` containing `offstack_recall`, `stackonly_recall`, `evidence_precision`, and `abstain_calibration` with non-trivial `n_cases`/`n_offstack`.
- The frozen corpus is self-contained: re-running `rerun|score` works with no live Socorro/BMO calls (only the agent's own tool calls — searchfox-cli — plus a raw-diff/Lando fetch for correctness checks), proving immutability and reproducibility.
- `offstack_recall` and `stackonly_recall` are reported side by side so the off-stack improvement is a single comparable number (the metric that justifies the approach, PLAN.md §8.1).
- `evidence_correctness` flags at least the three failure classes in tests: wrong changeset, fabricated/absent diff line, missing call-edge citation.
- A bounded-concurrency re-run of N corpus cases returns N `CrashTriageResult`s each correctly associated to its case `uuid` (no cross-case contamination under `asyncio.gather`); a concurrent-fixture test passes.
- `tests/test_eval_metrics.py` passes under flake8 (`.flake8`) and the repo's test runner with no network access.
- A committed baseline `metrics.json` exists and `compare_to_baseline` produces a clear pass/regress verdict that a tuning change can be gated against.

Key grounded paths: `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/models.py` (`CrashStack.get_by_uuid` L1264, `Changeset.find` L338, `Changeset.get_scores` L362, `Score` L1140, `Node.clean` L178, `Node.get_bugid` L169, `Node.hgauthor` FK L137), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/inspector.py` (`get_crash_data` L58, `inspect_stacktrace` L143, `git2hg` L31, `get_path_node` L125), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/buginfo.py` (Bugzilla query pattern), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/patch.py` (`RawRevision.get_url` + `Patch.parse_changeset`, FLAT line numbers only), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/report_bug.py` (`blocked='clouseau,{bugid}'` L49), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/worker.py` (`get_queue` L32, `default_timeout=6000` L35), `/home/calixte/dev/mozilla/crash-clouseau/crashclouseau/config.py`, `/home/calixte/dev/mozilla/crash-clouseau/config/global.json` (`max_ndays`=30), `/home/calixte/dev/mozilla/crash-clouseau/requirements.txt`.
