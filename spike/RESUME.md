# Phase-0 spike — RESUME (updated 2026-07-02)

Pick-up point for next session. Full narrative: memory `clouseau-phase0-findings`.
Decision block: top of `spike/README.md`.

## State — option (a) DONE, retry-verified → **GO (qualified)**
- **COMMITTED** 2026-07-02 as `4701168` on branch `phase0-callgraph-spike` (unsigned
  — env had no tty for the gpg passphrase; `git commit --amend -S` to sign; NOT
  pushed). Run logs + `corpus.dryrun.json` are gitignored (`spike/.gitignore`).
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

## Navigator tiers DONE (2026-07-02) — Sonnet 5 is the pick
Off-stack recall (n=7): mechanical 3/7, Haiku 1/7, **Sonnet 5 4/7**, Opus 4.8 default
1/7, Opus+persistence 2/7; UNION 5/7. Binding factor = exploration PERSISTENCE
(Haiku/Opus quit early `no-proposals`; Sonnet runs to `fixpoint`). Mechanical BFS stays
complementary → production wants BOTH. Tables in `README.md` (top). Compare with
`python -m spike.tiers --mechanical results.big2.json --tier NAME=results.<x>.json ...`.

## NEXT — pick one
1. **Stop** — Phase-0 fully answered (GO; Sonnet-5 navigator + mechanical-BFS floor).
   Move to Phase-1: production adapter #01/#02 — default the navigator to Sonnet 5, keep
   a mechanical-BFS fallback, make the searchfox adapter model-aware + retry (both done
   here in `searchfox_cli.py` / `explore.py`).
2. Deeper navigator work: larger curated corpus, or Sonnet-5 + persistence directive
   (already wins at 4/7 — see if it closes the last off-stack misses).

(This file documents its own commit state, so it stays one working-tree edit ahead of
the latest spike commit — that's expected.)

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
Code: searchfox_cli.py (retry) · crash.py · regressor_funcs.py · explore.py
(model-aware LLM leg) · run_spike.py · mine_corpus.py · classify_misses.py ·
compare_runs.py · head_to_head.py · tiers.py · README.md
Corpus: corpus.big.json (the 20-case set) · corpus.{recent,cpp,offstack,example}.json
Results: results.big2.json (mechanical, retry-verified) · results.both.json (mech+Haiku)
· results.sonnet-5.json · results.opus-4-8.json · results.opus-tuned.json
