# Jens Stutte's review of bug 2065373 — findings, and what to improve

Written 2026-08-21. Source: <https://bugzilla.mozilla.org/show_bug.cgi?id=2065373>, comments #1–#3
by :jstutte, replying to the Clouseau filing (comment #0, 2026-08-21T04:53:35Z).

Everything below that carries a number was measured today against live Socorro / hg / BMO or
against the code in this repo. Commands are in the appendix.

---

## 0. What was filed and what came back

The filing: `Crash in [@ IPC::MessageReader::FatalError | mozilla::ipc::shared_memory::HandleBase::FromMessageReader]`,
Core::IPC, nightly 156 build `20260819092600`, MOZ_CRASH reason
`Shared memory PlatformHandle is not safe to map`. Rendered at **97% worth investigating**, needinfo
to :jld, candidate `db1a48a97fed` (bug 2045119, Jed Davis) framed as "Starting point — NOT a
suspected cause".

Jens opened with **"This seems meaningful, but with some details to correct"** and made five points:

1. **Provenance is wrong.** The abort string was not added by bug 2045119; it was added 2025-03-04
   by bug 1942129 pt1 (`8c4833bf60ec`). Crashes with this exact reason exist on ESR 140/Windows
   since build `20260106170501`. What 2045119 changed is that the POSIX path can now fail.
2. **The mechanism's direction is backwards** and does not fit the data — the parent/GPU gating of
   the `DupReadOnly` probe does not explain a child sender producing an unsealed handle.
3. **The population is one machine.** 58 crashes/60d, 55 Linux all on kernel `7.1.8-1-cachyos`,
   one hardware fingerprint, ~10 install_time values one per day = daily nightly updates of a
   single installation. process_type content 27 / parent 25 / plugin 3 — "it fails in both
   directions on that machine".
4. **The explicit ask (comment #2):** *"I wonder if clouseau could do some OS / install distribution
   checks on the socorro data?"*
5. **Diagnostics (comment #3):** the three branches of the POSIX `IsSafeToMap` share one
   `FatalError` string and the `MOZ_LOG` warnings are off in the field, so which one fired is not
   recoverable — *"might be the only actionable thing here to improve diagnostics"*.

**Filing this bug was not the error.** Jens called it meaningful, and `_apply_bad_machine_gate`'s
own docstring argues that one machine crashing repeatedly on ONE signature is a reproducible bug,
not noise (bug 2060924 is ASSIGNED on exactly that shape). The defect is a **wrong claim inside a
useful report**. Nothing below should be built as a suppression gate.

---

## 1. Verified crash data

Signature, all channels/products, 364 days: **68 reports** — nightly 58, esr 5, release 4,
`Bosch` 1 (a junk term; "other channels" is not just esr+release).

| facet | top term | count |
| --- | --- | --- |
| `platform_pretty_version` | `CachyOS` | 55/58 (60d window) |
| `platform_version` | `7.1.8-1-cachyos #1 SMP PREEMPT_DYNAMIC Mon, 10 Aug 2026 19:39:40 +0000` | 55/58 |
| `cpu_info` | `family 25 model 117 stepping 2` | 55/58 |
| `process_type` | content 29 / parent 25 / plugin 4 | — |
| `platform` | Linux 55 / Windows NT 3 | — |

Field-name notes that cost time to find:

* **`os_version` is not a facetable SuperSearch field.** The kernel string lives in
  **`platform_version`**. `platform_pretty_version` renders the friendlier `CachyOS`. Print both.
* The exact-match operator matters: `signature=<sig>` returns 656,569 rows;
  `signature=<sig>` with a leading `=` (i.e. `signature==…` in URL terms) returns 58.

**A one-machine fingerprint neither of us stated explicitly:** the `install_time` facet counts are a
permutation of the `build_id` facet counts — 22, 7, 6, 6, 5, 3, 2, 2, 1, 1, 1, 1 against
20260819092600, 20260816214904, 20260813200918, … One installation minting a fresh `install_time`
on every nightly update. That is the cheap, mechanical version of the inference Jens made by hand.

**Signature age — three clocks, three answers:**

| clock | value | used by |
| --- | --- | --- |
| `SignatureFirstDate` (all-time, all-channel) | build `20250430203103`, 2025-05-14 → **475 days** | fetched every run, **wired to no gate** |
| all-channel 364d oldest build | `20260106170501` (esr/Windows) — *exactly Jens's build* | discarded below `other_channel_floor` |
| nightly-only 364d oldest build | `20260409210423` | the stale-signature gate |

Candidate `db1a48a97fed` pushdate 2026-07-27 is **110 days after** even the most conservative of
those. The run computed that number and published "the check did not exist before it".

---

## 2. The headline finding

**Every refutation Jens wrote was already in the run's hands.**

| Jens's correction | What the run already held | Where it went |
| --- | --- | --- |
| "the abort string is not from bug 2045119" | `mcp__patch__diff` printed `- 94: aReader->FatalError("Shared memory PlatformHandle is not safe to map")` **in the same hunk** as the `+ 104:` the skeptic cited — it is a MOVE past the size read, plus `IsSafeToMap(handle)` → `IsSafeToMap(handle, size)` | the model read the `+` half only |
| "crashes exist since build 20260106170501" | `_apply_signature_age_gate` computed `stale_signature: True, candidate_landed_after_first_seen_days: 110` | recorded to `corroborations`; `orchestrator.py:1391` returns without acting because `v.decision != Decision.lead`; never printed in the bug |
| "one kernel, one CPU fingerprint" | `sigage.hardware_noise` downloads the whole `cpu_info` facet at `_facets_size: 200` and keeps only `BROKEN_CPUS` matches | 58/58 on one term, fetched and discarded — and the brief then told both models "0% bit-flip, 0% broken-CPU", which reads as *reassurance* |
| "it fails in both directions" | `process_type` is already one of six facets on the SO's `crash_stats` tool | optional tool call, channel-scoped, no gate |

And the badge could not have moved regardless: `p_worth_investigating` reads only the confidence
rung through `{25: 0.5, 50: 0.8, 70: 0.9714, 85: 0.9714}` — **70 and 85 publish the identical 97%**
(deliberate: pooled fit, `config.py:712`). The single downgrade the gate layer could have applied
was a literal no-op on the published number.

---

## 3. Ground truth — why nothing stopped it

Established by reading the code; every claim has a file:line in the workflow transcript.

**Gates.** `_apply_bad_machine_gate` measures signature DIVERSITY on one installation and never
volume; it needs `distinct_signatures >= 10` and this installation produced exactly 1. Its
denominator is one `install_time`, product+channel-scoped, 14 days — it can never see "the same
physical machine under 10 other install_times". `_apply_bit_flip_gate` was structurally impossible
(no flip annotation, AMD CPU not in `BROKEN_CPUS`, not a singleton, signature-level rates 0.0/0.0).
`_apply_backout_gate` / `_apply_is_backout_gate` / `_apply_absent_thread_gate` were correct no-ops.
`_apply_compiled_out_gate` did not exist in any deployable revision at filing time (committed
~4.5h later). `_apply_corroboration_gate` can only raise, never lower.

**The "Starting point — NOT a suspected cause" reframing** came from `_record_window_membership`,
which is explicitly a LABEL and moves no rung — which is why the badge stayed at 97% while the
prose disclaimed causality.

**Two hardware modules point at each other across a hole.** `sigage.hardware_noise`'s docstring
defers the one-broken-machine case to `machine.py`; `machine.py`'s rule excludes single-signature
machines by design. Nothing owns "one machine, one signature, many reports".

**Installation counts are computed in three places for three consumers, none of which can act:**
`population.summarize`'s `single_install` → crashstack.html only (and on its own window it reported
`installs: 8, top_share 0.458, concentrated=false` — i.e. it would have raised no flag);
`report_bug.get_stats` → the one prose sentence in the comment; `machine.install_history` → a gate
requiring ≥10 distinct signatures.

**No provenance instrument exists at all.** There is no provenance step, no provenance gate, and no
provenance citation kind — citation kinds are `searchfox | diff_line | stack_frame | struct_layout |
ref`. "X did not exist before changeset C" is free text in `verdict.mechanism.statement`, published
verbatim. No tool can answer "when did this string first appear anywhere in the tree": the 7
searchfox tools are tip-only, the 3 history tools are path-scoped, `raw_file` is one file at one
rev, `mcp__patch__diff` is one changeset. hgweb cannot help either — `json-log?rev=keyword(...)`
returns **HTTP 503 after 127s**, and `rev=grep(...)` matches commit descriptions, not content.

**Blame corroborates the false answer.** `mcp__history__blame` on `SharedMemoryHandle.cpp` L103-104
at the crash rev returns `db1a48a97fed | Bug 2045119` for both lines — a move re-owns a line in
blame exactly as a backout does, and `history.py:172-180` warns only about backouts. The true origin
(`d100e97083cf`, bug 1942129) sits on L100 of the same output.

**The decisive check was one GET away, with a tool the agent already has.**
`raw_file('ipc/glue/SharedMemoryHandle.cpp', rev='e184115c58e2')` — the parent of `db1a48a97fed` —
returns the string at L94. But the agent cannot learn the parent node from any tool:
`mcp__history__changeset` builds its output from node/git_commit/date/user/bug/backedoutby/desc/files
and **drops the `parents` field hg already returns**.

**This is a repeat from the same reviewer.** :jstutte has commented on exactly two Clouseau bugs
ever (2062119 on 2026-08-10, and this one) and both times his first substantive comment was a
provenance correction. The 2062119 correction produced a per-crash archetype and a non-scoring
label — nothing that generalises. Across the last 20 filings: provenance 4, environment 4, 2 both,
6 with no human comment. Across all 51 filings since 2026-08-05: ~10 provenance, ~9 environment —
and **environment errors are what close a bug INVALID (5 of 7), while a provenance error usually
leaves the bug alive** (one such bug ended FIXED).

**Also found, unrelated to this review:** `_apply_callpath_gate` is unreachable in production — it
is guarded on `seed['is_offstack']`, and off-stack crashes are skipped before the agent runs because
`offstack.enabled` is false.

---

## 4. Ranked improvements

Every one of these survived an adversarial verification pass; the "corrections" are what that pass
changed. Nothing here is speculative — where a number appears, it was measured.

### 1. Moved-line detection in the diff tool — *small, kills the exact error*

Inline `+ 104: [ALSO DELETED AT -94] …` marker (ungated — it is a fact about the diff) plus a
per-file NOTE (gated — it is an inference).

Corrections from the verify pass:

* **Multiset, not set.** `Counter(added) & Counter(deleted)`, annotating at most
  `min(count_added, count_deleted)` and popping a deleted lineno per annotation so the marker names
  a line that exists. A plain set over-annotates by **10.1%** across the cached corpus (6194 vs 5626
  lines) and revives the "delete 2, add 20" failure `files_are_code_motion` was written to avoid.
* **Gate the NOTE.** Measured over `corpus_ship`'s 334 labelled true regressors (327 diffs, 2840
  files): a naive predicate fires on **452/2840 (15.9%)**, and 260 of those at `moved/added < 0.2`.
  The top "moved" texts are boilerplate that `_BRACES` does not filter: `}else{` ×32,
  `return false;` ×24, `);` ×23, `return;` ×19, `return nullptr;` ×17. Gating at
  `moved >= 2 and moved >= 0.5 * len(added_non_brace)` drops it to **54/2840 (1.9%)** while
  `SharedMemoryHandle.cpp` (2 of 3) still fires — **and `SharedMemoryPlatform_posix.cpp` (3 of 60)
  and `_android.cpp` (1 of 9) no longer do**. That matters: posix.cpp carries the one claim Jens
  *endorsed* ("the POSIX path can now fail"), and the ungated note would have told the model not to
  cite it.
* **Add a `_MOVE_NOISE` set** used only by this predicate (do not widen `_BRACES`, other predicates
  depend on it): the boilerplate above plus `#endif`, `#else`, `*/`, `///`, `){`, `]`, `[`, `},`,
  `})`, `break;`, `continue;`, `return true;`, `return NS_OK;`.
* **Reword the NOTE to keep the hedge `is_refactor` already carries:** a move *can* still change
  behaviour (ordering, lifetime, a changed condition around an unchanged line) — judge the change,
  not the line. This bug is itself that shape: the condition gained `size`.
* **The prompt sentence must name `blame` too**, not just `mcp__patch__diff`. Both prove only that
  the changeset last TOUCHED the line.
* **Placement:** emit the NOTE right after the `file … (modified)` line and before the hunks, so
  `_MAX_LINES = 400` truncation cannot drop it.
* Tests: pin `db1a48a97fed`'s `SharedMemoryHandle.cpp` hunk asserting the marker and the NOTE, plus
  a negative test on the same patch's `SharedMemoryPlatform_posix.cpp` asserting no NOTE.

### 2. Print `parents:` in `mcp__history__changeset` — *one line*

The field is already in the response libmozdata fetches
(`parents: ['e184115c58e269f3c15cb71c5993354d26515f41']`) and `history.py:213-233` discards it.
It is a list (merges have two) — print all short nodes, labelled so the agent knows what to do with
it: `parents: e184115c58e2   (read the file at a parent to prove a line is new)`.

### 3. The environment block — *Jens's explicit ask*

One unscoped SuperSearch: `_results_number=0`, `_facets=[platform, platform_version,
platform_pretty_version, cpu_info, process_type, release_channel, _cardinality.install_time]`,
`_facets_size=20`. **Measured: 0.53s, 1260 bytes**, on a run that costs ~20 minutes and ~$3.

Wire it as `sigage.environment_profile(...)` beside `hardware_noise` — **do not widen
`hardware_noise`'s own query**, its docstring forbids exactly that ("`total` stays on `channel`,
because its consumer needs the opposite scope"), and the new query must be channel-UNSCOPED or the
esr/Windows contrast does not exist. Then: seed key in `build_seed`, a `_record_environment_facts`
RECORDER (not a gate) modelled on `_record_signature_age_facts`, `triage._environment_lines` in
`_crash_facts` (which by construction reaches the blind second opinion), and
`build_environment_note` in `build_bug_comment` after `build_hardware_note`.

Corrections from the verify pass, measured on a 9-signature panel:

* **Drop `''`/`unknown`/`Unknown`/`N/A` terms before computing any share** — same rule as
  `machine.py:101-103`. Without it, `EMPTY: no frame data available` (67,008 machines) reads as one
  environment.
* **Sample floor `reports >= 20`.** The existing `min_signature_reports = 5` is too low: at 3
  reports the top share is 1.0 by construction (`mozilla::webgl::details::Serialize<T>` fires today).
* **Condition the judgement clause on the machine proxy** — `_cardinality.install_time <= 25`, which
  is in the same response and costs nothing. The claim is about machines; a share alone cannot
  support it.
* With all three applied the clause still fires on the target (68 reports / 18 installs) and on
  `abort | libgallium-24.2.8.so` (4,458 reports / **2** installs — correctly one machine), and no
  longer fires on `shutdownhang | RtlWaitOnAddress` (24,467), `SerializeJSONProperty` (1,886),
  `allocator_api2::…::push` (189), `amdh264enc64.dll | RtlpWaitOnCriticalSection` (30),
  `mozilla::webgl::details::Serialize<T>` (n=3), `EMPTY: no frame data available` (67,008).
* **Always print the facts and the process split; gate only the judgement clause.**
* **Never say "18 installations".** Say "at most 18 distinct installations over the year, and
  `install_time` is reset by every nightly update, so that is an upper bound on machines".
* **Say what `process_type` IS** — the process that CRASHED, i.e. the receiver for an IPC read
  fault. Without that sentence a model reading "parent 26" re-derives the exact backwards sender
  story Jens called out.
* **Do not build a second implementation of "the signature's environment".** `hardware_noise`
  already has the `cpu_info` facet in hand — return its top non-unknown term from there. And add
  `platform_version` + `_cardinality.install_time` to `_FACETS` in `agent/tools/socorro.py:41-44`,
  so the SO tool and the new block cannot disagree.
* Correct the rationale before it becomes a docstring: `platform_pretty_version` and `process_type`
  *are* faceted today — in the optional SO tool. The honest statement is that the SO could already
  ask, and the principal and the filed bug could not.
* **Do not ship the `get_stats` sub-fix as described.** Measured: the summing branch and a top-level
  `_cardinality.install_time` over the same range both give 2 for this signature. The sum does not
  double-count, because a nightly update mints a *new* install_time. Keep the caveat sentence, drop
  the arithmetic change.

### 4. Print the caveats the gates already computed

`stale_signature`, `candidate_landed_after_first_seen_days`, `machine_distinct_signatures`,
`machine_crash_count` and **the entire second-opinion result** live in `corroborations` and reach
only crashstack.html. The blind SO is the pipeline's strongest measured refutation instrument
(refutes 74% of leads) and a reader of the bug cannot see whether it agreed, disagreed, or ran.

### 5. Let the stale-signature finding act — **DOWNGRADED, read this before touching it**

`_apply_signature_age_gate` has no action for a non-`lead` verdict (`orchestrator.py:1391`), so its
most consequential customer — strong-evidence — is exempt. And even on a lead the clamp is
reversible: the gate runs at line 2374, `_fold_second_opinion` at 2381, and a corroborating SO
re-raises a clamped medium(50) back to probable(70) = the same 0.9714.

**Do NOT "fix" the ordering.** That change was already measured and the standing worklist records
the result: making the clamp survive the second-opinion fold **kills DUPLICATE 2061180 and FIXED
2061960 and 2063809, and catches 0 INVALID**. The re-inflation is load-bearing, not a bug.

This entry is itself an instance of the anti-overfit rule in §5, committed by me: I read the
ordering off this one bug, saw it explained this one bad outcome, and proposed a change whose
denominator I had not looked at. What survives of the item is narrower and unmeasured on its own:
the **70/85 collision** in the calibration table means the one downgrade the gate layer can apply
is invisible in the published number. Whether that is worth changing needs the same panel — what
does splitting 70 and 85 do across the 51 filings — before anything is written.

### 6. Skeptic: direction, and the population

Add `supports: Literal["supports","contradicts","irrelevant","unknown"] = "unknown"` to
`SkepticResult`, defaulted so old dossiers still parse. Render all four:
`supports` → `pass` unchanged; `contradicts` → `pass (fact confirmed, does NOT support the
conclusion)`; `irrelevant` → `pass (fact confirmed, not load-bearing)`; `unknown` → `pass`.

Corrections:

* **The blanket header already ships** (`report_bug.py:899-900`, landed `ef0ccd8` 2026-08-10) and
  was in comment #0 verbatim. Jens read it and still had to write the correction — so the missing
  piece is the DUTY, not a caveat, and not a field on its own.
* **The provenance duty is what actually fires here:** "before marking an 'introduces X' claim
  `pass`, look at the REMOVED side of the same hunk; if the string also appears on a `-` line the
  changeset MOVED it and the claim is `fail`. Then confirm against the parent with
  `mcp__source__raw_file`." Plus: pass `candidate_landed_after_first_seen_days` (already in
  `corroborations`) into the skeptic's context with the duty "a mechanism claiming the candidate
  INTRODUCED this crash is `fail` when the signature was first seen before the candidate landed".
  On this run that is 110 days.
