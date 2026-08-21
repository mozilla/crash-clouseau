# next_session.md — the overfitting audit

**Mission.** Go through crash-clouseau's filter rules, gates, thresholds, agent prompts and
archetypes and find every place where a rule learned in ONE CONTEXT is applied as a GENERAL rule.
Where the context is real, make the code identify the context first and apply the rule only then.
Where the rule has no context at all, either give it one or remove it.

You are starting with no conversation history. Everything you need is in this file, in the linked
memories, and in the repo's docstrings. Read §1 and §2 before touching anything.

---

## 1. The principle

Calixte, 2026-08-21:

> Reviewer feedback describes what is true **in the reviewer's context**. Implementing the sentence
> literally overfits and will fail. The job is to extract something MORE interesting than what was
> said — work out what context makes their statement true, find a way to identify that context from
> a crash report, and gate on THAT.

His worked example: a reviewer says *"the crash signature must start with `Foo`"*. Filtering all
analysis down to `Foo*` signatures is the overfit. What they actually said is "in my context only
`Foo*` crashes are interesting" — so find the context, detect it, and filter on it.

Saved as `memory/feedback-generalize-dont-overfit.md`. Two memories are instances of it:
`hardware-noise-denominator` (the denominator is the whole rule) and `js-engine-filings-are-noise`
(a flat `Core::JavaScript*` ban was refuted; the survivor was signature novelty).

### The ladder

For every rule: **literal statement → the mechanism that makes it true → the property observable in
ANY crash report.** Ship the third rung. The first two are evidence, not code.

### The three failure modes to hunt

1. **UNGATED CONTEXT RULE** — learned from one context (one signature, one component, one reviewer,
   one crash shape, one platform), applied to every crash with no predicate identifying that context.
2. **MISCALIBRATED CONTEXT PREDICATE** — the rule HAS a gate, but the gate does not identify the
   context it was written for. Too narrow (misses the cases it exists for) or too broad (fires out
   of context). Both are overfit failures.
3. **n=1 THRESHOLD** — a number read off the motivating case instead of fit on a panel with a stated
   counter-example.

A rule is **not** overfit merely because it came from one case. It is overfit when its **scope
exceeds its evidence**. A rule earned from one case, gated to that case's context, fit on a panel,
and shipped with a named counter-example, is correct — and this repo has 33 of those (§6).

### The bar every fix must clear

The good pattern is already in this repo. Hold every change to it:

**(a)** try the obvious predicate → **(b)** MEASURE it failing → **(c)** sweep on a panel with the
wrong-direction cases named → **(d)** ship the counter-example in the docstring.

`_apply_signature_age_gate`, `_apply_bad_machine_gate`, `_apply_is_backout_gate` and
`datacollector.get_maturity_bar` do all four. `compiled_out.py:20-27` and `config.py:669-674` even
record their own NULL results. A rule that does not do those four things is the outlier here — say
so in the commit rather than inventing a new format.

**A recommendation with no counter-example is the same overfitting error one level up.** Two of the
six verified items below killed their own proposed repair on the counter-example.

---

## 2. Why now — and a worked example of getting it wrong

On 2026-08-21 :jstutte reviewed filed bug 2065373 and corrected three things. Read literally they
are three unrelated fixes (a provenance tool, a direction check, an OS facet). Generalized they are
one defect: **every claim he corrected was checkable against a source of truth the run already held,
and none was checked.** "Add an OS check" was his instrument; "claims get checked against data we
already have" was his context. Full analysis in `jens_feedback_bug2065373.md`.

While writing that analysis I committed the exact error this audit is about. I read the
stale-gate/second-opinion-fold ordering off that one bug, saw it explained that one bad outcome, and
recommended changing it — without looking at the denominator. This session then measured it: the
change **kills 3 FIXED bugs (2 topcrash) plus 1 ASSIGNED, and gains 6 low-value filings avoided**.
See worklist rank 1. That is the failure mode, committed by the person auditing for it. Expect to do
it again; the defence is the counter-example requirement, not care.

---

## 3. How to measure anything here

These came out of the inventory pass and will save you hours. Read them before designing a panel.

* PROD DB IS NOT REACHABLE from this machine: `heroku auth:whoami` returns 'Invalid credentials provided' and there is no DATABASE_URL in the environment. Every panel below is reconstructible from PUBLIC BMO + Socorro, and a prior session did exactly that end to end. Do not plan a measurement that requires the `dossiers`, `archetypes` or `feedback` tables unless you first re-auth.

* THE 51/52-FILING PANEL, the workhorse for anything outcome-shaped: BMO `creator=cdenizet@mozilla.com`, `creation_time>=2026-08-05`, `short_desc` substring `Crash in [@`; pull the crash uuid out of comment 0; re-fetch the ProcessedCrash from Socorro. A prior session resolved 52/52. Outcomes come from `resolution` plus the `regressed_by` HISTORY (who set it and when — a value we wrote ourselves must score `unconfirmed`, not `correct`).

* THE 840-REPORT NIGHTLY CONTROL SAMPLE: 60/day across a 14-day window via the unauthenticated SuperSearch, then fetch each ProcessedCrash. This is the only way to get a DENOMINATOR for anything prompt- or matcher-shaped. To replay a matcher exactly, rebuild the facts dict `orchestrator._matching_archetypes` (orchestrator.py:768) builds — signature, `_stack_text` of the first 40 frames of `inspector.thread_for_analysis`, `crash_info.type or reason`, `crash_info.address or address` — because the matchers are pure functions of that dict and nothing else.

* RUN THE SHIPPED CODE, DO NOT PARAPHRASE IT. `DATABASE_URL=sqlite:///:memory: REDIS_URL=redis://localhost:6379 uv run python` imports the whole package fine, and two prior sessions used it successfully (a `_looks_poison` probe over 14 addresses, and the full stale-clamp/SO-boost reproduction through `apply_deterministic_gates`). Repo convention is `uv run python`; deps via `VIRTUAL_ENV=.venv uv pip install`.

* SIX GATES ARE STRUCTURAL NO-OPS OFFLINE (backout, is-backout, compiled-out, signature-age, bad-machine, bit-flip, plus the SO fold which receives None) because they key on seed keys or online resolvers `eval/runner.py` never sets. FOUR MORE ARE DEAD IN PROD (`_apply_callpath_gate`, `_apply_offstack_observe_only`, the prior-signature corroboration branch, `_looks_pref_flip`) because `offstack.enabled=false`. Any 'how often does this gate fire' measurement must exclude both sets explicitly or it reads as a null result — and the eval harness cannot measure any of the first six at all.

* THE ABSTAIN CHANNEL IS INVISIBLE TO EVERY OUTCOME MEASUREMENT THE REPO HAS. An abstain skips the second opinion (orchestrator.py:2268), files no bug, and therefore never reaches `Feedback`. So any rule whose failure mode is a FALSE ABSTAIN — the skeptic compiled-out clause, both archetype guidance texts, the backout gate, `_skeptic_veto` — must be audited by REPLAY (re-run the rule against real crashes and inspect what it would have eaten) rather than by outcome. Plan for that up front; it is the single biggest methodological constraint here.

* DO NOT TREAT AN EMPTY `Feedback` SCOREBOARD AS A NULL RESULT. `feedback.refresh()` is not in bin/schedule.py (verified: three jobs, none is feedback), so the table only reflects the last manual `bin/feedback.py` run. `by_archetype` is additionally denominated on FILED bugs, so a rule firing on 19 of 840 analysed crashes scores zero by construction.

* A TEST CAN BE THE TRAP. tests/test_feedback_archetypes.py:62 is the `shutdown-singleton` row's only back-test and it passes solely because it feeds SIGNATURES into the `stack` field — an input shape `orchestrator._matching_archetypes` never produces (measured 0/840 for those tokens). Before trusting any test as a back-test, check that its fixture matches what the production CALLER actually builds.

* DOCSTRINGS ARE THE PRIMARY EVIDENCE STORE IN THIS REPO, and several panels exist ONLY there. Treat a docstring number as a claim to verify. Panels that DO verify against committed artifacts: signature_age min_age_days=7 vs spike/SO_TIMING_VERIFICATION.json (exact), second_opinion effort=high vs spike/SO_INSTRUMENT_CALIBRATION_high.json (exact), the calibration table vs corpus_ship/calibration_table*.json (exact, and that is how rank 3 was found). Panel with NO committed artifact: the bad-machine 141k study — `grep -rln '141,\?[0-9]\{3\}\|11735\|17\.96' spike/` returns nothing, so it must be rebuilt from Socorro before it can be reproduced.

* READ config/global.json FOR WHAT RUNS, NOT config.py. Three shipped values differ from the code default a reader of config.py sees: `spike.floor` 5 vs 3 (Firefox-nightly), `abstain_below_confidence` 0.5 vs 0.85, and `comment_max_bug_age_days` has no global.json entry at all (the code default 30 is what runs). Also: config/global.json carries NO `signature_age`, `bit_flip`, `bad_machine` or `second_opinion` keys, so every threshold in those four gates is a Python default and changing one is a deploy, not a config change. The env kill switches are all-or-nothing, and `_apply_compiled_out_gate` has none at all.

* TWO KNOWN DOCSTRING/VALUE MISMATCHES, both in otherwise load-bearing prose, so expect more: `_SWEEP_DEFAULTS['max_age_s']` says 'a build from a month ago' for a 14-day value (config.py:332-333), and agent/second_opinion.py:7 and :19 still describe and defend effort=max three paragraphs after config.py:513-518 retired it for `high`.

* A CORROBORATION FLAG IS THE ONLY COUPLING MECHANISM BETWEEN GATES AND THERE IS NO REGISTRY OF WHICH ARE READ. Before adding, renaming or relying on one, grep BOTH writers and readers — `stale_signature_clamped` has one writer and zero readers (that is worklist rank 1), `downgraded_from_strong` has exactly one of each, and `_INSTANCE_SUPPRESSED` is a hand-maintained list of three flags out of eight suppressions. All three of the gate-interaction findings in this worklist are instances of that one structural gap.

