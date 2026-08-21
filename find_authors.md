# Finding the right person to ask

**Status:** findings + a design proposal. Nothing here is implemented yet.
**Date:** 2026-08-12. **Prompted by:** crash `1311495c-ff9d-40bc-b011-4ec4e0260810`.

## The question

Crash `1311495c` is a `lead` at `probable` (rung 70, i.e. exactly `autofile.min_confidence`) with a
specific, source-verified mechanism and **no candidate regressor**. Autofile declined it:

```
agent: 1311495c-… not filed — no candidate regressor to file against
```

That is correct and it is the only reason it was declined — it passed every earlier gate
(`enabled`, verdict fileable, confidence ≥ 70, not off-stack-observe-only, symbolicated, not
already filed, under the daily cap, token present). `build_bug_preview` has exactly two returns
(`report_bug.py:1050-1143`) and the falsy one is `if not candidate or not candidate.get("node")`,
so the log message states the literal condition rather than a guess.

We want to file bugs like this anyway — a latent bug with no regressor is a legitimate finding, and
Jens Stutte's archetype guidance says so explicitly ("a latent shutdown bug with no recent regressor
is a perfectly good verdict, and better than naming a changeset"). Filing it means deciding **who to
put in front of it**, and getting that wrong is expensive: bug 2062119 is the precedent, where we
needinfo'd the author of a 2022 changeset that had nothing to do with the crash.

The specific failure mode to avoid, in Calixte's words: *"I frequently see Sylvestre Ledru in
suggested contacts, but it'd be, in general, a very bad choice, because Sylvestre mostly make
cosmetic patch or apply patch from static analysis tools."*

## 1. The problem is ours, and it is measurable

Across every `done` dossier in prod: **3426 contact suggestions covering 254 distinct people.**

| suggested | times | distinct changesets |
|---|---:|---:|
| Emilio Cobos Álvarez | 366 | |
| Jan de Mooij | 182 | |
| Jon Coppeard | 149 | |
| **Sylvestre Ledru** | **143** | **13** |
| Nicolas Silva | 131 | |
| Glenn Watson | 106 | |
| Mike Hommey | 81 | 5 |
| Steve Fink | 79 | |
| Yoshi Cheng-Hao Huang | 71 | |
| Jens Manuel Stutte | 67 | 1 |
| … | | |
| **"Lando"** | **49** | **5** |

Two distinct defects:

**(a) Ranking by authorship is not ranking by ownership.** `experts.area_experts`
(`crashclouseau/agent/experts.py`) takes `build_seed`'s ranked candidate changesets and returns the
authors of the top non-noise, non-backed-out ones. The only question it asks is "who authored a
changeset that scored onto this crash". A tree-wide mechanical patch — clang-format, a
static-analysis autofix, an `#include` sweep — lands in the pushlog window of *every* crash in that
window and therefore scores like ownership without being ownership. 143 suggestions came out of 13
changesets.

**(b) The bot filter has a hole.** `_BOT_MARKERS` covers `ffxbld`, `no-reply`, `noreply`,
`servo-vcs-sync`, `l10n-bumper`, `release+`, `cron@`, `seabld`, `tbirdbld`, `bot@` — but not
`lando`. `lando@lando.moz.tools` (46) and `lando@lando.test` (3) are a landing service account, and
we have suggested it 49 times as a person to ask.

The machinery is not useless, and it is worth being precise about that: for `1311495c` its two
suggestions were **Mike Hommey** (`873b3783a1a2`, bug 2050344) and **Jens Manuel Stutte**
(`477fcdf4fd84`, bug 2055964). Stutte is a genuinely excellent contact for a worker-shutdown crash —
this is the person whose feedback we encoded as an archetype. The problem is not that the list is
random; it is that the ranking has **no way to tell that contact apart from a mechanical one**.
(I did not check whether Hommey's changeset here was substantive or mechanical — that is not the
point, and no claim is made about it.)

## 2. Two discriminators that DO NOT work — measured, so nobody re-tries them

### 2a. Suggestions per distinct changeset — REFUTED

Hypothesis: a mechanical patch gets suggested for many unrelated crashes, so
`suggestions / distinct changesets` should be high for mechanical committers.

```
        who         | times | changesets | suggestions_per_changeset
--------------------+-------+------------+---------------------------
 Jens Manuel Stutte |    67 |          1 |                      67.0   <-- the RIGHT contact
 Carl Corcoran      |    33 |          1 |                      33.0
 Kai Engert         |    32 |          1 |                      32.0
 Gabriele Svelto    |    29 |          1 |                      29.0
 …
 Mike Hommey        |    81 |          5 |                      16.2
```

