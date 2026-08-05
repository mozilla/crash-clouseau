# Which required fields in `agent/schema.py` can the model not know?

Audit date 2026-08-05. Repo HEAD `b314116` on `augmented`. **Correction to the brief:** the
working tree is CLEAN (`git diff` empty) — the `StackFrameCitation.node = ""` fix is
*committed* as `421e484`, and the `crashstack.html` change as `b314116`. Nothing in this audit
edited tracked source.

Corpus: all 1996 prod dossiers from `crash-clouseau-augmented` (2026-07-06 .. 2026-08-05),
dumped offline to `spike/_dossier_dump.jsonl`; 1950 carry a parseable ```json handoff.
Every number below labelled VERIFIED was produced by re-running a repro in phase 4 against
that corpus or against the real `parse_and_validate`; output is pasted.

---

## 1. The answer, in three sentences

Six required-field families are genuinely unknowable-or-unexpressible by the model, and at
HEAD they still destroy **41 of 1950 verdicts (2.1%) and 127 of 1950 crash briefs (6.5%)**:
the citation `kind` discriminator (no union member exists for the source/history tools), every
`StackFrameCitation` field in its NULL form (an opaque frame has no file/line/uuid), the
`SkepticResult.status` enum (annotated values), `CrashFrame.line`/`stackpos`, the
`FailureClass` enum (no `oom`), and the `DiffLineCitation.side` literal. **Fix the citation
`kind` hole first** — it alone accounts for 22 of the 41 lost verdicts, is the only *structural*
gap (the model reads a changeset and there is no legal `kind` to write), and it is still firing:
the most recent loss is 2026-08-04. The three verdict-killing fixes are exactly disjoint and
together leave **zero** residual (22 `kind` + 15 `stack_frame`-null + 4 `side` = 41/41,
VERIFIED), and one of the six — `SkepticResult.status` — is not a false abstain at all but a
**safety inversion** that already shipped a strong-evidence verdict whose skeptic said `fail`.

---

## 2. Ranked table

Rank = verified prod damage, with the safety inversion promoted above its frequency.

| # | model.field | can the model know it? | blast radius | seen in prod (HEAD) | fix | verdict |
|---|---|---|---|---|---|---|
| 1 | `Citation.kind` — no union member for `mcp__source__raw_file` / `mcp__history__*` | **No — structural.** It read a changeset; no legal tag exists | **WHOLE VERDICT** (VERIFIED) | 124 citations at validated positions; **22 of the 41 lost verdicts**; last 2026-08-04 | 5th member `RefCitation` (with a "points at something" guard) + 12 aliases | **ship now** (+ template branch) |
| 2 | `StackFrameCitation.{uuid,stackpos,filename,function,line,node}` in the **null** form | **No** — opaque/JIT/driver frames; symbolication emits nulls | **WHOLE VERDICT** (VERIFIED) | 8 dossiers emit a null stack-frame cite; **15 of the 41** (14 from coercion, 1 from defaults) | `None`→`""`/`0` before-validator + defaults, same as `CrashFrame` | **ship now** |
| 3 | `SkepticResult.status` — annotated (`"fail (noise)"`) | Partly — same annotation habit that already loosened `CallEdge.via` | **SAFETY INVERSION** (VERIFIED): the item is dropped, the binding veto never runs, strong-evidence + accusatory needinfo ships | 4 items / 8905; dossier 3817 shipped a `lead` with a `fail` skeptic silently dropped | prefix-tolerant `SkepticStatus._missing_` (**fail-closed**, never drop) | **ship now** |
| 4 | `CrashFrame.line` (null) and `CrashFrame.stackpos` (missing) | **No** — `_stack_text` shows the model `#7 None :None`; inlined frames have no stackpos | **WHOLE CRASH BRIEF** (VERIFIED) | 177 nulls / 89 dossiers → 88 of the 127 crash drops; 52 missing `stackpos` → ~13 more | add `line`,`stackpos` to the existing `_none_to_empty`; `stackpos: int = 0` | **ship now** |
| 5 | `CrashBrief.failure_class` — `oom` is not in the enum | **No** — vocabulary gap, not a typo | **WHOLE CRASH BRIEF** (VERIFIED) | 21 dossiers (`oom` x17) — 20 crash-only, 1 also lost the verdict | add `oom`/`stackoverflow` **and** `_missing_ -> other` | **ship now** |
| 6 | `DiffLineCitation.side` — `'+'`,`'old'`,`'left'`,… | **No** — vocabulary; 5th recurrence of this bug class | **WHOLE VERDICT** when in a claim (VERIFIED) | 61 OOV values / 27 dossiers; **4 of the 41**; 9 hunks, 3 edges | extend `_SIDE_ALIASES` (`+/new/right`→added, `-/old/left`→deleted, rest→context) | **ship now** |
| 7 | `StructLayoutCitation.type_name` / `offset` | Arguably not — named once in prose, in **zero** JSON examples | **WHOLE VERDICT** (VERIFIED mechanically) | **0 of 169** validated struct_layout citations — never fired | defaults are unsafe here (a content-free layout cite); `offset` hex-string coercion is free | **needs thought / low priority** |
| 8 | `Candidate.node` / `Candidate.author` (null) | Yes, mostly | candidate sub-object only — **NOT** the verdict (**REFUTED**) | 3 candidates in 1147; **0 verdicts moved** by the fix | none; a naked `author` coercion resurrects the fake node `"none-credible"` | **leave alone** |
| 9 | `DiffLineCitation.node` (and every other `diff_line` field) | **Yes** — 0 holes in 8433 emitted citations (**REFUTED**) | would be the whole verdict | **0** | none | **leave alone** |
| 10 | `SearchfoxCitation.symbol_id` / `permalink` / `repo` | Rarely not (search hits have no symbol) | whole verdict when in a claim | 1 null in 13343 at validated positions (`repo`: 4 omissions, all in the *unvalidated* skeptic list) | only with an "at least one of permalink/symbol_id" guard — otherwise it inflates the SF-3 gate | **needs thought** |
| 11 | `CallEdge.callee_symbol` | Rarely (a "search-hole" edge) | one edge; can strip the last lead anchor | 1 null in 7057 | optional; `str = ""` + coercion | **leave alone** |
| 12 | `Cited.citations` (empty list ⇒ hard failure) | n/a — this is the anti-hallucination rule | whole verdict for a claim | rare | **do not loosen** at `Cited`; if ever, do it at `Verdict` | **leave alone (deliberate)** |