* **Narrow the population duty or it eats real bugs.** Socorro's `process_type` is the *crashing*
  process; the mechanism restricted the *sender*, which Socorro cannot see — so a parent-process
  report is actually CONSISTENT with "children skip the probe". Wording: compare only when the
  mechanism restricts the crashing process; mark `unknown` when it restricts a sender or peer; mark
  `contradicts` only when an excluded cell holds a MAJORITY (>50%). A 12-report Windows tail under a
  correctly POSIX-only mechanism is a different sub-population sharing a signature, not a refutation.
* **Do NOT wire `contradicts` into `_skeptic_veto`.** A `fail` on a lead → abstain; on
  strong-evidence → medium → the stale clamp takes it to low, below `autofile.min_confidence` 70.
  The bug Jens called meaningful would never have been filed.
* **Do not grant `*_SOCORRO` to the skeptic role** — it is inert: `triage.py:901-905` registers only
  searchfox/patch/history/source, and `allowed_tools` at 907-911 has no socorro ids. Pass the #3
  environment block as TEXT instead. (The tool is also channel-scoped, which hides the ESR-140
  history that refutes provenance.)
* Add the `supports` tokens to the prompt's example JSON and a case to
  `tests/test_prompt_schema_drift.py`, whose `_assert_subset` guard is what stops a prompt-invented
  token collapsing the dossier to a false abstain.

