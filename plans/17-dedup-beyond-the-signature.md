# Plan #17 — Deduplication beyond the signature

> **Status:** design proposal (2026-08-12), not implemented. Every claim below is
> measured against crash-stats SuperSearch, BMO REST, and the tree at the crashing
> build's own revision. The triggering case is **bug 2063003**, filed by the canary
> on 2026-08-12T15:19:32Z, which is a duplicate of **bug 2062219** — resolved FIXED
> **11h16m earlier**, at 2026-08-12T04:03:56Z.

---

## 1. Summary

Clouseau filed a bug for crash `56e7a6b2-776d-4e66-9419-388b60260812` whose analysis
was *correct in every particular* — right regressor (bug 2043000 / `60d6dd5b849b`),
right mechanism (a raw `const StyleAtomString* mString` outliving its owning
`ComputedStyle`), right line (`layout/generic/TextOverflow.cpp:238`). It was still a
duplicate, and its own comment names the bug it duplicates, twice:

> the same author fixed this exact raw-pointer hazard the same day via
> `RefPtr<const nsAtom>` in bug 2062219

The failure is not in the analysis. It is that **every venue decision in the filer is
scoped to the Socorro signature**, and one defect does not produce one signature. The
two bugs are the same dangling pointer landing two lines apart:

| | bug 2062219 | bug 2063003 |
|---|---|---|
| signature | `nsAtom::IsStatic` | `nsCharTraits<T>::copy` |
| crash line | `TextOverflow.cpp:237` — `nsDependentAtomString str16(mString->AsAtom())` | `TextOverflow.cpp:238` — `DrawString(…, str16.get(), str16.Length(), …)` |
| where the garbage bites | reading the stale atom's header | header read survives, bogus length → oversized `memmove` |
| named regressor | bug 2043000 / `60d6dd5b849b` | bug 2043000 / `60d6dd5b849b` |

Faceting **every** Firefox crash whose stack passes through
`nsDisplayTextOverflowMarker::PaintTextToContext` (nightly, since 2026-08-01) by build
and signature gives the whole picture in nine rows:

```
20260811214300   8 | nsAtom::IsStatic(8)
20260811085340  26 | nsAtom::IsStatic(23), nsCharTraits<T>::copy(1),
                   | memcpy|nsCharTraits<T>::copy(1), _ZNSt3__1..str_find_first_of(1)
20260810202013  13 | nsAtom::IsStatic(13)
20260810154837  11 | nsAtom::IsStatic(11)
20260810093015   8 | nsAtom::IsStatic(7), FindSafeLength(1)
20260809214638  11 | nsAtom::IsStatic(11)
20260809094648  40 | nsAtom::IsStatic(40)
20260808210518  19 | nsAtom::IsStatic(19)
20260808093004   9 | nsAtom::IsStatic(9)     <- first build carrying bug 2043000
```

One dangling pointer, five signatures, **145 nightly crashes confined to the single
build window between the regressor landing and the fix** — nothing before
`20260808093004`, nothing after `20260811214300` (the fix merged to m-c at
2026-08-12T04:03). The crash we filed on is 1 of the 26 on its own build; the other 23
were already bug 2062219.

So three independent signals were available at filing time, and the filer consulted
none of them:

1. **The corroborator the analysis itself names.** `_explanation_comment` printed
   "bug 2062219" into the comment body while `_open_bugs_for_signature` searched for a
   different string.
2. **The proto-signature family.** "Is there an open bug on a stack through
   `PaintTextToContext` in this build window?" answers *yes, with 23 crashes on this
   very buildid*.
3. **A landed fix touching the cited line.** The verdict's own code reference points
   at `hg.mozilla.org/mozilla-central/file/1355b20fd8be/…#l237` — `1355b20fd8be` **is
   the fix for 2062219**, where that line already reads `mString.get()`. Clouseau
   quoted post-fix source as the regressor's modified line.

And the *output* of a dup decision is wrong even when the decision is right: closing a
bug as a duplicate without adding its signature to the target leaves the variant
signature looking unowned in Socorro, so the next report on it gets filed again.

---

## 2. What is broken, with file:line

| # | defect | where | consequence |
|---|---|---|---|
| A | venue lookup matches only the crash's own signature | `bugzilla_apply._open_bugs_for_signature` (`crashclouseau/bugzilla_apply.py:280`) | a same-defect bug on a sibling signature is invisible |
| B | lookup filters `resolution: "---"`, i.e. **open bugs only** | `bugzilla_apply.py:305` | a bug FIXED after this crash's build cannot suppress the filing — the exact case here |
| C | corroborated bugs never reach the venue decision | `report_bug._explanation_comment` renders `dossier["corroborations"]`; `_bug_for_this_regression` (`bugzilla_apply.py:391`) only receives `candidate_bug` | the pipeline prints the answer and discards it |
| D | the stats sentence is signature-scoped | `report_bug.build_stats_sentence` (`report_bug.py:245`) | "There is 1 crash" for a report belonging to a 26-crash cluster — a known cluster reads as a novelty, to the model *and* to the reviewer |
| E | a dup produces no signature addition | no code | Socorro keeps showing the variant signature as unowned; re-filing is inevitable |