---

## 3. The verified findings

### 3.1 `Citation.kind` — the union has no member for the tools the agent actually uses (VERIFIED, ranked #1)

The union predates `mcp__source__raw_file` and `mcp__history__{file_history,blame,changeset}`.
The model reads a changeset, cites it honestly, and there is no legal tag. `_KIND_ALIASES`
only repairs *spellings* of the four existing kinds.

```
$ uv run python spike/_p3i7_repro.py
CASE: A. verdict.consistency.citations[] has kind='changeset' (extra cite)
  decision      : abstain
  abstain_reason: dossier validation failed (verdict unusable): 1 validation error for Dossier
verdict.consistency.citations.1
  Input tag 'changeset' found using 'kind' does not match any of the expected tags:
  'searchfox', 'diff_line', 'stack_frame', 'struct_layout' [type=union_tag_invalid, ...
  survived      : {"crash": true, "candidate": true, "hunks": 1, "data_flow": true,
                   "verdict.mechanism.citations": null, "verdict.consistency.citations": null}
CASE: F. call_path.edges[0].citations[] has kind='changeset'
  decision      : strong-evidence      <- shallower positions are lossy, not fatal
```

Real-data replay of the proposed fix (`RefCitation` + 12 aliases) over all 1950 handoffs:

```
$ uv run python spike/_p3i7_realdata.py
--- HEAD ---     VERDICT DROPPED by salvage (fatal) : 41
--- PATCHED ---  VERDICT DROPPED by salvage (fatal) : 19
verdicts RECOVERED by the fix : 22      verdicts newly lost by the fix: 0
recovered dossier ids : [313, 389, 408, 411, 462, 570, 2879, 3201, 3832, 4059, 4514, 4980,
                         4982, 4984, 4986, 4999, 5012, 5021, 5030, 5069, 5339, 5800]
   id=  389  HEAD=abstain  -> FIXED=strong-evidence  conf=high      cand=d86be929745b
   id= 4980  HEAD=abstain  -> FIXED=strong-evidence  conf=high      cand=388857f92acb
```

Consumers checked (VERIFIED, not argued):