### 7. `diagnostic_gap` — Jens's comment #3

Add `diagnostic_gap: Claim | None = None` to `Dossier` (reusing `Claim`, which is `Cited`, so the
branch enumeration must carry citations). Render as its own section between the analysis and the
code references. Verified against hg at the pinned rev: `Platform::IsSafeToMap` at
`_posix.cpp:491`, three `return false` at 509/518/524 (missing `F_SEAL_SHRINK`, failed `fstat`,
`fileSize.value() < aSize`), each logging only via `MOZ_LOG_FMT`.

Corrections:

* **The trigger as first worded computes 1, not 3.** "Count the distinct paths that reach the abort
  site" → searchfox returns exactly ONE occurrence of the string tree-wide and `IsSafeToMap` has one
  use. The three branches live one level DOWN, inside the callee's predicate. Reword to: *open the
  predicate that guards the abort and count its distinct failure conditions.*
* **Add "or is not recorded anywhere at all".** Jens's literal wording ("only in `MOZ_LOG`") excludes
  the worse case: the Windows `IsSectionSafeToMap` has 2 false paths and NO logging.
* **Exempt it from `_skeptic_veto`.** `schema.py:678-679` collects failures with no filter on which
  claim, and `699-706` turns one `fail` on a lead into an abstain. A descriptive footnote must not be
  able to kill the filing. Add the test.
