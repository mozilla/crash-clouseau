# How Firefox Crash Regressors Are Identified — and What crash-clouseau Can Automate

Generated from `spike/collect_regressor_dataset.py` + the `regressor-strategy-mining` workflow 
(289 FIXED crash-regression bugs, 2025-07-21 .. 2026-07-21, all products; 289 classified by Opus-4.8/max-effort agents, 0 dropped).

Raw per-bug classifications + reports: `spike/regressor_dataset/strategy_analysis.json`. Dossiers: `spike/regressor_dataset_compact/`.

## Deterministic stats

**Primary strategy (how the regressor was actually pinned):**

| strategy | n | off-stack | on-stack |
|---|---:|---:|---:|
| stack_line_hit | 109 | 0 | 109 |
| bisection | 50 | 12 | 34 |
| keyword_domain_match | 31 | 23 | 8 |
| searchfox_reasoning | 28 | 14 | 12 |
| author_self_report | 28 | 7 | 19 |
| pushlog_area_match | 14 | 7 | 6 |
| feature_flip | 9 | 8 | 1 |
| analogy_prior_bug | 7 | 2 | 4 |
| exposer_not_cause | 6 | 5 | 0 |
| backout_confirmation | 3 | 1 | 1 |
| automated_bot | 2 | 2 | 0 |
| reviewer_module_signal | 2 | 1 | 1 |

**Off/on-stack:** off=82, on=195, unlabeled=12  
**Exposer-not-cause (regressor exposed a latent bug elsewhere):** 86/289 (30%)  
**Who identified:** {'human': 214, 'author': 50, 'bot_automation': 23, 'unclear': 2}

**Detectability (mechanical signal a tool would need):**

| detectability | n |
|---|---:|
| line_crossing | 168 |
| keyword_match | 41 |
| area_match | 32 |
| needs_bisection | 24 |
| needs_human_reasoning | 15 |
| metadata_signal | 6 |
| callgraph | 3 |

**Secondary-strategy frequency (corroborating signals across all bugs):**  
keyword_domain_match=132, exposer_not_cause=79, searchfox_reasoning=78, pushlog_area_match=70, author_self_report=57, stack_line_hit=54, backout_confirmation=31, reviewer_module_signal=28, bisection=24, analogy_prior_bug=19, automated_bot=14, feature_flip=13, callgraph_proximity=4

---

## Synthesis

All key structural claims verified. Notable refinements from the code:
- `build_seed` (orchestrator.py:130-132) returns `None` — **skipping the agent entirely** — when no changeset scored onto any frame. So the off-stack blind spot isn't just "no candidate," it's "the reasoning agent never runs."
- `config/interesting_extensions.json` confirmed: C/C++/Java/Rust/ObjC only — no `.yaml`/`.js`/build files (feature-flip blindness confirmed).
- `buginfo.BZ_FIELDS` = `[id, summary, status, dupe_of, cf_crash_signature]` — already has `dupe_of`, still missing `regressed_by`/`see_also`/`component`.
- A corroboration gate **already exists** (`_fault_address` + `_apply_corroboration_gate`) but only for struct-field null-deref offset match (raises lead→probable/0.70).
- Java stacks **are** parsed (`java.py:inspect_java_stacktrace`); the gap is R8 deobfuscation, not parsing.
- Production pushlog uses `Mercurial.get_repo_url(channel)` = central/beta/release only — comm-central confirmed absent from ingestion (though searchfox tooling knows it).

Here is the report.

---

# How Firefox Crash Regressors Are Identified — and What crash-clouseau Can Automate
**Study cohort:** 289 FIXED crash-regression bugs, created 2025-07 .. 2026-07, all products.

## Executive summary
- **Line-crossing dominates but is not the whole game.** 168/289 (58%) are reachable by a line-proximity signal — clouseau's existing production scorer — but that leaves **121 bugs (42%) that no line hit reaches**, and **82 bugs (28%) whose regressor is fully off-stack**.
- **Humans, not tools, do the identifying today.** who_identified = human 214 (74%), author 50 (17%), bot 23 (8%), unclear 2. Genuine *autonomous* bot identification is far rarer than 8% — the automated_bot primary cohort is only 2, and **both misfired** (2009260 confirmed FP, 1982261 exposer-at-best). The other ~21 "bot" labels are provenance mislabels (a human/author set `regressed_by`, the release-mgmt bot echoed it).
- **The regressor is often not the bug.** In **86/289 (30%)** the `regressed_by` changeset is an **exposer**, not the root cause — correct code whose fix lands elsewhere. This is the single most important caveat for the whole system and the systematic false-positive generator for any proximity scorer.
- **clouseau's reasoning is capable but starved.** The augmented (searchfox) agent already pins off-stack root causes live (2037923, 2042018, 2043955), but `build_seed` only runs it when the on-stack line scorer already produced a candidate. **Every off-stack strategy is gated behind an on-stack candidate that, by definition, doesn't exist.** Unblocking candidate seeding is the master lever.

---

## 1. Strategies ranked by prevalence (with off-stack rate)

| Rank | Primary strategy | n | % | Off-stack | Off-stack % | Who (typical) |
|---|---|---:|---:|---:|---:|---|
| 1 | **stack_line_hit** | 109 | 37.7% | 0 | **0%** | bot/sheriff/author |
| 2 | **bisection** | 50 | 17.3% | 12 | 24% | human (volunteers, bugmon) |
| 3 | **keyword_domain_match** | 31 | 10.7% | 23 | **74%** | human |
| 4 | **searchfox_reasoning** | 28 | 9.7% | 14 | 50% | human (+3 LLM) |
| 4 | **author_self_report** | 28 | 9.7% | 7 | 25% | author |
| 6 | **pushlog_area_match** | 14 | 4.8% | 7 | 50% | human triager |
| 7 | **feature_flip** | 9 | 3.1% | 8 | **89%** | human/author |
| 8 | **analogy_prior_bug** | 7 | 2.4% | 2 | 29% | human |
| 9 | **exposer_not_cause** | 6 | 2.1% | 5 | **83%** | human/expert |
| 10 | **backout_confirmation** | 3 | 1.0% | 1 | 33% | human (sheriff) |
| 11 | **automated_bot** | 2 | 0.7% | 2 | **100%** | bot (both wrong/dubious) |
| 11 | **reviewer_module_signal** | 2 | 0.7% | 1 | 50% | human (owner/reviewer) |

**Totals:** off-stack 82 (28%), on-stack 195 (67%), unknown 12 (4%).

**Read the off-stack column as the automation frontier.** stack_line_hit — clouseau's home turf — is 0% off-stack by construction. Every strategy above 40% off-stack (keyword_domain_match 74%, feature_flip 89%, exposer_not_cause 83%, automated_bot 100%) is precisely where the current pipeline is structurally blind. The three biggest off-stack contributors in absolute terms are **keyword_domain_match (23), searchfox_reasoning (14), and bisection (12)**.

### Detectability — the mechanical-signal ceiling (the denominator for section 5)
| Detectability | n | % | Which mechanical signal reaches it | clouseau today |
|---|---:|---:|---|---|
| line_crossing | 168 | 58% | line-proximity scorer | **HAS** (production) — misses a subset (see §2) |
| keyword_match | 41 | 14% | signature/moz_crash → subsystem index | **MISSING** |
| area_match | 32 | 11% | full-window + component/dir overlap | **MISSING** (candidate path) |
| needs_bisection | 24 | 8% | consume bugmon range | **MISSING** |
| needs_human_reasoning | 15 | 5% | LLM mechanism reasoning | augmented agent (if fed) |
| metadata_signal | 6 | 2% | backout/reland, crashtest filename, provenance | **MISSING** |
| callgraph | 3 | 1% | searchfox call-graph | **HAS** (augmented) |

---

## 2. What clouseau already does vs the gaps

### 2a. Already automated
- **stack_line_hit — this IS the production pipeline.** `inspector.inspect_stacktrace` reads up to 50 `json_dump` frames, git2hg-converts them, `Changeset.get_scores` scores `isnew` (file added) → flat max 10, else `utils.get_line_score` = 10 at the crash line decaying ~1pt/5 lines over added/touched/deleted ranges (`models.py:400-419`). clouseau *is* the `release-mgmt-account-bot` that posts "the regression may have been introduced by a patch" (`templates/bug.txt`), and it directly pinned ≥12 of the 109 (e.g. 1980035, 1998057, 1998302, 2051098, 2052624). A design strength worth preserving: it scores **line ranges, not function names**, so it is immune to the `@@`-hunk-header enclosing-function mislabels that corrupt naive matchers (1982199, 1992635, 2028082, 2051274).
- **callgraph_proximity — the augmented branch.** The searchfox agent (`calls_from/to/between/define/search` in `searchfox.py`) reaches the callgraph=3 cases (2038533 cycle-collector, 2018081 producer/consumer) and already pinned three off-stack `searchfox_reasoning` root causes live (2037923, 2042018, 2043955). It also has: area-experts, inline-function seed enrichment, a fault-address↔struct-field-offset corroboration gate that raises `lead`→`probable`/0.70 (`orchestrator.py:305-346`, the ab3238a5 signal), skeptic veto, and proto-signature dedup.

### 2b. The master gap: the reasoning agent is starved
`build_seed` (`orchestrator.py:130`) returns `None` — **skipping the agent entirely** — when `not any(f.get("changesets") for f in frames)`. Candidates only ever come from blaming files that appear on the crash stack (`inspect_stacktrace → filelog → amend → Changeset.to_analyze`). **A fully off-stack regressor never becomes a candidate, so the capable reasoning layer never even runs on it.** This one seam explains why 82 off-stack bugs are unreachable regardless of how good the agent is.

### 2c. Gaps quantified — new reach, especially off-stack
The 82 off-stack bugs partition cleanly by the detector that would unlock them:

| Gap / new detector | Detectability bucket | New reach (of 289) | Off-stack unlocked | Explains misses |
|---|---|---:|---:|---|
| **Off-stack candidate path + keyword→subsystem index + component match** | keyword_match 41 + area_match 32 | **~73** surfaceable | **~38** | keyword_domain_match (23 off), pushlog_area_match (7 off), feature_flip (8 off) |
| **Consume bugmon/mozregression range** | needs_bisection 24 | **24** (only path) | **~12** | bisection off-stack (1978585, 2036130), 2007270, 2042290 |
| **Backout+reland natural experiment** | metadata_signal | 3 primary + corroboration | **1–3** (off-stack-proof) | 2040246; names security-restricted regressors (2046246/2046250) |
| **Analogy / sibling-inheritance** | (cross-cuts) | 7 + corroborator | **2** | 1990034, 1979113, 2054485 |
| **Crashtest-filename→bug regex** | metadata_signal | 1–3 | — | 2017517 |
| **Stack-reach fixes** (comm-central, R8, panic-prologue, empty-stack, gated blame) | within line_crossing 168 | **~25 recovered** | recovers *mislabeled* off-stack | see below |
| **Residual: needs_human_reasoning** | 15 | agent-only (once fed) | ~15 | 1988967, 2007270, 2006941 |

**Stack-reach fixes** recover bugs the line scorer *should* catch but currently misses:
- **comm-central ingestion** — production pushlog is central/beta/release only (`Mercurial.get_repo_url(channel)`), so ~7 Thunderbird regressors get empty diffs: 1986642, 1986644, 1987759, 1991185, 2025791, 2046892, and 2007164. (searchfox source tooling already knows `comm-central`; only the pushlog/diff ingestion is central-only.)
- **R8/ProGuard deobfuscation** — Java stacks *are* parsed (`java.py`), but `R8$$SyntheticClass` frames need `module`-FQN→path mapping: recovers 1992849, 2042018, 1999784 (the last turned a real frame-12 `Store.dispatch` hit into a spurious off-stack via top-10 truncation + R8 rewrite).
- **Panic-prologue unwind** — consume the Socorro *signature* symbol below `rust_begin_unwind`: 1985911, 2043773, 2047449, 1985037, 2043955.
- **Empty structured stack** — several bugs had an empty signature-thread stack (sec bugs / debug-assert-only / hung-main-thread), so clouseau had nothing to score; ingest the comment-0 pasted stack: 1997818, 2008698, 1992195, 2037923.
- **Gated unbounded blame** — latent/genesis regressors older than the buildid−3-days window are structurally invisible: 1978488 (2013), 2014923 (~5.5y), 1993726 (5y), 2016493, 2016458, 2019532, 1982411.

---

## 3. The exposer-not-cause phenomenon

**Prevalence: 86 / 289 (30%) — roughly one in three fixed regressions.** (This is the cross-cutting flag; it also appears as a secondary signal 79 times and is the *primary* identification route in only 6.) An exposer is a **correct, never-backed-out** changeset that changed timing, coverage, data volume, or process ordering so a **pre-existing latent bug** finally crashes. The fix lands **outside** the regressor's diff and is **never a revert** of it.

**What it implies for "is the regressor the real bug": it depends entirely on the goal.**
- **Goal = nominate `regressed_by` / needinfo the author** → the exposer IS the right answer. Mozilla marks exposers as `regressed_by` too; line-crossing even reproduces Bugzilla's own label.
- **Goal = localize the defect/fix** → the exposer is *wrong* ~30% of the time. Roughly 1-in-6 on-stack line hits are exposers whose fix is off-diff (assertion/hardening adders 1982411, 2005095, 2024553, 2048851, 2047650; timing/usage changes surfacing a latent callee 1984918, 1986816, 1998057, 2036484, 2043773, 2035785). Off-stack exposers are worse: pref flips (2000305→fix in 2002134, 2017903, 2043307), vendored bumps (1992617 Glean non-monotonic clock), and startup-reordering unmasking old races (2041907/2042290 → 2011-era bug 675407; 2009140 → bug 1299611).

**The canonical trap is 2051442**: a recent `mailnews/search` feature + a `mailnews/search` crash scores as a confident culprit under line/area proximity — but it's an exposer whose null-deref fix lives elsewhere. **Exposers are the systematic false-positive generator for clouseau.** The implication for design is unambiguous: clouseau must **classify cause-vs-exposer and never auto-upgrade a proximity hit to "culprit" when exposer corroborators fire** — emit `lead` + needinfo the owner, and attach the likely real cause (the fix-elsewhere file, the cited older bug). Detectable exposer signals: fix-diff disjoint from regressor-diff + non-backout; poison/UAF/null crash reason (0xe5e5e5e5, "poison read", ACCESS_VIOLATION_READ null); a different/older bug named as root cause; opt-in/feature landing needing a non-default flag; crashtest named after the bug that added it (2017517).

---

## 4. Novel strategies worth building

These emerged from the small/"other" cohorts and the notes; several are off-stack-proof and reuse plumbing clouseau already owns.

1. **Backout + reland natural experiment (off-stack-PROOF).** Correlate per-`(signature,buildid)` crash counts against pushlog backout/reland events: crash **stops** at a build that backs out bug X and **restarts** at the build that relands X. This is the ONE signal that pins a completely off-stack culprit with high confidence and needs no diff (2040246 pref-flip; real fix in `HappyEyeballsConnectionAttempt.cpp`). It also **names security-restricted regressors** whose diffs are unreadable (2046246, 2046250 — backout changeset carries the bug number even when the regressor bug is hidden). clouseau has ~80% of the plumbing (`models.Stats`, `buildhub`, `pushlog.is_backed_out`/`get_bug`, `utils.is_spike`) — the missing piece is persisting the *decline* side of the series (`update.put_crashes` stores only spiking signatures at spike builds).

2. **Analogy / sibling-signature inheritance.** Inherit a sibling bug's `regressed_by` when it corroborates on ≥2 axes (identical signature AND (exact moz_crash reason OR component OR a `see_also`/`dupe_of` edge)). This reaches off-stack/exposer regressors the proximity scorer can never touch (1990034 ← 1989368; 1979113 ← 1974901; 2007774 ← 2004647). **Half-built already:** `buginfo.get_bugs(signature)` maps signature→prior bugs and already returns `None` for unreadable sec bugs (built-in abstain); `BZ_FIELDS` already has `dupe_of` but still needs `regressed_by`, `see_also`, `component`.

3. **Authorship / ownership booster.** Cross-reference each candidate changeset's author (via `hgauthors.py` / lando json-rev) against the bug's assignee/fixer/needinfo target. "Candidate authored by the person now on the bug" is a very high-precision re-ranker, and it correctly **relabels the 6 silent-field author cases** (2002697, 2029827, 2010056, 2010764, 2021327, 2049602) that currently look bot-driven. Dossiers carry `landing_revs` but no author field today.

4. **Exposer classifier (defensive).** Combine fix-diff-disjointness + non-backout + a poison/UAF address table + "real root cause is bug N" extraction to *label* exposers and suppress false culprits. This extends the existing `_fault_address` corroboration mechanism (today only struct-field null-deref, capped at one page) to the poison sentinels (0x4b4b4b4b swept-tenured GC-UAF, 0xe5e5e5e5 UAF, 0xCECACE11 MozPromise).

5. **Crashtest-filename → bug-number regex.** `.../crashtests/<bugno>.html` names the bug that added the test — a trivial, near-zero-FP detector for the increased-coverage exposer flavor (2017517 → bug 2014861).

6. **Bisection-range consumption** (see P2) — parse bugmon's stable "build range … fromchange=X&tochange=Y" comment to shrink the candidate window 20–200x. Not "novel" as a concept but entirely unbuilt and the highest-ROI cheap win.

---

## 5. Prioritized detector recommendations

Ranked by (off-stack reach × cost-efficiency), with the exposer classifier prioritized because it gates the false-positive risk of everything above it.

