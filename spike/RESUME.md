# Phase-0 spike — RESUME (updated 2026-07-02)

Pick-up point for next session. Full narrative: memory `clouseau-phase0-findings`.
Decision block: top of `spike/README.md`.

## State — option (a) DONE, retry-verified → **GO (qualified)**
- Branch `phase0-callgraph-spike`, **STILL UNCOMMITTED** — all of `spike/` +
  `requirements-spike.txt` are untracked on disk (safe, just not checkpointed).
- n=20 recent-crash-regression corpus (`corpus.big.json`; 17 scored, 3 skipped):
  **off-stack recall 3/7 = 0.43 (+43 pts over the 0 stack-only baseline)**, purely
  mechanical (no-LLM) BFS. Verified off-stack reaches: `HTMLEditor::NotifySelection
  Changed` (2049044, 2045172) + `ConditionVariableImpl::wait_for` (2041703); n=5 also
  hit in-tree Rust `quad::prepare_clip_task`. 4 misses are interpretable blind spots
  (2× layout, 1× ipc abort, 1× JS-GC/wasm), not flakiness.
- **Searchfox 5xx mid-run** → added retry-with-backoff to `searchfox_cli._run`
  (offline unit-tested) + `compare_runs.py`. Re-run (`results.big2.json`) = IDENTICAL
  outcomes to first (`results.big.json`), **no flips** → numbers robust.

## Environment (already set up on this machine)
- venv: `~/.venv-clouseau-spike` (anthropic, pydantic, libmozdata)
- CLIs: `searchfox-cli`, `socorro-cli` in `~/.cargo/bin`
- secrets: Bugzilla + Socorro + Anthropic tokens in `~/.mozdata.ini`

## NEXT — pick one
1. **Haiku navigator leg** (cheap-LLM reach vs mechanical brute-force):
   ```sh
   VP=~/.venv-clouseau-spike/bin/python; export PATH="$HOME/.cargo/bin:$PATH"
   cd ~/dev/mozilla/crash-clouseau
   $VP -m spike.run_spike --corpus spike/corpus.big.json --mode both \
       --budget-queries 100 --hops 4 --depth 2 --out spike/results.both.json -v
   ```
2. **Commit the `spike/` checkpoint** (no Claude co-author trailer; never push).
3. **Stop** — go/no-go is answered (GO); move to Phase-1 (production adapter #01).

## Don't regress (all fixed in code — these were hard-won)
- **Seed normalization:** adapter strips signatures/templates (`NS_ProcessNextEvent(
  nsIThread*, bool)` → `NS_ProcessNextEvent`) and reduces Rust to trailing
  `Type::method` (searchfox won't match the full `crate::module::Type::method`).
  Before this fix, 0 seeds resolved and the metric was silently just "regressor on stack".
- **Matcher:** requires full `Class::method` (bare-method matching gave `create`/
  `OnFocus` false positives).
- **Miner:** BMO product = Core+Firefox+Toolkit (Firefox-only hid native crashes →
  258 vs 11 candidates); picks the regressor bug's OWN `Bug <id>` native (C/C++/Rust)
  landing commit, skipping tests/backouts/other-bug commits.
- Only crashes within Socorro's ~12-month window resolve (the old clouseau-alias
  corpus is aged out — do NOT rely on it).

## Files (all in spike/)
searchfox_cli.py · crash.py · regressor_funcs.py · explore.py · run_spike.py ·
mine_corpus.py · classify_misses.py · README.md · corpus.example.json ·
corpus.{recent,cpp,offstack}.json · results.{recent,cpp,offstack}.json