```
$ uv run python spike/_p3i7_gates.py
  ref-only call_path edge      decision=abstain  anchor=False verified_callpath=False corr={}
  searchfox call_path edge     decision=lead     anchor=True  verified_callpath=True  corr={}
$ uv run python spike/_p3i7_sideeffects.py
### 6. templates/crashstack.html cite() macro on a 'ref' citation
   kind=ref            -> rendered '[]'          <- NEEDS A TEMPLATE BRANCH
   kind=struct_layout  -> rendered '[]'          (pre-existing, same hole)
### 4. eval.metrics._nodes_in_dossier over the real corpus
  dossiers whose node set changes : 5      distinct nodes newly added : 9
### 5. whole-corpus decision transition (HEAD -> FIXED)
  abstain -> strong-evidence  7   |  abstain -> lead  6
```

`_has_verified_callpath` (orchestrator.py:743, the off-stack SF-3 gate) keys on
`isinstance(c, SearchfoxCitation)`, so a `ref` citation **cannot** manufacture strong evidence —
confirmed above. Two caveats that are part of the recommendation, both VERIFIED:

* the **lax** ref accepts a content-free citation and lets it satisfy the min-citations rule;
  the **strict** variant (`model_validator`: at least one of node/filename/symbol_id/permalink/content)
  recovers the *same* 22 and refuses it —
  `spike/_p3i7_strictref.py`: `fatal verdicts HEAD=41 lax-ref=19 strict-ref=19`,
  `content-free citation under lax-ref -> strong-evidence`, `under strict-ref -> abstain`.
  Ship the strict one.
* `eval.metrics` node sets move on 5 dossiers, so any recall number computed before/after is
  not comparable.

### 3.2 `StackFrameCitation` in its NULL form — the shipped `node` default is half a fix (VERIFIED, ranked #2)

`CrashFrame` documents this exact hazard in a 7-line comment and coerces `None`→`""`. Its
citation twin — which cites *those very frames* — does not, and a `str` field rejects an
explicit `null` even with a default. Every field is fatal:

```
$ uv run python spike/_p3i8_repro.py
FORM=NULL   FIELD=filename
  verdict.consistency.citations[0]   decision=abstain   ... stack_frame.filename
      Input should be a valid string [type=string_type, input_value=None]
FORM=NULL   FIELD=line       -> abstain   (int_type)
FORM=NULL   FIELD=uuid       -> abstain   (string_type)
FORM=NULL   FIELD=function   -> abstain   (string_type)
FORM=NULL   FIELD=node       -> abstain   (string_type)   <- still fatal AFTER 421e484
FORM=NULL   FIELD=stackpos   -> abstain   (int_type)
  data_flow.crash_site / hunks[0] / call_path.edges[0] -> strong-evidence survives (lossy only)
```

The ablation separates the two halves of the fix and shows the coercion is the load-bearing one:

```
$ uv run python spike/_p3i8_ablation.py
stack_frame citation NULLs by field : {'node': 7, 'filename': 4, 'line': 3}
   ...of which under `verdict.*`    : {'node': 3, 'filename': 1, 'line': 1}
stack_frame citation MISSING by fld : {'node': 188, 'filename': 1}
HEAD                  : 41 of 1950 verdicts LOST (baseline)
HEAD + defaults only  : 40  (recovered 1)
HEAD + coercion only  : 27  (recovered 14)
HEAD + proposed fix   : 26  (recovered 15; ids 280,848,1165,1847,2940,3004,3250,3751,3922,
                                                3986,4394,4699,4841,5542,5559)
```

Grounding side-effect, VERIFIED and mitigated the same way as #1:

```
$ uv run python spike/_p3i8_fixcheck.py
  HEAD            | bare {'kind':'stack_frame'}  -> abstain
  defaults+coerce | bare {'kind':'stack_frame'}  -> strong-evidence  cite=uuid='' file='' line=0
  coercion only   | all-null stack_frame         -> strong-evidence  (content-free)
B. a citation whose stackpos is null renders 'frame #0 …' — i.e. the CRASHING frame
```

So the defaults/coercion need the same "points at something" guard (see §5), which keeps the
real case (an opaque frame whose `function` is known and everything else null) working.

### 3.3 `SkepticResult.status` — a dropped skeptic result is a bypassed safety gate (VERIFIED, ranked #3)

This is the one finding that makes the system say **more** than it should. `status` is a strict
enum; an annotated value fails; `_LIST_FIELDS` per-item salvage drops the whole `SkepticResult`;
`Dossier(**kwargs)` then re-runs `_skeptic_veto` **without it**. The schema comment at lines
295-301 says this must never happen. It happens:

```
$ uv run python spike/_audit_p3i9_skeptic_status.py
PART A - control: a canonical `fail`
--- control status='fail' ---
  decision       : lead          confidence : medium
  needinfo_draft : 'A skeptic review could not confirm the mechanism, but changeset 012345'
  skeptic kept   : [('mechanism', 'fail')]

PART B - the 4 annotated variants measured in prod
--- status='fail (noise)' ---
  decision       : strong-evidence   confidence : high
  needinfo_draft : 'please confirm: this changeset introduces the UAF'
  skeptic kept   : []                                     <- the veto input is GONE

PART F - does the bypassed verdict stay apply-eligible?
  annotated 'fail (noise)' -> action={'type': 'bugzilla.add_comment', 'params': {'bug_id': 123,
     'text': 'please confirm: this changeset introduces the UAF', 'is_private': True},
     'reasoning': "auto-drafted from the verdict's needinfo_draft (strong-evidence); ..."}
SUMMARY  control decision=lead accusatory=False | annotated decision=strong-evidence
         accusatory=True | VETO BYPASSED = True
```

Prod: 4 out-of-vocab statuses in 8905 items — dossier 3817 emitted `"fail (noise)"` and shipped
a `lead`; 4693 lost `"unverifiable-but-supported"`. Note `status` omitted or `null` has the same
effect (PART C). The fix must **preserve the token**, not merely accept it: a prefix-tolerant
`_missing_` (`fail*`→failed, `unverifiab*`→unverifiable, `pass*`→passed) and fail-closed
(unrecognised → `fail`) rather than dropping the item.

### 3.4 `CrashFrame.line` (null) and `stackpos` (missing) — the biggest loss by volume (VERIFIED, ranked #4)

`_none_to_empty` covers `function`/`filename`/`node` and stops one field short of `line`, which
is exactly the field symbolication nulls. A default does not save an explicit `null`.

```
$ uv run python spike/_verify_crashframe_line.py
VARIANT: crash.frames[2].line OMITTED        -> crash=OK      (the default works)
VARIANT: crash.frames[2].line = null         -> crash=DROPPED (all 3 frames lost)
VARIANT: crash.frames[2].line = 'None'       -> crash=DROPPED
VARIANT: crash.frames[2].stackpos = null     -> crash=DROPPED
VARIANT: crash.frames[2].filename = null     -> crash=OK      (covered by _none_to_empty)
VARIANT: verdict.consistency.citations[0].line = null (StackFrameCitation) -> abstain

$ uv run python spike/_measure_crashframe_line.py
  total frames 7332 | line present-and-null 177 | line non-int 11 | dossiers with >=1 bad line 89
  BEFORE  CrashBrief DROPPED by _salvage : 127   top causes: 177 frames.N.line[int_type],
          52 frames.N.stackpos[missing], 26 frames.N[model_type], 21 failure_class[enum]
  AFTER   CrashBrief DROPPED by _salvage :  39   NET: recovered 88
```

Consumer check: nothing reads a *dossier* `CrashFrame.line` (the rendered stack table comes from
the DB `stack['frames']`); `orchestrator._classify_exposer` reads only `failure_class` and
`phc_free_stack` — which is precisely why losing the brief is silent, and why it stayed unnoticed
for a month at 4.5% of runs.

### 3.5 `CrashBrief.failure_class` — `oom` is a real Firefox crash family the enum cannot say (VERIFIED, ranked #5)

```
$ uv run python spike/_verify_failure_class.py
CASE: crash.failure_class OMITTED         -> crash=SURVIVED (FailureClass.other)
CASE: crash.failure_class = 'oom'         -> crash=DROPPED
      Input should be 'uaf', 'null_deref', 'assertion', 'oob', 'shutdownhang' or 'other'
CASE: 'stackoverflow' / 'other:rust_panic' / 'jit_or_corruption' / null  -> crash=DROPPED

$ uv run python spike/_verify_failure_class_prod.py
dossiers with a crash.failure_class enum error   : 21   ('oom' x17)
  ...of those, verdict ALSO died                 : 1
  ...of those, ONLY the crash brief died         : 20
```

Variant C (add the members **and** `_missing_ -> other`) is the one to ship — it keeps the
information, tolerates case, and the prompt-drift guard still passes:

```
$ uv run python spike/_verify_failure_class_fix.py
  C  'oom' -> SURVIVED(oom) | 'OOM' -> SURVIVED(oom) | 'other:rust_panic' -> SURVIVED(other)
  FailureClass('oom') == FailureClass.uaf : False        (the only orchestrator read is safe)
  PROMPT-DRIFT GUARD: prompt <= schema (test passes): True
  CAVEAT: the TRACKED schema reading an 'oom' dossier persisted by variant C: FAILS
```