### P1 — Off-stack candidate path + signature-keyword→subsystem index + component match  *(the master unblock)*
- **Signal:** enumerate the **full first-bad-build pushlog window** independently of stack blame; rank each in-window changeset by (a) signature/moz_crash token → subsystem match against the changeset summary + diff-introduced symbols, and (b) Bugzilla-component equality + stack-directory overlap.
- **Data source:** `buildhub` + `pushlog.pushlog_for_buildid` (**have** — but drop the `interesting_extensions.json` filter and **retain the changeset `desc`**, both currently discarded); a **signature→subsystem keyword index** (build); a **`DIR_TO_COMPONENT` resolver** via `mach file-info bugzilla-component` (build).
- **Expected coverage:** unblocks **~73/289** (keyword_match 41 + area_match 32) as *surfaceable*, of which **~38 are off-stack** — the largest single lever. Also the prerequisite that lets the augmented agent run at all on off-stack crashes. Representative: 2003956, 1992617, 2035852 (keyword); 1992641, 2011700, 1981294 (area); 2000305, 2043307 (feature-flip via component).
- **FP risk: HIGH.** Windows carry 43–204 bugs; area/keyword match is high-recall/low-precision. **Rank, never auto-pin.** Down-weight generic basenames (`mod.rs`, `lib.rs`, `Assertions.h`), require FQN-level token matches, and cap confidence to `lead`/`probable` unless the signature exactly names the feature. The release-mgmt bot picked the *wrong* regressor for 1992640/1992641 doing exactly this.

### P2 — Consume bugmon / mozregression range
- **Signal:** parse the bugmon "introduced in the following build range … `pushloghtml?fromchange=X&tochange=Y`" comment (fixed format; trivial regex) plus manual-mozregression variants.
- **Data source:** Bugzilla comment regex → existing `pushlog.pushlog_for_revs(fromchange, tochange)` (**have**) to replace the 50–204-bug window with a 1–11-bug range, then score within it with the existing scorer.
- **Expected coverage:** **24/289 needs_bisection** are reachable ONLY this way (~12 off-stack: 1978585, 2036130, 2042290, 2007270), and it shrinks noise on the other ~29 bisected bugs (a 20–200× narrowing).
- **FP risk: MEDIUM.** **Never trust the range endpoint** — 2007139's endpoint was off-stack cookie code (2005748); the true on-stack regressor (1873716) is what the line scorer would flag. Disambiguate *within* the range; down-weight off-stack members unless nothing on-stack matches; abstain when the stack is empty and no range exists.

### P3 — Backout + reland natural experiment  *(off-stack-proof)*
- **Signal:** detect an **off-edge** (crash count → ~0 at a build whose pushlog window backs out bug X) AND an **on-edge** (count returns at a build that relands X). Two-sided coincidence = near-proof.
- **Data source:** `models.Stats` (**extend** `update.put_crashes` to persist the full per-build series, not just spike builds) + `pushlog.is_backed_out`/`get_bug` + `utils.is_spike` (**have**).
- **Expected coverage:** 3 primary + corroborates feature_flip/exposer/off-stack pref flips; **uniquely reaches off-stack regressors all code scorers miss** and names security-restricted ones (2040246, 2046246, 2046250).
- **FP risk: LOW if both edges required** (the reland leg is the guard); MEDIUM if one-sided (an unrelated backout in a low-ADI/weekend build). Normalize by `installs`; require a real pre-drop baseline.

### P4 — Analogy / sibling-signature inheritance
- **Signal:** inherit sibling `regressed_by` when signature matches AND ≥1 of {exact moz_crash reason, component, `see_also`/`dupe_of` edge} corroborates.
- **Data source:** `buginfo.get_bugs(signature)` (**have**); add `regressed_by, see_also, component, keywords` to `BZ_FIELDS` (currently `[id, summary, status, dupe_of, cf_crash_signature]`).
- **Expected coverage:** 7 primary (2 off-stack) + a broad off-stack corroborator the proximity scorer structurally cannot provide.
- **FP risk: MEDIUM.** Never inherit on signature substring alone (generic teardown/assert top frames collide); require the ≥2-axis rule and prefer an explicit `see_also`/`dupe` edge. Flag exposer inheritance (4/7 siblings here are exposers). Cap confidence when the sibling's own pin was speculative. Abstain on restricted siblings (2054485 → security-restricted 2043820).

### P5 — Authorship / ownership booster (re-ranker)
- **Signal:** candidate changeset author == bug assignee/fixer/needinfo target (normalize email aliases, e.g. `emilio@crisal.io`↔`ealvarez@mozilla.com`).
- **Data source:** `hgauthors.py` / lando json-rev (**have**) + Bugzilla participants (already in the dossier).
- **Expected coverage:** re-ranks the ~28 author_self_report bugs into top candidates and relabels the 6 silent-field cases as author-driven (not bot).
- **FP risk: LOW as a corroboration weight; HIGH if ever used as an auto-culprit** — prolific authors/owners touch huge swaths of code (the extract-method FP, bug 2023670 / `4cb85f483290`, is exactly this failure). Corroboration only.

### P6 — Exposer classifier + poison/UAF decode  *(defensive — build alongside P1)*
- **Signal:** fix-diff disjoint from regressor-diff AND fix `is_backout=false`; poison/sentinel/null fault address; "real root cause is bug N" / "just exposing this" language in-thread.
- **Data source:** the regressor and fix diffs clouseau already collects; **extend the existing `_fault_address` gate** (`orchestrator.py:262`) with a poison-address table; the principal already reads comments. (Caveat: comm-central fixes are invisible until P7 lands — 2041907, 2042290, 2051442.)
- **Expected coverage:** correctly labels the **~86 exposer bugs** and — crucially — **suppresses the proximity-scorer false positives** they generate (2051442 trap).
- **FP risk: this REDUCES net false positives.** When exposer corroborators fire, hold at `lead` + needinfo the owner; never upgrade to `culprit`.

### P7 — Stack-reach fixes (recover missed line_crossing)
- **Signal / data source:** (a) **comm-central** in the pushlog/diff pipeline; (b) **R8 synthetic-class deobfuscation** (`module` FQN→path) in `java.py`; (c) **panic-prologue unwind** to the Socorro signature symbol; (d) **ingest comment-0 pasted stack** when the structured stack is empty; (e) **gated unbounded blame-of-crash-line** second pass for latent regressors.
- **Expected coverage:** recovers **~25 bugs within the line_crossing 168** that the scorer currently misses, several of them mislabeled off-stack collector artifacts (comm-central ~7: 1986642/1986644/1987759/1991185/2025791/2046892/2007164; R8: 1992849/2042018/1999784; panic: 1985911/2043773/2047449; empty-stack: 1997818/2008698/1992195/2037923; latent: 1978488/2014923/1993726/2016493/2016458/2019532/1982411).
- **FP risk: LOW–MEDIUM.** The unbounded-blame pass is the only real risk (it can blame ancient refactors) — gate it with a recency prior and treat as low-confidence.

### P8 — Crashtest-filename → bug regex
- **Signal:** `.../crashtests/<bugno>.html` in the stack/testcase → bug N.
- **Data source:** filename regex only.
- **Expected coverage:** the increased-coverage exposer flavor (2017517 → 2014861); 1–3 bugs.
- **FP risk: LOW.** Always emit as exposer/increased-coverage, not culprit.

### Suggested sequencing
**P1 first** — it is the structural unblock; without an off-stack candidate path (and retaining the discarded `desc` + dropping the `.yaml`/source-only filter), the capable augmented agent never runs on the 82 off-stack bugs, and P4–P6 have nothing to attach to. **P6 in parallel with P1** so the first off-stack candidates ship with exposer guardrails (otherwise P1 becomes an FP firehose given the 30% exposer base rate). **P2 and P3 next** — both are cheap (mostly existing plumbing) and each opens an otherwise-closed door (needs_bisection 24; off-stack-proof backout). **P4/P5/P7/P8** are lower-cost polish/re-rankers to layer on. Across the board the governing rule the data mandates: **emit a ranked candidate set + explicit mechanism rationale + exposer flag; treat line/keyword/area/bisection/authorship as priors to reason over, never as a standalone verdict.**


---

## Per-strategy reports


### stack_line_hit (n=109)