* ORDERING IS ITSELF A RULE AND NOTHING TESTS IT. `apply_deterministic_gates` (orchestrator.py:2317-2443) fixes callpath -> exposer -> corroboration -> signature-age -> SO fold -> backout -> is-backout -> bit-flip -> bad-machine -> absent-thread -> compiled-out -> recorders -> reconcile -> observe-only -> worth-investigating. Every choice has a stated reason in the comments and most are good, but the reasons live only in comments and no test pins the sequence. Any fix that moves a gate must re-read those comments first — several arguments ('no amount of independent agreement makes a broken machine's crash a code defect') are load-bearing.

* THE GOOD PATTERN IS ALREADY IN THIS REPO AND EVERY FIX SHOULD BE HELD TO IT: (a) try the obvious predicate, (b) MEASURE it failing, (c) sweep on a panel with the wrong-direction cases named, (d) ship the counter-example in the docstring. `_apply_signature_age_gate`, `_apply_bad_machine_gate`, `_apply_is_backout_gate` and `datacollector.get_maturity_bar` do all four; `compiled_out.py:20-27` and `config.py:669-674` even record their own NULL results. A rule in this area that does not do those four things is the outlier, not the norm — say so in the commit rather than inventing a new format.

* SOCORRO MEASUREMENT TRAPS that will corrupt any panel built here: crash REPORTS are not a volume metric (one machine produced 81,843 of 86,196 in a prior study), `install_time` resets on update so only PER-DAY distinct counts are machine counts, `aurora` IS beta (omitting it loses ~1/3 of the channel — note `sigage.hardware_noise` sets `release_channel` RAW at sigage.py:414, bypassing `utils.get_search_channel`, which is harmless on nightly-only and becomes a silent 1/3 denominator shrink the day beta or Fenix is enabled), release is 10% sampled, and `SignatureFirstDate` does not answer for brand-new signatures (~7.6% of dossiers).

* hgmo RATE-LIMITS BULK ACCESS WITH 406 per IP. The `crash-clouseau` UA is allowlisted but libmozdata does not send it for hg endpoints — set it explicitly before any pushlog- or blame-heavy replay, and respect `hgedge`'s 8-way concurrency ceiling.

* SCOPE GAP TO CARRY FORWARD: `crashclouseau/vendor/` (hackbot runtime, agent_tools) was excluded from every sweep by all six readers. If a threshold or filter lives there it is completely unaudited.

* ARCHETYPE ROWS ARE DB-EDITABLE AND `seed(overwrite=False)` NEVER CLOBBERS, so prod guidance may differ from the source text in archetypes.py and nothing surfaces the divergence (no page renders the table). Any measurement of an archetype from source is an assumption — state it. `matcher={}` matches every crash and is pinned as intentional, so the table is also the sanctioned way to create an ungated context rule with no review, no test and no back-test.


---

## 4. Verified findings — six items, already measured

These six were taken all the way to data. **Do not re-derive them.** Three had their proposed repair
REFUTED by the counter-example; those are recorded so you do not rebuild them.


### Rank 1 — `_fold_second_opinion` refuses to re-inflate a suppressed lead only when `corroborations['downgraded_from_strong']` is set — a flag the stale-signature gate never writes

**Verdict: `partially`** · miscalibrated-context-predicate · `crashclouseau/agent/orchestrator.py:1227 (guard); sole writer at :964; `stale_signature_clamped` written at :1398`


**Measured**

> Corpus: `spike/_dossier_dump.jsonl` = 1996 persisted PROD dossiers (2026-07-06 → 2026-08-05), plus the 51 Clouseau filings (BMO `depends_on` of bug 1396527, ids >= 2060000, 2026-08-05 → 2026-08-21) re-scored against Socorro `signature_history` + hg `json-rev` pushdate.
> 
> (A) THE GUARD'S OWN PREDICATE IS DEAD IN PROD. `downgraded_from_strong` appears on 2 of 1996 dossiers. On BOTH the SO *refuted* (`corroborates=False`, `high`) — the other branch. Dossiers where the guard at :1227 actually blocked a boost: **0 of 1996**.
> 
> (B) THE UNNAMED SUPPRESSION ESCAPES IT. Window where both gates were live (2026-07-27 13:00Z → 2026-08-05): 741 dossiers, 57 reported verdicts, 29 at rung >= 70 (`autofile.min_confidence`). `stale_signature_clamped` = 67, split by (raw conf, final conf, boosted): 39 medium→low, 19 probable→low, **6 probable→medium (clamp held, filing blocked)**, **3 probable→probable (clamp reversed by the boost)** = 3/9 = **33% of the clamps that would otherwise block a filing**. Of the 29 fileable verdicts: 14 (48%) owe their rung to `second_opinion_boosted`, 1 to `fault_address_offset_match`, and **3 (10%) are stale-clamp reversals — 3 of the only 4 stale fileable verdicts**. The three: `27c1cafd…0729` ModuleLoaderBase::RegisterImportMap (110.5 d, SO medium), `4f51048e…0731` widget::WlLogHandler (744.4 d, SO high), `6685aff5…0802` nsID::Equals (85.2 d, SO high).
> 
> (C) EXECUTED REPLAY on those 3 real payloads through the shipped `_fold_second_opinion` + `_apply_worth_investigating`: today → `probable` / p_worth 0.9714 / autofiles=True; with the predicate widened to "any deliberate suppression" → `medium` / p_worth 0.80 / autofiles=False. The naive fix does exactly what it claims.
> 
> (D) BUT THE FILING CORPUS REFUTES THE FIX. 11 of 50 resolvable filings (22%) named a candidate landing >7 d after the signature's first-seen on the clock in force at filing time (nightly-only before `14b6ed9`, 2026-08-20 11:20; floored all-channel after) — all 11 filed at rung >= 70, i.e. all escaped the clamp. Outcomes: STALE n=11 → 3 FIXED + 1 ASSIGNED = **36% acted on**; FRESH n=39 → 7 FIXED + 2 ASSIGNED/REOPENED = **23% acted on**. The stale cohort is BETTER, not worse. All 3 FIXED stale filings carry a HUMAN-set `regressed_by` naming exactly the bug of the changeset Clouseau named (verified in bug history: not our own write).
> 
> (E) `min_boost_confidence`=50 and the clamp probable→medium=50 land on the same number by coincidence; neither was fit against the other. A one-rung difference in either erases the leak.


**Counter-example**

> A LONGSTANDING SIGNATURE THAT ACQUIRES A NEW CAUSE — the "signature REUSE" case the stale gate's own docstring says it must not kill. Real, queried, and it is FIXED code:
> 
> **bug 2062219, `Crash in [@ nsAtom::IsStatic]`** (filed 2026-08-10, comment says "Clouseau analysis (confidence probable)" = decision `lead`, rung exactly 70, so the clamp applied). Nightly first-seen 20260117213627; candidate `60d6dd5b849b` landed **202.5 days later** — the gate clamps it to `medium`/50 and `bugzilla_apply.autofile_bug` refuses anything below 70. It was filed anyway. `60d6dd5b849b` is bug 2043000; on 2026-08-13 **dveditz** independently set `regressed_by=2043000` and renamed the bug "(regression from bug 2043000)". `topcrash`, RESOLVED FIXED 2026-08-12, assigned kye@mozilla.com.
> 
> Two more of the same shape, also human-confirmed:
> - **bug 2061960** `nsFind::FindFromRangeBoundaries`, "confidence probable", 326.3 d stale, named `f22608dce605` = bug 2043347; **dmeehan** set `regressed_by=2043347`; FIXED by jjaschke.
> - **bug 2063809** `ff_vk_exec_add_dep_frame`, 97%, 38.8 d stale, named `585a77d8786a` = bug 2045970; **dmeehan** set `regressed_by=2045970`; topcrash, FIXED, assigned tboiko@nvidia.com.
> - (plus **bug 2061127** `SessionHistoryEntry::GetParent`, 28.5 d stale, ASSIGNED to :farre.)
> 
> Blocking the boost after a stale clamp costs exactly these: **3 FIXED (2 topcrash) + 1 ASSIGNED**, out of the 10 stale LEAD filings. The other 6 (3 NEW, 1 DUP, 1 INVALID, 1 WFM) are what you would gain.
> 
> The second required counter-example — a fault-offset promotion whose candidate predates first-seen — is SAFE under the widening but shows what is at stake: only 1 of 29 fileable verdicts carries `fault_address_offset_match` and 0 of the 14 boosts do, so the SO boost is the pipeline's single largest promoter (14 of 29 rung-70 verdicts) and the corroboration gate is nearly inert.


**Recommendation**

> DO NOT widen the predicate to "any deliberate suppression". Measured cost on shipped code: 3 FIXED bugs with human-confirmed exact regressors plus 1 assigned bug, against 6 low-value filings avoided — and the stale cohort's acted-on rate (36%) beats the fresh cohort's (23%). Close this audit item as "leak real, repair refuted", so the next session does not re-derive it.
> 
> The reason one predicate cannot serve both suppressions is that they suppress on different AXES, and this is the context identification the guard never did: the exposer downgrade says "related is not cause", and an SO `corroborates` ("this changeset is related") is precisely the uninformative signal there — that is why ce6b8fc blocked it. The stale clamp says "cannot be the ORIGIN, relevance stands" (its own docstring), so an independent blind agreement about the MECHANISM is genuinely new evidence on the other axis. Encode that instead of a flag name: give each suppressing gate an explicit `so_boost_policy` ("block" for exposer/SF-3, "allow" for stale) so the next gate author must choose rather than silently inherit "boostable", and delete the flag-name coupling at :1227. That is a one-line-per-gate change with no behaviour change today.
> 
> What IS a defect and is cheap: the round trip is invisible. `stale_signature_clamped` has ONE writer and ZERO readers outside tests — it reaches no prompt, no UI chip and no bug comment. A lead clamped for 202 days of staleness and re-inflated ships at p_worth **0.9714**, byte-identical to a clean rung-70 lead, and the recipient is never told the timing evidence ran against it. This is the jstutte lesson exactly (bug 2065373): the run already held the fact and did not print it. Emit the pair into the filed bug's onset paragraph next to `build_signature_age_note` — "this signature is N days older than the changeset below; an independent blind review still agreed with the mechanism" — and into the UI chip. That also creates a labelled cohort so the 3/9 figure can be re-measured on real outcomes instead of inferred.
> 
> Do not tune a threshold here: n=9 clamps / 3 reversals / 10 stale filings is enough to notice and nowhere near enough to fit. Re-measure at n>=30, and note that `min_boost_confidence`=50 sitting exactly on the clamp's landing rung is an unexamined coincidence — if anything is retuned, retune that, with the counter-example above as the fixture.


### Rank 2 — "This list is COMPLETE ... a mechanism that needs it is REFUTED, not merely unproven", gated on `len(distinct NAMED threads) <= 120`

**Verdict: `partially`** · miscalibrated-context-predicate · `crashclouseau/agent/triage.py:545 and :552; identical predicate reused by the gate at crashclouseau/agent/orchestrator.py:1591`


**Measured**

> Corpus: 840 Firefox-nightly processed crashes, 60/day x 14 days (2026-08-07..08-20), unauthenticated SuperSearch + per-uuid ProcessedCrash; plus all 52 Clouseau filings (BMO) replayed through the shipped code. Script + data: /tmp/thraudit/.
> 
> (1) THE SUSPICION REPRODUCES BUT THE NAIVE FIX IS REFUTED. 810/840 emit an inventory; 786 say COMPLETE, 24 say TRUNCATED. 347 of the 786 (44.1%) contain unnamed threads (median 14.5% of the process, p90 57.5%, max 83.5%) — so `complete` does mean "our ceiling did not clip the list", not "every thread is named". Sharpest case: the CRASHING thread itself is unnamed in 58/821 (7.1%), and 47 of those are still declared COMPLETE (5.7% of the sample) — the model is shown "Crashing thread: 12" above a "COMPLETE" list that does not contain thread 12. BUT the tightening `unnamed == 0` is dead: it drops COMPLETE from 786 to 439 of 810 (-44% of the rule's reach) and buys ~nothing, because unnamed threads are essentially never Gecko threads. Of 5,982 unnamed threads, 13 (0.22%) contain any xul.dll/libxul/`mozilla::`/nsThread frame, and all 13 are DLL-init (`_cairo_mutex_initialize` under LdrpCallTlsInitializers), AV injection (aswJsFlt.dll), breakpad, or `AnimateSkeletonUI` — not one pool/subsystem thread. Control on the same detector and the same 12-frame window: 29,747/32,840 NAMED threads (90.6%) are detected, and 99-100% of the canonical rosters (TaskController #N, StyleThread#N, IPC I/O Child, Socket Thread, Timer, HTML5 Parser, Compositor). So absence-from-the-named-list really does imply absence-of-the-subsystem, at ~0.2% leakage.
> 
> (2) THE 120 IS AN n=1 THRESHOLD AND IT COUNTS THE WRONG THING. The comment says it outright: "the widest hang report sampled ran 113 threads". It fires on 24/840 (2.9%) — all 24 parent-process, i.e. 11.8% of parent crashes lose the absence claim; 0 of 116 hang/shutdownhang reports are clipped. What pushes them over is INSTANCE numbering, not subsystems: `FSBroker<pid>` contributes 582 of those names across the 24 crashes, plus WRWorkerLP#N/TaskCon~ller #N/StyleThread#N/DNS Resolver #N. Collapse the `#N`/trailing-digit suffix and the widest crash in 840 has 79 distinct families; ZERO crashes exceed 120 families. Budget is not the constraint either: the widest inventory is 3,683 bytes (1,037 as families), median 482. And the clip takes `names[:120]` in thread-index order, i.e. it drops the most recently created threads.
> 
> (3) THE ABSENCE LICENCE IS UNCONDITIONED ON PROCESS TYPE, AND THAT IS MEASURABLY WRONG. 113 of the 159 thread families seen >=10x (71%) are >=95% confined to ONE process type. p(thread present | process type), measured: MediaTrackGrph parent=0.00 content=0.05 (17/17 occurrences are content); GraphRunner parent=0.00 content=0.07; MediaDecoderStateMachine parent=0.00 content=0.21; AudioIPC Client RPC parent=0.01 content=0.27; conversely AudioIPC Server RPC parent=0.51 content=0.00, IPDL Background parent=0.83 content=0.00. So on a PARENT crash the absence of `MediaTrackGrph` is the base rate, not evidence — which is why Pehrson needed a second argument ("There may be a MediaTrackGraph in a content process but then the shutdown blocker would live there too"). The code preserves that caveat (clamp, never abstain, orchestrator.py:1663); the prompt drops it and says "REFUTED ... say so and look elsewhere", and via `second_opinion.py:92 -> triage._crash_facts` that absolute reaches the blind SO, the specificity-1.00 instrument used to suppress leads.
> 
> (4) THE MEASURED DEFECT IS ONE LEVEL UP, IN `_QUOTED_THREAD_RE`. Replaying the shipped gate over all 52 filings: its ENTIRE reach is 3 quoted-thread matches, and 1 of the 3 is a false positive. Bug 2062286's consistency statement reads "Crash is a main-thread `EXCEPTION_ACCESS_VIOLATION_READ` reading a `char16_t` buffer..." — `\bthreads?\s+["'`]X["'`]` matches because "main-thread" ends in "thread", so the exception name is treated as a claimed thread, is absent from the 86-thread inventory (complete=True), and clamps the verdict. Bug 2062286 is RESOLVED FIXED with `regressed_by: 2043000` — the exact bug Clouseau named. At 97% (rung 70/85) the clamp to medium (rung 50) falls under `autofile.min_confidence: 70`, so the one confirmed-correct filing in the corpus would not have been filed. The construction is generic ("off-thread `Bar`", "background-thread `nsThread`" all match) and 10 of 52 filings already use an X-thread compound. The other 2 matches are correct: 2064436 fires (MediaTrackGrph absent), 2065075 does not (present).


**Counter-example**

> Crash ec1ff67a-a835-4740-be14-572e50260818 (bug 2064436, the case that earned the rule) — verified live against ProcessedCrash: 46 threads, 32 distinct names, 14 UNNAMED, declared COMPLETE today. A tightened `unnamed == 0` predicate would withdraw the absence claim on the exact crash whose absence claim was correct. Checked what those 14 are: threads 5,6,30-35,41 are `ntdll!ZwWaitForWorkViaWorkerFactory` (Win32 thread-pool), 4 and 29 `NtWaitForMultipleObjects`, 7 `NtRemoveIoCompletion`, 43/44 `win32u!ZwUserGetMessage` — 0 Gecko frames among all 14. None could have been a MediaTrackGraph driver or GraphRunner, so Pehrson's refutation was sound over a 30%-unnamed list. Second counter-example, for the regex fix and for the reverse direction: crash 8e0a65cc-9166-4d7b-9aa2-e2ed40260810 (bug 2062286, FIXED, regressed_by 2043000) — 86 threads, 12 unnamed. Its inventory being COMPLETE is what exposes it to the "main-thread `EXCEPTION_ACCESS_VIOLATION_READ`" false fire; ironically the naive `unnamed == 0` fix would have saved it by accident, which is the clearest sign the completeness predicate is not the thing that is broken.


**Recommendation**

> Do NOT tighten to `unnamed == 0` — refuted above (-44% reach, 0.22% leakage, eats bug 2064436). Four changes, in descending measured value:
> 
> 1. FIX THE CLAIM-DETECTOR, NOT THE COMPLETENESS GATE (orchestrator.py:1545). Require the token before "thread" not to be a hyphenated compound — e.g. `(?<![-\w])threads?(?:\s+named)?\s+["'`]...`. This is the only measured error in the whole mechanism: 1 of the gate's 3 lifetime fires, on the single filing BMO confirms FIXED with the regressor Clouseau named, and it would have blocked that auto-file. Add "Crash is a main-thread `EXCEPTION_ACCESS_VIOLATION_READ` reading a `char16_t` buffer" to tests/test_hang_thread.py as a must-not-fire case.
> 
> 2. CONDITION THE ABSENCE LICENCE ON PROCESS TYPE, which is the generalisation of Pehrson's caveat and mirrors the hardware-noise denominator lesson: the denominator is the whole rule. The inventory speaks only about THIS process, and 71% of thread families are ~exclusive to one process type. Cheapest correct version needs no new data: change the prompt sentence to "a subsystem whose thread is NOT here was not running IN THIS PROCESS; if the subsystem normally lives in another process type (a content-process MediaTrackGraph seen from a parent crash) its absence here is expected and refutes nothing — you must also show why the effect would not cross the process boundary." That is exactly the extra step Pehrson took. The richer version — p(thread present | process_type), computable free from the same 840-report sample — turns the boolean into a strength: p=0.00 for MediaTrackGrph in parent means absence is uninformative; p=0.83 for IPDL Background means absence is real evidence.
> 
> 3. COLLAPSE INSTANCE SUFFIXES BEFORE APPLYING THE CEILING. Count `FSBroker1234`, `TaskController #7`, `StyleThread#5` as one family for the `<= 120` test (still print the full list, or print `Foo x N`). Measured effect: max families 79 over 840 crashes, so the TRUNCATED branch stops firing on all 24 parent-process crashes (11.8% of parent crashes) that lose the absence claim for a purely cosmetic reason, and the widest prompt block shrinks 3,683 -> 1,037 bytes. Keep the ceiling as a real guard, but re-derive it from the family distribution (p99 = 74, max = 79) rather than from one hang report's 113.
> 
> 4. SAY THE UNNAMED COUNT IN THE RULE, NOT JUST THE HEADER. The header already prints ", 14 unnamed" and the rule then says COMPLETE with no qualifier. Make it graded: "...COMPLETE for named threads; 14 of these 46 threads carry no name (almost always OS/driver thread-pool threads, which never run Gecko code), so a Gecko subsystem thread that is absent here was not running — an unnamed OS thread is not a hiding place for one." That is honest about the 44% and still licenses the inference the 0.22% leakage measurement supports. Cheap bonus: the 47 crashes where the crashing thread is itself unnamed should say so next to `Crashing thread: 12`.
> 
> Verdict on scope-vs-evidence: the completeness predicate is a defensible proxy and is NOT the overfit that was suspected — this is a `partially`, with the overfit sitting in the n=1 ceiling (fit to one hang report's thread count, counting instances) and in the un-gated absoluteness of the licence, not in the 120 boolean itself.


### Rank 3 — `agent.calibration.table` = {25:0.5, 50:0.8, 70:0.9714, 85:0.9714} — the POSITIVES-ONLY fit, shipped as the number a reviewer reads

**Verdict: `overfit-confirmed`** · scope-exceeds-evidence · `config/global.json:116; applied crashclouseau/agent/orchestrator.py:1166; defended crashclouseau/config.py:713-716 and crashclouseau/eval/calibrate.py:22-24`


**Measured**

> FAILURE MODE 2 (miscalibrated context predicate), not mode 1: the gate exists, is written down, and misidentifies its own population.
> 
> (1) Byte-check confirmed. corpus_ship/calibration_table_positives.json == the shipped table, with n_negative 0, n_test 0 (NO held-out split at all). Refit from corpus_ship/results.jsonl: positives arm rung70 21/21=1.000, rung85 13/14=0.929, pooled 34/35=0.9714. Full arm rung70 21/27=0.778, rung85 13/20=0.650, pooled 34/47=0.7234 (file says 0.7647 on its cal split). The delta is 12 deleted rung-70+ rows and ALL 12 were failures: 14 of 26 culprit-absent negatives were REPORTED (53.8%), 12 at rung 70+, worth=0 on every single one. The published 97% is the precision left after deleting only losses.
> 
> (2) THE GATE'S PREMISE, MEASURED ON THE FILER'S OWN POPULATION. `corroborations.candidate_in_pushlog_window` is recoverable from every public filing: report_bug.py:949 prints "This changeset did not land in this build's pushlog window..." exactly when it is falsy (report_bug.py:858 `is_suspected_regression`). That branch landed 2026-08-10 in ef0ccd8, so the valid denominator is the 31 filings since then. 16 of 31 = 51.6% (Wilson95 0.348-0.680) are self-declared culprit-ABSENT-window. The corpus's synthetic negative arm was 26/90 = 28.9%. Production is MORE culprit-absent than the arm that was deleted for being "rare in prod". Predicate is inverted, not merely loose.
> 
> (3) DIRECT EMPIRICAL p(worth | rung 70+) ON THE 51 FILINGS (BMO, blocked=1396527, creator=cdenizet, since 2026-08-01). Outcomes: 10 FIXED, 2 ASSIGNED, 2 open-with-human-regressed_by, 2 DUPLICATE-of-a-pre-existing-bug, 5 self-duplicates of our own filings, 8 crash_invalid (7 INVALID + 1 WORKSFORME), 22 untriaged. Adjudicated worth-rate = 16/24 = 0.667 (Wilson95 0.467-0.820); 21/29 = 0.724 (0.543-0.853) if self-dups count positive. Side by side: shipped 0.9714 / full-corpus fit 0.7647 / filer's own population 0.667-0.724. 0.9714 lies OUTSIDE the 95% upper bound of both readings. The full arm is the right reference class; the positives arm is not.
> 
> (4) THE 8 crash_invalid ARE ALL THE NEGATIVE SHAPE, in reviewers' own words: 2061726 jdemooij "hardware bit flip rather than a code regression"; 2061961 emilio "single crash and high Possible bit flips ... looks like a bitflip indeed"; 2061124 jdemooij "only report ... often hardware related"; 2062173 jdemooij "same machine as 2062168, uptime 3 seconds"; 2063364 iireland "likely to be noise ... 231 crashes in 3 months"; 2064137 jdemooij "single crash report and there are crash reports with this signature for older releases"; 2064436 apehrson "the analysis seems pretty weak"; 2064066 jmathies "crashes stopped in the build of the 16th". Every one is a window with no in-window culprit — the deleted arm, in production, at 97%.
> 
> (5) IN THE RULE'S FAVOUR, and worth recording so the next session does not re-litigate it: the fit label is NOT changeset-exactness. metrics.py:317/367 define `worth` = hit OR person_hit (same-author "silver nugget"), so "worth investigating" is measured at the level the badge claims. The rung 70/85 pooling is also a genuine result (non-monotonic on every cut) and correctly documented. The defect is the ARM, not the label and not the pooling.
> 
> (6) CORRECTION TO THIS AUDIT ITEM'S OWN PREMISE. Bug 2061969 is NOT "a human independently set exactly the regressed_by we named": zero non-bot humans ever commented on it. dmeehan@mozilla.com (release management) set regressed_by in a 6-bug batch on 2026-08-17 12:41:22Z-14:47:23Z, answering the BugBot "could you fill the regressed_by field?" nag by copying our own comment. dmeehan accounts for 9 of the 14 non-filer regressed_by settings. models.py:2910 `Feedback.classify` only guards against what the FILER wrote (`claimed`), so it scores all 9 `correct` — the loop is reading ~64% of its wins off its own prose. Genuinely independent adjudications: 2062219 (dveditz), 2062806 (ryanvm+hzhao), 2063892 (abienner), 2062052 (aborovova), 2061975 (dtownsend), plus the 2 contradictions 2062119 (jstutte replaced ours) and 2064137 (jdemooij removed ours).


**Counter-example**

> BUG 2062806 — verified by query, and it is the exact `-neg` shape the deleted arm consists of. The run itself concluded there was no in-window culprit and said so in the filed comment: "Starting point - NOT a suspected cause: d5f7230f4124 (bug 2057317)... This changeset did not land in this build's pushlog window, so there is no evidence here that the crash is a recent regression from it; it is named only as the closest thing found on the crash path." It was published at 97% anyway. Two days later hzhao@mozilla.com — the author we named — replied: "Confirming the mechanism - this is correct. MozAdsContextIdProvider.context_id() is a Sync foreign callback, but the MozAdsClient methods that call it are AsyncWrapped and run on a background thread... That trampoline is only reachable because bug 2057317 wired a real provider, so this comes from that change." He attached and landed a BACKOUT of that exact changeset the same day; ryanvm set regressed_by=2057317; RESOLVED FIXED. A culprit-absent window produced the cleanest confirmed fix in the whole set of 51.
> 
> The naive fix this kills: "the negatives are what deflated the number, so stop filing (or gate out) leads whose candidate is not in the pushlog window." That eats 16 of the last 31 filings including this one.
> 
> SECOND counter-example, killing the other naive fix ("97% was inflated, so raise autofile.min_confidence / file less"): BUG 2062119, filed at rung 85, attribution flatly WRONG — jstutte: "I do not think bug 1768581 is the regressor... The real regressor here is bug 1412726 (edc98f08f38d)", a 2017 bug roughly nine years outside the analysed window. That filing produced two patches from jstutte, landed 2026-08-18 and uplifted to BOTH mozilla-beta and mozilla-esr153 — the single highest-value output of all 51 filings. It scores hit=0 / worth=0 in the corpus vocabulary. Any recalibration read as a reason to file less eats it; report_bug.py:918 already records that this bug is why the "Starting point" wording exists.


**Recommendation**

> Do NOT touch `autofile.min_confidence` or the filing policy. 0.667-0.724 is well above any "stop filing" bar and both counter-examples are out-of-window / wrong-attribution shapes that produced landed fixes (one uplifted to ESR). The defect is the PUBLISHED NUMBER, and the fix is to make the gate real instead of deleting the arm — Calixte's own move: identify the context, detect it, gate on THAT.
> 
> 1. TWO TABLES, SELECTED BY THE PREDICATE THAT ACTUALLY IDENTIFIES THE CONTEXT. The corpus already contains both fits, one directory apart. Key `config.get_agent_calibration()` on `corroborations.candidate_in_pushlog_window` — the same field `report_bug.is_suspected_regression` (crashclouseau/report_bug.py:858) already reads to decide whether the prose may say "Suspected regressor". True -> positives arm; False/None -> full arm (rung 70+ = 0.7234-0.7647). Today the 16 out-of-window filings print the honest prose caveat and the dishonest number in the same comment. `is_suspected_regression`'s tri-state None already treats silence as "no", so the wiring is done.
> 
> 2. RE-DERIVE THE POSITIVES NUMBER BEFORE QUOTING IT. 0.9714 was fit with `holdout_folds=0` (n_test 0) — there is no held-out evidence for it at all, and the full arm's held-out rung-70 test bin read 4/6 = 0.667. Run `python -m crashclouseau.eval.calibrate --corpus-dir corpus_ship --positives-only --holdout-folds 3` and publish the number that survives a split.
> 
> 3. THE CORPUS CANNOT EXPRESS THE GATE. `corroborations` in all 90 rows of corpus_ship/results.jsonl carries only {call_path_verified, exposer_signals, exposer_strong, exposer_suspected, offstack_observe_only} — `candidate_in_pushlog_window` is absent, which is precisely why the arm split was asserted in prose rather than measured. Emit it in `eval.metrics` row-writing so the two-table fit is validated, not argued.
> 
> 4. FIX THE FEEDBACK LOOP'S OWN SELF-CONFIRMATION (independent of calibration, same root defect). `models.Feedback.classify` (crashclouseau/models.py:2890-2918) should score `unconfirmed` when the only non-filer `regressed_by` setter is a release-management batch on a bug with no substantive human comment. 9 of 14 currently read `correct` on that basis, so any forward labelling loop built on this table will confirm 97% by construction — the exact failure the `unconfirmed` state was invented to prevent.
> 
> 5. Delete or rewrite the justification comment at crashclouseau/eval/calibrate.py:154-158. "culprit-absent negatives are rare in prod" is measured at 51.6% (16/31 since ef0ccd8, Wilson95 0.348-0.680) versus the corpus's own 28.9%; leaving it in place is what makes the next session re-derive the same wrong table.


### Rank 4 — Skeptic: open the definition of any lock/barrier/assert/counter a mechanism rests on, walk `set_define` -> `option()`, and `fail` the claim if the option is default-off

**Verdict: `overfit-confirmed`** · ungated-context-rule · `crashclouseau/agent/roles.py:182-203 (the dead sub-clause at :190-192)`


**Measured**

> SCOPE vs EVIDENCE. Trigger population, 1996 prod dossiers 2026-07-06..08-05 (spike/_dossier_dump.jsonl): 842 reportable verdicts carry a mechanism, 384 (45%) name lock/barrier/assert/counter/atomic/flag — "assert" alone 167 (20%). Evidence panel: 3 refutations, one reviewer (jcoppeard), one subsystem; the paired deterministic gate's own back-test fires on 2 of 56 filings (3.6%).
> 
> WHAT THE SKEPTIC ACTUALLY REFUTES ON BUILD GROUNDS (same corpus, 8901 skeptic claims, 1765 `fail`): 124 claims (1.4%) reason about a build guard, 39 of them `fail`. Classified: PLATFORM 15 (38%), DEBUG/assert 6 (15%), moz.configure-option 7 (18%, only 2 of which are the concurrent-marking shape), other 11 (28%, cargo features / USE_MEMFD_CREATE / prefs). So the clause's instrument serves ~2/39 = 5% of the fails it is written for, while 21/39 name a macro in `compiled_out.GUARD_DENY` — exactly the 20 the code refuses to touch, and the prompt has no deny-list.
> 
> THE ORACLE IS WRONG WHERE MEASURED. Running the walk over js/moz.configure at a real build node (8e966e6c894a) labels 9 macros default-off; 3 are ON in official Nightly — MOZ_RUST_SIMD (`ac_add_options --enable-rust-simd` in build/mozconfig.rust, inherited by every official build), MOZ_INSTRUMENTS (browser/config/mozconfigs/macosx64/nightly), and MOZ_PROFILING (implied by it at js/moz.configure:342). The rule never reads a mozconfig. Pref arm: 16 prefs whose StaticPrefList.yaml value is `false` ship `true` from firefox.js/all.js (privacy.trackingprotection.*, extensions.webextensions.remote, browser.startup.preXulSkeletonUI), and 82 more have a build template as their "default" (66 `@IS_NIGHTLY_BUILD@`, 11 `@IS_EARLY_BETA_OR_EARLIER@`, 4 `@IS_NOT_RELEASE_OR_BETA@`, 1 nightly-or-devedition) — all ON in the only channel we analyse.
> 
> DENY-LIST GAP. `MOZ_DIAGNOSTIC_ASSERT_ENABLED` DOES have a set_define (moz.configure:174, `True, when=moz_debug | milestone.is_nightly | moz_dev_edition`) — compiled_out.py's docstring says it has none — and the nearest `option()` above it is `option("--enable-debug", nargs="?")` with no `default=`, i.e. the literal shape the prompt calls "did not run in the binary that crashed", for a macro ON in every Nightly. 9-11% of the crashes we analyse are MOZ_DIAGNOSTIC_ASSERT crashes (23/255 corpus_study, 9/83 corpus_ship, 20/216 corpus_neg75; Socorro: 1106 nightly reports in 30 days). Mitigation measured: searchfox-cli ranks 12 gtest/#ifdef hits and no moz.configure for the bare macro, so today the likelier outcome is a wasted multi-hop walk ending in `unverifiable`, not a wrong `fail`.
> 
> THE CODE GATE IS CORRECTLY SCOPED AND ALREADY DOES THE JOB. At a real pinned node, all 20 GUARD_DENY macros return False from `_option_is_default_off` (the docstring's "second lock" claim verified 20/20); on the motivating changeset 3f0439a2aec8 the diff ranker puts `gc::AutoMarkingLock` #1 of 8 and `hollow_symbols` fires on it and on none of the other 7. So `_apply_compiled_out_gate` alone suppresses bugs 2063782/2063902 with no LLM — the prompt clause is redundant for 2 of its 3 motivating cases and 2062114 was never catchable by either (jcoppeard's actual refutation there was semantic: "chunks are never freed until after marking has finished"). One deployment coupling: `hollow_symbols(..., rev="")` silently returns {} because hg-edge `raw-file/tip/js/moz.configure` 404s ("not found in manifest" — m-c tip is a .hgtags-only commit); the gate works only because 2d71e11 landed on the same branch, and dies silently if OFFSTACK_PINNED=0.
> 
> DEAD SUB-CLAUSE: 4 of 44 corpus_ship top-frame crash lines sit inside an `#if` (9%), and 3 of those 4 are include guards (GLCONTEXT_H_, SANDBOX_WIN_SRC_POLICY_ENGINE_PARAMS_H_) or MOZ_HAS_MOZGLUE — its trigger is broad-ish and almost entirely noise, on top of its author's 0-of-3.


**Counter-example**

> Crash 560c0f2f-07cc-46c6-950c-1d8240260731 — Firefox nightly, build 20260730132738, Windows NT 10.0.19044, signature `mozilla::FileBlockCache::Flush` (verified live via the Socorro ProcessedCrash API). Its only candidate, ff789e9f149e (backout of bug 2051354 part 5), touches 6 files of which 4 are under `widget/gtk/` (verified via hg-edge json-rev). The skeptic `fail`ed it with "GTK-gated Linux ibus/fcitx key-event plumbing, not compiled into Windows builds" and that fail was BINDING — the stored dossier's abstain_reason is literally "skeptic flagged this lead as noise / unrelated (failed: candidate_ff789e9f149e)".
> 
> This is the shape a naive fix eats. It is 15 of the 39 build-guard fails in one month and 3 of the 8 binding vetoes; MOZ_WIDGET_GTK / XP_* are all in GUARD_DENY and `_option_is_default_off` returns False for all 20 of them, so the deterministic gate is designed never to see it, and the clause's moz.configure walk answers nothing for it either (there is no `option()` behind MOZ_WIDGET_GTK). So "delete the compiled-out clause and trust the code gate", "only `fail` on macros the walk can resolve", or reverting defe860 (fail -> unverifiable) each turn ~38% of the correct noise-kills back into filed leads. Sibling case, same shape, same month: 8b7edf2e-7e4f-4a44-9b6d-a92370260731 (Windows nightly, `shutdownhang | InfallibleQuoteJSONString`, same GTK candidate).


**Recommendation**

> Narrow the prompt, do not delete the idea, and move the teeth into code.
> 
> 1. CUT the 279-word walk (roles.py:182-203): the `set_define` -> `option()` -> `default=` instrument serves 2 of 39 build-guard fails, and those 2 filings are already suppressed deterministically — verified end-to-end at a real pinned node (`gc::AutoMarkingLock` ranks #1 off the diff, `hollow_symbols` fires on it and on none of its 7 siblings). Also delete the citation-line sub-clause (:190-192): 0/3 by its author, 9% trigger on real crash lines and 3 of 4 of those are include guards.
> 
> 2. KEEP ~40 words carrying the CONTEXT rather than the instrument: "Code that is not in THIS build is `fail`, not a searchfox hole." Then the two predicates the reviewer's context actually implies, both inline: (a) NEVER conclude "off" for DEBUG / NDEBUG / MOZ_DIAGNOSTIC_ASSERT_ENABLED / MOZ_ASSERT_ENABLED / NIGHTLY_BUILD / EARLY_BETA_OR_EARLIER — they are ON in the channel we analyse (this is the deny-list the code already has and the prompt does not); (b) a platform macro is answered by the report's own `OS:` line, which triage._crash_facts already puts in the prompt, never by moz.configure; (c) a pref's StaticPrefList default is not what shipped (16 counter-prefs in firefox.js, 82 build templates) — so a pref-gated path is `unverifiable`, never `fail`.
> 
> 3. PUT THE BINDING DECISION IN CODE. `_skeptic_veto` (agent/schema.py:667-671, 699-705) should refuse to let a `fail` bind when its stated ground is a build-time compile-flag claim unless `corroborations["compiled_out_suppressed"]` agrees. That keeps every platform/feature-flag fail (they cite the crash's own OS, not a configure switch — 15 of 39, 3 of 8 binding vetoes) and removes the single place where the cheapest tier carries the longest multi-hop chain with the harshest consequence.
> 
> 4. Two cheap repo defects found on the way. crashclouseau/compiled_out.py:41-48 claims "DEBUG/MOZ_DIAGNOSTIC_ASSERT_ENABLED have no set_define at all" — false, moz.configure:169-178; the walk is safe by REGEX (it requires a bare identifier argument), not by absence, so GUARD_DENY is load-bearing and the docstring should say so. And `_configure_text` returning "" must be logged/distinguished from "not established off": `hollow_symbols(..., rev="")` silently no-ops because hg-edge tip 404s, so the whole gate is one empty `pin_rev` away from dead with no signal (measured: {} at rev="", the AutoMarkingLock hit at a real node).


### Rank 5 — `shutdown-singleton` archetype — small fault address + a shutdown token anywhere in the stack text asserts a cleared-singleton mechanism and steers the search outside the window

**Verdict: `overfit-confirmed`** · miscalibrated-context-predicate · `crashclouseau/archetypes.py:41 (max_fault_address), :42 (stack alternation), :48 (guidance)`


**Measured**

> Reproduced independently on a fresh uniform sample: 1051 Firefox-nightly processed crashes (150/day x 7 days, 2026-08-14..20), facts rebuilt exactly as `orchestrator._matching_archetypes` builds them (`inspector.thread_for_analysis` -> 40 frames -> `_stack_text`) and scored with the shipped `models.Archetype.matches`.
> 
> FIRINGS: 23/1051 (2.2%). 21 of the 23 carry a `moz_crash_reason` — Socorro's own record that the process aborted deliberately: 15x `AsyncShutdownTimeout | ...` ("###!!! ABORT: ..."), 3x `shutdownhang | ...` ("Shutdown hanging at step ..."), 2x `WlLogHandler` ("(kde) interface 'wp_image_description_v1' ..."), 1x `ServiceWorkerRegistrar::GetShutdownPhase` ("Failed to get async shutdown service"). Only 2 have no moz_crash_reason: the two `nsJARProtocolHandler::MimeService` at 0x28 — bug 2062119's own shape. So the guidance's opening sentence is false on 21 of 23 firings (91%), and on 3 of them the sibling `shutdown-hang` row is injected into the SAME prompt saying "A shutdown hang is not a fault: nothing crashed" (58ffaf90, 60681b60, 79f79bad). Those 3 double-fires are NEW: rebuilding the stack with the pre-5f169b6 watchdog thread gives 0 — the 2026-08-19 hang-thread fix put `AdvanceShutdownPhaseInternal` into exactly the stacks the sibling row owns.
> Out of shutdown entirely: 3 of 23 have `shutdown_progress` unset. 61228138 is a MOZ_Crash during STARTUP (`nsXREDirProvider::DoStartup` -> `ProfileStarted`), matched because the crashing FUNCTION is named `GetShutdownPhase`.
> 
> BRANCH BY BRANCH (hits in the stack field the row actually tests, n=1051): ClearOnShutdown 0, KillClearOnShutdown 0, XPCOMShutdown 0 (the real symbol is `ShutdownXPCOM`, 16), ::Teardown 0, UnloadLoaders 0, AsyncShutdownTimeout 0 (50 in the signature), shutdownhang 0 (36 in the signature), profile-change-teardown 0 (15 in the signature). The whole predicate is carried by ShutdownPhase 74 / AppShutdown 70 / AdvanceShutdownPhase 70, and `ShutdownPhase` matches as an ARGUMENT TYPE (`nsThreadManager::SpinEventLoopUntilInternal(..., mozilla::ShutdownPhase)`, 54 lines).
> 
> REACHABLE IN PROD, NOT HYPOTHETICAL: three real Clouseau filings re-scored — bug 2062062 (`AsyncShutdownTimeout | profile-change-teardown | LoginManagerRustStorage`, REOPENED) fires the row; bug 2061969 (`shutdownhang | __fstatat`, NEW) fires BOTH rows; bug 2063892 correctly does not (address 0x7ffc...). The row's own docstring cites 3 of its 4 "this is not a one-off" exemplars from precisely the deliberate-abort family.
> 
> CANDIDATE PREDICATES, both corpora (A = the 1051; B = 96 nightly EXCEPTION_ACCESS_VIOLATION_READ with `shutdown_progress` set, 3 months — the genuine-fault population): shipped A=23/B=25. +`shutdown_progress` set +`moz_crash_reason` empty: A=2 (both MimeService) / B=25 — every unit of loss is a false firing, zero recall cost. Replace the stack tokens with `shutdown_progress` alone: A=32/B=72 — strictly worse, the annotation says the process is shutting down, not that this stack is on the shutdown path. Mechanism-tokens-only (ClearOnShutdown|KillClearOnShutdown|StaticRefPtr): A=1 (a hang, via `StaticRefPtr` in an inline list) / B=3, and 0 of the 13 MimeService reports.
> 
> THE 0x1000 BOUND IS NOT n=1: across corpus B the small addresses are {0x0:50, 0x28:13, 0x8:2, 0x10:1, 0x14:1, 0x80:1, 0x470:2} and the next value up is 0x80000 — a two-order-of-magnitude gap. Keep it.
> Incidental: on 413d6058 and b65b3c02 the processed crash's top-level `address` is 0x0 while `crash_info.address` is 0x28 (instruction `mov rbx, qword [r15 + 0x28]`). The pipeline reads crash_info first, so it is right — but SuperSearch exposes the top-level one, so a back-test written against SuperSearch's `address` would measure a different rule.


**Counter-example**

> Against the obvious fix (a non-zero `min_fault_address`, "0x0 means nothing was dereferenced"): a genuine read at EXACTLY 0x0 during shutdown is the majority shape, not an artefact — 50 of the 96 nightly in-shutdown ACCESS_VIOLATION_READ crashes have `crash_info.address == 0x0`, all 50 with a recorded memory access and 0 with a moz_crash_reason. A floor deletes 10 of the row's 25 firings there (40%).
> Verified nightly instance: e23bec95-9350-40c7-80d3-827d20260531 — `MOZ_StripRelativeComponents`, Firefox nightly, `shutdown_progress=xpcom-shutdown`, crash_info.address 0x0, faulting instruction `movzx eax, byte [r9]`, no moz_crash_reason; 10 such nightly reports in 3 months, and the shipped row fires on all 10.
> Verified mechanism-exact instance (the archetype's own mechanism, at address 0x0): 032c9db1-f5c5-49a8-80ba-0c0500260616 — `mozilla::URLQueryStringStripper::ManageObservers`, address 0x0, `mov rax, qword [rcx]`, `shutdown_progress=xpcom-shutdown`, and the stack literally reads `URLQueryStringStripper::Shutdown()` <- the `GetSingleton` lambda <- `mozilla::KillClearOnShutdown(mozilla::ShutdownPhase)` inlined in `AdvanceShutdownPhaseInternal`. A ClearOnShutdown-managed StaticRefPtr singleton crashing at null during its own clear — the exact bug 2062119 mechanism — and a floor eats it. (150 reports/6 months: esr 95, release 54, default 1; the signature has not appeared on nightly, but the code path is channel-independent.)
> Against the second obvious fix ("shrink the alternation to the tokens that name the mechanism"): the motivating crash itself is the counter-example. Neither `ClearOnShutdown` nor `KillClearOnShutdown` nor `StaticRefPtr` appears anywhere in the stack of 413d6058-1cf5-4d04-afc0-994d70260819 or b65b3c02-a1f3-4f4b-89aa-cd7e50260820, nor in any of the 13 nightly `nsJARProtocolHandler::MimeService` reports — the singleton is read through an inlined accessor, so the mechanism is never on the stack. That fix scores 1 firing per 1051 nightly reports and 0 on the crash it was learned from.
> Against a third ("suppress the archetype on anything Socorro calls report_type=hang"): b65b3c02 is report_type=crash but 413d6058's family is filed from both shapes, and bug 2063892's `shutdownhang` at 0x7ffc... is already excluded by the address bound — the hang label is not the discriminator, the abort record is.


**Recommendation**

> Keep the row, keep `max_fault_address: 4096`, and fix the two halves of the context predicate that were never written — using facts the run already holds and already prints to the same prompt (`triage.py:615` MOZ_CRASH_REASON, `:635` Shutdown phase reached), which is jstutte's bug-2065373 principle applied verbatim.
> 1. Add two declarative matcher keys to `models.Archetype.matches` (same shape as `max_fault_address`: missing/unknown must NOT satisfy them) and set them on this row: `require_shutdown_progress: true` (facts gain `shutdown_progress`) and `no_moz_crash_reason: true` (facts gain `moz_crash_reason`). Measured effect: 23 -> 2 on a week of nightly, keeping both in-context MimeService reports, and 25 -> 25 on the 3-month genuine-fault corpus. This also makes the pair fire at most one row — every `shutdownhang` carries "Shutdown hanging at step ...", so the 3 double-fires vanish without touching the `shutdown-hang` row.
> 2. Do NOT add an address floor, and do NOT swap the stack alternation for `shutdown_progress` alone (A=32/B=72, strictly worse). AND them.
> 3. Prune the alternation to the three branches that actually fire (`ShutdownPhase|AppShutdown|AdvanceShutdownPhase`) plus `ClearOnShutdown|KillClearOnShutdown` kept as cheap mechanism evidence. Delete `XPCOMShutdown` (0/1051; the symbol is `ShutdownXPCOM`), `::Teardown`, `UnloadLoaders`, and move nothing to the `signature` key: `AsyncShutdownTimeout|shutdownhang|profile-change-teardown` are signature tokens (0 in stack, 101 in signature) and putting them under `signature` would re-admit precisely the 18 abort firings this fix removes.
> 4. Rewrite the guidance's opening as a conditional with its own check, not an assertion: "IF the faulting instruction shows a base+offset read and MOZ_CRASH_REASON is empty, a small address during shutdown is usually a cleared global ...; if MOZ_CRASH_REASON is set, nothing was dereferenced and this row does not apply." Gate the "the origin will NOT be in this build's pushlog window" steer on having first found a `StaticRefPtr`/`ClearOnShutdown` declaration in searchfox — that sentence is the expensive half, and it currently fires on 21 crashes a week where no pointer was read.
> 5. Replace the back-test at tests/test_feedback_archetypes.py:62: it passes only because it feeds SIGNATURE strings ("AsyncShutdownTimeout | profile-change-teardown | LoginStore::shutdown") into the `stack` field, an input `_matching_archetypes` never builds. Pin real `_stack_text` output from stored processed crashes — 413d6058 (must fire), 79f79bad and bug 2061969's 424b0ab0 (must NOT fire, and must not fire alongside `shutdown-hang`), 61228138 (startup MOZ_Crash, must not fire), e23bec95 (address 0x0, must still fire).
> 6. Two follow-ups this audit surfaced, out of scope here: `_stack_text`'s `_PROLOGUE_PATTERNS` test only `frame["function"]`, so an inlined `MOZ_Crash` at #0 (61228138, c17263ca) is not recognised as abort machinery; and the top-level `address` vs `crash_info.address` disagreement (0x0 vs 0x28 on the archetype's own crash) should be pinned by a test before anyone measures this rule via SuperSearch.


### Rank 6 — `_apply_corroboration_gate` — the only PROMOTING gate in the pipeline, fit on n=1, lands a bare lead on exactly `autofile.min_confidence`

**Verdict: `partially`** · n1-threshold · `crashclouseau/agent/orchestrator.py:892 (offset arm); `_MAX_FIELD_FAULT` at :850`


**Measured**

> THE SUGGESTED MEASUREMENT IS UNRUNNABLE, AND THAT IS ITSELF THE FINDING. `fault_address_offset_match` appears 0 times in `corpus_ship/results_gate_facts.jsonl` — 0/64 positive arm, 0/26 negative arm. It cannot appear: all 1257 `processed_crash.json` fixtures across all 9 corpus dirs carry `json_dump.crash_info == {"crashing_thread": N}` and nothing else — `address` is stripped at fixture-write time, so `_fault_address` returns None on 255/255 distinct crashes. Both strong flags fire 0 times in 90 runs, and 0 of the 47 rung-70/85 verdicts in the calibration bin reached that rung via either flag (all 27 rung-70 leads are model-self-asserted, legal since `Verdict._consistency_rule` schema.py:576 lets a lead claim `probable`). The 0.9714 the shipped table attaches to rung 70 therefore describes only the self-asserted population; the gate-promoted population has never been observed.
> 
> EXPOSURE IS REAL, NOT HYPOTHETICAL. Socorro, Firefox/nightly, date>2026-08-01, 5000 sampled reports: 435 (8.7%) have 0 < address <= 0x1000. Of the 52 Clouseau-filed bugs on BMO (creator=cdenizet@mozilla.com since 2026-07-01, first comment contains "Clouseau"), 6 (11.5%) carry a gate-eligible fault in their `Crash Reason:` block: 0x2 (2062173, INVALID), 0x8 (2063002, DUPLICATE), 0x10 (2063234, NEW), 0x36 x2 (2063678 FIXED / 2063864 DUPLICATE), 0xb0 (2064342, NEW).
> 
> THE COINCIDENCE RATE. Production 8-aligned small faults live on a 13-value alphabet {0x8,0x10,0x18,0x20,0x38,0x40,0x48,0x58,0x68,0x78,0xd0,0xe8,0xf0}. Ran `searchfox-cli --field-layout` on an 18-class panel drawn from the top-100 nightly signatures: the mean class has a field STARTING on 4.3 of those 13 (33%); frequency-weighted P(coincidental match | this class is cited) = mean 34%, median 18% — nsPresContext 93.5%, mozilla::net::DocumentLoadListener 91.4%, mozilla::ipc::MessageChannel 89.2%, nsINode 65.6%, and 0% for nsDocShell / nsGlobalWindowInner / GMPChild.
> 
> THE STRUCTURAL DEFECT. `_corroborations` iterates every citation on the dossier and tests only `cit.offset == fault`. Nothing links the matched field to the CANDIDATE. The match verifies the CRASH ("a null-deref of field X of class T") — which the signature usually already states — and says nothing about the changeset. Its sibling flag in the same function, `prior_signature_match`, does tie to the candidate (`cand.bug in pbugs`) AND carries a focus guard (`len(pbugs) == 1`). One function, one properly scoped corroborator, one not.
> 
> LIVE PROOF. `mozilla::dom::ThreadSafeWorkerRef::Private`: 60 nightly reports since 2026-07-01, 36 at fault 0x8, spread over 9 DISTINCT nightly buildids (20260713202634 ... 20260810093015) = 9 distinct pushlog windows = 9 distinct candidate sets. `ThreadSafeWorkerRef::mRef` is at offset 8. One offset fact promotes all nine candidate sets identically to the filing bar. All-channel since 2026-01-01: 116/237 = 48.9% of that signature's reports are at 0x8 — the offset match is the signature's modal shape, i.e. zero candidate information.
> 
> MISSING FLOOR — a direct internal contradiction. `_fold_second_opinion` (:1229-1242) refuses to boost below `min_boost_confidence = 50`, with the stated reason "a boost would jump two rungs (low -> probable, p_worth 0.50 -> 0.97) ... the corroborate side was never part of the calibration fit". `_apply_corroboration_gate`'s `is_bare_lead` is only `confidence != probable`, so it takes lead/LOW (rung 25, p_worth 0.50) straight to 70 (0.9714). Ship corpus promotable population: 9/90 (7 lead/medium, 2 lead/low).
> 
> TWO THINGS THAT ARE CORRECTLY SCOPED, in fairness. (1) `_MAX_FIELD_FAULT = 0x1000` has a stated principle ("one page cap"), not a number read off the 0x8 case. (2) The predicate looks too broad on crash type — 277/436 (63.5%) of gate-eligible nightly faults are SIGSYS/SYS_SECCOMP, where `address` holds a syscall number, not an address, and the gate has no crash-type check — but exact field-START equality kills them anyway: 8 real non-address small values (0x36 n=251, 0x5, 0x7d, 0x1c, 0x2c, 0x17, 0x1, 0x2) x 18 real class layouts = 144 tests, exactly 1 hit (0.7%). Latent, not active. (3) Real mitigation exists: `_will_corroboration_promote` (:2226) was written precisely so a corroboration-rescued lead still gets the blind second opinion, and a medium-confidence SO refutation clamps it back to medium.
> 
> NOT ENFORCED. Nothing verifies a `struct_layout` citation's (type_name, field, offset) against searchfox. `triage._crash_facts` prints "Fault address: 0xN" into the prompt and roles.py:144-152 instructs the model to emit a matching `struct_layout` citation. The gate docstring's "a signal the model cannot fabricate" is a claim the code does not enforce.


**Counter-example**

> The motivating case itself, and it kills the obvious fix. Bug 2053521 "crash at null [@ ComputeKeyHash]" (VERIFIED/FIXED, `regressed_by: 2053211`, `cf_crash_signature: [@ mozilla::detail::nsTStringLengthStorage<T>::operator unsigned long long | mozilla::detail::nsTStringRepr<T>::Length | mozilla::HashString ]`). `searchfox-cli --field-layout mozilla::detail::nsTStringRepr` confirms `mLength` at offset 8, so the 0x8 corroboration is a true fact.
> 
> The tempting fix is the one that would make the flag corroborate the CANDIDATE instead of the crash: require the matched struct/field to be touched by the candidate's diff. I fetched the regressor: bug 2053211 landed as d86be929745b ("Simplify Pre/PostIdMaybeChange", r=smaug), raw-rev via hg-edge = 373 lines touching exactly three files — `dom/base/Element.cpp`, `dom/base/Element.h`, `dom/html/nsGenericHTMLElement.cpp`. grep of that diff: 0 occurrences of `mLength`, 0 of `nsTString`. The real regressor never goes near the field whose offset corroborated the fault. That fix would suppress the one Bugzilla-verified case the gate exists for.
> 
> Second, weaker naive fix to avoid: "require the fault to be 8-aligned" (it would drop 63.5% seccomp noise cheaply). nsINode really does place `mSelectorFlags` at 64 and `mChildCount` at 68 = 0x44 — a 4-aligned uint32 member deref is a real shape, and production shows live 4-aligned small faults (0x1c, 0x2c). The alignment rule is fitting to x86-64 pointer members, which is the same class of error.


**Recommendation**

> Do not delete the gate and do not add a candidate-diff link (verified above: it eats bug 2053211). Three changes, cheapest first.
> 
> 1. ADD THE FLOOR ITS SIBLING ALREADY HAS (one line, no measurement debt). Change `is_bare_lead` at orchestrator.py:909 to also require `rung >= config.get_agent_second_opinion()["min_boost_confidence"]` (50), the same guard `_fold_second_opinion` applies with the same written justification ("the corroborate side was never part of the calibration fit"). This closes the deterministic 25 -> 70 two-rung jump into the autofile bar. Ship-corpus cost: 2 of 90 verdicts. Leaving the two promoters inconsistent is indefensible on its own terms.
> 
> 2. GATE ON DISCRIMINATIVENESS, WHICH IS THE CONTEXT THE RULE WAS ACTUALLY EARNED IN. The motivating case's offset was informative because 0x8 is not what that signature normally does (its `nsTStringRepr::Length|HashString` signature is 86.7% 0x0, mode 0x0); `ThreadSafeWorkerRef::Private` is 48.9% 0x8, so there the match is the signature restating itself. Before promoting, facet Socorro on `address` for the crash's own signature (one free unauthenticated SuperSearch call, the same shape `sigage` already makes) and suppress the promotion when the matched offset is the signature's modal fault above some share fit on a panel — do NOT read the threshold off these two points; both are stated here as the panel's first two members, not as the fit.
> 
> 3. MAKE THE "MODEL CANNOT FABRICATE" CLAIM TRUE, AND MAKE THE GATE MEASURABLE. (a) At gate time, re-run `field_layout(cit.type_name)` and confirm `cit.field` really starts at `cit.offset` before setting the flag — the model is shown the target number in `_crash_facts` and told to produce a matching citation, so today the flag is model-reported, not deterministic. (b) Stop stripping `crash_info.address` (and `type`/`instruction`) from the corpus fixtures. Until the fixture writer keeps them, no back-test of this gate is possible in either arm and the next session will re-derive the same 0-vs-0 non-answer. Fix (b) first if only one thing ships, because it is what converts every other claim here into something the repo can re-measure.


---

## 5. Remaining worklist — 17 items, ranked, not yet measured

Each carries the question to answer and the panel to answer it with. Work top-down; the ranking is
(probability the rule is actually overfit) x (damage when it misfires), where damage means a real
bug not filed, a wrong bug filed, or a model steered to a wrong mechanism.


### Rank 7 — `shutdown-hang` guidance: "expect NO regressor ... a good verdict"

`crashclouseau/archetypes.py:107` · **scope-exceeds-evidence**


**Evidence so far.** Generalised from ONE INVALID (bug 2064436, Pehrson) with no counter-example named, on 2026-08-19. On the same panel, already in the filing record when the row was written: bug 2063892 (`shutdownhang | RtlpWaitOnAddressWithTimeout`, filed 08-16) — Clouseau named bug 2058982 at 97%, dmeehan@mozilla.com set `regressed_by: 2058982` on 08-20, abienner@mozilla.com took it ASSIGNED and attached a patch; and bug 2061969 (`shutdownhang | __fstatat`) — dmeehan set `regressed_by: 1998600` on 08-17. So 2 of the product's 3 shutdown hangs got a human-confirmed regressor, and the clearest confirmed-correct attribution in the whole product is a shutdown hang WITH one. The damage channel is a false ABSTAIN, which skips the second opinion (orchestrator.py:2268), files no bug, and never reaches `Feedback` — invisible to every measurement the repo has.


**Question.** Whether the clause should be inverted, graded, or deleted, keeping the row's genuinely good clauses (the investigation ordering, the two cross-process caveats).


**How to measure.** The three-filing panel is already adjudicated — re-pull `regressed_by` history from BMO for 2064436 / 2063892 / 2061969. Extend it cheaply: query BMO for every bug since 2025-08 whose summary matches `Crash in [@ shutdownhang |` in Core/Toolkit and count how many carry a human-set `regressed_by`. Counter-example the corrected text must NOT eat: bug 2064436 itself — inventing a MediaTrackGraph mechanism there really was wrong, so the replacement must still discourage manufacturing a regressor without ASSERTING there is none.


### Rank 8 — `broken_cpu_rate >= 0.7` computed against a ONE-element `BROKEN_CPUS`, on a facet that already holds the answer to the real question

`crashclouseau/sigage.py:340 (list), :451 (`_sum(keep=...)`), :354 (facet fetch); gate at crashclouseau/agent/orchestrator.py:1861` · **miscalibrated-context-predicate**


**Evidence so far.** The audit brief's own calibration case, confirmed in source: `hardware_noise` fetches the FULL `cpu_info` facet at `_facets_size: 200` and then sums only the rows equal to one hard-coded Raptor Lake stepping, discarding exactly the rows that would answer 'is this signature's hardware population degenerate'. The shape jstutte found on bug 2065373 (55/58 reports on ONE ORDINARY cpu_info plus one kernel string) scores `broken_cpu_rate = 0.0` and passes. 0.7 is mozilla/bugbot's number fitted on bugbot's denominator (all-channel, few weeks); ours is deliberately different (own product + channel, 364d) and the rate was never re-fit — and the only corpus case that exercises the arm clears it by ONE point (`js::jit::CompilerFrameInfo::sync`, 71% vs a 70% line). Background is 4.1% (`POPULATION_BROKEN_CPU_RATE`), so the CPU arm demands a 17x lift while the flip arm demands 8x, an asymmetry inherited rather than measured. `population.summarize` (population.py:116-119) ALREADY computes the degenerate-population statistic and 64907e1 shipped it deliberately reporting-only.


**Question.** Whether the right statistic is 'share on a known-bad CPU' or 'top cpu_info share / facet concentration' over the rows already fetched, and where the concentration threshold sits.


**How to measure.** `sigage.hardware_noise` already returns the full facet — re-run it over the signatures of the 51 filings plus the 18 FIXED/DUPLICATE/ASSIGNED controls that the 31b5f3b panel used, and compute top-cpu-share alongside `broken_cpu_rate`. The distribution prior is already measured once: `population.concentrated_share` was fit at 0.5 from median 0.18 / p75 0.47 over 59 loudest nightly signatures (config.py:180-186) — reuse it rather than re-deriving. Counter-examples the fix must NOT eat: bug 2062219 (`nsAtom::IsStatic`), RESOLVED FIXED, which the all-channel denominator already kills and the nightly one spares; and all 18 of the FIXED/DUPLICATE/ASSIGNED filings the current rule spares 18/18. Any concentration rule that suppresses one of those 18 is worse than what ships.


### Rank 9 — The compiled-out gate publishes "the mechanism rests on `{symbol}`" for a symbol its predicate never showed the mechanism rests on, judges the body at TIP against a switch read at the build rev, and has no kill switch

`crashclouseau/agent/orchestrator.py:2107-2113 (reason string); symbol source at crashclouseau/compiled_out.py:253; moz.configure read at compiled_out.py:186` · **scope-exceeds-evidence**


**Evidence so far.** Read in source this session. `mechanism_symbols` draws HALF its input from the top-8 identifiers by OCCURRENCE COUNT IN THE CANDIDATE'S DIFF — and the gate's own docstring records that on its motivating case (bug 2063782) the single citation is ordinary always-compiled code and the hollow symbol was reachable ONLY through the diff. So on the very case it was built for, the published sentence is false as written; the true finding is 'the candidate's changeset is mostly about a compiled-out subsystem'. That is exactly the bug-2065373 shape the whole audit is about — a claim the run's own data does not support. Second, `hollow_symbols` reads the symbol body from searchfox (TIP-only) while `_configure_text` reads `moz.configure` at `rev or 'tip'` = the build rev now that `pin_rev` resolves (2d71e11, landed the SAME DAY as the gate), so two clocks judge one question. Third, the 56-filing / 274-symbol back-test contains exactly ONE distinct hollow symbol, so '0 false positives on 54' measures RARITY, not precision. Fourth, verified by grep: it is the only suppression in `apply_deterministic_gates` with no `_env_bool` kill switch (signature_age / bit_flip / bad_machine have them at config.py:568 / 625 / 677).


**Question.** Whether the reason string must be split by symbol PROVENANCE (mechanism-cited vs diff-derived), and whether a diff-derived hollow symbol should suppress at all or only clamp.


**How to measure.** The back-test is re-runnable: replay `compiled_out.mechanism_symbols` over all 56 filings and record, per hollow hit, whether the symbol arrived from a mechanism citation or from the diff top-8 — that alone decides the wording fix. Then hold the clock: re-run `hollow_symbols` with the BUILD rev for both firing filings and confirm the verdict is stable against the TIP answer. Counter-examples the fix must NOT eat: bugs 2063782 and 2063902 must still be suppressed (both owner-refuted for exactly this reason), and the 16 filings a human FIXED or duplicated must stay unsuppressed. Bug 2062114 is documented as not reachable this way (compiled_out.py:35-38) and must not be counted as a regression if it stays uncaught.


### Rank 10 — `config/interesting_extensions.json` — the file-type inclusion list that decides what the on-stack scorer can see at all

`config/interesting_extensions.json; predicate crashclouseau/utils.py:120; applied crashclouseau/pushlog.py:62, :81, :101` · **ungated-context-rule**


**Evidence so far.** Read this session: c/h/H, cpp/cc/cxx/hh/hpp/hxx, java, rs, mm/m and nothing else. No provenance anywhere — no docstring on `get_extensions` or `is_interesting_file` beyond a restatement, and it predates the agent. Only changesets touching these extensions get `Changeset` rows, so only they can score onto a stack frame, and a crash with no scored changeset is marked `useless` and never reaches the agent. It structurally excludes `modules/libpref/init/*/all.js` and `StaticPrefList.yaml` — so the canonical off-stack archetype (bug 2056116, mccr8's pref flip) can NEVER become a scored candidate, which is part of why the off-stack path exists; `moz.build` and `*.configure` — the build-config changes the compiled-out gate exists to reason about; `.idl`/`.webidl`; `.js`/`.mjs`; and `.kt`, the Fenix blocker in plans/16. The off-stack path bypasses it (`file_filter=lambda f: True`, orchestrator.py:298) but off-stack is `enabled: false`.


**Question.** How many window changesets are invisible, how many human-adjudicated regressors are among them, and whether adding the pref/build/IDL families improves or floods the on-stack top-20.


**How to measure.** Cheap first cut: for the 12 filings with a human-set `regressed_by`, check whether that bug's landing touched ANY interesting-extension file — a regressor with zero interesting files is a case the scorer structurally cannot have found. Full read: re-walk the pushlog window of each of the 51 filings with `file_filter` unrestricted and count (a) window changesets with zero interesting files, (b) how many are the adjudicated regressor. Counter-example the fix must NOT eat: on-stack PRECISION. Admitting `.js` re-admits every test file and l10n bump, so the measurement must report how the top-20 on-stack candidate list moves for the 12 confirmed-regressor filings, not just the recall gain — a recall-only report is not a decision.


### Rank 11 — `_bug_for_this_regression` fails OPEN on an unknown landing date, its age test is one-sided, and `_is_specific_signature` admits generic piped signatures with no `[meta]` exclusion anywhere

`crashclouseau/bugzilla_apply.py:505 (age test), :450 (function), :402-406 (`_candidate_landed`), :300 (`_is_specific_signature`)` · **scope-exceeds-evidence**


**Evidence so far.** Both branches were run directly against the shipped functions by a prior session. With `landed=None` the function returns the OLDEST open bug with no age test at all — restoring exactly the pre-162d0a5 behaviour that commit fixed — and `_candidate_landed` returns None on ANY hg failure, which is not hypothetical: 2d71e11 records that pinned hg reads had been resolving nothing. The age test is also one-sided: a bug created 2031-01-01 was accepted as the venue for a candidate that landed 2026-08-07. `_is_specific_signature` scores `IPCError-browser | ShutDownKill` as specific (len 31, contains `|`), and its live BMO summary search returns bug 1279293, a 2016 `[meta] Crash in [@ IPCError-browser | ShutDownKill]`, as the oldest open match — and grep confirms nothing anywhere excludes `[meta]` bugs. The 16-char threshold sits between one negative (`memcpy`, 6) and one positive (`mozilla::MediaDecoder::SetCDMProxy`, 34) with no panel; 16 was not even read off either.


**Question.** The prod rate of `_candidate_landed` returning None, and whether the unknown-landing branch should refuse to comment (fail closed) rather than take the oldest bug.


**How to measure.** Replay `_candidate_landed` over the candidate nodes of the 51 filings and count Nones — that is the exposure. Then replay `_bug_for_this_regression` for each filing against the real BMO bug list, once with the known landing date and once forced to None, and diff the chosen venue. Counter-examples the fix must NOT eat: bug 1990812 (14019f0's case — signature only in the SUMMARY, correctly found and commented) and the '-9 days' correct comment that 162d0a5 preserved; a fail-closed unknown-landing branch must not turn either into a new duplicate bug, so pair the change with an explicit 'file new' path rather than a silent skip.


### Rank 12 — `_open_bugs_for_signature` restricts to `resolution="---"`, so the ONLY duplicate check cannot see a bug resolved hours ago

`crashclouseau/bugzilla_apply.py:338 (filter), function at :311` · **miscalibrated-context-predicate**


**Evidence so far.** `resolution=---` correctly identifies 'a bug I can usefully comment on', which is the question the function was written for — but the same call is asked a second question it was not written for ('has this already been reported'), and there 'already fixed' is exactly the case where a duplicate is worst. Measured instance: bug 2063003 was filed 2026-08-12T15:19 as a duplicate of bug 2062219, RESOLVED FIXED 11h16m earlier. 7 of 52 filings (13.5%) resolved DUPLICATE. plans/17-dedup-beyond-the-signature.md carries the analysis, and Calixte's stated rule ('a dup MUST add its signature to the target's `cf_crash_signature`') requires this filter to be lifted for the has-this-been-reported question.


**Question.** How many of the 7 DUPLICATE filings would have been caught by a resolution-agnostic lookup, and whether lifting the filter for the dedup question (while keeping it for the venue question) creates wrong comment venues.


**How to measure.** For each of the 7 DUPLICATE filings, re-run `_open_bugs_for_signature` with and without the resolution filter against BMO history as of the filing timestamp (BMO exposes `last_change_time`, so an as-of read is possible) and count how many surface the dup target. Counter-example the fix must NOT eat: a long-closed WONTFIX/INVALID bug on the same signature must not become a comment VENUE — the fix is two different queries for the two different questions, and the measurement must confirm the venue query is unchanged on all 51 filings.


### Rank 13 — `_OTHER_APP_PRODUCTS` — a closed-set claim about an open set; Fenix and Focus are classified as ours

`crashclouseau/config.py:155-158 (list + the completeness claim); applied crashclouseau/bugzilla_apply.py:359, crashclouseau/report_bug.py:828; a second hand-written copy at crashclouseau/agent/tools/bugzilla.py:94` · **miscalibrated-context-predicate**


**Evidence so far.** The docstring claims the list is 'complete in the only sense that matters: these are the only non-Firefox products whose application reports crashes to Socorro at all'. Live Socorro contradicts it — the last-week product facet is Firefox 273,803 / Fenix 107,644 / Thunderbird 54,552 / Focus 1,383 / ReferenceBrowser 20 — and BMO's `/rest/product?names=Focus` returns `is_active: true`. So a Focus bug is an accepted comment venue for a desktop Firefox crash while SeaMonkey, which IS in the list, reports nothing at all. Fenix is deliberately on the Firefox side and pinned there by tests/test_autofile.py:359. The rule's stated context is 'another application built on Gecko'; Fenix and Focus are exactly that. Dormant while `products: ["Firefox"]`, and it arms the day plans/16-fenix-nightly-support.md lands. `agent/tools/bugzilla.py:94` repeats the list in prose without deriving it from config, so the two will silently disagree the first time one is corrected.


**Question.** The right predicate for 'a different application's crash population' — probably a Socorro-product-to-BMO-product map derived from the live product facet rather than a hand-maintained exclusion list — and whether the agent-facing prose can be generated from config instead of duplicated.


**How to measure.** Re-run the two live queries (Socorro `_facets=product`, BMO `/rest/product`) to get today's application set; then, for each of the 51 filings, re-run `_split_by_application` against the venue candidates with the corrected map and diff the chosen venue. Counter-example the fix must NOT eat: report 05381864 -> bug 2057980 (`MailNews Core :: Networking: Exchange`) must still be excluded, and the Firefox-side bug in tests/test_autofile.py:359 must still be ACCEPTED — a map keyed on Gecko-app-ness without a Firefox-family notion would wrongly exclude Core/Toolkit bugs and stop us filing at all.


### Rank 14 — `_apply_absent_thread_gate`'s own unmeasured properties: fire rate, false-fire rate, `_MIN_THREAD_NAME`=4, `_TRUNCATED_PREFIX`=6, and a squashed-vs-raw completeness mismatch

`crashclouseau/agent/orchestrator.py:1621; thresholds at :1551 and :1554; `_process_thread_names` at :1575` · **n1-threshold**


**Evidence so far.** This is the one part of 5f169b6 with no panel — the commit's 40-hang sample measures the THREAD-SELECTION fix (`inspector.thread_for_analysis`), not this clamp. Nobody has measured how often a verdict quotes a thread name, nor how often the quote is legitimately another process's; the docstring names that false-fire mode and explicitly declines to detect it. A false fire costs one rung, and 70 -> 50 crosses `autofile.min_confidence`, so it silently kills an automatic filing. 4 and 6 are unattributed; 6 is one SHORT of the motivating string's 7-char head (`Shutdow~minator`), so it admits a looser match than the case requires. Separately, `_process_thread_names` computes `complete` over SQUASHED (lowercased alnum) names while `triage._thread_inventory` computes it over RAW distinct names, so above 120 names the gate can believe the list complete while the prompt told the agent it was truncated — the docstring's claim that they use 'the SAME ceiling ... so the gate and the agent cannot disagree' does not hold in that corner.


**Question.** Fire rate and false-fire rate, and whether a clamp is the right action at all once the unnamed-thread problem (rank 2) is fixed.


**How to measure.** `absent_named_threads` is written to `corroborations`, so the fire rate is directly countable over the persisted dossiers — do that first; if it is near zero the item closes cheaply. For false fires, take the fired rows and check by hand whether the quoted name belongs to a CONTENT process, using Socorro's `process_type` plus sibling reports of the same signature. For the ceiling mismatch, run both functions over the 33 truncated crashes already identified in the 840-report sample. Counter-example the fix must NOT eat: bug 2064436's quoted 'MediaTrackGrph' — that clamp was correct and must survive any tightening.


### Rank 15 — Exposer classifier still applies the pre-pivot goal to every crash, and `_POISON_BYTES` carries two bytes the comment does not name

`crashclouseau/agent/orchestrator.py:1031 (classifier), :1007 (byte set), :1010 (`_looks_poison`)` · **ungated-context-rule**


**Evidence so far.** Verified in source this session: the comment enumerates TEN bytes (jemalloc 0xe5 / 0xe4 / 0x5a, MOZ 0xdd, MSVC 0xcd / 0xcc / 0xfd / 0xab, ASan 0xbe / 0xfb) and the frozenset holds TWELVE — 0xA5 and 0x2B appear with no provenance anywhere in the repo. The dominance rule is `count(top) >= max(2, len(parts)-1)`, so a two-byte address 0x2b2b qualifies and can demote a strong-evidence verdict on a byte nobody wrote a reason for (`_looks_poison(0x2b2b)`, `(0xabab)`, `(0xa5a5)` all verified True by execution). Separately, `_classify_exposer` was built for OFF-STACK (c89dc9b, 2026-07-22 13:02) and extended to ALL crashes (cda321e, 15:12) citing spike/STRATEGY_REPORT.md — whose own text splits the finding BY GOAL at lines 143-144: 'Goal = nominate `regressed_by` / needinfo the author -> the exposer IS the right answer' vs 'Goal = localize the defect/fix -> the exposer is wrong ~30% of the time'. The goal pivoted to the FIRST of those two ~7 hours later (b9485c3, 22:12) and the downgrade was never revisited. Only tests/test_offstack.py:118-134 touches it, pinning `_looks_poison` arithmetic.


**Question.** Whether a strong -> lead downgrade on a poison fault is still correct under a triage-worthiness goal (STRATEGY_REPORT.md already answers this as a policy question), and whether 0xA5 / 0x2B belong in the set at all.


**How to measure.** Settle the policy question from spike/STRATEGY_REPORT.md:143-144 FIRST — it is already measured and needs no new panel. Then size the byte question: replay `_looks_poison` over the fault addresses of all `corpus_ship` cases and the 51 filings, split by which byte dominated, to see how many downgrades 0xA5 and 0x2B alone are responsible for. Counter-example the fix must NOT eat: the study's ~30% exposer-not-cause finding is real, so removing the downgrade must be paired with the off-stack prompt text that already says 'prefer a lead + soft needinfo over accusing it' — the softening must survive even if the rung clamp does not.


### Rank 16 — `bad_machine.min_span_seconds = 1800` — an n=1 number whose only cited evidence is a case the gate's OTHER conjunct already spares

`crashclouseau/config.py:659 (value + docstring); third conjunct at crashclouseau/agent/orchestrator.py:1738` · **n1-threshold**


**Evidence so far.** Justified by exactly one case: bug 2047016 (RESOLVED FIXED), whose first crash came from a machine that emitted 5 distinct signatures in 22 minutes. 22 min -> 30 min is the value read off that one case, with no distribution of spans, no sweep, and no counter-example at the boundary. And the cited counter-example is NON-BINDING: 2047016's machine had 5 distinct signatures while the FIRST conjunct already requires >= 10, so the diversity test alone spares it — the span threshold's only stated evidence is a case it never had to catch. It sits inside a gate whose other two conjuncts are exemplary (min_signatures 10 measured over 141k nightly crashes with a split-half and both neighbours rejected; max_cpu_infos 1 validated as a mechanism test with the null arm reported at p=0.77). The error direction is a wrong bug FILED: a lower span means the gate fires more, so 1800 means bad machines that scatter fast are not suppressed.


**Question.** The actual distribution of `span_seconds` among installations that clear the >= 10 distinct-signatures and <= 1 cpu_info conjuncts, and whether the conjunct earns its place at all.


**How to measure.** `machine.install_history` is causal (bounded at the triaged crash's `date_processed` via `before=`) and cheap — replay it over the same 141k-crash nightly population the min_signatures study used, restrict to installs clearing the other two conjuncts, and histogram the span. Then re-run the gate's outcome variable ('later reproduced on DIFFERENT hardware') at 0 / 600 / 1800 / 3600 s. Counter-example the fix must NOT eat: bug 2047016 must still be spared — but the measurement must report WHICH conjunct spares it, because if the diversity test alone does the work, the span conjunct should be deleted rather than re-fit. NOTE the panel trap: the 141k study has no committed artifact (grep for 141k / 11735 / 17.96 under spike/ returns nothing), so the population must be rebuilt from Socorro before any of it can be reproduced.


### Rank 17 — `_short_value(limit=300)` head-truncates the one field whose own label says the ANSWER is at the end

`crashclouseau/agent/triage.py:118 (truncation), applied :640-643; the contradicting label at :637-639` · **miscalibrated-context-predicate**


**Evidence so far.** The prompt line calls `xpcom_spin_event_loop_stack` 'innermost last — this NAMES the stuck subsystem; treat it as the primary lead for a shutdown hang', and `_short_value` keeps the HEAD and drops the TAIL. Reproduced by a prior session: a 348-char spin stack ending in 'INNERMOST: QuotaManager::Observer::Observe' renders as '... Pool04, default: nsThreadPool::ShutdownWithTimeout...' — the named subsystem is exactly what is cut. The field is present on 6 of 9 sampled diverging hangs, each naming a different subsystem. The same unconditional cap also truncates the PHC alloc/free stacks and `async_shutdown_timeout`. The failure is CONDITIONAL on the field exceeding 300 chars in the wild, which could not be measured offline — that is the whole audit task here.


**Question.** The real length distribution of `xpcom_spin_event_loop_stack`, `async_shutdown_timeout` and the PHC stacks on nightly hangs; then whether the fix is a tail-preserving truncation for those three fields or a raised cap.


**How to measure.** One live pass: pull ProcessedCrash for the `shutdownhang | *` (31) and `AsyncShutdownTimeout | *` (55) reports already identified in the 840-report sample and histogram `len(str(field))` for each of the three fields. If the p50 is under 300 the item closes; if not, the fix is directional. Counter-example the fix must NOT eat: the reason the cap exists — the prompt budget. `triage._user_prompt` renders at ~7,100 chars today, so if these fields run to kilobytes the answer is a per-field tail-preserving truncation, not a global raise, and the measurement must report the added prompt bytes.


### Rank 18 — `_apply_backout_gate` abstains on `backedoutby` being set — which stays set forever, so a RELANDED patch is suppressed, and the abstain permanently closes the cluster

`crashclouseau/agent/orchestrator.py:1480; cluster behaviour via crashclouseau/models.py:34 and :1349` · **miscalibrated-context-predicate**


**Evidence so far.** The commit says it in writing: 'A backed-out-then-RELANDED change is still suppressed. `backedoutby` stays set forever and hg exposes no "relanded as" pointer.' A relanded patch IS in the tree and IS actionable, so it fails the property while passing the predicate. The reland rate among suppressed candidates has never been measured; the outcome is an outright abstain (not a clamp); and because `candidate_backout_suppressed` is deliberately absent from `_INSTANCE_SUPPRESSED`, it permanently closes the proto-signature cluster — the `ea4f87f` failure shape, a loss with no record anywhere. Recall precision is strong (19 of 20 flagged nodes hg-confirmed) and the timing-agnosticism is separately argued, which is why this is mid-list rather than top-5.


**Question.** What share of suppressed candidates were relanded, and whether a cheap reland detector exists (a later m-c changeset with the same bug id whose description is not a backout).


**How to measure.** Recover the suppressed nodes from `corroborations` on the persisted dossiers (or rebuild from the acd0d85 canary read: 1501 dossiers / 847 reported verdicts, 19 of 20 flagged nodes confirmed). For each, search the m-c pushlog forward 30 days for a changeset carrying the same bug number whose description does not parse as a backout via `pushlog.is_backed_out`. Counter-example the fix must NOT eat: crash dcfc4da0-7015-4845-8494-ec3380260729 / node 507de5c66b0d — genuinely backed out and not relanded, and must still abstain. Second guard: whatever the reland rate, the CLUSTER-CLOSING half is separable and cheaper to fix (rank 19) — measure them independently so one does not block the other.


### Rank 19 — `_INSTANCE_SUPPRESSED` names three of eight suppressions; the three omitted ones are all findings about the CANDIDATE THIS RUN CHOSE, yet they close the cluster forever

`crashclouseau/models.py:34; missing flags at crashclouseau/agent/orchestrator.py:2124, :2192, :1305; flagless branch at :968` · **scope-exceeds-evidence**


**Evidence so far.** The list's own stated criterion is 'specific to ONE CRASH REPORT ... rather than to the signature OR THE CANDIDATE'. `compiled_out_suppressed`, `candidate_backout_suppressed` and `second_opinion_abstained` are all findings about the candidate THIS run happened to choose — a different run on the next crash in the same cluster could choose a different candidate — and none is in the list, with no discussion anywhere. The stated justification for excluding the backout family ('equally true for every crash in the cluster') is true of the CHANGESET but not of the CONCLUSION. The anchorless `abstain` branch of `_downgrade_to_lead_or_abstain` sets no flag at all. Per `ea4f87f`, a wrongly-closed cluster is a permanent loss with no record: that measurement found 107 of 2178 done dossiers unusable, having suppressed 15 later crashes across 11 clusters, 5 with a real on-stack score.


**Question.** Whether the candidate list actually differs across proto-clusters of one (signature, buildid) — if it does not, the omissions are harmless and the item closes.


**How to measure.** Reuse the `ea4f87f` query shape against prod: count `done` dossiers carrying each of the three flags, then count how many LATER crashes in those clusters were closed by `UUID.proto_already_analyzed`. Independently, for a handful of multi-cluster signatures, compare the seeded candidate lists across clusters — identical lists mean the conclusion really is cluster-wide. Counter-example the fix must NOT eat: `hardware_noise_signature_suppressed` must STAY OUT of the list — that finding genuinely is true of every report in the cluster and re-deriving it costs ~$3 with no possible different answer (models.py:29-33 argues this correctly).


### Rank 20 — `_BOT_MARKERS` is not applied to the one action its docstring says it exists to prevent, and its substrings are unanchored

`crashclouseau/agent/experts.py:17 (list), :23 (`_is_bot`); call sites experts.py:47 and crashclouseau/agent/orchestrator.py:420` · **ungated-context-rule**


**Evidence so far.** Verified by grep this session: `_is_bot` has exactly TWO call sites and neither is on the filing path. `report_bug._needinfo_person` (report_bug.py:1173) and `_needinfo_account` never consult it, so a bot-authored candidate IS needinfo'd on an unattended filing whenever its address resolves to a BMO account — 'an automated / non-human committer we should never needinfo' is the docstring's own words. The hazard is documented in the same file that does not guard against it: `_match_author` (report_bug.py:1085) rejects a fourth matching key precisely because '10 of the 17 it adds are `moz-wptsync-bot`, which we must never ask to investigate a crash'. Matching is also unanchored substring over `email + name + nick`: `bot@` matches `abbot@` / `talbot@`, `release+` and `cron@` likewise, and the set is open (update-bot@, wpt-sync, pontoon, sheriff/treescript bots absent). A wrongly-excluded expert leaves no trace anywhere.


**Question.** Whether any filing has actually needinfo'd a bot, and the false-exclusion rate of the substring match against real m-c committer addresses.


**How to measure.** Direct and cheap: for the 51 filings, read the needinfo requestee off the BMO bug and test it against the marker list plus a hand list of known automation accounts. For false exclusions, run `_is_bot` over the distinct author emails appearing in the pushlog windows of those 51 builds and inspect every match. Counter-examples the fix must NOT eat: `ffxbld`, `l10n-bumper` and `moz-wptsync-bot` must stay excluded from the expert list; anchoring `bot@` to `^bot@` / `@bot.` must not lose a real automation address that embeds the token mid-string, so verify the anchored form against the same email corpus before shipping it.


### Rank 21 — The two selection filters that leave NO trace: `thresholds.protos` silent cluster truncation, and `inspect_stacktrace` discarding the whole stack on any frame/build revision mismatch

`config/global.json:33 (applied crashclouseau/datacollector.py:270, :328); crashclouseau/inspector.py:235` · **ungated-context-rule**


**Evidence so far.** `protos` = 50 nightly / 20 beta / 20 release, with no docstring and no commit reason found, and the truncation follows Socorro's COUNT-ordered facet so the quietest clusters are the ones dropped — `if len(protos) < threshold` just stops appending, with nothing written to `models.Selection`, unlike the spike near-misses that 5278410 instrumented. `inspect_stacktrace` was read this session and returns `([], set())` — no frames, no crash stack row, `useless=True` — if ANY single frame's resolved source revision differs from the build node. Its stated context is 'the crash occurred during an update', but the predicate is a bare inequality applied to every report, it leaves no Selection row, no dossier and no log line, and it now runs downstream of git2hg (inspector.py:34) whose partial resolution changes which frames it even compares.


**Question.** How often each fires — currently unanswerable, which is the finding.


**How to measure.** Both need instrumentation before they can be audited: add a `Selection`-style row (or at minimum a counted log line) at datacollector.py:258 and inspector.py:235, deploy, and read a week against the ~85-120 dossiers/day baseline. A prod-free pre-check exists for the second: over the 840-report nightly sample, resolve each frame's node with `get_path_node` and count reports with at least one mismatch — that gives the drop rate without touching prod. Counter-example the fix must NOT eat: a genuinely cross-update stack, whose line numbers really ARE meaningless. The right shape is to drop the MISMATCHED frames (or mark them unscoreable) rather than the report, and the measurement must confirm that does not reintroduce blame on a stale line — check it against the known-good scored candidates of the 12 confirmed-regressor filings.


### Rank 22 — The feedback loop cannot see the failure mode most of these rules cause: `refresh()` is unscheduled, `by_archetype` is denominated on FILED bugs, `classify` has no DUPLICATE state, and an abstain reaches nothing

`bin/schedule.py:13 (three jobs, none is feedback); crashclouseau/models.py:2943 (`by_archetype`), :2890 (`classify`); crashclouseau/agent/orchestrator.py:2268 (SO skipped on abstain)` · **scope-exceeds-evidence**


**Evidence so far.** Verified this session: bin/schedule.py has exactly three jobs (`update_all` 20 min, `reap_stale_agent_jobs` 15 min, `sweep_untriaged_crashes` 6 h) and no feedback entry, though `feedback.refresh()`'s docstring says 'safe to run on a schedule' — it is reachable only from the manual `bin/feedback.py`. `by_archetype` tallies FILED bugs, so a row that fires on 19 of 840 analysed crashes and 0 of the 25 filings since it went live scores nothing at all; both shipped archetypes end by licensing an abstain, and an abstain skips the SO, files no bug, and never reaches `Feedback`. `classify` has no DUPLICATE state though 7 of 52 filings (13.5%) resolved DUPLICATE, and three of those carry a `regressed_by` (2063002 -> 2043000, 2063864 -> 2045970, 2064537 -> 2059597) that will be scored as an adjudication of OUR attribution rather than the dup target's.


**Question.** Whether the loop can be made to see abstains at all, and whether `classify` needs a `duplicate` state before any of the outcome-based measurements above are trustworthy.


**How to measure.** This item is a PREREQUISITE, not a measurement: schedule `feedback.refresh()`, add the DUPLICATE state, and add an archetype-firing counter denominated on ANALYSED dossiers rather than filed bugs. Verify against known ground truth before trusting it: the 7 DUPLICATE filings must not be scored `correct`/`wrong` on our attribution, and the two archetypes must show non-zero firings once the denominator is analysed crashes (19/840 and 31/840 measured). Counter-example the change must NOT eat: the `unconfirmed` state — since 6ae24a1 the filer writes `regressed_by` itself, so a value echoed back must keep scoring `unconfirmed`, not `correct`; a DUPLICATE state added carelessly can reintroduce exactly that self-confirmation bug.


### Rank 23 — `filters.penalty = 0.1` plus three hand-curated noise lists — described as 'down-rank, never drop', but the experts path drops the author outright

`config/global.json:156-159; crashclouseau/config.py:442; applied crashclouseau/agent/orchestrator.py:521-533 and crashclouseau/agent/experts.py:38` · **ungated-context-rule**


**Evidence so far.** The docstring justifies the CATEGORY ('a break there would crash all of Firefox, not one signature') but names no case, no back-test, no count of how many candidates the lists move, and no reason for 0.1 over 0.5 or 0.01. The lists (16 ubiquitous symbols, 8 paths, 13 anchor-frame regexes) came from #15 phase 3 (73a473b) with no motivating bug. Its own promise is contradicted one file over: `experts.py:38` reads `if c.get("noise") or c.get("backedout"): continue`, so a noise-tagged candidate silently yields no area expert to needinfo — a drop, not a down-rank, and one that leaves no trace. Note `mozilla::ipc::MessageChannel::` sits on the anchor list, which is a real subsystem for IPC crashes and not only a bottom-of-stack anchor.


**Question.** How many candidates the lists actually move, whether 0.1 ever changes the top-20 ordering in a way that matters, and whether the experts drop should be a de-prioritisation instead.


**How to measure.** Replay `build_seed` over the 51 filings with the penalty at 1.0 / 0.5 / 0.1 / 0.01 and diff the top-20 on-stack candidate list and the chosen candidate; the seed is deterministic given the pushlog, so no LLM spend is needed. Separately count how many of the 51 filings had zero area experts and whether a noise tag caused it. Counter-example the fix must NOT eat: the 'main() problem' the anchor list exists for — a change to `nsThread::ThreadFunc` or the event loop must not rank above a specific crashing-area candidate, so report the ordering for the 12 confirmed-regressor filings, where the right answer is known.


---

## 6. Cleared — 33 rules audited and found correctly scoped

**Do not re-audit these.** Each was checked against the four-part bar in §1 and passes. They are
also the style guide: when you fix something in §4 or §5, make it look like one of these.


1. `_apply_bad_machine_gate` min_signatures=10 and max_cpu_infos=1 (config.py:678-679) — the strongest panel in the repo: 141k nightly crashes, 11,735 single-machine signatures, outcome = later reproduced on DIFFERENT hardware, -7.0pp z=-4.4 holding across a split-half, both neighbours rejected (5 gives -1.8pp, 15 flips sign), the rival predicate (crash COUNT) measured dead at every threshold, the CPU conjunct validated as a MECHANISM test with the null arm reported (+1.0pp, p=0.77 where the id does not resolve to one CPU), and one candidate signal (uptime, AUC 0.497) measured dead and deliberately omitted. Only min_span_seconds is unfit — see worklist rank 16.


2. `signature_age.min_age_days = 7` (config.py:569, gate orchestrator.py:1327) — the model of a correctly-scoped rule: the WRONG comparison (build date) was tried first and back-tested out as discriminating nothing, 7d was swept against 0d and 90d on 23 real prod leads with an independent yardstick, and the docstring's numbers reproduce EXACTLY from the committed spike/SO_TIMING_VERIFICATION.json (23 rows; refuted_high_conf n=10 with 10/10 postdating, median 178.5d; corroborated n=6, 2 postdating at >0d). Ships its own counter-example ('1 of 6 INDEPENDENTLY-CONFIRMED leads still trips this'). Its exposure is downstream (rank 1), not in the rule.


3. `signature_age.other_channel_floor = 20` (config.py:570, measurement sigage.py:110-121) — the repo's own worked example of getting a denominator right: both arms stated ('All-channel unfloored is worse: it split evenly between filings humans refuted and filings humans fixed'), replayed over 35 filings for 6 -> 12 caught and 0 lost, named counter-examples in both directions (refuted at 26/21/26/43/80 off-channel reports, acted-on at 9/3/1), purely additive below the floor, and honest about its own n ('CALIBRATED ON TEN POINTS, though: re-measure it before trusting it far').


4. `_apply_is_backout_gate` rules A and B (orchestrator.py:2170, :2196) — the discipline this audit is looking for, in full. The claim is a proof rather than a heuristic and was verified byte-for-byte on the motivating case (dom/onnx/InferenceSession.cpp sha1 1eea729e identical at the fix's parent, the revert and the build rev); the tempting cheaper predicate (match the push's MEMBERS by title) was implemented, measured wrong 6.4% of the time / 1.8% outright wrong abstains, and rejected with a named counter-example (f6ca012f57e3); the hg-short-hash branch was measured dead (0 of 909 post-migration); and rule B separates 'is a backout' from 'changed nothing' and applies the right action to each while preserving the shipped-then-reverted counter-case.


5. `second_opinion.effort = "high"` (config.py:518) — head-to-head A/B on 51 corpus cases with known ground truth, both arms reported (clean-label sensitivity 15/15 vs 14/15, specificity 26/26 for both) at half the cost and 2.6x the speed, and it reproduces from the committed spike/SO_INSTRUMENT_CALIBRATION_high.json (n_pos 25 + n_neg 26 = 51). Note the stale prose at agent/second_opinion.py:7 and :19 still defends effort=max — that is documentation drift, not a live threshold.


6. `second_opinion.min_confidence = 25` (config.py:526) — cannot be overfit by construction: it is derived from the report gate rather than chosen ('there is NO separate report gate: ANY lead is shown, so this must sit at the LOWEST rung a lead can hold'), and the cost of the previous value was measured (4 of 31 reported leads over the first three prod days had no SO despite being displayed).


7. `bit_flip.min_confidence = 50` (config.py:626) — argued from the INSTRUMENT's own construction rather than from the motivating case's score of 62: rust-minidump combines hand-picked weights with a noisy-OR over a 0.25 baseline, so 25 means only 'some single-bit variant happens to be mapped', and a poison register halves the result. The knife-edge was checked ('production values cluster with a gap between 43 and 62'), and it is deliberately NOT set at 62.


8. `bit_flip.min_signature_reports = 5` (config.py:628) — 47-filing back-test with the boundary counter-example named: floors of 3 and 5 give identical answers (0 of the 18 FIXED/DUPLICATE/ASSIGNED suppressed, the INVALID bug 2062173 caught), while 8 or more loses bug 2064600 itself, whose signature has just 6 nightly reports in a year. Also states why it diverges from bugbot's 20.


9. `bit_flip.max_reports = 1` (config.py:627) — 1 is the definition of 'nobody else has ever hit this signature', so there is no free parameter, and the docstring states the failure mode of dropping the conjunct (the same flip score is common on high-volume signatures where one flaky machine contributes hundreds of reports).


10. `inspector.thread_for_analysis` and `_HANG_WATCHDOG_FRAME` (inspector.py:160-164) — textbook contextualisation with the control arm counted: three REQUIRED conditions (report_type hang, the two thread fields disagree, the crash_info thread tops out in RunWatchdog), measured over 40 sampled nightly hang reports where 9 diverge — every one a shutdownhang with RunWatchdog on top — and the other 31 agree and are left alone. Matched on the FUNCTION rather than thread_name for a measured reason (the 15-byte Linux cap), with a stated fallback so a bad index cannot lose the stack.


11. `shutdown-hang` archetype MATCHER `^shutdownhang \|` (archetypes.py:87) — exact for what it names: Socorro prefixes the signature of every shutdown hang whatever the frames under it, measured at 31/840 reports and 12/298 distinct signatures with no false fires, and pinned by tests/test_hang_thread.py:333. (The row's GUIDANCE is worklist rank 7; the matcher itself is sound, and the narrowness question — 77 further AsyncShutdownTimeout / ShutDownKill reports it excludes — belongs to the singleton row's over-reach, rank 5.)


12. `_record_window_membership` / `report_bug.is_suspected_regression` (orchestrator.py:1760, report_bug.py:858) — the correct response to reviewer feedback: it did not add a rule, it STOPPED asserting something the pipeline never established, made the tri-state fail toward silence, and falsified the old premise on a panel (over the canary's first 22 filings the 'regressor is in this build's window' premise held THREE times; the rest named code 24, 58 and 1335 days older). The on-stack looseness in the docstring under-claims, which is the safe direction.


13. `_record_signature_age_facts` and `sigage.RENAME_DRIFT_DAYS = 30` (orchestrator.py:1795, sigage.py:736) — deliberately MEASURED BEFORE ACTED ON, with the reason stated ('the lesson of 31b5f3b, where a gate returned before recording and spent nine days unmeasurable'). 30 is fit on 450 non-novel controls whose maximum observed inversion is 1 day against two real inversions at 40.5 and 45.6 days — a stated GAP, not a point estimate — and it fires on 6 (1.2%), all six confirmed against processor_history, 0 false positives. One-directional by construction: an older report proves a rename, no older report proves nothing.


14. `Dossier.already_commented(bug_id, signature)` (models.py:2486) — earned from three measured cases (3 of the 31 bugs commented on) and gated to exactly the grain the cases implicate, with the counter-example spelled out (a SECOND signature on the same bug is real information) and the fail-closed direction stated. The tempting deeper fix (dedup the protohash) was separately measured dead at 0.7% and unbackfillable.


15. `models._UNUSABLE_VERDICT_PREFIXES` (models.py:50) — measured with the damage counted: 107 of 2178 done dossiers on prod 2026-08-12 were one of these and had permanently suppressed 15 later crashes across 11 clusters, five carrying a real on-stack score; the strings are pinned against agent.schema by a test so they cannot drift. (The sibling `_INSTANCE_SUPPRESSED` is worklist rank 19 — different list, different problem.)


16. `datacollector.get_maturity_bar` / `mature_after_days=5`, `mature_installs=4` (datacollector.py:87, config.py:224-237) — a sweep over four candidate values on a real window (59 extra signatures at 2 installs, 37 at 3, 15 at 4, 5 at 6), named counter-examples for what 4 drops (igdusc64.dll, mfx_mft_h264ve_64.dll — two-installation third-party drivers), a named case that survives it (the target, on 24 installs), and a channel predicate justified by a MEASURED off-channel harm (beta's spike floor 10 sits above its install threshold 6, so applying it there would flip 6-to-9-crash build-days to immature).


17. `bugzilla_apply._is_unsymbolicated` (bugzilla_apply.py:532) — the ALL-parts quantifier IS the contextualisation, and it was verified live against nightly signatures matching `~@0x`: skips the 6 fully-bare ones, files the 3 partly-symbolicated ones, exactly as documented. Justified against a real case on the other side (bug 2060920's `OOM | unknown | memcpy_repmovs_Intel | ...` still files), with incidence stated (1 of 66 rung-70 verdicts) rather than claimed common.


18. `bugzilla_apply._CLOSED_STATUSES` and `_last_reopened` (bugzilla_apply.py:409, :440) — an enumerable, genuinely CLOSED set (BMO has exactly RESOLVED/VERIFIED/CLOSED), chosen over the naive string match that the motivating case would have broken: bug 1990812 was reopened straight to NEW, so matching 'REOPENED' would have missed it.


19. `_split_by_application`'s fail-open on a missing product (bugzilla_apply.py:359) and `get_other_app_products(None)` — only a POSITIVELY identified foreign product costs a bug its venue, and an unknown crash product exempts nobody; the fail direction matches the module's standing rule (a missed filing is recoverable, a duplicate is not) and both halves are pinned by tests. (The LIST's completeness claim is worklist rank 13; the fail-open logic around it is correct.)


20. `_link_regressed_by` restricted to `mode == 'new_bug'` (bugzilla_apply.py:688, :741) — a small measured panel with a named contradiction: 2 of the 6 filings that commented on an existing bug found a human-set `regressed_by` there, and on bug 2057980 ours would have contradicted it. The predicate is the exact condition distinguishing the two contexts.


21. `autofile.comment_max_bug_age_days = 30` (config.py:412) — explicitly DECLINES to tune, states its n, and states the direction of error: 'the two real cases separate by three orders of magnitude — the correct comment landed on a bug filed 9 days AFTER its regressor, the wrong one on a bug filed 1375 days BEFORE'. An n=2 acknowledged as n=2, with the asymmetry argued, is not an n=1 threshold. (The FAIL-OPEN branch around it is worklist rank 11.)


22. `sigage.MAX_WINDOW_DAYS = 364` (sigage.py:44) — forced by the API (Socorro hard-rejects more than 365 days and the implicit 'to now' bound pushes an exact 365 over the line), and deliberately public so the second-opinion agent's tool text and the module cannot drift apart.


23. `sigage._FIRST_DATE_URL_BUDGET = 3800` (sigage.py:243) — measured in the right unit with the failure points named: 10 per request is 3217 bytes and fine, 20 is 6559 and a 400, 30 is 9925 and a 414; 3800 leaves headroom under the ~4094-byte limit. Also records why the count-based version is silently wrong.


24. `sigage.NEW_SIGNATURE_DAYS = 7` and `CLOCK_DISAGREEMENT_DAYS = 30` (sigage.py:745, :740) — explicitly fenced off from every decision: 'for WORDING ONLY ... moves no rung, score or decision. Deliberately not the stale-signature gate's threshold, so nobody later reads it as one.' Exactly the discipline this audit exists to enforce.


25. `orchestrator._MAX_FIELD_FAULT = 0x1000` (orchestrator.py:850) — structural, argued from the shape of the fault: 0x0 is the generic null pointer and ambiguous, a page bounds what a struct-field offset can be, and a large address is not a field deref. No free parameter.


26. `population.*` thresholds (config.py:176-201, population.py) — a distributional fit with fire rates reported (top share median 0.18 / p75 0.47 -> 0.5 fires on 13/59; median gap p10 4430s -> 300s fires on 4/59, and those four are 20s/62s/89s/142s), sample floors forced by the statistic, `_MIN_INSTALL_TIME` a domain fact, and the whole module explicitly unable to gate, score or suppress anything. It is also the counterweight to BROKEN_CPUS: this module CAN see the degenerate-population shape (see worklist rank 8).


27. `compiled_out.GUARD_DENY` (compiled_out.py:50-56) and `MAX_DIFF_SYMBOLS = 8` (compiled_out.py:62) — the deny-list is proven REDUNDANT rather than assumed ('none of these can reach `_option_is_default_off` anyway ... a second lock on a door that is already shut'), and the cap is measured on the corpus with the motivating case's rank and the slack both stated (AutoMarkingLock is #1 at 13 occurrences so even a cap of 3 would find it; 8 costs a mean of 5.1 lookups per filing against 35.9 uncapped). The PROMPT's lack of an equivalent deny-list is worklist rank 4 — the module list itself is sound.


28. `eval/corpus._NOOP_DESC_PATTERNS` (corpus.py:191-213) — panel-sized (103 of 385 resolved landing nodes, 27%, unusable), with the asymmetry stated and a broader alternative tried and REJECTED with its false positive named ('a <verb> ... tests rule wrongly flagged "Fix a crash in nsDocShell when tests run"'). Anchored or unanchored per entry, each with a reason.


29. Archetype guidance withheld from the blind second opinion (triage.py:443, pinned by tests/test_feedback_archetypes.py:120) — argued from a measured instrument ('a fact constrains two analyses toward the same right answer, an archetype is a suggested DIRECTION') and it is the one thing preventing a wrong prior from being echoed back as independent agreement. Verified: zero occurrences of 'archetype' in second_opinion.py.


30. `_apply_callpath_gate` / SF-3 (orchestrator.py:976) and the `prior_signature_match` focus guard (orchestrator.py:872) — the context predicate names precisely the population the rule was written for and the docstring states the counter-case it must not touch ('an on-stack candidate already has its stack-frame anchor, so requiring a searchfox call path would wrongly demote it'); the focus guard `len(prior_regressor_bugs) == 1` is the right predicate for 'a hot signature yielding several priors is not corroborative'. Both are additionally dead in prod (offstack.enabled=false).


31. `_reconcile_bridged_action` (orchestrator.py:1081) and `_apply_offstack_observe_only` (orchestrator.py:1114) — the first is mechanical invariant enforcement ('an apply-eligible action must not contradict the verdict'), correctly unconditional and idempotent; the second is a canary switch with a matching context predicate, a documented default-on posture, and its bypass at the FILER already found and fixed with the rate measured (14 of 66 rung-70 verdicts carried the flag).


32. `abstain_below_confidence = 0.85`, `min_citations_per_claim = 1`, `bugzilla_apply._EXECUTABLE`, `triage._GPU_VENDORS`, `agent/tools/socorro.py:41-44` `_FACETS`, `agent/schema.py:145` `_HG_REV_RE` — vocabulary, allow-lists and total functions rather than tuned quantities, each with the non-obvious part documented: the 0.85 coupling to system.md's 'strong-evidence REQUIRES confidence:high' and what moving it in either direction does; unknown GPU ids passing through unchanged; build_id deliberately excluded from the facet list because that facet is count-ordered and silently drops the oldest build; and `_HG_REV_RE` deliberately NOT applied to `Candidate.node` because gates and the autofiler read that one.


33. `net.DEFAULT_TIMEOUT = (10, 60)` (net.py:54), `hgedge` `_SEM(8)` (hgedge.py:33), `machine._MAX_ROWS = 200` (machine.py:40), `models._OWN_JOB_RECLAIM_AFTER_S` (models.py:62), `sweep.max_per_run = 3` / `min_age_s = 21600` (config.py:326-330), `reap_max_attempts = 2` / `proto_max_unusable = 2` — infrastructure values each derived from something other than a case: the read half is a gap-between-bytes limit (stated), 8 sits under a verified 12-way burst, truncation at 200 rows can only UNDERCOUNT a value the gate fires on being high, the reclaim window is a stated multiple of the heartbeat pinned by tests/test_reaper_recovery, and the sweep rates are computed arithmetically from measured arrival (~3.4/day) and drain (~11/hour).


---

## 7. Scope gaps in this audit

* **`crashclouseau/vendor/`** (hackbot runtime, agent_tools) was excluded by all six readers. If a
  threshold or filter lives there it is completely unaudited.
* **Archetype rows are DB-editable** and `seed(overwrite=False)` never clobbers, so prod guidance may
  differ from the source in `archetypes.py` and nothing surfaces the divergence (no page renders the
  table). Any measurement of an archetype from source is an assumption — state it. `matcher={}`
  matches every crash and is pinned as intentional, so **the archetypes table is the sanctioned way
  to create an ungated context rule with no review, no test and no back-test.** That is worth a
  structural fix on its own.
* **The abstain channel is invisible** to every outcome measurement the repo has. Rules whose failure
  mode is a false abstain can only be audited by replay.

---

## 8. Deliverables

1. Fixes for the §4 items whose repair was NOT refuted, each with a panel and a counter-example in
   the docstring.
2. §5 worked top-down, as far as the session gets. Each item ends either as a landed fix or as a
   recorded "leak real, repair refuted" — both are results; only an unanswered item is a failure.
3. **The structural fix that stops this recurring.** Three candidates, in order:
   * a **registry of corroboration flags** with their writers and readers. All three gate-interaction
     findings in §4/§5 are instances of one gap: a flag is the only coupling mechanism between gates
     and nothing lists which are read (`stale_signature_clamped` has one writer and zero readers).
   * a **back-test requirement for archetype rows** — a row cannot seed without a fire-rate measured
     on the 840-report control sample, since the only existing back-test passes on an input shape
     production never produces (`tests/test_feedback_archetypes.py:62`).
   * **`feedback.refresh()` on the schedule** plus an `error_class` label, so the next reviewer
     correction is a row rather than a memory. See `jens_feedback_bug2065373.md` §4.8 for the three
     measured traps in the obvious implementation.
4. Update `memory/clouseau-worklist.md` with what landed.

## 9. Repo conventions

* `uv run python …`; deps via `VIRTUAL_ENV=.venv uv pip install`.
* **A genuine improvement ships LIVE, not behind a default-on flag.** Flags are for real kill
  switches and A/Bs only.
* **Commit messages stay short.** Do not infer style from `git log` — the long evidence-dump bodies
  are not wanted going forward. The evidence goes in the docstring.
* Never push. No Claude co-author trailer.
* Run `bin/predeploy.py` before deploying.
* Read `config/global.json` for what actually runs, not `config.py` (§3).

## 10. Files

* `jens_feedback_bug2065373.md` — the review that started this, with the eight improvements it
  justified and the corrections an adversarial pass forced on them.
* `memory/feedback-generalize-dont-overfit.md` — the principle.
* `memory/clouseau-worklist.md` — the standing product worklist (compiled-out, machine-spike at the
  selector, the novelty escape). This audit is orthogonal to it; do not drop it.
* Workflow transcripts with the full inventory (23 worklist items, 33 cleared, per-rule evidence):
  `~/.claude/projects/-home-calixte-dev-mozilla-crash-clouseau/05fd775e-e330-4d08-af1f-bdf2de5f104d/subagents/workflows/wf_62a20f4c-5a9/journal.jsonl`
