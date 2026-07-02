# Phase-0 — Call-graph Explorer spike (go/no-go)

## DECISION: **GO** (qualified) — n=20, retry-verified 2026-07-02

Mechanical (no-LLM) call-graph BFS reaches genuine **off-stack** regressors that
stack-only cannot. On a 20-case recent-crash-regression corpus (17 scored, 3
skipped for non-code/wrong-commit diffs):

| metric | value | note |
|---|---|---|
| stack_only_recall | 0.588 (10/17) | today's baseline; from crash frames, not searchfox |
| mechanical_recall | 0.647 (11/17) | small lift because most cases are already on-stack |
| **off-stack recall** | **3/7 = 0.43** | **the load-bearing number** — stack-only is 0 here by definition, so **+43 pts** |
| off-stack by distinct regressor | 2/5 = 0.40 | 2031575, 2015967 reached; 2044578/2045635/2014986 missed |

Verified off-stack reaches (exact `Class::method`, hand-checked — not name
collisions): `HTMLEditor::NotifySelectionChanged` (bugs 2049044, 2045172) and
`ConditionVariableImpl::wait_for` (2041703). The n=5 run also reached in-tree
**Rust** (`quad::prepare_clip_task`, webrender).

The 4 off-stack misses are **interpretable blind spots, not tool flakiness**: 2×
layout frame-construction (regr 2014986 → `nsCSSFrameConstructor`), 1× generic
`ipc::FatalError` abort (no static call path), 1× `js::GCData`/wasm (the JS-GC
lane searchfox's C++/Rust graph doesn't cover). These are exactly the lanes the
tiered LLM design routes elsewhere.

**Why "+43 pts but qualified":** clears the README's ≥+30-pts GO bar, but 43% is
not yet a "clear majority" — so mechanical BFS is the **floor**; the LLM navigator
(and complementary tools for the JS-GC / virtual-dispatch lanes) are what push
recall higher. That is the design thesis, now empirically supported.

**Trust:** searchfox threw intermittent 5xx during both runs; a retry-with-backoff
fix (`searchfox_cli._run`) was added and a re-run (`results.big2.json`) produced
**identical** hit/miss outcomes to the first (`results.big.json`) — **no flips**
(`spike/compare_runs.py`). Every off-stack miss had **0 failed queries**; the only
2 residual deterministic-500 cases are both **on-stack** (zero metric impact). So
the numbers are robust, not artifacts.

Not yet run: the cheap-LLM navigator leg (`--mode both`).

---

Throwaway, standalone spike for the one assumption the evidence-agent rebuild
rides on (see `../PLAN.md` §9 and `../plans/00-phase0-callgraph-spike.md`):

> Can a call-graph *neighborhood* built from a crash's stack frames (via
> `searchfox-cli`) **reach the true regressor function even when it is
> off-stack** — and can a *cheap* LLM navigate there without brute force?