* **Scope to the crash's own OS.** `IsSafeToMap` has four definitions (`_android.cpp:129` 1 branch;
  `_mach.cpp:226` literally `return true;` — the crash is impossible there; `_posix.cpp:496` 3;
  `_windows.cpp:249` 2). Name the file. Also say which branches are conditionally compiled — the
  POSIX seal branch is inside `#ifdef USE_MEMFD_CREATE`, and `_apply_compiled_out_gate` inspects only
  `verdict.mechanism`, so this is an ungated channel for the failure mode that gate exists to stop.
* Three plumbing lines the sketch misses: add `"diagnostic_gap": Claim` to `_SINGLE_FIELDS`
  (`schema.py:916-921`) or salvage drops it silently; add it to system.md's JSON shape block
  (119-129), since the prompt says "emit only fields you can fill" against that shape; include it in
  `build_code_references`'s claim loop (`report_bug.py:324`), which reads only
  `mechanism`/`consistency` today, or its citations never become links.
* Correct the justification: `autofile_bug` requires verdict-in-`verdicts` AND confidence ≥ 70, so
  this is a footnote on a filing that already accuses someone — not a third outcome.

### 8. Ingest the correction — *why this is a repeat*

Add a `feedback.refresh()` job to `bin/schedule.py`; extend `feedback._fetch` to also read
`/rest/bug/{id}/comment`; store reviewer comments in a **new `reviewnote` table** via `_ADDED_TABLES`
(`models.py:3026`, currently `("archetypes","feedback","selection","sweepmarks")`) with a hand-set
`error_class`; print a second block in `bin/feedback.py`.