**Automatable:** YES — and it is already automated: crash-clouseau IS the release-mgmt-account-bot that posts "In analyzing the backtrace, the regression may have been introduced by a patch [1]" (templates/bug.txt), and ~12 of these 109 bugs were pinned by exactly that bot (e.g. 1980035, 1998057, 1998302, 2051098, 2052624). The live algorithm: inspector.inspect_stacktrace reads up to 50 frames from the Socorro json_dump, maps each frame's file+line, git2hg-converts frames since the hg->git migration; Changeset.get_files pulls changesets in the pushlog window; Changeset.get_scores scores isnew(file added)=max(10) else utils.get_line_score = 10 at the exact crashing line decaying ~1 point per 5 lines over the added/touched/deleted line arrays; UUID max_score is the max over all frames x changesets. Data/tools it needs and already has: Socorro processed-crash json_dump (50 frames), buildhub/pushlog for the first-bad-build window, hg diffs parsed to per-file added/touched/deleted LINE RANGES (parsepatch), and Lando git2hg. Notably it scores by line RANGES, not function names, so it is ROBUST to the git @@-hunk-header enclosing-function mislabels that corrupt the dataset's parsed function names (1982199, 1992635, 2028082, 2051274) — a real design strength. Gaps to close (each maps to specific misses in the cohort): (1) RECENCY WINDOW — nightly window is only buildid-3days (prev-build delta on beta/release), so latent/genesis regressors that landed years earlier are structurally invisible (1978488 from 2013, 2014923 ~5.5y, 2016493/2016458 ~2y, 2019532 ~5y, 1982411 ~2y); these were found by humans via blame-to-origin (unbounded hg annotate on the crash line) or feature recognition, and bugmon bisection also FAILED on several ('reproduces on start build'). Add an unbounded blame-of-crashing-line second pass, gated to control FPs. (2) DEEP FRAMES / PANIC PROLOGUES — clouseau's 50-frame reach already beats the dataset's ~10-frame parse (so many on_stack=false/null labels are collector artifacts, not real off-stack: 1998057, 2036484), but Rust unwrap panics bury the app frame below the prologue and name it only in the Socorro SIGNATURE's 2nd component (1985911) — consume the signature symbol and skip generic panic/assert frames. (3) COMM-CENTRAL — the pipeline fetches mozilla-central only, so ~6 Thunderbird regressors had empty diffs (1986642, 1986644, 1987759, 1991185, 2025791, 2046892); add the comm-central repo. (4) HUGE VENDOR COMMITS — ensure file-added (isnew) is detected even when the diff body is byte-capped (1987003 llama.cpp). FALSE-POSITIVE RISK is the crux and is goal-dependent. If the goal is to NOMINATE the regressed_by bug (needinfo the author), line-crossing is very accurate — it even reproduces Bugzilla's own label on EXPOSERS, because Mozilla marks the exposer as regressed_by too. If the goal is to LOCALIZE THE DEFECT/FIX, roughly one in six of these on-stack hits is an exposer where the fix lives off-diff (assertion/hardening-adders 1982411/2005095/2024553/2048851/2047650; usage/threading/timing changes surfacing a latent callee bug 1984918/1986816/1998057/2036484/2043773/2035785). Two hard false positives even for nomination: file-MOVE / mass-refactor that rewrites every line so all frames score max (1986107, the repo's known 'refactor+blame' failure mode; confirmed diff status=deleted/renamed), and cosmetic/logging changes on an on-stack file (2052624 MOZ_LOG_FMT migration) that carry a strong line hit but zero logic. When multiple in-window changesets touch the same area (2014692 siblings, 2052026 two regressors), line-crossing returns a cluster and needs a tie-breaker (exact-line > same-function > same-file; file-added > modified; de-prioritize whitespace-only/logging-only/file-move diffs). Mitigations: flag newly-added-assert and file-move/cosmetic diffs as low-confidence 'exposer/likely-not-fix-site' rather than dropping them, and present the score with its frame depth and add/modify/assert basis so a human can judge.

**Key signals:** Regressor diff ADDED a file (status=added) that appears on a crash frame -> the crashing function is brand-new code (clouseau scores this a flat max; e.g. 2004168, 2004939, 1987003, 2048858, 1983101); Regressor MODIFIED the function on a crash frame and its changed line-range covers or is within ~5 lines of the crashing line (line-proximity; the core signal in most bugs); The crashing line is a newly-added assertion/diagnostic (MOZ_CRASH, MOZ_DIAGNOSTIC_ASSERT, MOZ_RELEASE_ASSERT); blame of that line -> the changeset that added it (change_tags include 'assert'; e.g. 1993904, 2005095, 2019360); Multi-frame corroboration: the regressor touches several consecutive frames (caller+callee), i.e. it introduced the whole crashing call chain (1980035, 1983101, 1992304, 1992512, 2008810) -> disambiguates when the top frame is generic; Caller-frame hit: the top-frame FILE is untouched but its on-stack CALLER was changed by the regressor -> must score all ~50 frames, not just frame 0 (1991268, 2007897, 1995038, 2004022); Signature / moz_crash_reason text names the same symbol or feature as the regressor's own new code (keyword corroboration); for Rust panics the crashing symbol is only in the Socorro SIGNATURE, below the panic prologue (1985911, 2043773); Regressor lands in the first-crashing-nightly pushlog window AND shares the crash's Bugzilla component (narrows candidates; near-universal here); Backout of the regressor stops the crash (backout_confirmation) — after-the-fact validation (2004939, 1988694, 2021881, 2015785, 2008970)

**Representative bugs:** 1980035, 1981271, 2004168, 1979112, 1993904, 1998302

## Strategy report: `stack_line_hit`

**Primary strategy for 109 / 289 fixed Firefox crash-regression bugs — the single largest cohort.**

### Bottom line
`stack_line_hit` = "the regressor is the recent changeset whose diff intersects (or created) the crashing line." This is crash-clouseau's core signal, and **clouseau already runs it in production**: it is the `release-mgmt-account-bot` that comments *"In analyzing the backtrace, the regression may have been introduced by a patch [1]"* (`templates/bug.txt`). At least 12 of these 109 bugs were pinned by that bot with no human reasoning; most of the rest were pinned by sheriffs/triagers/authors doing the same thing by eye.

### How it works (as applied here)
Walk the crash stack (up to 50 `json_dump` frames). For each frame's source file, find changesets in the **first-bad-build pushlog window** that touched it, and score by proximity of the crashing line to the diff:

- `inspector.inspect_stacktrace` extracts file+line per frame (git2hg-converted post hg→git migration).
- `Changeset.get_scores`: **file newly ADDED → flat max (10)**; else `get_line_score` = 10 at the exact crashing line, decaying ~1 point per 5 lines over the changeset's added/touched/deleted line arrays.
- Crash `max_score` = max over all frames × changesets.

Three recurring shapes:
1. **Regressor added the crashing file/function** (brand-new code) — strongest, flat-max.
2. **Regressor modified the crashing function** — blame of the crash line lands on it.
3. **Crash line is a newly-added assertion** the regressor introduced — blame-the-assert-adder.

**Credit caller frames, not just frame 0.** Many hits are at frames 1–8 while the top frame is a generic assert/GC/IPC/Rust-panic prologue (`1991268`, `1995038`, `2007897`, `2051098`, `1985911`). A top-frame-only matcher misses these.

### Reusable signals (ranked)
1. Diff `status=added` for a file on a crash frame → crash runs new code (`2004168`, `2004939`, `1987003`, `2048858`).
2. Diff modified the crash-frame function; changed lines within ~5 of the crash line.
3. Crash line is a new `MOZ_*_ASSERT`/`MOZ_CRASH` (`change_tags:assert`) → blame the adder (`1993904`, `2005095`, `2019360`).
4. Multi-frame hit: regressor introduced the whole call chain (`1980035`, `1992512`, `2008810`).
5. Caller-frame hit (top file untouched, on-stack caller changed).
6. Signature / `moz_crash_reason` names the regressor's feature/symbol; for Rust panics use the **Socorro signature symbol**, not `top_frames` (`1985911`).
7. Regressor in first-crashing-build window **and** same component.
8. Backout stops the crash (confirmation).

### Clearest examples
- **1980035** — bot-found; regressor *created* the top frame `ProcessLNAActions` and modified its caller `OnStartRequest`.
- **1981271** — two consecutive frames (`IsDistrustedCertificateChain@1351`, `IsChainValid@1410`) are functions the regressor changed.
- **2004168** — top frame is in `backend.cpp`, a file the regressor added wholesale (flat-max path).
- **1979112** — triager pinned it from the stack; regressor introduced top-frame `BlitSd` + modified frame-1 `TexOrSubImage`.
- **1993904** — line-proximity *beat* a wrong human guess: reporter blamed the wrong bug; the newly-added assert at the top frame pointed at the real one.
- **1998302** — literally clouseau's own bot output; clean frame-0/1/2 hit.

### Gaps to close (with the misses they explain)
- **Recency window** (nightly = `buildid − 3 days`): latent/genesis regressors are invisible — `1978488` (2013), `2014923` (~5.5y), `2016493`/`2016458`/`2019532` (~2–5y). Humans used unbounded blame-to-origin; bugmon bisection also *failed* ("reproduces on start build"). → add an unbounded blame-of-crash-line second pass, gated.
- **Deep frames / panic prologues**: clouseau's 50-frame reach already beats the dataset's ~10-frame parse (many `on_stack=false/null` labels are collector artifacts, e.g. `1998057`, `2036484`), but Rust panics need the **signature symbol** (`1985911`).
- **comm-central**: mozilla-central-only pipeline left ~6 Thunderbird regressors with empty diffs (`1986642`, `1986644`, `1987759`, `1991185`, `2025791`, `2046892`).
- **Huge vendor commits**: keep detecting `isnew` even when the diff body is byte-capped (`1987003`).

### False-positive risk (read this before trusting the score)
The score is **goal-dependent**:

- **Goal = nominate `regressed_by`** → very accurate. It even reproduces Bugzilla's label on *exposers*, because Mozilla marks the exposer as `regressed_by` too.
- **Goal = localize the defect/fix** → ~**1 in 6** on-stack hits is an **exposer** whose fix lives off-diff: assertion/hardening adders (`1982411`, `2005095`, `2024553`, `2048851`, `2047650`) and usage/threading/timing changes that surface a latent callee bug (`1984918`, `1986816`, `1998057`, `2036484`, `2043773`, `2035785`).

Two **hard false positives** even for nomination:
- **File-move / mass-refactor** rewrites every line → all frames score max (`1986107`, the repo's "refactor+blame" failure mode; diff status `deleted`/`renamed`).
- **Cosmetic / logging** changes on an on-stack file (`2052624`, a `MOZ_LOG_FMT` migration) — strong line hit, zero logic.

**Mitigations:** (1) tie-break exact-line > same-function > same-file, and file-added > modified; (2) **down-weight or flag** whitespace-only, logging-only, and file-move/rename diffs; (3) label newly-added-assert hits as *"regressor likely, but fix probably off-diff (exposer)"* rather than dropping them; (4) surface the frame depth + add/modify/assert basis so a human can judge. A useful design strength to preserve: clouseau scores by **line ranges, not function names**, so it is immune to the `@@`-hunk-header enclosing-function mislabels that corrupt naive matchers.


### bisection (n=50)

**Automatable:** Two-part answer. (A) Clouseau CANNOT run bisection itself: mozregression needs a deterministic repro plus build-download/run infrastructure, and clouseau analyzes Socorro crash reports that usually have no testcase attached. Running a bisector is out of scope for the ingestion pipeline. (B) But clouseau CAN and should CONSUME bisection, which is the high-ROI integration — bugmon already auto-bisects and posts a stable, machine-parseable range to Bugzilla. Concretely: (1) add a Bugzilla-comment parser (a regex over the fixed bugmon 'build range ... fromchange=X&tochange=Y' text plus the manual-mozregression variants); (2) feed those revs into the pre-existing crashclouseau/pushlog.py:pushlog_for_revs(fromchange, tochange) to REPLACE the wide first-build window (today pushlog_for_buildid, 50-204 bugs) with the narrow bisection range (1-11 bugs); (3) score within that narrowed set with the existing line-proximity scorer (models.get_scores) PLUS two cheap additions — a file/directory area-overlap fallback and a moz_crash_reason/signature->component keyword index.

Why it is worth it, quantified over these 50: 21/50 (42%) are ON-stack (detectability=line_crossing) and clouseau's EXISTING scorer already finds them WITHOUT any bisection — so for the plurality of cases bisection is redundant/corroborating. Another 8/50 (5 area_match + 3 keyword_match, 16%) become reachable with the coarse file/area + keyword scorers ONCE the window is narrowed so the weak signal is not drowned in 100+ changesets. The remaining 21/50 (42%, detectability=needs_bisection) are off-stack / empty-stack / exposer cases where NO static signal works and consuming bugmon's range is the ONLY automatable path.

False-positive risk (the critical caveat): auto-picking the range ENDPOINT is dangerous. Bug 2007139 is the cautionary tale — bugmon's ~7h range's End changeset was bug 2005748 (cookie code), completely off-stack and unrelated; a bot set regressed_by=2005748 by taking the endpoint and the author rejected it, while the true regressor (1873716) was on-stack and is exactly what clouseau's line-crossing scorer WOULD have flagged. So clouseau must disambiguate WITHIN the range (down-weight off-stack members unless nothing on-stack matches) rather than trust the endpoint. Second risk: exposer confusion — ~1/3 of bisected regressors are exposers whose fix lands elsewhere; when the top-ranked changeset does not touch the crashing function, clouseau should emit 'likely trigger, root cause may be elsewhere' instead of a confident needinfo. Third: flaky/config-specific repros produce wrong, empty, or conflicting ranges (bugmon failures in 1997489/2014331/2024044; platform zoom-range conflict in 1981495). Net: clouseau should treat a parsed bugmon range as a strong window-narrowing PRIOR feeding its own scorers, never as a standalone verdict, and should abstain (not guess) when the stack is empty and no range exists.

**Key signals:** bugmon range comment (parseable, stable format): 'introduced in the following build range: Start <rev> (<buildid>) > End <rev> (<buildid>) Pushlog: .../integration/autoland/pushloghtml?fromchange=X&tochange=Y' — yields an exact autoland fromchange/tochange window; Range-endpoint identity: the range tochange/End (or a manual bisection's 'First bad revision') equals a candidate bug's landing changeset => single-push bracket, near-certain pick (but see FP risk when the range spans >1 bug); Human-bisection comment markers: literal 'Bisection:' prefix, 'I bisected ... to Bug N', 'mozregression suggest(s) Bug N', 'first interesting commit' (autobisect), 'Regression range/window:' + 'Suspect: Bug N'; A deterministic repro exists (fuzzer Grizzly/oomTest testcase, or a clear STR); Bugzilla keywords 'testcase'/'pernosco'. No repro => bisection is impossible (bugmon posts 'Unable to bisect'); Within-range pick disambiguators: the candidate touches an on-stack file/line, shares the crash bug's Product/Component, or matches a moz_crash_reason/feature keyword (e.g. 'snapshot'->View-Transitions, 'Sqlite.sys.mjs shutdown blocker'->tab-notes sqlite feature); regressed_by set (or a BugBot needinfo to 'the author of the changes in the range') within a comment or two of a bugmon range => a bisection-driven attribution just happened; the release-mgmt-account-bot 'Set release status flags' comment is downstream noise, not identification; Narrowing magnitude: clouseau's first-build pushlog window is 50-204 bugs / 100-341 changesets; a bugmon/mozregression range is typically an ~2-8 min autoland push (1-11 bugs). The value of a bisection IS this 20-200x window shrink

**Representative bugs:** 1998521, 1978585, 1987777, 2007139, 2036130, 1993726

# Regressor-ID Strategy Report: **Bisection**

**50 of 289 fixed crash-regression bugs (17%)** were identified primarily by bisection — empirically pinning *when* the crash started rather than reasoning from the stack.

## By the numbers (these 50)
| Dimension | Split |
|---|---|
| Detectability by a *static* signal | **21 line_crossing**, 5 area_match, 3 keyword_match, **21 needs_bisection** |
| Reachable without bisection | **29/50 (58%)** by some static signal; **21/50 (42%) reachable ONLY via bisection** |
| Already caught by clouseau today | **21/50 (42%)** are on-stack — clouseau's line scorer nails them, bisection is redundant |
| Who identified | 40 human (community volunteers + sheriffs), 7 pure bot (bugmon), 3 author |
| Off-stack / empty-stack | 16/50 |
| Exposer (fix lands elsewhere than the blamed changeset) | **16/50 (~1/3)** |
| Outright false positives | 1 (bug 2007139) |

## How it works
1. A **deterministic repro** (fuzzer Grizzly/oomTest testcase, or a clear STR) is run across builds until a last-good / first-bad boundary is found → a **regression range** (autoland `fromchange`→`tochange`, or Start/End build-ids).
2. Two engines:
   - **bugmon (bot)** posts a parseable comment: *"introduced in the following build range: Start <rev> (<buildid>) > End <rev> (<buildid>) Pushlog: .../autoland/pushloghtml?fromchange=X&tochange=Y"*. It emits a **range, never a bug**.
   - **Manual mozregression** by volunteers (mayankleoboy1, alice0775, matt.fagnani), sheriffs (ryanvm), devs (tnikkel, dholbert). Fallback when bugmon fails.
3. **Pick step:** the range often collapses to a single push whose `tochange`/End == the regressor's landing rev (mechanical pick); wider ranges are resolved by on-stack / same-component / feature-keyword match.
4. `release-mgmt-account-bot`/BugBot then set `regressed_by` and needinfo the range author — **bookkeeping after the fact, not identification.**

## Can clouseau automate it?
**No — it can't *run* bisection** (needs a testcase + build-run infra clouseau doesn't have in ingestion). **Yes — it should *consume* bugmon's ranges**, the single highest-ROI change:

- **Parse** the bugmon "build range" comment (fixed format; trivial regex) + manual-mozregression variants.
- **Feed** the revs into the existing `pushlog.pushlog_for_revs(fromchange, tochange)` to replace clouseau's wide first-build window (**50–204 bugs**) with the bisected range (**1–11 bugs**) — a 20–200x narrowing.
- **Score within** that range with the existing line scorer (`models.get_scores`) **plus** a cheap file/area-overlap fallback and a `moz_crash_reason`→component keyword index.

### Recommended actions (for the maintainer / RE)
1. **Ship the bugmon-range parser + window swap.** Recovers the 8 area/keyword cases and shrinks noise everywhere. Cheap.
2. **Never trust the range endpoint alone.** Disambiguate *within* the range; down-weight off-stack members unless nothing on-stack matches. This is exactly the 2007139 failure below.
3. **Flag exposers.** When the top candidate doesn't touch the crashing function, emit *"likely trigger, root cause may be elsewhere"* — don't fire a confident needinfo (authors have pushed back: 1981495, 2007139).
4. **Accept the ~42% floor.** Off-stack / empty-stack crashes are unreachable by static signals; clouseau should defer to bugmon's range and **abstain** when the stack is empty and no range exists — not guess.

## False-positive risk
- **Endpoint auto-pick (2007139):** bugmon's 7h range End was off-stack cookie code (2005748); the bot set it, the author rejected it, and the *true* on-stack regressor (1873716) is what clouseau's line scorer would have flagged. Bisection **lost** to line-crossing here.
- **Exposer confusion (~1/3):** bisection finds the trigger; the fix often lands in a different file.
- **Flaky / config-specific repro:** wrong, empty, or conflicting ranges (bugmon failed in 1997489/2014331/2024044; platform zoom conflict in 1981495).

## Representative bugs
| Bug | Why it's illustrative |
|---|---|
| **1998521** | Modal happy path: bugmon narrows a **204-bug** nightly window to an ~8-min autoland range; human fills `regressed_by`; on-stack. |
| **1978585** | Clean **off-stack** case (AbortSignal vs TimeoutManager stack) — the canonical "why bisection exists"; human mozregression + bugmon corroboration. |
| **1987777** | Range collapses to a **single 5-changeset / 1-bug push**; `tochange` == landing rev — the strong-signal mechanic. |
| **2007139** | **False positive:** endpoint auto-pick chose an off-stack unrelated bug; author rejected; line-crossing would have won. |
| **2036130** | bugmon localizes an **off-stack** flex/grid regressor to 20 changesets; keyword/area pick; shows off-stack ≠ exposer. |
| **1993726** | **5-year-old** regressor found only by manual nightly bisection — unreachable by any recency/blame/line tool. |


### keyword_domain_match (n=31)

**Automatable:** See automatable field.

**Key signals:** placeholder

**Representative bugs:** 2003956, 1992617, 2035852, 2019759, 2042859, 1980992

# Strategy report: `keyword_domain_match`

**What it is.** Identify the regressor by reading the crash's *text* — signature, moz_crash/assertion reason, or Java/Rust exception message — extracting a distinctive token, and matching it to the recently‑landed patch whose summary/title/diff is *about that same subsystem or feature*. Symptom‑names‑X ↔ patch‑is‑about‑X.

**Prevalence.** 31 / 289 fixed crash‑regression bugs (primary strategy). Human‑identified in 29/31 (one was Clouseau itself, one the patch author).

## When it fires
It is the go‑to strategy **when the stack is useless**:
- **23/31 are off‑stack** — the regressor's files never appear on the crashing frames.
- Of the on‑stack 8, most hit only the *assertion checker* (2046665, 1997896) or a **spurious basename collision** (`mod.rs`, `lib.rs`) — not the defect.
- A line‑proximity/blame scorer therefore **misses or misfires**; you must reason from crash text to patch description.

Six flavors:

| Flavor | Signal → patch | Examples |
|---|---|---|
| **A. Vendored dep bump** | crash names 3rd‑party lib ↔ "Update/Vendor X" (diff is only Cargo/versions files) | 1978822 time, **1992617 Glean**, 1992642/2011999 wgpu, 2033458 media3 |
| **B. Pref flip / enable** | crash names feature ↔ `StaticPrefList.yaml` flip | **2003956**/2005449 MicroTaskQueue, 2030700 CompressionDict, 1992573 GPU assert |
| **C. New feature/symbol** | signature names class/fn/exception the feature introduced | **1980992** GetFaviconForPage, **2035852** FilterListLoader, 1995662, 2003234, 2050090, 2017910, 2019599, 2026487, 2030604 |
| **D. IPC_FAIL invariant** | abort msg quotes fn + false invariant → grep string → blame | **2019759**, 2019763, 2024633 (a11y) |
| **E. Exact string in diff** | crash msg quotes resource the diff added verbatim | **2042859** `warning-large.png` |
| **F. Semantic (no literal overlap)** | needs symptom→domain step | 1987786 libresolv→DNS→SRV, 1978822 "subtract w/ overflow"→time, 2030700 dcb/dcz→compression |

## The recipe (actionable)
1. **Bound candidates** with the first‑affected build's pushlog window (Buildhub). ~70–200 bugs; `regressor_in_window` is usually true.
2. **Extract distinctive tokens** from the *full* crash text — signature, moz_crash reason, **and the raw Java/Rust exception message** (not the truncated signature). Grab: symbol/class/fn FQNs, feature/pref names, `chrome://`/`resource://` URLs, enum values, IPC_FAIL invariant strings.
3. **Down‑weight generic basenames** (`mod.rs`, `lib.rs`, `Assertions.h`, `MozPromise.h`) — these are the #1 false‑positive source.
4. **Match** tokens against each candidate's *summary + diff paths + diff‑introduced symbols*. For new‑symbol cases use the **diff**, not the summary (summaries omit the token — "rich content blocking engine" never says `FilterListLoader`).
5. For exact‑string/IPC_FAIL cases, **grep the quoted string across candidate diffs and blame** to the introducing changeset.
6. **Check exposer**: if the crash's fix would land in files the regressor never touched (and it's a bump/pref‑flip/correctness‑tightening), flag *"likely exposer — right bug to back out, fix lands elsewhere."*

## ⚠️ Exposer caveat (~12/31)
Nearly 40% are **correct patches that only exposed a latent bug** (Glean's non‑monotonic clock, a missing libresolv error check, stale ScriptPreloader bytecode, corrupt cache entries, unguarded shutdown callers). The token match still names the right changeset to **back out / needinfo**, but the actual code fix is *outside* the regressor's diff. Do not tell the author to "fix your patch."

## Automatable? Yes.
Already proven: **2035852** was produced by Clouseau and human‑confirmed. Build:
- **Raw‑text ingestion** (full moz_crash + exception message + CI/treeherder log) — the decisive token is empty/absent from the structured signature in 2042859, 1992617, 2030466.
- Token extractor with basename down‑weighting; pushlog candidate set; diff‑symbol/path matcher; **hg blame + grep‑in‑diff**; **searchfox** for symbol→file; an **LLM/subsystem index** for the semantic (F) cases.

**FP guardrails:** FQN‑only matching (no basenames); disambiguate same‑domain window collisions via the signature string (2019763 gfx‑vs‑a11y trap); flag exposers; ingest raw text (avoid truncated‑signature misses); note that **train‑hop system‑addon regressors (2012009) are out of the pushlog window** and need a separate candidate source.

## Representative bugs
- **2003956** — `MOZ_CRASH(Didn't consume MicroTask)` ↔ "Enable JS MicroTaskQueue by default" (textbook off‑stack pref flip; token in structured field).
- **1992617** — Kotlin `…glean.private.StringMetricType` ↔ "Vendor Glean SDK v65.2.1" (+ `third_party/rust/glean-core` in diff).
- **2035852** — signature `mozilla::FilterListLoader` ↔ the content‑blocking‑engine patch that introduced the class; **Clouseau identified it**.
- **2019759** — IPC_FAIL "Attempt to embed doc which already has an embedder" names fn+invariant ↔ the a11y patch that added the check (grep→blame).
- **2042859** — moz_crash quotes `chrome://…/warning-large.png` ↔ the exact string the CSS diff added (but structured `moz_crash_reason` was empty — ingest raw).
- **1980992** — moz_crash reason embeds creation site `GetFaviconForPage` ↔ the favicon async rewrite.


### searchfox_reasoning (n=28)

**Automatable:** PARTIALLY-TO-LARGELY YES, and not hypothetically: 3/28 of these regressors were already pinned by an LLM doing exactly this reasoning (2037923, 2042018, 2043955), then relayed and confirmed by humans in Bugzilla — crash-clouseau/augmented IS this automation and is already landing correct root-causes live. Separate two sub-tasks. CANDIDATE SURFACING is broadly automatable: 22/28 (79%) are reachable by at least one mechanical signal (line_crossing 9, keyword/symbol match 6, pushlog area_match 6, callgraph short-hop 1); only 6/28 (21%) are needs_human_reasoning (corruption/OOM/Rust-panic-plumbing stacks whose captured frames have zero connection to the regressor). PINNING/RANKING the true cause among the surfaced decoys is where the reasoning is essential and is exactly what the agent adds. Data/tools needed (most already present or cheap): (a) symptom decoder for moz_crash/assertion text + poison-address table + exception type (pure parsing); (b) a signature/symbol->subsystem index that extracts type/function/namespace identifiers from the signature AND the frame function-signature TEXT (not just the file field) and matches them against each candidate changeset's touched_identifiers and bug summary — catches the type-coupling / off-stack-but-named cases (1979078,1985037,2007754,1997854,1992849); (c) buildhub/pushlog window + AREA match at three granularities (same-file, same-dir, 2-level prefix) needing buildid->revision (recoverable via the TaskCluster index even with Buildhub dead) + pushlog — the coarse prefix is what catches subtree-mismatch exposers (1997854); (d) searchfox call-graph for producer/consumer + data/lifetime edges (calls_from/to/between/define/search ALREADY exist in spike/searchfox_cli.py) — catches 2038533 and 2018081; (e) hg/git blame to pin an introducing changeset for a construct; (f) bisection/mozregression ONLY as a candidate generator, never the decider — it repeatedly pins the wrong bug (1991950->1982950, 1980730->1572644, release bot 1987047->1984088); (g) an exposer classifier comparing fix-diff location vs regressor diff. Two concrete collector fixes (spike/collect_regressor_dataset.py builds stack_files/on_stack from os.path.basename(file) intersections only): (i) normalize Android R8/ProGuard frames by mapping the module FQN -> source path (frame.file='R8$$SyntheticClass' but frame.module names the real class) — this turns current FALSE NEGATIVES 1992849 and 2042018 into hits; (ii) unwind Rust-panic-plumbing top frames to the signature symbol / below rust_begin_unwind (1985037,2047449,2043955). Also ingest the comment-0 pasted stack: several bugs had an EMPTY structured crash_stack (sec bugs / debug-assertion-only / hung-main-thread not the signature thread: 1997818,2008698,1992195,2037923), so clouseau-as-deployed had nothing to score. FALSE-POSITIVE RISK is the crux and is HIGH if any single mechanical signal is trusted as the answer: the signals surface the true regressor together with convincing decoys and misranking is the default failure mode — a recent exposer above the older true cause (the release bot did exactly this on 1987047, a 4-week gap), bisection on a follow-up (1991950,1980730), an on-stack innocent consumer above the off-stack producer (2018081), a same-file different-function change (2034899,2001989), or an extract-method refactor that merely MOVED the crashing line (the canonical known FP: 4cb85f483290 / bug 2023670). Keyword match also over-fires on broad efforts ('Fluent migration' spans many bugs; only the filter-strings one is right, 2047449). Mitigation: emit a RANKED candidate set with an explicit causal-mechanism rationale and an exposer flag, treating line/keyword/area/bisection as priors to reason over rather than as the answer — the augmented design already demonstrated on 3 of these 28.

**Key signals:** moz_crash / assertion text as a semantic fingerprint of the broken invariant (e.g. 'mBlocked > 0' parser-block underflow 1997818; 'kind != BailoutKind::Unknown' JIT bailout-not-propagated 2018081; 'Cycle collected object used on a thread without a cycle collector' 2038533; 'entered unreachable code' Rust unreachable!() 2007754; 'Shutdown hanging at step XPCOMWillShutdown' 2037923); Poison / sentinel / bogus-argument addresses that name the failure CLASS: 0x4b4b4b4b JS_SWEPT_TENURED_PATTERN GC-UAF (1980730), 0xe5e5e5e5 UAF (1991950), 0xCECACE11 MozPromise sentinel (2034899), ElementAt(aIndex=18446744073709551615,aLength=0)=LastElement-on-empty (2001989); Crash-signature SYMBOL naming a type/function/namespace even when its file is not the regressor's file: mozilla::HardwarePreference inside an EnumSerializer<> frame (1979078), glean_core::event_database::EventDatabase::record (1985037), wgpu_hal::dx12::Adapter::expose (2007754), sandbox::ParameterSet::Get (1997854), org.mozilla.fenix...PlayStoreReviewPromptController in the Android module field (1992849); Object-lifetime / UAF mechanism reasoning: raw pointer or hashtable-entry reference held across a call that frees/mutates/moves the target (2007199, 1988931, 2008698, 1987047, 2034899, 2038533; GC-moves-buffer 1991950); Coupled-code / incomplete-change reasoning: enum extended but paired IPC serializer bound not updated (1979078); wrong event subtype set at send, crash at deserialize (2034966); -3 underflow into size_t (1984505); missing null-check lets optimizer drop a bounds check (1992195); missing return-value propagation (1997818); missing setBailoutKind in a new MIR producer (2018081); Timing / ordering / concurrency reasoning: present-before-submit (2001989); offline driver advances time when not rendering (1985195); low-priority I/O held under a lock during shutdown (2037923); ActorDestroy empties a promise holder before a late reply resolves it (2034899); Code-navigation artifacts that pin the mechanism/introducer: searchfox permalinks to exact lines (1979078,1984505,1990785,1997818,2008698,2007199); hg/git-blame ('Since <rev>#l1.124' 1988931; 'was added in bug X' 2007199/1979078); pernosco record/replay sessions (1984505,1985195,2018081); Exposer-vs-cause discriminator: does the FIX land inside the named regressor's diff (true cause) or outside it (regressor is a correct patch that exposed a latent bug)? Fix-outside-diff flags an exposer in 2001989, 2034899, 2043955, 2047449, 1997854, 2013289, 2012855, and 2-of-3 regressors in 1992849; Corroborators (secondary): ESR/branch regression-range testing (1980730); first-crashing-build date vs a landing (1985037,2012855,2001989); backout eliminating the crash (2006934,1992849); duplicate bugs with deobfuscated stacks (1992849); module-owner recognition of own recent rework (Places/Gtk/parser/WebRTC/sandbox/WebGPU owners)

**Representative bugs:** 1979078, 1991950, 1987047, 2043955, 2018081, 1992849

# Strategy report: `searchfox_reasoning`

**Primary for 28 / 289 fixed crash-regression bugs.** The investigator reads the source (searchfox, blame, pernosco, candidate diffs) and reasons *symptom to mechanism to code-location to blame to bug-number* — as opposed to mechanically asking "did a recent changeset touch a file on the crash stack." It is the strategy that finds off-stack culprits and, on-stack, ranks the true cause above look-alikes.

## Distribution (what the 28 look like)

| Axis | Split |
|---|---|
| Regressor location | off-stack **15**, on-stack **11**, unknown **2** |
| Who identified | human **24**, regressor-author **1**, **LLM 3** (2037923, 2042018, 2043955 — "Claude thinks…"/"Opus 4.8") |
| Auto-detectability | line_crossing **9**, keyword/symbol **6**, pushlog-area **6**, callgraph **1**, **needs_human_reasoning 6** |

Read the detectability row two ways: **22/28 (79%) are at least *surfaceable* by a mechanical signal**, but *surfaced ≠ pinned* — the reasoning is what disambiguates. Only **6/28 (21%)** are truly out of mechanical reach (corruption / OOM / Rust-panic-plumbing stacks).

## How it works

1. **Decode the symptom.** The moz_crash/assertion text, signature symbol, poison address, or exception type already names the failure class (UAF, underflow, null-deref, panic-kind, hang).
2. **Reason to the defective construct** through the subsystem's semantics (object lifetime, coupled enum/serializer, send/deserialize type, timing/lock ordering).
3. **Blame it to a bug** via searchfox/blame/diff-reading.
4. **Disambiguate exposer vs cause.** In ~9 of the 28 the fix lands *outside* the named regressor's diff: the regressor is a correct patch that unmasked a latent bug. Only mechanism reasoning resolves this.

It frequently **overrides a wrong automated/bisection answer** (1987047, 1991950, 1980730): the bot/mozregression pins a recent exposer or follow-up; a human pins the older/true cause by mechanism.

## Key signals (reusable)

- **Assertion / moz_crash text** = the broken invariant (`mBlocked > 0`; `kind != BailoutKind::Unknown`; "Cycle collected object used on a thread without a cycle collector"; "entered unreachable code").
- **Poison / bogus-arg addresses** = failure class (`0x4b4b4b4b` swept-tenured GC-UAF; `0xCECACE11` MozPromise sentinel; `ElementAt(aIndex=18446744073709551615,aLength=0)` = LastElement-on-empty).
- **Signature symbol naming a type/fn/subsystem even off its own file** (`mozilla::HardwarePreference` inside an `EnumSerializer<>` frame; `wgpu_hal::dx12::Adapter::expose`; `glean_core::event_database::EventDatabase::record`; the Android `module` FQN naming the real class).
- **Lifetime/UAF, coupled-change, and timing/ordering mechanism patterns** (raw ptr held across a free/mutate/move; enum grown but serializer bound not; wrong subtype at send vs deserialize; present-before-submit; low-prio I/O under a lock at shutdown).
- **Exposer discriminator:** fix-diff location vs regressor-diff location.

## Can crash-clouseau automate it?

**Yes, partially-to-largely — and it already has:** 3 of these 28 were pinned by an LLM doing this reasoning and confirmed by humans. Surfacing candidates is ~79% mechanizable; the *ranking/pinning* is the agent's job.

**Tools/data needed** (★ = already in-repo):
- Symptom decoder (assertion text + poison-address table + exception type). Cheap parsing.
- **Signature/symbol → subsystem index**: extract identifiers from the signature *and the frame function-signature text*, match vs each candidate's `touched_identifiers` + summary. Catches type-coupling / off-stack-named cases.
- **Buildhub/pushlog area match** at 3 granularities (file, dir, 2-level prefix); needs `buildid→revision` (recoverable via the TaskCluster index even with Buildhub dead). Coarse prefix catches subtree-mismatch exposers (1997854).
- **Searchfox call-graph** ★ (`calls_from/to/between/define/search` in `spike/searchfox_cli.py`) for producer/consumer + data/lifetime edges (2038533, 2018081).
- hg/git **blame**; **bisection as a candidate generator only** (it pins the wrong bug in 1991950/1980730/1987047).
- **Exposer classifier**: fix-diff vs regressor-diff.

**Concrete pipeline fixes** (`spike/collect_regressor_dataset.py` builds `stack_files`/`on_stack` from `basename(file)` intersections only):
- **Normalize Android R8/ProGuard frames**: map `module` FQN → source path (`file` is `R8$$SyntheticClass`, `module` names the class). Turns the current **false-negatives 1992849 and 2042018 into hits.**
- **Unwind Rust panic plumbing** to the signature symbol / below `rust_begin_unwind` (1985037, 2047449, 2043955).
- **Ingest the comment-0 pasted stack**: several bugs had an *empty* structured stack (sec bugs / debug-assert-only / hung-main-thread ≠ signature thread: 1997818, 2008698, 1992195, 2037923), so clouseau-as-deployed had nothing to score.

**False-positive risk — HIGH if any single signal is trusted as the answer.** The signals surface the true regressor *with* convincing decoys; misranking is the default failure:
- recent **exposer above the older true cause** — the release bot did exactly this on 1987047 (4-week gap);
- **bisection on a follow-up** (1991950 → 1982950, 1980730 → 1572644);
- **on-stack innocent consumer above the off-stack producer** (2018081);
- **same-file/different-function** change (2034899, 2001989);
- **extract-method refactor** that merely *moved* the crashing line — the canonical known FP (`4cb85f483290` / bug 2023670);
- **keyword over-fire** on broad efforts ("Fluent migration" spans many bugs; only the filter one is right — 2047449).

*Mitigation:* emit a **ranked candidate set + explicit causal-mechanism rationale + exposer flag**, treating line/keyword/area/bisection as priors to reason over, not as the answer — the augmented design already proven on 3 of these bugs.

## Representative bugs

- **1979078** — off-stack, zero stack overlap: enum grown (`HardwarePreference`) but the paired IPC serializer bound not; culprit type rides in the *frame function text*, not the file. Signature-symbol match, not line-crossing.
- **1991950** — off-stack GC-vs-overlapped-IO corruption surfacing in the JS GC; mozregression pinned the wrong follow-up and was overridden by code reading. Reasoning-beats-bisection.
- **1987047** — human-over-bot: true cause landed **4 weeks before** crashes began; the bot picked the recent exposer. The window/ranking is the hard part, not the signal.
- **2043955** — **LLM-driven** (Opus 4.8) off-stack exposer: regressor in `ScriptLoader.cpp`, crash+fix in `ScriptLoadContext.cpp` (same dir). Proof the automation works on a `needs_human_reasoning` case.
- **2018081** — producer/consumer: on-stack `LIRGenerator` is innocent; off-stack `SubarrayReplacer` producer is the culprit. Shows *surfaced ≠ pinned*.
- **1992849** — Fenix R8 obfuscation + 3-regressor cause/exposer/amplifier disambiguation; the fix is normalizing the `module` FQN so the file matcher stops missing it.


### author_self_report (n=28)

**Automatable:** Mostly yes for SURFACING the regressor, no for the final verdict. Clouseau cannot perform a social self-report, but it does not need to: in 25/28 bugs the regressor is independently reachable by a structural signal (detectability = line_crossing 16, area_match 5, callgraph 2, keyword_match 2); the author self-report was just the faster human path to the same answer. Only 3 are out of static reach (needs_human_reasoning 1994855 and 2018012; needs_bisection 2007270).

Two cheap, high-value additions would let clouseau converge on its own:
(1) Authorship/ownership booster. The compact dossiers carry landing_revs but NO author field (verified: grep for '\"author\"' returned 0). Fetch changeset authors (hg json-rev / lando git) and cross-reference against the bug's assignee/fixer/needinfo target. 'Candidate regressor authored by the person now on the bug' is an extremely high-precision re-ranker and also relabels the 6 silent-field cases (first mention = bot) as author-driven rather than automated.
(2) Close the stack-parsing gaps. on_stack is null in 11/28 and stack_files is empty in 10/28, because C++ fuzzer backtraces carry symbols but no file/line, and Java/Kotlin (Fenix) stacks are not parsed at all. Add symbol->file resolution (searchfox / debug-symbol map), a Java/Kotlin parser with R8 synthetic-class deobfuscation, and read past the top-10 frames. Bug 1999784 proves the payoff: top-10 truncation plus an R8$$SyntheticClass file rewrite turned a genuine on-stack line hit (Store.dispatch at frame 12) into a spurious off-stack classification.

Data/tools needed, most already available: buildhub pushlog first-bad-build window (present as first_build.window_sample); a signature/moz_crash/pref -> subsystem keyword index (keyword_match cases); searchfox one-hop caller/callee callgraph plus symbol->file (callgraph 1993404/2010601 and every empty-stack case); VCS authorship map (the booster above). Bisection/mozregression is the ONLY route for 2007270 and is expensive (needs a reproducible STR), so treat it as last resort / confirmation.

False-positive risk is real and must be gated. (a) Exposers (~7 bugs: 1994855, 2007270, 2010601, 2018012, 1986223, 1999784, 2036042): the correct patch changed timing/threading/frame-tree and the fix lands OUTSIDE its own diff, so blame-on-fixed-file or 'author's most-recent touch' heuristics mis-blame the wrong pre-existing commit or over-blame a benign refactor -- exactly the extract-method false positive already seen in the canary. Authorship match must be corroboration, never an auto-culprit. (b) Whole-subsystem refactors: 101-file wasm (1987624), 54-file view (2010601) give weak area signals competing with other in-window changes -- rank, do not conclude. (c) Generic exception classes: Fenix FileUriExposedException/IllegalStateException are stock OS types, so keyword match is near-useless; lean on component + directory-area match. (d) Timing gaps: 2036042's labeled root cause predates the crash's own regression window by ~4 days, so a naive pushlog-window filter surfaces the exposer (2027803), not the cause.

Bottom line: clouseau can rank the true regressor into its top candidates in ~85-90% of these bugs using line/area/callgraph/keyword scoring plus an authorship booster -- the actionable win for a release engineer -- but it cannot by itself resolve cause-vs-exposer or the 2-3 reasoning/bisection-only cases, so it should present ranked candidates with evidence (and flag likely exposers), not a single verdict.

**Key signals:** Authorship identity match: the changeset author of a candidate regressor equals the bug's assignee, fixer, or needinfo target. Must normalize email aliases (iorgamgabriel@yahoo.com <-> giorga@mozilla.com; emilio@crisal.io <-> ealvarez@mozilla.com; canaltinova@gmail.com dataset name).; regressed_by set with NO regressor-citing comment and NO bisection/mozregression in-thread, with the first comment mention being a release-mgmt bot echoing the field (silent author attribution; 6/28 bugs).; Fast turnaround: bug filed/taken and fix attached the same day, fix authored and pushed by the same person.; Component/module ownership: crash component == regressor component and the namer owns that module (GC=jcoppeard, Layout/DOM refactors=emilio, GMP=aosmond, wasm=rhunt, editor=masayuki).; On-stack line hit: regressor diff touches the exact crashing file+function/line (1981751 FileID, 1991250 CalculateCacheFlag, 2004166 PushAbsoluteContainingBlock, 2029827 MozGetProcAddress) -- the co-signal an automated line scorer shares.; Feature-gated reproduction: the crash reproduces only with a Nightly pref the regressor introduced (1995409 use_js_microtask_queue; 1985765 --enable-symbols-as-weakmap-keys).; Signature/summary keyword tie: the crash signature symbol or moz_crash reason names the feature the regressor added (2029827 MozGetProcAddress, 1997613 GetAnchorPositionTargetDetailsRelation, 1995409 MicroTaskQueue, 2014723 RecvRequestScreenPixels).; First-bad-build pushlog window containing the regressor (first_build.regressor_in_window=true) to bound the candidate set.; Assertion-flip tell: the crash is a MOZ_DIAGNOSTIC_ASSERT / MOZ_ALWAYS_SUCCEEDS the regressor just added or upgraded from MOZ_ASSERT (1985765, 1986223, 1987845, 2004166) -- frequently an exposer rather than a logic defect.

**Representative bugs:** 1981751, 1995409, 2029827, 2010601, 2007270, 2049602

# Strategy report: `author_self_report`

**Prevalence:** 28 / 289 fixed Firefox crash-regression bugs (~10%) had this as their primary regressor-ID strategy. In every one, `who_identified = author`.

## What it is
The engineer who wrote the regressing patch is the same person who diagnoses the crash (and, in almost all cases, writes the fix). Identification is recall from memory, not a search: the author recognizes their own recent change from the signature, the summary, or a needinfo ping, and names the **exact** bug out of an 80-300-bug first-bad-build pushlog window with no bisection.

## How it shows up (4 trigger patterns)
1. **Needinfo -> owner-who-is-author.** A triager routes to the module owner ("Maybe Nazim has ideas"; "petru, could it be your recent changes?") who turns out to be the regressor's author and recognizes it instantly. — 1981751, 1986436, 2014613
2. **Unprompted self-recognition.** Author names their own bug in the first substantive comment ("most likely from bug 1994942"; "aligns too well in time to not be the case"). — 2010601, 1986223, 1993404, 2007047, 2036042, 1998838
3. **Dogfooding / feature-gated.** Author hits the crash running their own Nightly pref, or names the bug on the sec-approval form. — 1995409 ("been running with use_js_microtask_queue for weeks"), 1985765
4. **Silent field-set.** Author files/takes the bug and just sets `regressed_by`; no bug-number comment, no bisection; a release-mgmt bot later echoes it — so the "human-identified" heuristic misfires to *bot*. — 2002697, 2029827, 2010056, 2010764, 2021327, 2030461, 2049602 (6/28 have a bot as first mention)

**Confirmation** is the author landing a fix in their own code, often same-day. When bisection (bugmon/mozregression) appears, it arrives *after* the self-report as corroboration (1998838, 2004166, 2010601, 2014613).

## Signals to key on
- **Author == assignee/fixer/needinfo target** (normalize email aliases: `iorgamgabriel@yahoo.com` ↔ `giorga@mozilla.com`; `emilio@crisal.io` ↔ `ealvarez@mozilla.com`).
- **`regressed_by` set with no citing comment + no bisection**, first mention a release-mgmt bot → silent author attribution.
- **Same-day file→fix**, fix authored+pushed by the same person.
- **Component/ownership:** crash component == regressor component and the namer owns it (GC=jcoppeard, Layout/DOM refactors=emilio, GMP=aosmond, wasm=rhunt).
- **On-stack line hit:** regressor diff touches the exact crashing file+function (1981751, 1991250, 2004166, 2029827).
- **Feature-gated repro:** crash only with a Nightly pref the regressor added (1995409, 1985765).
- **Signature/summary keyword tie:** signature symbol or moz_crash names the added feature (2029827 `MozGetProcAddress`, 1997613 `GetAnchorPositionTargetDetailsRelation`, 2014723 `RecvRequestScreenPixels`).
- **Assertion-flip tell:** crash is a `MOZ_DIAGNOSTIC_ASSERT`/`MOZ_ALWAYS_SUCCEEDS` the regressor just added/upgraded (1985765, 1986223, 1987845, 2004166) — often an *exposer*.

## Can crash-clouseau automate it?
**Mostly yes for surfacing, no for the verdict.** Clouseau can't do a social self-report, but it doesn't have to: the regressor is independently reachable by a structural signal in **25/28** cases (line_crossing 16, area_match 5, callgraph 2, keyword_match 2). Only **3** are out of static reach (needs_human_reasoning 1994855/2018012; needs_bisection 2007270).

Two cheap, high-value additions:
1. **Authorship/ownership booster.** Dossiers carry `landing_revs` but *no author* (verified). Fetch changeset authors (hg json-rev / lando) and cross-reference the bug's assignee/fixer/needinfo. "Candidate authored by the person now on the bug" is a very high-precision re-ranker and relabels the 6 silent-field cases as author-driven (not bot).
2. **Fix the stack-parsing gaps.** `on_stack` is null in **11/28** and `stack_files` empty in **10/28** — C++ fuzzer backtraces carry symbols but no file/line, and Java/Kotlin (Fenix) stacks aren't parsed at all. Add symbol→file resolution (searchfox/debug-symbol map), a Java/Kotlin parser with **R8 synthetic-class deobfuscation**, and read past the top-10 frames. **1999784** proves the payoff: truncation + an `R8$$SyntheticClass` rewrite turned a real on-stack hit (`Store.dispatch`, frame 12) into a spurious off-stack.

Already/easily available: buildhub pushlog window (`first_build.window_sample`), a signature/pref→subsystem index (keyword_match), searchfox one-hop callgraph (1993404, 2010601). Bisection is the only path for 2007270 — expensive (needs STR); keep as last resort/confirmation.

### False-positive guardrails
- **Exposers (~7 bugs)**: correct patch changed timing/threading/frame-tree, fix lands *outside* its own diff (1994855, 2007270, 2010601, 2018012, 1986223, 1999784, 2036042). Blame-on-fixed-file or "author's latest touch" mis-blames the wrong commit or a benign refactor — the extract-method false positive already seen in the canary. **Authorship match = corroboration, never auto-culprit.**
- **Whole-subsystem refactors**: 101-file wasm (1987624), 54-file view (2010601) → weak area signal competing with other in-window changes. Rank, don't conclude.
- **Generic exceptions**: Fenix `FileUriExposedException`/`IllegalStateException` are stock OS classes → keyword match useless; lean on component + directory match.
- **Timing gaps**: 2036042's root cause predates the crash's own regression window ~4 days; a naive window filter surfaces the *exposer* (2027803), not the cause.

**Bottom line:** clouseau can rank the true regressor into its top candidates in **~85-90%** of these bugs via line/area/callgraph/keyword scoring plus an authorship booster — the actionable win. It should present ranked candidates + evidence (and flag likely exposers), not a single verdict.

## Representative bugs
| Bug | Why it's representative |
|-----|-------------------------|
| **1981751** | Clean on-stack line hit; author pinged via needinfo, self-recognizes (profiler `FileID`). |
| **1995409** | Author dogfooding his own pref-gated feature; signature literally names it (`MicroTaskQueue`) — author + keyword + feature-flip. |
| **2029827** | Crashing frame *is* the shim the regressor added; `regressed_by` preset at filing (aosmond). |
| **2010601** | OFF-stack exposer; emilio self-reports first, bisection corroborates; one-hop callgraph the only static route. |
| **2007270** | OFF-stack, cross-subsystem; no static signal survives — bisection-only. |
| **2049602** | Fenix, empty/Java stack; author==fixer via email alias, silent `regressed_by`, bot echoes; area_match only. |


### pushlog_area_match (n=14)

**Automatable:** PARTIALLY -- this is the most automatable of the off-stack strategies and Clouseau already owns most of the raw inputs, but it needs a new candidate path plus an area-scorer, and it is intrinsically a high-recall/low-precision RANKER, not an auto-pinner.

ALREADY HAVE: (1) buildhub.py + pushlog.pushlog_for_buildid() already turn a buildid into the build revision and the exact hg json-pushes window -- the spine of the strategy, and the same window is already captured per-bug in the dataset (first_build.window_sample / regressor_in_window). (2) crash-stats first-crash-date + build correlation to find the first-bad build. (3) A searchfox call-graph tool (mcp__searchfox__*), history/blame tools, deterministic area_experts, and an agent whose prompt already treats 'surfacing an area' as a valid lead and assigns low confidence when the only link is area/proximity.

MUST BUILD: (1) Candidate seeding from the FULL first-bad-build pushlog window, NOT just stack-file blame. Today candidates (interesting_chgsets) come only from blaming files that appear on the stack (inspector.inspect_stacktrace -> filelog -> amend -> Changeset.to_analyze), so a fully off-stack regressor is NEVER a candidate -- the documented off-stack-culprit gap and the exact reason this strategy is unautomated. (2) A changeset->Bugzilla-component resolver (moz.build DIR_TO_COMPONENT, `mach file-info bugzilla-component`, or the in-tree components map) so each window changeset can be scored by component-equality with the crash bug; this is the single highest-value automatable signal. (3) A signature/moz_crash -> subsystem keyword index, matched against candidate bug summaries and patch paths, as the discriminator that separates the true regressor from same-component siblings. (4) Optionally a searchfox call-graph / data-flow bridge to link an off-stack culprit to the stack frame -- helps call-adjacent cases but will NOT reach third-party-driver stacks (1992640/1992641), IPC-hop (2016312), data-flow (2011700 prepare_quad_impl->render_task_sanity_check), or R8-obfuscated Fenix (2031285) cases.

DATA/TOOLS: buildhub pushlog (have), crash-stats first-build/spike signal (have), moz.build/DIR_TO_COMPONENT component index (NEED), signature->subsystem keyword index (NEED), searchfox call-graph (have, limited reach), hg blame/history (have).

FALSE-POSITIVE RISK: HIGH if used to auto-pin. Windows carry 43-127 bugs and area/component match is high-recall/low-precision -- same-component siblings and decoys are routine (the WebGPU window was literally 'lots of WebGPU in there'; bug 1991206's window held a tests-only flat-tree decoy 1965847; bug 2023029 had multiple co-landed Selection changesets). Real cautionary case: the release-mgmt 'analyze the backtrace' bot proposed the WRONG regressor (bug 1990641) for BOTH 1992640 and 1992641 and was overridden by humans using area reasoning. And 3/14 are EXPOSERS where the matched changeset is correct and must NOT be reverted. Safe automation = emit a RANKED SHORTLIST of same-area in-window changesets plus a needinfo to the area owner/author, gated by the keyword/domain discriminator and calibrated to low confidence when the only link is area/proximity -- never an automatic backout or high-confidence culprit claim.

**Key signals:** First-appearance build / volume-spike marker in the crash bug ('started in buildid X', 'basically zero volume until X then hundreds/day') -- identifies the exact build whose pushlog to pull; First-bad-build pushlog window: buildhub build->build revision->hg json-pushes fromchange..tochange. This is the closed candidate set (43-127 bugs) and the spine of the whole strategy; the true regressor was in-window in every reachable case; Crash Bugzilla component == regressor Bugzilla component -- the single strongest discriminator (holds in ~13/14: HTTP, WebGPU, Gtk, Memory Allocator, DOM:Selection/Navigation, GMP, a11y, Fenix:Tabs); Directory/module overlap between crash stack files and the changeset's touched files (netwerk/protocol/http, memory/build, accessible/, gfx/wr/webrender) -- fires at file/dir level even when the crashing FUNCTION is untouched; Signature / moz_crash -> subsystem keyword+domain match used as the discriminator (WebGPU<->'Update WGPU to upstream'; render_task_sanity_check<->'size of a quad render task'; 'file picker'<->nsFilePicker portal rework; 'Shadow DOM/flattened tree'; scaled-coordinate caching<->AppWindow::Center recursion; OnSocketThread assert<->Http2Session teardown); Process/platform localization narrows the area (GPU process -> graphics/WebGPU; macOS/Apple-Silicon-only -> the page-size change; Linux-only -> Gtk widget); Patch-summary landmark recognition -- the changeset self-describes its area ('Update WGPU to upstream', 'Implement picker portal directly using dbus', the initial-about:blank rework, 'Don't cache scaled coordinates'); Recency + own land/backout timing: 'recent change in the relevant code', 'landed the day before', and the regressor's landing/backout coinciding with the crash appearing/disappearing; Confirmation-only signals (secondary, not the identification): local or tree backout stops the crash (2011700, 2023029, 2030017); area author self-confirms (emilio, egubler, pbone, azebrowski); reviewer/module ownership (r=webgpu-reviewers, r=Jamie, Selection owner Masayuki)

**Representative bugs:** 1992641, 2015436, 1981294, 2011700, 1981882, 1991206

# Strategy report: `pushlog_area_match` (primary for 14 / 289 bugs)

## TL;DR
The regressor is found by intersecting **WHEN** (crash localized to its first-appearance nightly build -> pull that build's pushlog window) with **WHERE** (the one changeset in that window whose *subsystem/area* matches the crash), **without any crashing stack line**. In 7/14 the regressor is fully off-stack (third-party driver frames, Rust panic machinery, obfuscated Fenix frames, garbage stack-overflow stacks); in the rest it is on the stack *file* but touched none of the crashing *functions*. 13/14 were pinned by a human triager/sheriff, usually speculatively, then confirmed by backout / author / domain reasoning.

## How it works (mechanics)
1. **Localize to the first-bad build.** Crash bug carries a fresh-signature / volume-spike marker: *"started in buildid 20250802092837"*, *"basically zero volume until 20260114211245, then hundreds a day"*.
2. **Pull that build's pushlog** (regression range) -> closed candidate set of 43-127 bugs.
3. **Match area to the window.** Rank/pick the changeset whose **Bugzilla component == crash component** (~13/14), whose **touched directories overlap the stack files**, and/or whose **summary matches a domain/keyword read** of the signature.
4. **Confirm** via local/tree backout, area-author self-report, or domain reasoning (these are confirmations, not the identification).

Two relationship flavours: **genuine cause** in the regressor's diff (11/14) vs. **exposer** -- a correct, kept patch that merely unmasked a latent bug fixed elsewhere (3/14: 1981294, 1998188, 2015436). Automation must not conflate them (see FP risk).

## Signals that make it work
| Signal | Why it works | Automatable from |
|---|---|---|
| First-appearance build / spike | Names the exact build to pull | crash-stats first-crash-date + buildhub |
| First-bad-build pushlog window | The closed candidate set; regressor always in-window | `pushlog.pushlog_for_buildid` (**have**) |
| Crash component == regressor component | Strongest discriminator (~13/14) | moz.build `DIR_TO_COMPONENT` (**need**) |
| Stack-file directory/module overlap | Fires even when the crashing function is untouched | stack files + changeset files (**have**) |
| Signature/`moz_crash` -> subsystem keyword | Discriminates true regressor from same-area siblings | keyword->subsystem index (**need**) |
| Process/platform (GPU / Apple-Silicon / Linux) | Narrows the area | crash metadata (**have**) |
| Patch-summary landmark | Changeset self-describes its area | pushlog descs (**have**) |
| Recency + own land/backout timing | "landed the day before"; appears/disappears with the patch | pushlog dates (**have**) |

## Can Clouseau automate it?
**Partially -- most automatable off-stack strategy, but a ranker not a pinner.**

- **Have:** buildhub->build-revision->hg pushlog window (the whole spine, already implemented and already captured per-bug); first-build/spike signal; searchfox call-graph + blame + area-experts; an agent that already treats "an area to point a human at" as a valid low-confidence lead.
- **Gap (why it's not automated today):** candidate changesets are seeded **only** by blaming files that appear on the crash stack (`inspect_stacktrace -> filelog -> amend -> Changeset.to_analyze`). A fully off-stack regressor is **never** a candidate -- the documented off-stack-culprit gap.
- **Build:** (1) seed candidates from the **full first-bad-build window**, not stack-file blame; (2) a **changeset->Bugzilla-component** resolver (`DIR_TO_COMPONENT` / `mach file-info bugzilla-component`) for component-equality scoring -- highest-value signal; (3) a **signature->subsystem keyword index** as the sibling discriminator; (4) optional searchfox call-graph/data-flow bridge (helps call-adjacent cases; won't reach third-party-driver / IPC / data-flow / R8 cases).

### False-positive risk: HIGH if auto-pinning
Windows hold 43-127 bugs; area match is high-recall/low-precision. Same-component siblings and decoys are routine (WebGPU window was "lots of WebGPU"; 1991206 had a tests-only flat-tree decoy; 2023029 had multiple co-landed Selection patches). The release-mgmt backtrace bot pinned the **wrong** regressor (1990641) for both 1992640/1992641 and was overridden by humans. 3/14 are **exposers** whose matched changeset is correct and must not be reverted.

**Safe automation:** emit a *ranked shortlist* of same-area in-window changesets + a **needinfo to the area owner/author**, gated by the keyword discriminator, low-confidence when the only link is area/proximity. **Never** an auto-backout or high-confidence culprit claim.

## Representative bugs
| Bug | Why it's representative |
|---|---|
| **1992641** | Paradigm off-stack: 100% third-party D3D12 + Intel driver frames, zero Gecko. First-build (20251004093758) pushlog "lots of WebGPU" -> expert picks "Update WGPU to upstream". Bot's backtrace guess was wrong. |
| **2015436** | On-stack-by-file-not-function: touched mozjemalloc.cpp but not the crashing Purge/PurgeLoop. Memory-Allocator component + Apple-Silicon domain disambiguator. **Exposer** (latent purge race). |
| **1981294** | Purest verbal form: *"That's the only thing I see in the range"* -- sole Networking:HTTP changeset in the first-build pushlog. **Exposer** (crash-fix unmasked a latent null-deref). |
| **2011700** | Full funnel: first-build spike -> filter window to WebRender/gfx (3 candidates) -> summary-keyword discriminator ("quad render task size" <-> `render_task_sanity_check`) -> local-backout confirm. Off-stack. |
| **1981882** | On-stack **cause**: nsFilePicker.cpp on the stack + "landed the day before"; lost `NS_ADDREF_THIS` -> UAF. Widget:Gtk both sides. |
| **1991206** | Bisection window + human area pick: bugmon bounds the range, htsai picks the one "Shadow DOM related" changeset over a tests-only flat-tree decoy. |

## Recommended action for release engineering
For any fresh-signature nightly crash: pull the first-bad-build pushlog and rank it by **component-equality + stack-directory overlap + signature-keyword match**, then **needinfo the top-ranked changeset's author/area owner** with the ranked shortlist. Treat this as *where to look*, not *what to back out* -- confirm with a backout or author before any revert, and expect ~1 in 5 to be an exposer whose fix belongs elsewhere.


### feature_flip (n=9)

**Automatable:** Verdict: PARTIALLY automatable -- enough to SHORTLIST and FLAG the regressor as a lead, but not to confidently PIN it alone, and only after a structural gap is closed. Today clouseau CANNOT reach these at all: its candidate set is built solely from changesets scored onto crash-stack frames by file+line proximity, and /home/calixte/dev/mozilla/crash-clouseau/crashclouseau/pushlog.py:collect() keeps only 'interesting' source files (config/interesting_extensions.json = c/h/cpp/cc/.../rs/java -- NO .yaml). A pure pref-flip's files are filtered out at collection, its description is discarded (only the bug number is kept, pushlog.py:49), it never enters the changesets table, it is never scored onto a frame, and build_seed (crashclouseau/agent/orchestrator.py:130) returns None / yields no candidate for it. So 8 of 9 of these regressors are invisible to the pipeline by construction -- the documented off-stack-culprit gap. To automate the strategy clouseau needs a NEW candidate path that is independent of stack proximity: (1) a full first-bad-build pushlog-window enumerator (it already has pushlog_for_buildid + buildhub.py) that does NOT apply the interesting-file filter and RETAINS the changeset desc; (2) a pref-flip/feature-enable detector -- diff touches only StaticPrefList.yaml/libpref (+ tests) AND/OR desc matches an enable/ship/ride-the-trains regex (a pretagger regex already exists but misfires: it missed 1989368's 'setting the pref to true'); (3) a signature/keyword->feature ranker that tokenizes the signature + crashing functions + MOZ_CRASH reason and matches them against the pref name and bug summary, ideally via a keyword->subsystem index; (4) component/subsystem corroboration from libmozdata buginfo (crash component vs regressor component); (5) first-seen-window corroboration from Buildhub (already on the roadmap). The offline gold-standard confirmation -- pref-toggle repro or bisection -- clouseau cannot run, though it could add a post-hoc 'disable-and-watch-crash-rate' telemetry check. False-positive risk: MEDIUM-HIGH. Every nightly window carries several 'enable X' pref flips, so the diff-shape detector alone over-fires; the failure modes are (a) blaming an unrelated co-shipped flip and (b) blaming a correct flip when the fix actually belongs to on-stack code. Controls: require at least one corroboration (keyword/component/first-seen) before surfacing; rank, don't assert; cap confidence to a 'probable/lead' rung unless the signature exactly names the feature; always label it an EXPOSER and never recommend a blind backout. Keep the on-stack line-proximity detector too -- 2033298 shows a feature flip that is simultaneously an on-stack line hit and the true cause, so the two detectors should corroborate rather than compete.

**Key signals:** Pref-flip diff shape (strongest, deterministic): the changeset diff touches ONLY modules/libpref/init/StaticPrefList.yaml (plus test manifests / .ini/.list/.js / WPT .meta) and NO C/C++/Rust source -- present in 8 of 9.; Enable/ship description: bug summary or changeset desc matches an enable-by-default pattern -- 'Enable X (by default|on nightly|on early beta)', 'Ship X', 'Let X ride the trains'.; Signature<->feature keyword bridge: a token in the crash signature / crashing function name / MOZ_CRASH reason matches the pref or feature name (MicroTask~MicroTaskQueue, ProcessCloseRequest~CloseWatcher, RaiseUiaNotificationEvent~ariaNotify, GetExecutionGlobalFromJSMicroTask~MicroTaskQueue).; Component/subsystem match: regressor bug component == crash bug component (Networking: HTTP, Disability Access APIs, JavaScript Engine, Layout), or crash subsystem == the feature's subsystem even without an exact keyword (Happy Eyeballs <-> nsHttpConnectionMgr).; First-seen / temporal coincidence: the crash's first-bad build IS the exact build that shipped the flip (regressor-in-window), and the crash-rate spike starts the day of default-enable ('since X got enabled today').; Repro/testcase feature token: the STR or testcase exercises the feature even when the signature does not name it (a <details>+<dialog> testcase pinning the details-content pref flip).; Empirical toggle/backout confirmation: flipping the pref off, or backing the changeset out, stops the crash.; Exposer tell: the landed fix is a code guard/null-check/keyword-handling in the feature's implementation (often in a different bug), NOT a revert of the pref -- so the flip is the exposer, not the root cause (7 of 9).

**Representative bugs:** 2000305, 2017903, 2043307, 2041239, 1982701, 1989368

## Strategy: `feature_flip` (enable-by-default / pref flip)

**Prevalence:** primary strategy for **9 / 289** fixed crash-regression bugs. **8/9 off-stack**; identified by a **human triager/peer in 7**, by the **feature author in 2**; the flip was an **exposer (real fix lands elsewhere) in 7/9**, the newly-enabled code was itself the bug in 2 (2033298, 2053610).

### How the regressor gets pinned
1. A landing **enables a feature by default** — the diff is basically just `modules/libpref/init/StaticPrefList.yaml` (+ test manifests / WPT meta). Descriptions: *"Enable X by default / on nightly / on early beta"*, *"Ship X"*, *"Let X ride the trains."*
2. The dormant code path goes live and a **new signature appears in the exact build that shipped the flip** — usually a topcrash, because it now hits by default.
3. Bisection only yields a **window (~113 changesets median)**; an off-stack pref flip touches no crash code, so range/line/callgraph scorers can't surface it.
4. A human makes the **semantic pin** via a keyword/subsystem/testcase bridge, then confirms by **pref-toggle or backout**. In most cases the flip merely **un-hid a latent bug** — the fix is a code guard elsewhere, *not* a pref revert.

### Reusable signals (ranked by strength / automatability)
| Signal | How to compute | Note |
|---|---|---|
| Pref-flip diff shape | diff touches only `StaticPrefList.yaml`/libpref (+ tests), no C/C++/Rust | Deterministic; 8/9. Over-fires alone. |
| Enable/ship desc | regex on bug summary / changeset desc | Pretagger exists but misses "set pref to true" phrasings |
| Signature↔feature keyword | tokenize signature + crashing fn + MOZ_CRASH; match pref/feature name | `MicroTask`↔MicroTaskQueue, `ProcessCloseRequest`↔CloseWatcher, `RaiseUiaNotificationEvent`↔ariaNotify |
| Component/subsystem match | crash-bug component vs regressor-bug component | Networking: HTTP, Disability Access APIs, JS Engine, Layout |
| First-seen / temporal | Buildhub: first-bad build == build that shipped flip | "since X got enabled today" |
| Testcase feature token | tokens from STR/testcase HTML | `<details>` testcase → details-content pref |
| Toggle/backout | pref off or backout stops crash | Gold-standard confirmation |

### Can clouseau automate it?
**Partially — shortlist + flag, not confidently pin, and only after closing a structural gap.**

Today the pipeline is **blind by construction**: candidates come only from changesets scored onto stack frames, and `crashclouseau/pushlog.py:collect()` keeps only source files in `config/interesting_extensions.json` (c/h/cpp/…/rs/java — **no `.yaml`**). A pure pref-flip is filtered out at collection, its desc discarded (only bug number kept), so it never becomes a candidate and `build_seed` (`crashclouseau/agent/orchestrator.py:130`) never sees it. This is the known off-stack-culprit gap.

**Build list (new candidate path, independent of stack proximity):**
- Full first-bad-build **pushlog-window enumerator** (reuse `pushlog_for_buildid` + `buildhub.py`) **without** the interesting-file filter, **retaining the changeset desc**.
- **Pref-flip detector**: diff = only `StaticPrefList.yaml`/libpref (+tests) and/or enable/ship/ride-the-trains regex on desc.
- **Signature→feature ranker**: token match of signature/crashing-fn/MOZ_CRASH against the pref name + bug summary (add a keyword→subsystem index).
- **Component + first-seen corroboration** (libmozdata buginfo already available; first-seen is on the roadmap).
- Surface as an **EXPOSER lead** ("fix is likely a code guard elsewhere; disabling the pref on beta is a stopgap, not the fix").

**False-positive risk: MEDIUM-HIGH.** Every nightly window has several "enable X" flips → diff-shape alone over-fires; the traps are blaming an unrelated co-shipped flip, or blaming a correct flip when the fix belongs to on-stack code. **Controls:** require ≥1 corroboration (keyword/component/first-seen); rank not assert; cap to a "probable/lead" rung unless the signature exactly names the feature; label exposer and never auto-recommend a backout. Keep the on-stack detector too (2033298 is a flip that's *also* an on-stack line hit and *is* the cause) so the two corroborate.

### Representative bugs
- **2000305** *(MicroTask)* — signature `RunMicroTask` ↔ "Enable JS MicroTaskQueue by default"; pure pref flip; crash-spike-on-enable; fixed by code elsewhere (bug 2002134). Textbook keyword match.
- **2017903** *(CloseWatcher)* — `RecvProcessCloseRequest` ↔ "Enable Close Watcher by default"; author-confirmed; fix = null-guard, not a revert.
- **2043307** *(ariaNotify)* — signature names `RaiseUiaNotificationEvent`; "Ship ariaNotify" pref flip; same component (Disability Access APIs); fix = availability check.
- **2041239** *(Happy Eyeballs)* — "Enable Happy Eyeballs on nightly"; no exact keyword, pinned by **subsystem/component** match; fix in `HappyEyeballsConnectionAttempt.cpp`.
- **1982701** *(details)* — off-stack exposer where the signature does NOT name the feature; pinned via **testcase token** `<details>` ↔ details-content pref flip.
- **1989368** *(webkit-fill-available)* — hardest case: needed the **semantic bridge** (`IsLengthPercentage` assert → stretch-like keyword size) + pref-toggle repro.


### analogy_prior_bug — the regressor is not derived from THIS bug's own stack/pushlog; it is inherited from a prior or sibling crash bug that a human recognized as "the same crash" (identical/near-identical crash signature + moz_crash reason + component) and that already had a confirmed regressed_by. The recurrence is recognized ("same signature as / dup of / another regression of / a continuation of bug N") and the sibling's regressor is carried over. (n=7)

**Automatable:** Yes — and it is arguably the single highest-value, lowest-cost signal to add, because it reaches the off-stack/exposer regressors (3 of these 7) that the current on-stack line-proximity scorer in inspector.py cannot. The retrieval half already exists: crashclouseau/buginfo.py:get_bugs(signature) already maps a crash signature to prior Bugzilla bugs via a cf_crash_signature substring search plus Socorro's bug↔signature table, and already returns None for unreadable security bugs (i.e. it already knows to abstain on restricted siblings). Missing pieces are small and deterministic: (1) add regressed_by, see_also, component, keywords, resolution/dupe_of to buginfo.BZ_FIELDS; (2) a 'sibling-inheritance' pass that keeps siblings corroborating on >=2 axes (signature AND (exact moz_crash reason OR component OR an explicit see_also/dupe edge)), reads their regressed_by, and proposes inheriting it; (3) confidence tied to signature specificity, corroboration count, and the PROVENANCE of the sibling's regressed_by (confirmed-by-backout/mozregression > speculative). Data/tools needed, in priority order: Bugzilla (regressed_by/see_also/component — already a dependency), Socorro bug-signature table (already used), an exact moz_crash-reason index, and optionally one searchfox call to check whether the inherited regressor's diff touches any stack file (to LABEL exposer vs on-stack). No bisection is required for the analogy itself. False-positive risks: (a) generic-signature collisions — many unrelated bugs share a teardown/assert top frame, so signature-substring alone must never auto-inherit; require the >=2-axis corroboration and prefer an explicit see_also/dupe edge; (b) exposer inheritance — 4 of 7 regressors here are enablers/exposers (assert promotion, a pref flip, a validation-tightening) rather than root cause, so inherited answers should be flagged 'exposer/enabler — verify it introduced the defect, not just made a latent one reachable' when the regressor diff shares nothing with the stack; (c) stale/speculative sibling regressed_by propagating an error — cap confidence when the sibling's own pin was speculative; (d) restricted-sibling gaps — abstain (as buginfo already does) unless an authenticated token can read the hidden regressor. Net: high precision if gated on corroboration + provenance; treat as a corroborating/needinfo-grade signal for off-stack cases rather than an autonomous verdict.

**Key signals:** First-human-comment analogy phrase naming the sibling: 'same crash signature as bug N', 'looks like / almost a dup of bug N', 'another regression of bug N', 'a continuation of bug N', 'duplicate of bug N — mark the same way'. Regex-detectable and it hands you the sibling bug id.; Identical or near-identical cf_crash_signature to a prior/sibling bug, allowing small symmetric variants (std::__atomic_base fetch_add vs fetch_sub; the generic mozilla::DefaultDelete<T>::operator() / MozPromise teardown top frame).; Identical moz_crash reason / assertion string (MOZ_DIAGNOSTIC_ASSERT(IsLengthPercentage()); MOZ_DIAGNOSTIC_ASSERT(!IsHTMLWhitespace(...First())); MozPromise "created from 'ObtainAndCacheFaviconAsync'") — a far stronger discriminator than the top frame alone.; Same Bugzilla component as the sibling (Core::DOM: Navigation, Graphics: Canvas2D, Layout, Widget: Win32).; The sibling already has regressed_by set — ideally empirically confirmed (backout→crash-stopped→reland→crash-returned, or mozregression), not itself speculative — which is the number to inherit.; Bugzilla relationship edges linking the two bugs: see_also, dupe_of, duplicate marks, and 'regressions'; and temporal adjacency / repeat-offender wording ('another', 'continuation') implying the sibling is a known recurring regressor.; Provenance gotcha: the regressor number often first appears from release-mgmt-account-bot AFTER a human set regressed_by via the analogy — a bot first-mention does NOT mean the identification was automated or derived from the stack (mislabels who_identified).

**Representative bugs:** 1990034, 2003517, 2007774, 1988912, 1979113

## Strategy: `analogy_prior_bug`

**Primary strategy for 7 / 289 fixed Firefox crash-regression bugs.**

The regressor is **not** derived from this bug's own stack or pushlog. A human recognizes the crash as a **recurrence of an already-diagnosed bug** and **inherits that sibling's `regressed_by`**. This is the main route by which humans nail **off-stack / exposer** regressors that a proximity scorer can never reach.

### How it works
1. New crash comes in. A triager/module owner recognizes it as "the same crash" as a prior bug, stated in the **first human comment** with an analogy phrase.
2. The link is anchored on the crash's own fingerprint — **cf_crash_signature top frame(s)**, the **exact moz_crash/assertion reason string**, and the **component** — not on any regressor code.
3. The sibling already carries a `regressed_by` (often itself confirmed by backout→reland or mozregression). That number is **copied over**, frequently as a spun-off bug for a distinct sub-path.
4. **Provenance trap:** after the human sets `regressed_by`, `release-mgmt-account-bot` posts "Set release status flags based on info from the regressing bug N", so the regressor number's *first textual mention* is a bot — even though a human reasoned it out.

### Key signals (reusable)
- **Analogy phrase in comment 0/first human comment** — `same crash signature as bug N` / `another regression of bug N` / `almost a dup of bug N` / `a continuation of bug N` / `duplicate of bug N — mark the same way`. Regex-detectable; hands you the sibling id.
- **Identical / near-identical `cf_crash_signature`** to a prior bug (allow symmetric variants: `fetch_add` vs `fetch_sub`; generic `DefaultDelete<T>::operator()` / MozPromise teardown).
- **Identical moz_crash / assertion string** — a much stronger discriminator than the top frame alone.
- **Same Bugzilla component** as the sibling.
- **Sibling already has `regressed_by`** — prefer ones empirically confirmed (backout/reland, mozregression) over speculative.
- **Bugzilla edges**: `see_also`, `dupe_of`, duplicate marks; plus repeat-offender wording (`another`, `continuation`).

### Representative bugs
| Bug | Analogy (first human comment) | Sibling → inherited regressor | Off-stack? |
|---|---|---|---|
| **1990034** | dholbert: "same crash signature as bug 1989368 … likely same regressor" | 1989368 → **1988938** (`-webkit-fill-available` pref flip) | **Yes** (pref flip, exposer) |
| **2003517** | hbenl: "another regression of **Bug 543435**" | repeat offender → **543435** | on-stack (docshell) |
| **2007774** | hsivonen: "duplicate of bug 2004647 … mark the same way" | 2004647 → **543435** | on-stack |
| **1988912** | lsalzman: "almost like a dup of bug 1933572, but for fetch_add instead of fetch_sub" | 1933572 → **1933572** | on-stack |
| **1979113** | ryanvm: "a continuation of bug 1974901?" | 1974901 → **1915762** (favicon caching) | **Yes** (favicon, exposer) |

(Edge case **2054485**: vhilla ties it to the sibling family of bug 2048851/2045635; the true regressor 2043820 is **security-restricted** — a case to *abstain* on without an auth token.)

### Can clouseau automate it? — **Yes, high-value, low-cost**
The proximity scorer (`inspector.py`) maps frames→files→window-changesets and is **purely on-stack**, so it structurally misses 1990034, 1979113, 2054485. This strategy is the fix for that gap, and **half of it already exists**:
- `buginfo.py:get_bugs(signature)` already resolves a signature → prior Bugzilla bugs (`cf_crash_signature` substring + Socorro's bug↔signature table) and already returns `None` for unreadable security bugs (built-in abstain).

**Build (small, deterministic):**
1. Add `regressed_by, see_also, component, keywords, dupe_of` to `buginfo.BZ_FIELDS`.
2. **Sibling-inheritance pass**: keep siblings corroborating on **≥2 axes** (signature AND (exact moz_crash reason OR component OR an explicit `see_also`/`dupe` edge)); read their `regressed_by`; propose inheriting.
3. **Confidence** = f(signature specificity, corroboration count, provenance of the sibling's `regressed_by`).
4. Optional: one searchfox call to check whether the inherited regressor's diff touches any stack file → **label exposer vs on-stack**.

**Data/tools:** Bugzilla + Socorro (already dependencies); an exact moz_crash-reason index; optional searchfox. **No bisection needed** for the analogy itself.

**False-positive risks & guardrails:**
- **Generic-signature collision** — never auto-inherit on signature substring alone; require the ≥2-axis corroboration (prefer an explicit `see_also`/`dupe` edge).
- **Exposer inheritance** — 4/7 regressors here are enablers/exposers (assert promotion, pref flip, validation tightening), not root cause. Flag: *"exposer/enabler — verify it introduced the defect, not just made a latent one reachable"* when the regressor diff shares nothing with the stack.
- **Stale/speculative sibling pin** — cap confidence when the sibling's own `regressed_by` was speculative.
- **Restricted siblings** — abstain (as `buginfo` already does) unless an authenticated token can read the hidden regressor.
- **Labeling** — a bot first-mentioning the regressor number ≠ automated identification; don't record `who_identified=tool`.

**Bottom line for release eng:** ship this as a **corroborating / needinfo-grade** signal, not an autonomous verdict — it is the cheapest way to catch off-stack regressors the proximity scorer will always miss, provided it's gated on signature specificity + cross-bug corroboration and marks exposer-flavored inheritances.


### exposer_not_cause (n=6)

**Automatable:** Partially, and the highest-value automation here is DEFENSIVE (avoiding a false 'culprit'), not offensive (finding the exposer). Split it in two. (A) SURFACING the exposer as a candidate: clouseau's current line-proximity scorer fails on 5/6 because the regressor touches no crash-stack file (the touched-file check yields no candidate — this is exactly the documented off-stack blind spot); on the 6th (2051442) it surfaces the regressor but as a false high-confidence cause. Call-graph BFS / area-experts (searchfox) are weak here because the link is a data/timing dependency (safepoint tables, startup ordering, rule-count overflow), not a static call edge — Phase-0 off-stack recall is only 43-71% and these data-dep cases are the hard end. The one reliable automatable surfacing path is crash-rate-spike bisection: crash-stats build_id faceting to find the build where the signature jumps, then Buildhub/hg pushlog to enumerate the window (2041907/2042290 used exactly this) — but it lands on a ~100-170-changeset WINDOW and picks an EXPOSER, never the defect. One cheap free win: the crashtest-filename->bug-number convention (2017517) is a trivial regex and nails the increased-coverage flavor. (B) LABELING it correctly (exposer, not cause) — the part worth building. Automatable corroborators clouseau mostly already has: (1) FIX-DISJOINTNESS — fix.is_backout==false AND fix.files disjoint from the regressor's diff files; clouseau already collects both, and 'a proposed regressor + a non-backout fix in unrelated files' is the exposer signature (caveat: cross-repo/out-of-tree Thunderbird fixes land in comm-central, so in-tree fix data is empty for 2041907/2042290/2051442 and needs comm-central fix resolution). (2) POISON/UAF DECODE — match crash_address/crash_reason against a small jemalloc-poison/uninitialized/null-deref table (0xe5e5e5e5, 'poison read', ACCESS_VIOLATION_READ null) to bias toward 'latent lifetime bug exposed by timing' over 'the recent change is the defect'. (3) SAME-COMPONENT-BUT-OFF-STACK shape (2009140). (4) EXPOSER-LANGUAGE extraction from the thread ('exposing/surface/not wrong/finally exposing/real root cause is bug N') — the principal already reads comments; 'real root cause = bug <n>' is a structured extract. FALSE-POSITIVE RISK is the headline: the exposer pattern is the anti-pattern of proximity scoring. 2051442 is the canonical trap — a recent mailnews/search feature + a mailnews/search crash scores as a confident culprit under line/area proximity, but it is an exposer whose fix lives elsewhere. So exposers are a systematic false-positive GENERATOR for clouseau. Net: clouseau generally cannot, from static signals alone, both find the off-stack exposer AND name the real defect (the 'process-isolation -> startup timing -> IMAP race -> 2011 bug' leap is human domain reasoning). The right target is: when the exposer corroborators fire (off-stack + fix-disjoint + poison-smell + exposer language / older-bug mention), DO NOT upgrade a proximity/area hit to 'culprit' — emit LEAD + needinfo the module owner and attach the likely real cause (fix-elsewhere file, cited older bug).

**Key signals:** Fix is disjoint from the regressor and is NOT a backout: fix.is_backout==false and fix.files have no overlap with the regressor's diff files; the fix reads as a guard/mitigation ('Guard AddResultElement against cleared scope', 'Allow extra room ... for padding') or lands in a different subsystem (IonAnalysis vs regalloc; WorkerPrivate vs the added test).; Explicit exposer language in the thread: 'not wrong just exposing this code path more', 'timing changes are finally exposing it', 'having the SA forces us to have better invariants', 'likely changed stuff to surface these'.; A DIFFERENT, often older bug is named as the real root cause (bug 1299611, bug 675407 from 2011, a pre-existing WorkerEventTarget/MozPromise shutdown race) while the named regressor is only the trigger.; Regressor is off the crash stack (5/6): none of its diff files appear in the crash frames, so the crash address is reached through data/timing, not a call path the regressor is on.; Poison / uninitialized / UAF crash reason implies a latent lifetime bug rather than a fresh defect: jemalloc poison address ~0xe5e5e5e5 (2041907), 'poison read' of uninitialized elements (1988967), EXCEPTION_ACCESS_VIOLATION_READ null-deref of a field that gets cleared (2051442 m_scope).; Increased-coverage tell: a crashtest named after the bug that added it (dom/media/test/crashtests/2014861.html => bug 2014861); the test itself PASSES but its teardown trips latent machinery.; Regressor is a feature/opt-in 'add/enable/isolate/ship X' landing, not a bugfix, and the repro needs a non-default flag ('--ion-regalloc=simple'; 'the simple allocator isn't enabled by default').; Regressor shares the crash bug's component/area yet touches no stack file (2009140: both Security: Process Sandboxing), i.e. an adjacent perturbation rather than an on-line change.; The pin comes from a domain expert or crash-rate-spike window, not a stack-line hit: reporter says 'started recently but I don't see recent changes to the affected code', and bisection lands on a build window (100-170 changesets), not the bug.

**Representative bugs:** 1988967, 2017517, 2009140, 2042290, 2051442

# Strategy report: `exposer_not_cause` (6 / 289 bugs)

**One-liner.** The bug in `regressed_by` is *correct code that was never backed out*. It didn't introduce the defect — it changed timing, code-path coverage, data volume, or process ordering so a **pre-existing latent bug** finally crashes. The real fix lands **outside** the named regressor's diff and is **never a revert** of it.

## How it works (as seen in these 6)
A human pins the regressor (bisection, crash-rate spike, a domain guess, or a test-filename), then **reframes** it: *"not wrong, just exposing this more."* The true defect is located elsewhere — often a much older bug (1299611, 675407) or latent machinery (safepoint keep-alive, WorkerEventTarget/MozPromise shutdown, IMAP sync-runnable race). Four flavors:
- **Latent-invariant via a new opt-in path** — 1988967 (simple Ion regalloc), 2051442 (encrypted-body search)
- **Increased test coverage** — 2017517 (a new crashtest trips latent worker teardown)
- **Data-volume overflow of a fixed budget** — 2009140 (more font rules overflow the sandbox config)
- **Startup/timing re-ordering unmasks a lifetime race** — 2041907, 2042290 (process-isolation change unmasks a 2011-era IMAP UAF)

**Five of six regressors are off-stack**: the regressor→crash link is a *data/timing* dependency, not a call edge, so the regressor never appears on the crashing stack.

## Key signals (a triager's checklist)
- [ ] **Fix is disjoint + not a backout** — `is_backout=false`, fix files don't overlap the regressor's diff; reads as a guard/mitigation ("Guard … against cleared scope", "Allow extra room … for padding").
- [ ] **Exposer language** in the thread — "just exposing this more", "timing changes are finally exposing it", "surface these", "forces us to have better invariants".
- [ ] **A different / older bug named as root cause** (1299611, 675407) while the regressor is only the trigger.
- [ ] **Regressor off the crash stack** — no diff file appears in the crash frames.
- [ ] **Poison / UAF / null crash reason** — jemalloc poison `~0xe5e5e5e5` (2041907), "poison read" of uninitialized memory (1988967), `ACCESS_VIOLATION_READ` null-deref of a cleared field (2051442). Screams *latent lifetime bug*, not a fresh defect.
- [ ] **Increased-coverage tell** — a crashtest named after the bug that added it (`…/crashtests/2014861.html` ⇒ bug 2014861); the test *passes* but its teardown trips latent machinery.
- [ ] **Feature/opt-in landing, not a bugfix** — repro needs a non-default flag (`--ion-regalloc=simple`; "isn't enabled by default").
- [ ] **Pin came from a domain expert or a build-window spike, not a stack-line hit** — "started recently but I don't see recent changes to the affected code."

## Can crash-clouseau automate it?
**Partially — and the win is defensive (don't emit a false culprit), not offensive.**

*Surfacing the exposer:* the line-proximity scorer misses 5/6 (off-stack blind spot); call-graph/area-experts are weak because the link is data/timing, not a call edge (Phase-0 off-stack recall 43–71%, and these data-dep cases are the hard end). The reliable path is **crash-rate-spike bisection** (crash-stats `build_id` faceting → Buildhub/hg pushlog), but it yields a ~100–170-changeset *window* and lands on an exposer, never the defect. Free win: the **crashtest-filename→bug-number** regex nails the increased-coverage flavor (2017517).

*Labeling it (the part worth building):*
1. **Fix-disjointness** — proposed regressor + non-backout fix in unrelated files = exposer signature. Clouseau already stores both diffs. *Caveat:* Thunderbird fixes land in comm-central, so in-tree fix data is empty for 2041907/2042290/2051442 — needs comm-central fix resolution.
2. **Poison/UAF decode** — match `crash_address`/`crash_reason` against a jemalloc-poison/uninitialized/null table to bias toward "latent bug exposed by timing".
3. **Same-component-but-off-stack** shape (2009140).
4. **Exposer-language / "real root cause is bug N" extraction** — the principal already reads the thread.

**False-positive risk = the headline.** The exposer pattern is the *anti-pattern* of proximity scoring. **2051442 is the canonical trap**: a recent `mailnews/search` feature + a `mailnews/search` crash scores as a confident culprit under line/area proximity — but it's an exposer whose fix lives elsewhere. Exposers are a systematic false-positive **generator** for clouseau.

**Recommended clouseau behavior:** when the exposer corroborators fire (off-stack **+** fix-disjoint **+** poison-smell **+** exposer language / older-bug mention), **do not upgrade** a proximity/area hit to `culprit`. Emit **`lead` + needinfo** the module owner and attach the likely real cause (the fix-elsewhere file, the cited older bug). Clouseau cannot, from static signals alone, both find the off-stack exposer *and* name the real defect — that final leap is human domain reasoning.

## Representative bugs
| Bug | Regressor | Exposer flavor | Detectability | Why it's representative |
|---|---|---|---|---|
| **1988967** | 1958280 (simple Ion regalloc) | latent invariant, opt-in path | needs_human_reasoning | Author self-report; poison read; fix in `IonAnalysis.cpp`, not the regalloc diff. Textbook off-stack. |
| **2017517** | 2014861 (media-on-worker crashtest) | increased coverage | metadata_signal | Test filename encodes the bug#; test passes but teardown trips a latent worker race; fix in `WorkerPrivate.cpp`. Most automatable *label*, but real cause needs reasoning. |
| **2009140** | 1996225 (user-font subkey rules) | data-volume overflow | area_match | Same sandbox component, off-stack; fix is an admitted mitigation for pre-existing **bug 1299611**, not a backout. |
| **2042290** | 2011326 (process isolation) | timing unmasks lifetime race | needs_bisection | Crash-rate-spike bisection; real cause = 2011-era **bug 675407**; fix forward-lands in comm-central IMAP. (2041907 is its twin.) |
| **2051442** | 1562737 (encrypted-body search) | latent null-deref, opt-in path | area_match | **The false-positive trap** — proximity would confidently blame the recent search feature; actual fix is a null-guard elsewhere. |


### backout_confirmation (n=3)

**Automatable:** PARTIALLY, and the automatable part is high-value because it is OFF-STACK-PROOF. The natural-experiment form (sub-pattern A, bug 2040246) needs no diff, call-graph or keyword index: it is pure crash-rate-by-buildid correlated with pushlog backout/reland events -- exactly the regressor class every code-proximity scorer (line_crossing, callgraph, area_match, keyword_match) misses. Clouseau already has ~80% of the plumbing: (1) Stats(signatureid, buildid, number, installs) = per-build crash counts; (2) buildhub buildid->revision + pushlog.pushlog_for_buildid = per-build pushlog window; (3) pushlog.is_backed_out(desc) + get_bug(desc) already flag backouts AND extract the reverted bug; (4) utils.is_spike/get_spike_indices already detect the upward (appearance) edge. MISSING: (a) update.put_crashes persists Stats only for SPIKING signatures at spike builds -- the decline/zero side and full series are never stored, so the off-edge is invisible today; fetch the full per-build series around the candidate signature. (b) A new correlator: detect off-edges (count->~0) and on-edges (count returns); in an off-edge window find a backout of bug X, in an on-edge window find a (re)land of the SAME X; when both align, emit X as regressor with HIGH confidence even if the diff is off-stack/unreadable. For sub-pattern B (CI bursts) clouseau also needs Treeherder/autoland + orangefactor ingestion it lacks today (it is nightly-crash-stats oriented). False-positive risk: main risk is a ONE-SIDED coincidence -- crash drops in a build that happens to contain a backout of X for an unrelated reason (infra/telemetry gap, weekend/low-ADI, or a different real fix in the same build). The RELAND leg is the decisive guard: requiring backout->off AND reland->on for the same bug makes a false pin very unlikely (why 2040246 convinced). Secondary guards: normalize by installs (already tracked), require a real pre-drop baseline; on multiple backouts per window disambiguate via the reland or a reviewer/area secondary. Security-restricted regressors (2046246/2046250): diff unreadable so the diff-reading agent is blocked, BUT the backout changeset still names the bug, so clouseau can NAME the regressor from metadata even when it cannot score the patch. Recommendation: treat backout+reland as a HIGH-confidence signal that fires even when all code-proximity scorers abstain -- closing the off-stack-culprit gap and the under-confidence-on-off-stack-TP problem.

**Key signals:** Off-edge: per-(signature, buildid) crash count collapses to ~0 at build B whose pushlog window contains a backout changeset (is_backed_out=true) naming bug X; On-edge (DECISIVE leg): count returns at a later build C whose pushlog window contains a (re)landing of the SAME bug X -- the two-sided backout->stop / reland->restart coincidence is near-proof; First-appearance edge: crash first spikes in the build == regressor's first nightly build (clouseau's get_new_crashing_bids already computes this upward side); Explicit confirmatory sheriff/releng text: 'Fixed by backout.', 'the regressing changeset has been backed out', 'My guess is backout'; Resolution timing tell: RESOLVED/FIXED at the exact backout-comment timestamp / within ~1h of filing == standard releng backout flow; Deterministic high-frequency burst on autoland/CI (orangefactor N-failure day, PROCESS-CRASH in a named test) => integration-branch first-bad push => backout; The backout changeset in the pushlog carries the bug number (pushlog.get_bug) even when the regressor bug is SECURITY-RESTRICTED -- so the regressor can be NAMED without reading its diff; reviewer/module tag on the alternative candidate (e.g. necko-reviewers) used to rule out the one other patch in the window

**Representative bugs:** 2040246, 2046246, 2046250

# Strategy report: `backout_confirmation`

**Population:** 3 / 289 fixed crash-regression bugs (~1%). Small slice, but it is the primary evidence route for genuinely **off-stack** regressors no code scorer can reach — it punches above its frequency.

## TL;DR
Pin the regressor by **reverting a suspect change and watching the crash respond**, not by reading code. The gold-standard form is a two-sided *natural experiment*: the crash **stops** in the build that backs a bug out and **restarts** in the build that relands it. This is the one regressor-ID signal that works with a completely off-stack culprit — and clouseau already stores most of the data to compute it.

## How it works (two sub-patterns)
| | (A) Natural experiment | (B) Sheriff "fixed by backout" on a CI burst |
|---|---|---|
| Example | **2040246** | **2046246**, **2046250** |
| Mechanism | Crash on/off across nightly builds tracks a **backout (→stop)** and a **reland (→restart)** of the same bug | Deterministic burst on autoland/CI → sheriff backs out first-bad push → crash stops |
| Documented in bug? | Yes — human enumerates both pushlog windows | Only the reversal ("Fixed by backout."); real regression-range reasoning is off-record |
| off_stack | **true** (pref flip in `StaticPrefList.yaml`; real bug in `HappyEyeballsConnectionAttempt.cpp`) | null / false (cookie code; regressor diff unreadable) |
| who | human (McCreight + Jesup) | human (sheriff ryanvm / releng aryx) |

Key nuance: `backout_confirmation` is fundamentally a **human confirmation/pinning** mechanism. As a *standalone automatable detector* it stands on its own only in form (A). In form (B) the real detector was an autoland regression range; the backout is just the visible proof, and both (B) regressors were **security-restricted** (bug 1966397 — diff unreadable).

## Key signals (reusable)
1. **Off-edge:** per-`(signature, buildid)` crash count collapses to ~0 at build B whose pushlog window contains a **backout** changeset (`is_backed_out=true`) naming bug X.
2. **On-edge — the decisive leg:** count returns at a later build C whose window contains a **reland of the same bug X**. The two-sided coincidence is near-proof.
3. **First-appearance edge:** crash first spikes in the build == regressor's first nightly build (clouseau's `get_new_crashing_bids` already does this upward side).
4. **Confirmatory text:** "Fixed by backout." / "the regressing changeset has been backed out" / "My guess is backout".
5. **Timing tell:** RESOLVED/FIXED at the exact backout-comment timestamp (~1h after filing) = standard releng backout flow.
6. **CI burst:** deterministic PROCESS-CRASH in a named test + orangefactor N-failure day → integration-branch first-bad push → backout.
7. **Restricted-regressor escape hatch:** the backout changeset in the pushlog still carries the **bug number** (`pushlog.get_bug`), so the regressor can be *named* even when its bug is security-restricted and its diff is unreadable.

## Can clouseau automate it?
**Yes for form (A) — and it's the highest-value case because it needs no diff.** Pure crash-rate-by-buildid × pushlog backout/reland correlation reaches exactly the off-stack regressors that `line_crossing`/`callgraph`/`area_match`/`keyword_match` all miss.

**Already have (~80% of the plumbing):**
- `models.Stats(signatureid, buildid, number, installs)` — per-build crash counts (the time series).
- `buildhub` buildid→revision + `pushlog.pushlog_for_buildid` — per-build pushlog window.
- `pushlog.is_backed_out(desc)` + `get_bug(desc)` — flags backout changesets *and* the bug they revert.
- `utils.is_spike` / `get_spike_indices` — the upward (appearance) edge.

**Must build:**
- `update.put_crashes` persists `Stats` **only for spiking signatures at spike builds** — the decline/zero side is never stored, so the *off-edge is invisible today*. Fetch and store the full per-build series around a candidate signature.
- A new **correlator**: detect off-edges (count→~0) and on-edges (count returns); in an off-edge window look for a backout of bug X, in an on-edge window look for a (re)land of the *same* X; when both align, emit X with **HIGH confidence even if all code scorers abstain**.
- Form (B) additionally needs **Treeherder/autoland + orangefactor** ingestion clouseau doesn't do today (it's nightly-crash-stats oriented).

**False-positive risk & guards:**
- Biggest risk = **one-sided coincidence** (crash drops in a build that happens to contain a backout of X for an unrelated reason: infra/telemetry gap, weekend/low-ADI, or a different real fix in the same build). The **reland leg is the guard** — requiring backout→off *and* reland→on for the same bug makes a false pin very unlikely (why 2040246 convinced).
- Normalize by `installs` (already tracked) and require a real pre-drop baseline before trusting an off-edge.
- Multiple backouts in one window → disambiguate via the reland or a reviewer/area secondary (humans used `necko-reviewers` to rule out the alt candidate in 2040246).
- Security-restricted regressor → diff-reading agent is blocked, but the backout changeset still **names** the bug: clouseau can identify by metadata even when it can't score the patch.

## Representative bugs
- **2040246** — clearest example; off-stack pref-flip regressor pinned purely by backout↔stop / reland↔restart. The case to build the detector against.
- **2046246** — sheriff "Fixed by backout." on an autoland CI burst; security-restricted regressor; automation route is `area_match` (cookies), backout is the confirmation.
- **2046250** — Nightly sibling of 2046246 (same cookie `InvalidArrayIndex`); releng halt + backout within the hour.

## Recommendation for release engineering
Treat a **two-sided backout+reland natural experiment** as a first-class, HIGH-confidence regressor signal that fires **independently of code proximity**. It is cheap (reuses `Stats` + `buildhub` + `pushlog`), directly closes clouseau's off-stack-culprit gap, and — crucially — still names the regressor bug for **security-restricted** landings where the diff-reading pipeline is blind.


### automated_bot (n=2)

**Automatable:** Yes -- trivially, because it already IS automation: BugBot + bugmon are exactly this strategy, and crash-clouseau can reproduce the cheaper feeder from data it already consumes. The window path needs only (a) Crash-Stats/Socorro for the crash signature + first-crash build, and (b) Buildhub + hg/lando json-pushes to turn that buildid into a pushlog window and its member bugs -- the collector already computes regressor_in_window/window_sample. The range path additionally needs bisection infrastructure (bugmon-style), which only applies to reproducible/fuzzing testcases, so it is not generally available.

BUT the false-positive risk is HIGH and is the load-bearing takeaway: this is the low-precision baseline clouseau exists to beat, not a signal to trust. Both labeled cases are off-stack, cross-component, window/range-membership-only picks; one is a confirmed FP a human overturned in the next comment, the other an exposer-at-best/possible FP. Range/window membership is weak, non-causal evidence (temporal co-occurrence), and mechanically picking one bug from a multi-bug window is precisely what produced the misfires.

Recommendation for clouseau: treat a bot-set regressed_by as a CANDIDATE/prior, never ground truth. Before endorsing, require independent code-level corroboration -- stack-file/line overlap, searchfox call-graph reachability from a modified regressor function to a crashing frame, area-owner/component match, or a signature/keyword->subsystem hit. If the only support is window/range membership and the candidate is off-stack and cross-component, ABSTAIN or downgrade to weak. Two concrete data/tool asks beyond the feeders above: a signature/stack->subsystem index and searchfox call-graph + blame, used to REJECT bot picks that lack a code path. Finally, a labeling caveat: bot-authored regressed_by values in the training corpus are noisy labels (is_bot=true) and should be down-weighted or human-verified when used as ground truth.

**Key signals:** Commenter identity is a bot: creator matches release-mgmt-account-bot@mozilla.tld / bugmon@mozilla.com / *@bmo.tld / 'automation'. In the pipeline: regressor_first_mentions[].is_bot=true AND regressor_identified_by_human=false (the regressor bug number is first named by a bot).; Boilerplate templates that fingerprint the pick: 'By analyzing the backtrace, the regression may have been introduced by a patch [N] to fix Bug X'; 'Setting `Regressed by` field after analyzing regression range found by bugmon in comment #N'; '..since you are the author of the (potential) regressor, could you take a look?' (BugBot needinfo_regression_author.py).; The named regressor lands inside the crash's first-seen build pushlog window or bugmon build range (regressor_in_window=true), and that window is a small multi-bug set the bot picks one member from (6 changesets/3 bugs; 16 changesets/8 bugs).; No human confirmation before regressed_by is set: the author is needinfo'd rather than a human asserting causation; the first (and only) naming of the regressor is the bot comment.; Off-stack + cross-component pick: regressor files/component are disjoint from the crash-stack files (genai CSS 'Machine Learning: General' vs WebRTC dom/media/MediaManager.cpp; microtask/profiler/RuntimeService vs js/src/vm getter). No line/area/keyword overlap.; Fix is NOT a backout of the named regressor (fix.is_backout=false) and edits files outside the regressor's diff (guard added, assert downgraded) -- a strong tell the bot's pick was exposer-at-best or wrong.; A human dispute/override appears shortly after the bot comment ('I don't think this is right'), or humans engage only the crash site and never endorse the regressor.

**Representative bugs:** 1982261, 2009260

# Strategy: `automated_bot` (2 / 289 bugs, ~0.7%)

**What it is.** A provenance category, not a human technique: the `regressed_by` field was set by a Mozilla automation account (**release-mgmt-account-bot / "BugBot"**, fed by **bugmon**), and the regressor was *picked by the bot* — no human read code to reach it. Treat these labels as candidates, not facts.

## How the bot picks a regressor
Two feeders seen in this dataset:

1. **Backtrace -> first-crash-build pushlog window** (bug 1982261). BugBot pastes the top-10 frames, then: *"By analyzing the backtrace, the regression may have been introduced by a patch [1] to fix Bug 1976971 ... could you take a look?"* It maps the crash's first-seen Nightly build to that build's pushlog window (6 changesets / 3 bugs) and names the one plausible member.
2. **bugmon bisection -> build range -> bot picks one bug** (bug 2009260). bugmon bisects a fuzz testcase to a range (16 changesets / 8 bugs); BugBot then *"Setting `Regressed by` field after analyzing regression range found by bugmon"* and needinfos the author.

Neither reads code — both rest on **temporal co-occurrence** (regressor landed in the window/range that first showed the crash).

## The catch: both examples are dubious
| Bug | Crash | Bot-named regressor | Reality | Detect |
|---|---|---|---|---|
| **1982261** | `IPCError-browser \| SetCookies Invalid cookie` (JS getter stack) | 1976971 "Track flows for microtasks" (xpcom/dom microtask+profiler) | Off-stack; **exposer-at-best / possible FP**. Fix = downgrade a `MOZ_ASSERT` in `netwerk/cookie` (not a backout, none of the regressor's files) | metadata_signal |
| **2009260** | `MOZ_CrashSequence` — WebRTC `MediaManager::Dispatch` during shutdown | 1998819 genai CSS chevron rotate (Machine Learning: General) | **Confirmed FALSE POSITIVE.** Human: *"I don't think this is right."* Real cause: pre-existing `DeviceListener::Clone`/shutdown race, fixed with a `!sHasMainThreadShutdown` guard (not a backout) | needs_human_reasoning |

Both are **off-stack and cross-component**, both were **set with no human confirmation**, and **neither fix is a backout** of the named bug.

## Signals to recognize / detect it
- **Bot authored the pick**: creator is `release-mgmt-account-bot@mozilla.tld`, `bugmon@`, `*@bmo.tld`; `is_bot=true`, `regressor_identified_by_human=false`.
- **Fingerprint boilerplate**: "By analyzing the backtrace…"; "…found by bugmon in comment #N"; "since you are the author of the (potential) regressor" (`needinfo_regression_author.py`).
- **Window/range membership only** (`regressor_in_window=true`), one bug chosen from a multi-bug window.
- **Red flags for FP**: off-stack + cross-component; `fix.is_backout=false`; fix edits files outside the regressor's diff; a human dispute lands soon after the bot comment.

## Can crash-clouseau automate it? Yes — but it's the baseline to beat
The window feeder is cheap and clouseau already has the inputs: **Crash-Stats** (signature + first-crash build) + **Buildhub / hg-lando json-pushes** (buildid -> pushlog window -> member bugs; the collector already computes `window_sample`). The range feeder needs **bugmon-style bisection** and only works on reproducible/fuzz testcases.

**But precision is poor** — both labeled cases misfired. Actionable guidance:
- Use a bot-set `regressed_by` as a **candidate/prior, never ground truth**.
- **Corroborate before endorsing**: require stack/line overlap, searchfox call-graph reachability to a crashing frame, area-owner/component match, or a signature->subsystem hit.
- **Abstain / downgrade to weak** when support is window-or-range membership only AND the pick is off-stack + cross-component.
- **Corpus hygiene**: down-weight or human-verify bot-authored `regressed_by` — they are noisy labels.

**Extra tooling to add value over the raw bot**: a signature/stack->subsystem index and searchfox call-graph + blame, used specifically to *reject* co-occurrence picks that have no code path.

## Representative bugs
- **1982261** — window-membership pick, exposer-at-best / possible FP.
- **2009260** — bisection-range pick, confirmed false positive overturned by a human.


### reviewer_module_signal — a human who reviewed or owns the regressor's code recognizes the crash as landing in "their" recently-changed area, and names/confirms the regressor from that ownership relationship rather than from bisection, a regression range, or a backout. (n=2)

**Automatable:** PARTIALLY, and only as a low-weight CORROBORATION re-ranker on top of an already code-anchored candidate — NOT as a primary discovery mechanism, and never to promote an off-stack candidate on its own.

What clouseau already has: mcp__history__changeset returns the regressor's commit desc first line (which contains 'r=<reviewer(s)>' / 'r=#<group>') plus author; blame/file_history surface author; the crash-bug thread (commenters) is already fetched by the collector. What it does NOT do: history.py only parses the BUG number out of the desc (_bug()), it never extracts the reviewer; there is no module-ownership map; and there is no cross-reference between (regressor reviewer/author) and (crash-bug commenters / crashing-module owner).

Data/tools to close the gap: (1) parse r=<reviewer(s)> and r=#<review-group> from the landing desc clouseau already fetches — nearly free; (2) a file -> owning-team/reviewer-group map, available from in-tree moz.build BUG_COMPONENT (mach file-info bugzilla-component) and Phabricator review groups (#win-reviewers, #thunderbird-back-end-reviewers, #sync-reviewers), to relate the crashing area to the regressor's reviewers; (3) the crash-bug participant list (already in the dossier) to test 'did the regressor's reviewer/author comment here?'; (4) comm-central/Thunderbird support in the pushlog/diff pipeline — _HG_LINK_RE at collect_regressor_dataset.py:317 matches only mozilla-central/autoland/releases and silently drops comm-central, which is why 2007164's regressor diff came back empty (on_stack=null) and had to be recovered by hand (cached at spike/regressor_dataset_cache/2007164_regressor_1976655_c36ae706dbcd.diff). Buildhub/bisection are NOT needed — this strategy is the substitute for them.

FALSE-POSITIVE RISK: HIGH if used alone. The naive reviewer pretag fires on 247/290 dossiers because 'r=' appears on essentially every landing; the same review groups (win-reviewers, sync-reviewers) and prolific reviewers cover huge swaths of code, and module owners comment in many bugs, so 'the regressor's reviewer is active here' is weakly discriminating. Only 2/289 bugs are genuinely primary reviewer_module_signal. Risk is manageable only if the signal is a small tiebreak applied to candidates already surfaced by line-proximity/blame/call-graph, gated so it cannot by itself justify an off-stack culprit. The off-stack case (2006941, detect=needs_human_reasoning) is essentially not automatable from code — no line/blame/call-graph/keyword signal reaches from an AV-DLL FileRead stack to a sandbox handle change; only domain expertise bridges it. The on-stack case (2007164, detect=line_crossing) is already catchable by clouseau's existing line-proximity scorer once comm-central is ingested, so there the reviewer signal is redundant confirmation rather than the thing that finds the bug.

**Key signals:** FIRST-NAMER IS THE REGRESSOR'S REVIEWER/AUTHOR: the human who first names the candidate regressor in the crash bug is the person listed as r=<them> on, or the author of, that regressor's landing changeset (2007164: mkmelin names 1976655, and 1976655 is r=mkmelin).; OWNER CONTINUITY ACROSS REGRESSOR AND FIX: the same domain/module owner appears on both patches — regressor author comments in the crash bug and/or the regressor's reviewer also reviews the fix (2006941: author Bob Owen comments; reviewer handyman reviews both the regressor and the fix, r=handyman,win-reviewers).; REVIEWER-GROUP / COMPONENT OVERLAP: the regressor landed under a review group or reviewer whose module matches the crashing area's Bugzilla component or product (r=mkmelin & Thunderbird::Search; r=handyman,win-reviewers & a Windows-sandbox 'content process' change).; OWNERSHIP-FRAMED, SPECULATIVE, NON-BISECTION LANGUAGE: identification reads as recognition ('caused/exposed by bug N', author self-recognition) with speculative=true and NO bisection/mozregression/regression_range/backout cited.; DOMAIN-MECHANISM BRIDGE OVER AN OFF-STACK GAP: the identifier's subsystem expertise supplies a causal chain the crash stack cannot (KsecDD sandbox handle -> AV DLL FileRead hook -> C++ exception), so an off-stack regressor is proposed on ownership + mechanism, not on any stack/line evidence.; PROCESS-SCOPE MATCH TO THE REGRESSOR'S SUMMARY: crash scoping corroborates the owner's hunch — content-process-only crashes (comment 1) matching a regressor titled 'windows content process'; a purge-service-triggered search crash matching a 'Search Messages may not finish' change.

**Representative bugs:** 2007164, 2006941

## Strategy report: `reviewer_module_signal`

**Population:** 2 of 289 fixed Firefox crash-regression bugs (primary strategy). Both `who_identified = human`.

### What it is
A **person**, not a code signal, does the identifying. Someone who **reviewed or owns** the regressor's code recognizes a crash landing in "their" recently-changed area and names/confirms the culprit from that relationship — used when nobody ran bisection/mozregression/a regression range/a backout (all those pretags are false in both bugs).

### The two bugs (they bracket the strategy)

| Bug | Crash | Regressor | On-stack? | The signal |
|---|---|---|---|---|
| **2007164** (TB::Search UAF) | `nsMsgSearchOfflineMail::Search` UAF, #1 topcrash 147.0b3 | 1976655 (comm-central `c36ae706dbcd`, r=mkmelin) | **yes** (`line_crossing`) | **Direct:** mkmelin, the *reviewer* of 1976655, names it in comment 0 ("caused/exposed by bug 1976655"). Author h.w.forms then owns the mechanism walk + fix (bonus author self-report). |
| **2006941** (AV `fcagff.dll` startup crash) | stack is 3rd-party Trellix + `nss3!FileRead` + `nsFileStreamBase::Read` | 1997149 "Close KsecDD device handle in **windows content process**" (Sandboxing) | **no** (`needs_human_reasoning`) | **Structural:** regressor never named in-thread; inferred from owner continuity — author Bob Owen comments (#17), reviewer handyman reviews **both** the regressor and the fix (`r=handyman,win-reviewers`). Sandbox change *exposed* a latent AV-DLL crash. |

### Signals a release engineer / tool can reuse
1. First-namer of the candidate regressor **is its reviewer (`r=<them>`) or author**.
2. **Owner continuity:** regressor author comments in the crash bug and/or the regressor's reviewer also reviews the fix.
3. **Reviewer-group ↔ component overlap** (`r=mkmelin`↔TB::Search; `r=#win-reviewers`↔a Windows-sandbox change).
4. Language is **recognition + speculative** ("caused/exposed by…"), with **no bisection/range/backout** cited.
5. For off-stack cases, a **domain-mechanism bridge** the stack can't give (KsecDD handle → AV FileRead hook → C++ exception).

### Can crash-clouseau automate it?
**Only as a small corroboration re-ranker on an already code-anchored candidate — not as a discovery mechanism, and never to promote an off-stack culprit alone.**

- **Almost free wins:** `mcp__history__changeset` already fetches the landing desc containing `r=<reviewer>` — but `history.py` only parses the *bug number* (`_bug()`), never the reviewer. Add a `r=…`/`r=#group` parse and cross-reference it against the crash-bug commenters (already in the dossier).
- **Needs new data:** a file→owning-team map (in-tree `moz.build` `BUG_COMPONENT` / `mach file-info`; Phabricator review groups) to relate the crashing component to the regressor's reviewers.
- **Concrete pipeline bug:** `_HG_LINK_RE` (`spike/collect_regressor_dataset.py:317`) matches only mozilla-central/autoland/releases and **silently drops comm-central**, so 2007164's regressor diff came back empty (had to be recovered by hand → `spike/regressor_dataset_cache/2007164_regressor_1976655_c36ae706dbcd.diff`). Thunderbird support is the real gap that instance exposes.
- **Buildhub/bisection:** not needed — this strategy is the *substitute* for them.

**False-positive risk: HIGH if used alone.** The naive reviewer pretag fires on **247/290** dossiers (`r=` is on nearly every landing); prolific reviewers/groups cover vast code and owners comment everywhere, so "the regressor's reviewer is active here" barely discriminates — only **2/289** are genuinely this strategy. Gate it to a tiebreak weight on top of line-proximity/blame/call-graph hits.

**Bottom line:** the *on-stack* case (2007164) is already reachable by clouseau's line-proximity scorer once comm-central is ingested — the reviewer signal is redundant confirmation. The *off-stack* case (2006941) is not automatable from code at all; only subsystem expertise links an AV-DLL FileRead crash to a sandbox handle change.