The metric measures **how busy the pushlog window was**, not how mechanical the patch was. One
changeset in a wide window gets attached to every crash in it. The person at the top of this
ranking is the one we most want to keep.

### 2b. Per-author changeset breadth from our own tables — REFUTED

Hypothesis: mechanical patches touch many files across many top-level directories, and
`changesets`/`files` already store per-node file lists locally (no network needed).

```
     author      | changesets | avg_files | max_files | avg_dirs | max_dirs
-----------------+------------+-----------+-----------+----------+----------
 Henrik Skupin   |         12 |     312.2 |      1853 |      1.6 |        4
 Mark Hammond    |          6 |      95.5 |       224 |      1.2 |        2
 Jamie Nicol     |         45 |      60.2 |      2365 |      1.2 |        2
 …
 agoloman        |         90 |      32.2 |      1871 |      1.6 |        5
 Mike Hommey     |         19 |      23.4 |       396 |      1.2 |        3
 Atila Butkovits |         68 |      21.8 |       690 |      1.6 |        6
```

The top of the list is **sheriffs and test-harness work**, not mechanical patchers, and Sylvestre
does not appear at all — the `changesets` table only holds nodes Clouseau actually parsed, so it is a
biased sample of the tree's history. Breadth might still work computed against real hg history, but
it cannot be computed from what we store, and on this evidence it separates "big change" from "small
change" rather than "mechanical" from "substantive".

**Conclusion: do not build the fix on statistically detecting the mechanical committer.** Both
obvious formulations fail, and the failure mode of a wrong prior here is a wrong needinfo.

## 3. What does work: stop inferring the person; ask for the designated owner

Mozilla ships the ownership mapping in-tree. Verified end to end for this crash:

```
dom/workers/ScriptLoader.cpp
  → dom/workers/moz.build:   with Files("**"): BUG_COMPONENT = ("Core", "DOM: Workers")
  → BMO GET /rest/product?names=Core&include_fields=components.name,components.triage_owner
      "DOM: Workers"      triage_owner = abienner@mozilla.com
      "DOM: Core & HTML"  triage_owner = mozilla@keithcirkel.co.uk
      "JavaScript: GC"    triage_owner = gtonietto@mozilla.com
```

Cost: one in-tree file read plus one REST call (159 components come back in a single request for
`Core`, so it caches trivially).

Why this is the right primitive:

* **Robust by construction, not statistically.** Reformatting every file in `dom/workers` does not
  change the triage owner. No discriminator to tune, no threshold to measure, no false positives of
  the Sylvestre kind — the signal simply is not derived from who touched the code.
* It is **the same routing Bugzilla itself uses**, so we are not inventing a second opinion about
  ownership.
* A triage owner is a **role somebody accepted**, unlike an inference about a person from their
  commit history.

Implementation subtlety: `BUG_COMPONENT` is set per-directory via `Files()` patterns, so resolution
walks up from the file's directory to the nearest matching `moz.build`. This is what
`mach file-info bugzilla-component` does.

**This also fixes a second gap.** `report_bug.resolve_product_component` is entirely
candidate-driven today — the regressor bug's component, else the most frequent component across the
regressor author's recent bugs — so with `candidate = null` it returns `(None, None)`. Without a
candidate we currently have no *component* either, not just no person. The `moz.build` path answers
both from the crashing file, which we always have.

## 4. The cautious filing design

**File into the right component and needinfo nobody.**

BMO already routes a new bug to the component's triage owner and watchers, so the bug reaches the
right team through normal triage with **zero** risk of naming the wrong person. A wrong needinfo
costs a developer an afternoon and costs us credibility; an un-needinfo'd bug in the right component
costs a triage cycle. That asymmetry is the whole argument.

Supporting evidence that needinfo is the risky half, already in the code: an unknown requestee fails
the entire `create` with BMO error 51 (`bugzilla-autofile` notes), which is why the existing path
resolves the address from hg before using it.

Reserve the needinfo for cases with a **specific** justification — i.e. the existing regressor path,
where we can point at a changeset and its author. For a no-regressor bug there is no such
justification by definition, so the honest thing is not to invent one.

Do not assume the triage owner wants a needinfo either. Filing in-component is a notification they
have opted into; a needinfo is a personal request.

## 5. If we later do want to name a person

Rank by **ownership evidence**, not authorship, in this order:

1. **Component triage owner** (§3) — a role, immune to blame noise.
2. **Reviewers rather than authors.** This inverts the problem directly: a mechanical patch is
   *authored* by whoever ran the tool and *reviewed* by the module owner. Mozilla commit messages
   carry `r=nick`, so the data is right there in the pushlog we already fetch.
3. **Who has been assigned and FIXED bugs in that component recently** — from BMO, not from hg.
   "Fixed a bug here" is a far stronger ownership signal than "touched a line here".
4. **Blame on the crashing lines** (`_crashing_area_experts`) — last, and only after excluding
   mechanical changes.

Exclusions that should apply at every level: service accounts (fix `_BOT_MARKERS` — see §6.1), and
commit-message markers of mechanical work (`clang-format`, `static analysis`, `Reformat`,
`Update … to version`, `Vendor`, `Import`, `Bump`, `no bug`, `NPOTB`, `Backed out`, `Merge`).

## 6. Next steps, smallest first

### 6.1 Fix the bot filter — trivial, and 49 bad suggestions already shipped
Add `lando` to `experts._BOT_MARKERS`, and audit the other 254 suggested identities for service
accounts. One line plus a test.

### 6.2 File-to-component resolution — the piece that makes `1311495c` fileable
`moz.build` `BUG_COMPONENT` walk-up + BMO triage-owner lookup, wired into `report_bug` so a
no-regressor lead can be filed into the right component with no needinfo. Needs: a cache (the
`Core` product response is ~159 components), a fallback when no `moz.build` matches, and a decision
about what `build_bug_preview` should require instead of `candidate.node`.

### 6.3 Validate before ever naming a person on a no-regressor bug
Same shape as the 289-bug regressor-strategy study (`spike/collect_regressor_dataset.py`,
`spike/STRATEGY_REPORT.md`). Take crash bugs already RESOLVED FIXED; ground truth is who actually
fixed them (patch author / assignee); score each strategy's top-1 and top-3 recall:

* today's `area_experts`
* component triage owner
* reviewer-weighted (`r=` on substantive changes to the crashing files)
* recent fixers in the component, from BMO

The question it answers is not "which strategy is best" but **"does a named needinfo beat
component-only filing at all"** — if it does not, §4 is the whole answer and §5 never gets built.

## Reproduction

Suggestion frequency (prod, read-only):

```sql
WITH e AS (SELECT jsonb_array_elements(d.payload->'dossier'->'area_experts') AS x
           FROM dossiers d
           WHERE d.status='done'
             AND jsonb_typeof(d.payload->'dossier'->'area_experts')='array')
SELECT x->>'name' AS suggested, x->>'email' AS email, count(*) AS times,
       count(DISTINCT x->>'node') AS changesets
FROM e GROUP BY 1,2 ORDER BY 3 DESC;
```

Per-author changeset breadth (§2b):

```sql
WITH cs AS (
  SELECT n.node, coalesce(nullif(h.real,''), h.email) AS author,
         count(DISTINCT c.fileid) AS files,
         count(DISTINCT split_part(f.name,'/',1)) AS top_dirs
  FROM nodes n JOIN hgauthors h ON h.id=n.hgauthor
  JOIN changesets c ON c.nodeid=n.id JOIN files f ON f.id=c.fileid
  WHERE n.merge IS NOT TRUE GROUP BY 1,2)
SELECT author, count(*) AS changesets, round(avg(files),1) AS avg_files, max(files) AS max_files,
       round(avg(top_dirs),1) AS avg_dirs, max(top_dirs) AS max_dirs
FROM cs GROUP BY 1 HAVING count(*) >= 2 ORDER BY avg_files DESC;
```

Component and triage owner (§3):

```sh
searchfox-cli --get-file dom/workers/moz.build | grep -A2 BUG_COMPONENT
curl -s 'https://bugzilla.mozilla.org/rest/product?names=Core&include_fields=components.name,components.triage_owner' \
  | python -c 'import json,sys; [print(c["name"], "->", c.get("triage_owner")) for c in json.load(sys.stdin)["products"][0]["components"]]'
```

## Code touched by any of this

| file | what |
|---|---|
| `crashclouseau/agent/experts.py` | `area_experts` (authorship ranking), `_BOT_MARKERS` |
| `crashclouseau/agent/orchestrator.py:363` | `_crashing_area_experts` (blame on crashing lines) |
| `crashclouseau/report_bug.py:634` | `resolve_product_component` — candidate-driven, returns `(None, None)` without one |
| `crashclouseau/report_bug.py:1050` | `build_bug_preview` — returns `None` iff no `candidate.node` |
| `crashclouseau/bugzilla_apply.py:581` | the `"no candidate regressor to file against"` skip |