Corrections — the storage call is right, the labels are what fail:

* **Keep the new states OUT of `Feedback.attribution`.** That column is the causal verdict and feeds
  `scoreboard()["by_archetype"]`; of the 16 open filings that would get `corrected`, **two already
  carry a reviewer-set `regressed_by`** (2061975 `[2023197]`, 2063892 `[2058982]`) and writing over
  them destroys the verdict the table exists for. Also, two values in one column cannot both be set.
* **`needinfo_returned` is 17 chars against `db.Column(db.String(16))`** (`models.py:2881`) — a
  Postgres `StringDataRightTruncation` inside an unguarded `db.session.commit()` that would kill the
  new hourly job, and CI would not catch it because `tests/test_feedback_archetypes.py:18` sets
  `DATABASE_URL=sqlite://`.
* **The needinfo signal is 94% noise.** Of 51 filings, 18 ever had a needinfo aimed at cdenizet —
  **17 set by `release-mgmt-account-bot@mozilla.tld` in two mass sweeps** (2026-08-06, 2026-08-10).
  Exactly one is a human (this bug). And bug 2062119, the precedent, never had one at all. Source it
  from `/history` `flagtypes.name` additions (durable after the flag clears), filter to human
  setters.
* **Rename `corrected` → `human_replied`.** "A human commented" is not "a human corrected us":
  the predicate fires on 16 of 26 open filings including outright endorsements — bug 2060920
  (docfaraday: *"Seems like an easy enough fix… I'll probably do it all in one go"*) and bug 2063892
  (abienner: *"I have a fix almost ready"* + attachment 9627227). Only the hand-set `error_class`
  may assert a correction.