That caveat matters operationally: once `oom` is persisted, a rollback to the old enum cannot
read those rows back (`dossier_from_db_json` fails on them). `_missing_` alone does not have that
property. Ship the members with the `_missing_`, and do not roll back selectively.

### 3.6 `DiffLineCitation.side` — the alias table only ever maps *spellings of real sides* (VERIFIED, ranked #6)

```
$ uv run python spike/_verify_side.py
POSITION: verdict.consistency.citations[1]
--- OMITTED --- / --- null --- / --- side='+' ---   decision=abstain, mechanism=None

$ uv run python spike/_verify_side_corpus.py
  IN-VOCAB 8510 | OUT-OF-VOCAB 61 across 11 spellings
      'n/a' x12 | '+' x11 | 'modified' x8 | 'left' x7 | 'new' x6 | 'right' x6 | 'old' x5
      | 'current' x2 | 'changed' x2 (not in fix) | 'unspecified' x1 (not in fix) | '-' x1
  BEFORE verdicts lost 41, hunks dropped 20, edges dropped 43
  AFTER  verdicts lost 37, hunks dropped 11, edges dropped 40
  verdicts recovered: 4 (ids 3639, 4250, 4338, 5289)   newly lost: []
```

Note `'changed'`/`'unspecified'` are still unmapped by the proposed table — which is the argument
for a *total* mapping (unknown → `context`) rather than another finite list. See §5.

### 3.7 REFUTED: `Candidate.node` / `Candidate.author` do **not** kill the verdict

The claim was that a null `candidate.node` force-abstains via `_skeptic_veto` rule (2). The
repro shows `_salvage` validates `candidate` and `verdict` independently — the verdict survives;
the abstain only appears in the extra condition the claim omitted (decision==lead **and** no
hunk **and** no edge, i.e. candidate is the sole anchor: 1 of 685 prod leads).

```
$ uv run python spike/_verify_candidate_node.py
[node null] strong-evidence, other anchors present -> decision=strong-evidence, candidate=DROPPED
[node null] LEAD, candidate is the ONLY anchor     -> abstain "lead has no cited candidate/hunk/
                                                      edge anchor; nothing to act on"
$ uv run python spike/_verify_candidate_node_fix.py
### LEAD, candidate ONLY anchor  [node null]
   AFTER   candidate=OK node=''  anchor=False   <-- THE FIX DOES NOT PREVENT THE ABSTAIN
Full prod replay: candidate sub-object recovered : 3 | verdicts MOVED by the fix : 0
$ uv run python spike/_author_prevalence.py
    author EXPLICIT null : 3 [2713, 3920, 5989] -> VERDICT changed by it : 0 []
```

And the `author` half of the fix is actively harmful: it resurrects dossier 3920's candidate
`{"node": "none-credible", "author": null}`. `"none-credible"` is a truthy model-invented
sentinel, so after the fix it counts as a lead anchor and `_soft_lead_draft` produces
*"changeset none-credible looks like a plausible lead"* — a fabricated changeset id headed for a
needinfo and for the $1 second-opinion verify pass.

```
$ uv run python spike/_author_fix_prod.py
NODE+AUTHOR resurrected candidate on 3920: node='none-credible' (verdict abstain)
$ uv run python spike/_bogus_node_prevalence.py
dossiers whose candidate SURVIVES with a non-empty node: 1144
  node looks like an hg sha : 1143 | node is NOT an hg sha : 1  ('none-confirmed')
```

**Do not ship this one.** The "seen in prod 112 times" figure attached to it was
`StackFrameCitation.node` misattributed (135 of the 136 `.node` errors); the true count is 1.

### 3.8 REFUTED: `DiffLineCitation.node` is not a hole at all

Mechanically it *would* be fatal (one nodeless `diff_line` in a claim drops the verdict — the
`_verify_diffline_node.py` output confirms it), but the model always supplies it:

```
$ position-aware census over 1950 handoffs (phase-4 rerun)
  diff_line   n=5264 at validated positions   holes: none (0 missing, 0 null)
  (corpus-wide, including the unvalidated skeptic list: 8433 citations, still 0 holes)
```