It reports one headline number: **off-stack recall (call-graph neighborhood) vs.
stack-only recall (today's baseline)**. Nothing here touches the DB, the RQ
workers, or the `crashclouseau` package.

## Status (draft)

| Piece | File | State |
|---|---|---|
| searchfox-cli adapter | `searchfox_cli.py` | **validated live** — parser confirmed against real `--calls-from` output; extracts symbols + `path#line` refs |
| crash fetch + frames | `crash.py` | frame parse **validated live** on a real nightly crash; fetch via libmozdata ProcessedCrash (the same call the app uses) |
| explorer (mechanical + LLM) | `explore.py` | mechanical BFS **validated live**; the Haiku 4.5 leg is not yet run |
| regressor → changed funcs | `regressor_funcs.py` | drafted; needs libmozdata; `@@`-context heuristic **unverified** per language |
| runner + metric | `run_spike.py` | drafted; needs a curated corpus (+ libmozdata for uuid resolution) |
| corpus miner | `mine_corpus.py` | drafted; BMO scrape heuristic, not yet run |
| corpus | `corpus.example.json` | template — **curation is the real work** |

The searchfox adapter and mechanical explorer ran end-to-end against **live**
searchfox; the crash frame-parse was validated on a real processed crash (the
libmozdata fetch is the same call the app already uses). Still to validate: the
regressor-diff `@@`-context parse, the Haiku leg, and the full recall metric — all
need `pip install -r ../requirements-spike.txt` and a curated `corpus.json`.

## Setup

```sh
python -m venv .venv-spike && . .venv-spike/bin/activate
pip install -r ../requirements-spike.txt
cargo install searchfox-cli          # required -- genuinely new capability (call graph)
# socorro-cli is NOT needed: the spike reads crashes via libmozdata, same as the app
```

Secrets go in **`~/.mozdata.ini`** (your home dir), *not* the repo's `./mozdata.ini`
— the latter is **tracked** in git (URLs are committed to it; the `.gitignore`
entry is vestigial). libmozdata reads both and the home file overrides/merges, so
keep credentials out of the repo. For `--mode llm`, add your Anthropic key to
`~/.mozdata.ini` (read via `libmozdata.config`, like the repo's Bugzilla token); an
`ANTHROPIC_API_KEY` env var also works as a fallback:

```ini
[Anthropic]
api_key = sk-ant-...
```

## Build the corpus (the load-bearing step)

The metric is only as good as the corpus. Two ways to populate `spike/corpus.json`:

1. **Auto-mine a starting set**, then curate by hand:
   ```sh
   python -m spike.mine_corpus --out spike/corpus.mined.json
   ```
   This lists the bugs blocking the `clouseau` alias, pulls each one's
   `regressed_by` + `cf_crash_signature`, and scrapes the regressor's landed hg
   nodes. It leaves `uuid`/`build_window` blank and `off_stack` unknown.
2. **Curate** (required): for each candidate, confirm the regressor function is
   **not** on the crash's stack (that's what makes it a real test), add a
   representative nightly `uuid`, set `build_window`, and set `off_stack: true`.
   Save the kept cases as `spike/corpus.json`. Target **≥ 6** solid off-stack cases.

Case schema (see `corpus.example.json`):
```json
{
  "clouseau_bug": 0, "regressor_bug": 0, "regressor_nodes": ["<hg-short-rev>"],
  "signature": "...", "channel": "nightly", "uuid": "<representative crash>",
  "build_window": {"start": "YYYYMMDD", "end": "YYYYMMDD"},
  "off_stack": true, "notes": "..."
}
```
`uuid` may be left `""` — the runner will try to resolve one from
`signature` + `build_window` via SuperSearch (best-effort; old crashes age out of
Socorro's ~30-day retention, so freezing a `uuid` is safer).

## Run

```sh
# mechanical BFS only (no API key; the decisive searchfox-reachability test)
python -m spike.run_spike --corpus spike/corpus.json --mode mechanical

# add the cheap-LLM navigator, and compare
python -m spike.run_spike --corpus spike/corpus.json --mode both -v
```

Useful flags: `--hops` (mechanical BFS rounds), `--depth` (searchfox `--depth`),
`--budget-queries`, `--repo`, `--resolve` (canonicalise regressor funcs via
`--define`), `--out`.

**Why two modes.** Mechanical BFS is the *upper bound* on what searchfox can reach
and needs no model — if it can't reach the regressor, no LLM will (→ likely
NO-GO, a §7 searchfox hole). The LLM leg tests whether a *cheap* model gets there
selectively. A hit in mechanical but a miss in LLM is a navigation problem, not a
tool problem.

## Reading the result

`run_spike` prints a per-case table and writes `results.json`:

```
 clouseau    regr  stack  mech:nbhd   llm:nbhd
  1900001 1899500      -    HIT/312    HIT/121
  ...
--- aggregate ---
  stack_only_recall: 0.10
  mechanical_recall: 0.70
  mechanical_recall_on_stack_misses: 0.67
  ...
```

- `stack_only_recall` — today's baseline (regressor reachable from on-stack frames).
- `<mode>_recall_on_stack_misses` — **the number that justifies the whole approach**:
  on cases the stack alone missed, did the neighborhood reach the regressor?
- Each hit records a `matched` pair (target → neighborhood symbol). **Eyeball it** —
  last-`::`-component matching can false-positive on common names (`Run`, `get`).

## Go / No-Go

- **GO**: a neighborhood recall materially exceeds `stack_only_recall` on the
  stack-miss subset (e.g. ≥ +30 pts / reaches a clear majority), verified by hand
  on ≥1 case (open the cited neighborhood; confirm the regressor is genuinely
  reachable, not a name collision).
- **NO-GO**: no material gain — searchfox holes (virtual/indirect/IPC/macro/
  cross-language edges, revision drift, `--calls-between` class-granularity)
  dominate. Record which §7 blind spots drove the result.

Write the decision (with the metric table and the blind spots hit) at the top of
this file when the run completes.