* **Scope to reactions to us.** Skip `mode == "comment_on_existing"` rows — `bugzilla_apply.py:693`
  sets `filed: True` for those, so the feedback set includes bugs we did not create, where all human
  traffic is ordinary discussion.
* **Do not identify our own comments by author email** — the filer posts as cdenizet, who is also a
  real reviewer on these bugs (8 such comments across 7 of 51). Match the body marker
  (`Crash report: https://crash-stats.mozilla.org/report/index/`) or persist the posted comment id.
* **Add `comment_count` to `_FIELDS`** and persist it: there is no bulk comment endpoint
  (`/rest/bug/comment?ids=…` returns error 100; libmozdata does one GET per bug), so an ungated
  sweep is 51-and-growing serial GETs per tick.
* **Schedule at 6h, not hourly**, matching `sweep_untriaged_job`. BMO rate-limits at ~45min.
* Deliberately out of scope: feeding these into a prompt automatically. The deliverable is the
  labelled corpus.

### Optional, higher-effort: `first_appearance`

A `json-diff/<node>/<path>` walk classifying each entry add-only / remove-only / add+remove.
Verified end-to-end on this file — it yields `db1a48a97fed add=1 rem=1` (the move the skeptic
mis-passed), `d100e97083cf add=1 rem=0` (**the true origin — the same changeset Jens named, in hg
namespace**), and `ef41d9e4c7de` (2025-03-03, the pre-backout original, invisible to a raw-file
walk). 3KB/fetch vs 373KB for a large file.