Note the interaction of A and B, because it decides the order of work. Adding
`[@ nsCharTraits<T>::copy]` to bug 2062219 — correct on its own terms, and standard
triage practice — does **not** fix the next occurrence by itself: 2062219 is RESOLVED
FIXED, so defect B keeps it invisible to the lookup that would now match it. **B is
what turns the signature-field hygiene into a working gate**, which makes it the
highest-leverage single change here.

---

## 3. The work

### Step 1 — consider FIXED bugs whose fix postdates the crash's build (defect B)

The narrowest change with the largest effect. In `_open_bugs_for_signature`, stop
filtering to `resolution: "---"`; fetch resolution and, for a `FIXED` bug, resolve when
the fix landed (the `cf_last_resolved` field, or the pulsebot m-c push) and compare
against the crash's **build date**, which Clouseau already holds.

Decision table:

* fix landed **after** the crash's build date → the crash is a pre-fix report of an
  already-fixed defect. **Do not file, do not comment.** Record it against the fixed
  bug so the UI can show why nothing was filed.
* fix landed **before** the crash's build date → the defect is back, or this is a
  different cause on a reused signature. File, and say in the comment that the crash
  is from a build that *contains* the fix — that is a strong, useful statement.
* not `FIXED` (WONTFIX, INCOMPLETE, DUPLICATE) → ignore, as today.

Fail toward *not filing* on an unresolvable fix date, consistent with the existing
"fail closed" comment at `bugzilla_apply.py:312` — a missed filing is recoverable, a
duplicate on BMO costs a human's attention.

### Step 2 — feed corroborated bugs into the venue decision (defect C)

`_bug_for_this_regression` already has the right shape: `candidate_bug` outranks the
age test because "the bug the regressor was written FOR" is the venue whatever the
dates say. A bug the analysis cites as sharing the *same regressor and the same
crashing line* deserves exactly the same standing.

Extend the signature `(bugs, landed, max_age_days, candidate_bug=None)` with
`corroborated_bugs=()`, drawn from `dossier["corroborations"]`, and let a member of
that set win the same way `candidate_bug` does. Then union those ids into the candidate
venue list so they are considered even when the signature lookup never returned them —
which is the whole point, since in this case it could not.

Guard it: a corroborated bug qualifies only if it shares the crash's suspected
regressor **or** cites a file the verdict cites. A bare "bug N is related" in model
prose must not redirect a filing.

### Step 3 — widen the family from signature to proto-signature (defect A, D)

Add a SuperSearch by `proto_signature=~<crashing frame symbol>` restricted to the
regression build window, and use it for two things:

* **venue** — bugs already attached to any signature in that family;
* **the stats sentence** — report the *family* count next to the signature count.
  "There are 26 crashes in this stack on this buildid (1 under this signature)" is
  both true and the sentence that would have stopped this filing.

The frame symbol to key on is the topmost XUL frame the verdict cites, which the
dossier already carries as a `StackFrameCitation`.

### Step 4 — make the dup action include the signature addition (defect E)

When the filer decides an existing bug is the venue, or a human resolves one of our
bugs as a duplicate, the accompanying action is `bugzilla.update_bug` adding
`[@ <this crash's signature>]` to the target's `cf_crash_signature`. `_execute`
(`bugzilla_apply.py:470`) already supports `bugzilla.update_bug`, so this is a payload,
not a new capability. Append to the field; never rewrite it.

This is the step that keeps the loop closed: today's addition of
`[@ nsCharTraits<T>::copy]` to 2062219 is what lets Step 1's lookup catch the next
pre-fix report of this same defect.

---

## 4. Validation

The replay corpus makes this measurable without spending a filing. `spike/_dossier_dump.jsonl`
holds 1996 raw agent outputs and `spike/_recheck.py` replays them in about a second.

1. Replay every filing the canary has made against Steps 1–3 and count venue changes.
   Any filing that *becomes* a comment or a no-file must be inspected by hand — this
   is a precision-for-recall trade and the failure mode is suppressing a genuine new bug
   on a reused signature.
2. Specifically assert the 2063003 case: same inputs, same dossier, and the decision
   must become "already fixed by bug 2062219 in `1355b20fd8be`, not filing".
3. Assert the mirror case does **not** regress: a crash from a build that *contains*
   the fix must still file. `nsAtom::IsStatic` is the ideal fixture — it has bug 1798397
   open since 2022 and a 2026 regressor, which is the case `_bug_for_this_regression`
   was written for.

---

## 5. What this does not fix

The analysis quoted `1355b20fd8be`'s post-fix source as the regressor's modified line
and did not notice. Nothing in Steps 1–4 addresses that: the source-reading tools
resolve `tip` (see `postgres-enum-and-pinned-source-dead`, where `pin_rev` is always
`""`), so an agent reasoning about a crash from an older build reads code that has
moved underneath it. Pinning source reads to the crash's own build revision is a
separate and larger fix, and it would have produced a *third* independent catch here.
Worth its own plan.