The stated *reason* to keep it required ("a silently-empty node would depress recall") is also
backwards — `_nodes_in_dossier` (metrics.py:35) skips falsy nodes and never reads verdict
citations, and in the one case where the set moves, **requiring** the field is what loses a real
node. Keep it required on frequency grounds; do not propagate the false rationale into a comment.

---

## 4. Cleared fields — examined, ruled safe, with the reason

Position-aware census over the 1950 raw handoffs (phase-4 rerun; "validated positions" =
`verdict.mechanism/consistency.citations[]`, `call_path.edges[].citations[]`,
`hunks[].citations[]`, `data_flow.citations[]`, `data_flow.crash_site`):

| field | evidence | why it is safe |
|---|---|---|
| `DiffLineCitation.node/filename/line/side*/content` | 5264 cites, **0 missing, 0 null** | the model knows the diff shape cold (*`side` values are the exception — §3.6) |
| `StructLayoutCitation.type_name` / `offset` | 169 cites at validated positions, **0 holes**; but the omission IS fatal (VERIFIED below) | never fired in a month; the prompt-derived "HIGHEST risk" ranking is not borne out |
| `SearchfoxCitation.permalink` | 13343 cites, 0 missing/null | prose calls it "the anchor"; the model never drops it |
| `SearchfoxCitation.symbol_id` | 1 null in 13343 (a `call_path` edge; cost = 1 dropped edge) | too rare to justify the SF-3 risk a naked default would create |
| `SearchfoxCitation.repo` | 0 holes at validated positions (4 omissions, all in the *unvalidated* skeptic list) | free to default, but not a live hole |
| `CallEdge.caller_symbol` / `callee_symbol` | 7057 edges, 1 null (`callee_symbol`) | "search-hole" edges are real but the model names both ends anyway |
| `DiffHunk.node` / `filename` | 0 holes | in both worked examples; redundant with the citation |
| `DataFlowHypothesis.summary` | 0 holes | first key of the worked example |
| `CrashBrief.uuid` | 0 holes in 1950 briefs | handed to the model verbatim in the user prompt |
| `Verdict.decision` | 0 missing, 0 out-of-vocab in 1950 | would be catastrophic, has never drifted |
| `Verdict.confidence` | 71 omissions (default applies), 0 invalid, 0 null | a `null` *would* kill the verdict; not observed |
| `Cited.citations` >= 1 | by design | this is the anti-hallucination guarantee; §5 explains why loosening it is the wrong lever |

The struct_layout mechanism, for the record (so nobody re-derives it):

```
$ phase-4 probe, citation inside verdict.mechanism+consistency
  complete (control)                  -> strong-evidence
  type_name OMITTED                   -> abstain  (dossier validation failed)
  offset OMITTED                      -> abstain
  offset as hex string '0x8'          -> abstain
  key named byte_offset               -> abstain
  searchfox, repo OMITTED             -> abstain
  searchfox, symbol_id OMITTED        -> abstain
  searchfox, permalink OMITTED        -> abstain
  diff_line, content OMITTED          -> abstain
```

Every required field on a citation model is a whole-verdict grenade *mechanically*. The audit
question is which ones the model actually drops, and the census above answers it.

---

## 5. The general fix

**There is one, and it is not the salvage surgery.** Measured, in order of what the evidence
supports:

### 5.1 The structural idea (per-CITATION salvage) is SAFE but INSUFFICIENT — VERIFIED

Making `_salvage` drop the offending *citation* instead of the claim that holds it was the most
attractive candidate ("defuses the whole family at once"). It was implemented at runtime and
replayed over the corpus:

```
$ phase-4 experiment: prune individually-invalid citations from every `citations` list,
  then run the tracked _salvage
replayed handoffs                       : 1950
HEAD   verdicts lost                    : 41
PRUNE  verdicts lost                    : 28
recovered by per-citation salvage       : 13
newly lost                              : []
still lost, claim emptied of citations  : 28
recovered verdicts (id, decision, conf, #citations pruned, claim-emptied):
    (313,'lead','low',6,False) (389,'strong-evidence','high',1,False) (408,'lead','low',3,False)
    (411,'lead','medium',4,False) (570,'abstain','medium',3,False) (3751,'lead','medium',1,False)
    (3832,'lead','medium',4,False) (4394,'lead','medium',2,False) (4514,'lead','probable',9,False)
    (4841,'lead','medium',1,False) (4984,'strong-evidence','high',11,False)
    (5289,'lead','probable',3,False) (5559,'lead','probable',1,False)
```

Two honest conclusions:

* **It IS compatible with the min-citations rule.** In all 13 recoveries the claim still had at
  least one *valid* citation (`claim-emptied=False` in every row), so no uncited claim is ever
  resurrected; `Cited._require_citations` still fires when pruning empties a list. Zero
  regressions.
* **But it only fixes 13 of 41 (32%).** In the other 28, the unparseable citation was the claim's
  *only* citation — pruning it empties the claim and the anti-hallucination rule correctly kills
  it. Those 28 need the citation to be **accepted**, not dropped. So the structural change cannot
  replace the vocabulary/coercion work, and once that work ships it adds nothing (measured: 0
  extra recoveries on top of the union). **Recommendation: don't do it.** It buys a subset of the
  same outcome at the cost of touching the anti-hallucination boundary.

### 5.2 What to actually ship: the six targeted fixes, with two guards — VERIFIED, zero residual

```
$ phase-4 union replay (A stack_frame coercion+defaults, B side aliases, C RefCitation,
  D CrashFrame line/stackpos, E FailureClass, G SkepticStatus) + a "points at something"
  model_validator on RefCitation and StackFrameCitation
HEAD         lost=41  decisions={'lead':1172,'abstain':700,'NO-VERDICT(->abstain)':41,'s-e':37}
             drops={'crash':127,'verdict':41,'call_path.edges':43,'candidate':3,'hunks':20,
                    'data_flow':20,'skeptic':4}
SAFE UNION   lost=0   decisions={'lead':1205,'abstain':701,'strong-evidence':44}
             drops={'crash':7,'candidate':3,'call_path.edges':3,'hunks':1,'data_flow':5}
safe-union recovers: 41   still lost: []
content-free ref     -> abstain     (the guard holds)
content-free frame   -> abstain     (the guard holds)
real opaque frame    -> strong-evidence   (function known, uuid/filename/line/node all null)
```

Full tracked test suite against that same patched schema (module swapped in `sys.modules`,
tracked file untouched):

```
$ baseline               Ran 782 tests in 5.941s   OK (skipped=17)
$ SAFE UNION patched     Ran 782 tests in 7.019s   OK (skipped=17)   SUCCESS
```

The three verdict-killing fixes are **exactly disjoint and exactly cover the 41**:
`kind` {313,389,408,411,462,570,2879,3201,3832,4059,4514,4980,4982,4984,4986,4999,5012,5021,5030,5069,5339,5800} (22)
∪ `stack_frame`-null {280,848,1165,1847,2940,3004,3250,3751,3922,3986,4394,4699,4841,5542,5559} (15)
∪ `side` {3639,4250,4338,5289} (4) = 41, no overlap, nothing left.
Crash briefs: 127 → 7, and the residual 7 are not a required-field problem at all —

```
residual crash-brief drops under the union: 7 [160, 339, 3133, 4018, 4146, 5587, 5863]
causes: {('frames','model_type'): 26, ('frames.line','int_parsing'): 9,
         ('frames.stackpos','int_parsing'): 1}
```

i.e. frames emitted as bare strings instead of objects — a shape problem, out of scope here.

### 5.3 The rule to write down, so this is the last time

The recurring bug is not any one field; it is that **`schema.py` mixes two kinds of fields on
model-supplied models and treats them identically**:

1. **Descriptive fields** (`filename`, `line`, `function`, `uuid`, `node`, `content`, `symbol_id`):
   nothing gates on them — the UI renders them and `eval` reads a couple. These must be
   `default + None-coerced`, always. `CallEdge.via`, `DiffHunk.lines`, `DataFlowHypothesis.operation`
   and `CrashFrame.function/filename/node` already learned this lesson individually, each after a
   burn. The remaining offenders are the whole of `StackFrameCitation` and `CrashFrame.line/stackpos`.
   *A default alone is not the fix* — pydantic rejects an explicit `null` regardless; the
   before-validator is the load-bearing half (measured: defaults recover 1 verdict, coercion 14).
2. **Vocabulary fields** (`kind`, `side`, `failure_class`, `status`): these need a **total**
   function, not another finite alias list. `_KIND_ALIASES`/`_SIDE_ALIASES` are partial maps and
   have now been extended four times; `'changed'` and `'unspecified'` are already outside the
   *proposed* extension. Either an `_missing_` with a default arm, or a fallback that maps the
   unknown to the non-behaviour-asserting member (`context` for `side`, `other` for
   `failure_class`, `ref` for an unknown `kind`).

Two exceptions where laxness is the wrong instinct, both VERIFIED above:

* **`SkepticStatus` must never be dropped and never be loosened to a free-form str** — the veto
  keys on the token. Map it, fail-closed.
* **A citation must point at something.** Defaulting every field on `StackFrameCitation`/
  `RefCitation` makes `{"kind": "stack_frame"}` a valid citation that satisfies the min-citations
  anti-hallucination gate. The one-line `model_validator` guard (§5.2) preserves the guarantee at
  no measured cost — same 41 recovered, content-free still refused.

A cheap standing guard worth adding with the fix: extend `tests/test_prompt_schema_drift.py`,
which today only checks enum *values* quoted in `roles.py` (`system.md` is not tested at all), to
assert that **every required field of every model-supplied model appears in at least one worked
```json example** in `system.md`/`roles.py`. That test would have failed on
`StructLayoutCitation.type_name`/`offset` and on all five prose-only `StackFrameCitation` fields.

---

## 6. What this audit could NOT establish

1. **Whether the 41 recovered verdicts are RIGHT.** Recovery is measured; precision is not. The
   union turns 7 abstains into strong-evidence and 6 into leads. Given the standing finding that
   only ~28% of prod leads name the true regressor, these could be 13 recovered analyses or 13
   new false accusations. *Settle it by:* running the blind second opinion over the 41 recovered
   dossiers (offline, ~$45 at SO prices), or checking the 13 against Bugzilla regressor fields.
2. **The cost figures.** "$178.14 lost to the 41" and "$500.01 / 6.8% of spend lost historically"
   come from phase 1b's DB join and were **not** re-verified here — the offline dump carries no
   cost column. The dossier counts and ids were re-verified.
3. **Whether the prompt is the cheaper lever.** Every fix measured here is schema-side. Adding a
   worked `stack_frame` + `struct_layout` citation to `system.md`'s single JSON example (today it
   shows only searchfox and diff_line) might cut emission of these shapes at the source, but no
   A/B was run and the effect size is unknown.
4. **The 9 "no parseable ```json block" failures** (0.5% of runs) — a different failure family,
   not analysed.
5. **Whether a `ref` citation kind changes model behaviour.** Giving the model a legal way to
   cite a changeset may *increase* off-schema citing (it is now a sanctioned shape) or decrease it.
   Offline replay cannot see that; only a canary can.
6. **Subagent fragments.** `schema.validate_role_fragment` has no production caller and role
   fragments are never persisted, so this audit covers the principal handoff only. If fragments are
   ever validated in-line, the skeptic path is the one to watch: `validate_role_fragment('skeptic',…)`
   *raises* on `"fail (noise)"` (VERIFIED, PART G) rather than salvaging.
7. **Frequencies are conditional** on the current prompt + Opus tier. The `oom`/`changeset`/
   `source_raw_file` vocabulary gaps are properties of the tool set, so they will persist; the
   null-frame rate is a property of the crashes sampled.

---

## 7. UNVERIFIED — reasoned only, not repro'd (do not read these as findings)

Carried over from the static enumeration and the prompt cross-reference. Each is plausible; none
has a repro attached, and several are contradicted by the census in §4.

1. `DiffHunk.node`/`filename` → *indirect* false abstain via lost lead anchor (`_skeptic_veto` (2)),
   with the salvage log saying "dropped hunks[0]" so it would be misdiagnosed. Census: 0 holes.
2. `CallEdge.caller_symbol` → same indirect anchor-loss path. Census: 0 holes.
3. `Cited.citations == []` on a **lead** (not strong-evidence) → whole verdict lost even though the
   lead never claimed proof. Claimed prod counts (1 claim, 1 hunk, 1 edge, 4 data_flow) not
   re-measured.
4. `Verdict.confidence = null` → whole verdict lost despite the default. Mechanism obvious, never
   observed.
5. `Verdict.decision` spelling drift (`strong_evidence`) → whole verdict lost. Never observed.
6. `StructLayoutCitation.type_name`/`offset` as the *highest-risk* prompt gap. The mechanism is
   VERIFIED (§4) but the risk ranking is prompt-derived only, and prod says 0/169.
7. `SearchfoxCitation.repo` asymmetry vs `StructLayoutCitation.repo` (which defaults) — a real
   inconsistency, no consumer anywhere, never observed failing.
8. `system.md`'s JSON-shape line advertising only `low|medium|high` while the prose below offers
   `probable` — a live prompt/schema inconsistency the model has so far navigated correctly. Free
   to fix, unmeasured.