Do not ship it without these, all measured:

* **Unwrap the reason first.** Socorro stores `IPDL error: "<literal>". abort()ing as a result.`
  (36 reports) and `MOZ_CRASH(IPC FatalError in the parent process!)` (22). The raw stored string
  returns **0** searchfox hits; unwrapped, 1. Of the top-20 nightly reasons, 10 resolve to exactly
  one hit.
* **Cap exhaustion must be `unknown`, never an answer.** **6 of those 10 still contain the needle at
  revision #40**, so a 40-rev cap silently produces a wrong answer. For
  `MOZ_CRASH(IPC FatalError in the parent process!)` — 5 of the 22 crashes in this very build — the
  naive answer is `468d8d03508e "Backed out 6 changesets (bug 1875528)"`: the tool reinvents the
  failure it exists to kill, deterministically, above the analysis.
* **File creation is not provenance.** `SharedMemoryHandle.cpp` was created as `new file mode
  100644` with zero copy/rename headers, and 9 of its literals already existed in
  `ipc/glue/SharedMemory.cpp` at the parent. Report unknown when the filelog runs out.
* **`"explicit panic"` resolves to `third_party/rust/cssparser/src/tests.rs:1366`** — 78 nightly
  crashes/week — and would publish provenance about a vendored test file.
* Go through `crashclouseau.hgedge` (allowlisted UA, 406/429 retry, `Semaphore(8)`), and pin the
  start node like `history._pin` does.
* **Do not phrase the published line as exoneration.** Jens's own comment says 2045119 IS relevant.
  State what the candidate's diff did do: moved the check after the size read and changed the
  condition to `IsSafeToMap(handle, size)`.
* Name the **hg** node and link hg.m.o. `json-rev/d100e97083cf` returns `git_commit: None` and
  lando's `hg2git` raises `LandoMissingCommit` — only git→hg resolves. Jens quoted `8c4833bf60ec`
  (git); the tool produces `d100e97083cf` (hg). Same changeset, different string.

---

## 5. The generalization — do not overfit to what he said

See `memory/feedback-generalize-dont-overfit.md`. Reviewer feedback describes what is true **in the
reviewer's context**; implementing the sentence literally overfits and fails. Climb the ladder:
*literal statement → the mechanism that makes it true → the property observable in any crash report*.
Ship the third rung.

Jens made three substantive points. Read literally they are three unrelated fixes. Generalized they
are **one** defect:

> Every claim in comment #0 that he corrected was checkable against a source of truth the run
> already held, and none of them was checked.

| his correction | the truth we held | the generalized check |
| --- | --- | --- |
| wrong introducing changeset | the tree at the candidate's parent | an "X did not exist before C" claim is unverified until you read C's parent |
| direction backwards | the `process_type` distribution | a mechanism predicting a restriction must be tested against the observed distribution of that variable |
| one machine, one kernel | the facet response we already download | a mechanism must explain the population, not one report |

"Add an OS check" is his instrument. "Claims get checked against data we already have" is his
context.

**Where the list above still overfits, and what to do:**

* **#3, the 0.70 share threshold, is a textbook violation.** It was derived by noticing this
  signature sat at **0.797** on filing morning and picking a number under it. Fit it on the 51
  filings plus a control panel, and name the counter-example it must not eat (a genuinely Linux-only
  regression; a first-day crash with one install).
* **#3 also treats OS as the axis because Jens said "OS".** The axis should be facet-agnostic — top
  term dominance over ANY facet — or it misses the next case where the degenerate dimension is a
  GPU, a locale, a graphics driver, or a single build. Same instrument, one rung up, no extra cost:
  the facets arrive in one response.
* **#1 is a rung too low.** It catches moves *within one patch*, which is where Jens's example
  landed. This very file proves the class is wider: `SharedMemoryHandle.cpp` was created with no
  `copy from` header and nine of its literals already existed in `SharedMemory.cpp`. A string that
  moved *between files* passes the `-`/`+` check clean. Ship the detector, but state the general
  rule in the prompt: **read the parent**, not **look for a minus line**.
* **#7 already got caught for the same reason** — wording it around `MOZ_LOG`, his literal words,
  excluded the worse case where the Windows path logs nothing.
* **#8 is the anti-overfit machinery**, not bookkeeping: its value is accumulating enough labelled
  corrections that the NEXT rule is fit on a population instead of on one bug. That argues for
  moving it up.

Of the four proposals carrying a threshold, exactly one was fit on a real panel
(`moved >= 0.5 * added_non_brace`, on 334 corpus regressors / 2840 files). The other three came from
this case or from a 9-signature sample.

---

## Appendix — reproduction

```bash
# The whole of Jens's comment-#1 data paragraph, one query
python3 - <<'PY'
import urllib.parse, urllib.request, json
sig = 'IPC::MessageReader::FatalError | mozilla::ipc::shared_memory::HandleBase::FromMessageReader'
params = [('signature', '=' + sig), ('date', '>=2026-06-22'), ('_results_number', '0'),
          ('_facets', 'platform'), ('_facets', 'platform_version'),
          ('_facets', 'platform_pretty_version'), ('_facets', 'cpu_info'),
          ('_facets', 'process_type'), ('_facets', 'release_channel'),
          ('_facets', 'install_time'), ('_facets', 'build_id'), ('_facets_size', '15')]
r = json.load(urllib.request.urlopen(
    'https://crash-stats.mozilla.org/api/SuperSearch/?' + urllib.parse.urlencode(params)))
print('total', r['total'])
for k, v in r['facets'].items():
    print('---', k)
    for t in v[:12]:
        print('   %5d  %s' % (t['count'], t['term']))
PY

# The three age clocks
curl -s 'https://crash-stats.mozilla.org/api/SignatureFirstDate/?signatures=IPC%3A%3AMessageReader%3A%3AFatalError%20%7C%20mozilla%3A%3Aipc%3A%3Ashared_memory%3A%3AHandleBase%3A%3AFromMessageReader'
# -> first_build 20250430203103, first_date 2025-05-14   (475 days; wired to no gate)

# Provenance, settled in one GET (parent of db1a48a97fed)
curl -sL https://hg-edge.mozilla.org/mozilla-central/raw-file/e184115c58e2/ipc/glue/SharedMemoryHandle.cpp \
  | grep -n 'is not safe to map'
# -> 94:    aReader->FatalError("Shared memory PlatformHandle is not safe to map");

# The move, in the tool output the model already had
curl -sL https://hg-edge.mozilla.org/mozilla-central/raw-rev/db1a48a97fed | grep -n 'is not safe to map'
# -> one '-' line and one '+' line, same hunk

# The parent node the changeset tool drops
curl -sL https://hg.mozilla.org/mozilla-central/json-rev/db1a48a97fed | python3 -m json.tool | grep -A2 parents
```

Workflow transcript with all 128 ground-truth findings and the full verify pass:
`~/.claude/projects/-home-calixte-dev-mozilla-crash-clouseau/05fd775e-e330-4d08-af1f-bdf2de5f104d/subagents/workflows/wf_5a575f8d-e0f/journal.jsonl`
