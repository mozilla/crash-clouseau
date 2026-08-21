# Clouseau × Fenix nightly — findings and plan

*Investigation 2026-08-11. Question asked: “add support for Fenix nightly; Fenix on
buildhub is broken, so we need another way to get the pushlog info for a given
buildid — what do we have to accomplish?”*

**Short answer:** the buildid→pushlog problem is real but *small*, and cheaper to solve
than expected — the Fenix nightly buildid **is** the mozilla-central push timestamp, so
one hg query resolves the revision and TaskCluster is only needed to answer *which
pushes produced an APK*. The work is everywhere else: a Postgres enum that cannot be
widened by the mechanism built to widen it, two crash-noise safety gates that are
structurally dead on Android, a product-blind dedup that can regress **desktop**
coverage, and an autofile path that would make Clouseau the dominant filer of Fenix
crash bugs on day one.

This document is self-contained. `plans/16-fenix-nightly-support.md` is the same plan in
the repo's `plans/` format; if you want a single doc, that one can be deleted.

---

## Contents

1. [Method and how to read the confidence tags](#1-method)
2. [The source-resolution problem — solved](#2-the-source-resolution-problem--solved)
3. [The real blocker: why Fenix produces zero rows today](#3-the-real-blocker)
4. [Is there anything worth triaging? (feasibility)](#4-feasibility)
5. [Coupling inventory — what breaks that has nothing to do with buildids](#5-coupling-inventory)
6. [Safety: which gates are dead on Fenix](#6-safety)
7. [Bugzilla specifics](#7-bugzilla)
8. [Kotlin / JVM](#8-kotlin--jvm)
9. [Pre-existing desktop defects found on the way](#9-pre-existing-desktop-defects)
10. [Refuted — do not spend time here again](#10-refuted)
11. [The plan](#11-the-plan)
12. [Out of scope](#12-out-of-scope)
13. [Decisions needed](#13-decisions-needed)
14. [Operational notes](#14-operational-notes)
15. [Appendix: reproduction commands](#15-appendix-reproduction-commands)

---

## 1. Method

Produced by a 13-agent fan-out over five dimensions (product coupling, source
resolution, Socorro feasibility, TaskCluster coverage, Kotlin toolchain), followed by
one adversarial refuter per dimension aimed at that dimension's own riskiest claim, two
independent plan proposals, and a completeness critic. **Three of the five riskiest
claims were refuted or materially corrected** — those corrections are folded into the
body below, and §10 records what was wrong so it is not re-litigated.

Confidence tags used throughout:

| tag | meaning |
|---|---|
| **[verified]** | I ran the command or read the code myself in this session |
| **[measured]** | measured by a fan-out agent, single source |
| **[measured ✓✓]** | measured, then independently reproduced by an adversarial verifier |
| **[corrected]** | the first measurement was wrong; this is the corrected figure |
| **[code]** | read out of the tree, file:line given |

Scratch and repro scripts for the fan-out numbers: `spike/_fenix_scratch/` (118 files,
untracked). Every hgmo request must send the allowlisted `crash-clouseau` User-Agent.

---

## 2. The source-resolution problem — solved

### 2.1 Buildhub genuinely has no Fenix **[verified]**

`source.product` buckets over all 1,792,717 docs:

```
firefox 1,295,137 · thunderbird 182,176 · devedition 175,380 · fennec 140,020 · flowstate 4
```

A full-text query for `fenix` returns **0 hits**. `buildhub.PRODS`
(`crashclouseau/buildhub.py:17`) has no Fenix entry, so `PRODS.get(x, x)` passes the
literal `"Fenix"` into an ES `terms: source.product` filter that matches nothing:

```
buildhub.get(2026-08-04, 'nightly', prods='Fenix')   → {}          (0 builds)
buildhub.get(2026-08-04, 'nightly', prods='Firefox') → 17 builds
```

So the premise in the request is correct, and no amount of patching `buildhub.py` fixes
it.

### 2.2 The structural fact that makes this cheap **[measured ✓✓]**

**The Fenix nightly buildid is the UTC push timestamp of the mozilla-central revision it
was built from.** Not a coincidence — the mechanism is in-tree:

* `taskcluster/gecko_taskgraph/decision.py:372` — `build_date = parameters["pushdate"] or int(time.time())`
* `:374` — `moz_build_date` is formatted from it via `gmtime`
* `.taskcluster.yml:279` — passes `--pushdate='${push.pushdate}'` on the branch covering
  hg-push, action **and** cron

Confirmed end-to-end on the decision tasks' own `public/parameters.yml`
(`moz_build_date: '20260722214024'`, `pushdate: 1784756424`, `tasks_for: cron`).

Two consequences:

1. **buildid → hg revision needs no TaskCluster at all.** One hg pushdate query gives it.
2. Because desktop nightly and Fenix nightly come off the *same* pushes, **Buildhub asked
   for `source.product=firefox` at the Fenix buildid returns the right revision** —
   114/114 agreement over 62 days, 0 fenix-only buildids **[measured ✓✓]**.

### 2.3 The verified chain **[verified]**

This is the load-bearing identity, because `inspector.inspect_stacktrace`
(`crashclouseau/inspector.py:172-173`) discards **the entire stack** when a frame's node
≠ the stored build node:

```
Fenix crash frame:  git:github.com/mozilla-firefox/firefox:mfbt/assertions.h:cb6222f0865b…
      │   inspector.get_path_node → GIT_PAT → inspector.git2hg (libmozdata Lando)
      ▼
hg rev:  b059f3919dbbc623e378ecec37edbb05f3d4f70a        ←── equal, verified
      ▲
      │   metadata.source of task FD1yHxTKR3m4VL5zZyaGjQ ("signing-apk-fenix-nightly")
index:  gecko.v2.mozilla-central.pushdate.2026.08.09.20260809094648.mobile.fenix-nightly
```

So the revision persisted in `nodes` must be **that hg hash**, and the git→hg bridge
already in the tree produces it. No new conversion layer is needed. (Fenix crash frames
carry the *git* hash, exactly like desktop post-migration.)

### 2.4 Three routes, ranked

| route | cost | coverage | use for |
|---|---|---|---|
| **hg pushdate** — buildid as UTC instant → the one m-c push at that instant | 1 hg GET | ~100% back to at least 2024-07 (9/9 sampled expired-index buildids still resolve) **[measured]** | revision; the only route that works for a historical corpus |
| **Buildhub as `source.product=firefox`** at the Fenix buildid | 1 POST, code already vendored | 114/114 over 62 days **[measured ✓✓]** | revision (cheapest — reuses `buildhub.get_rev_from`) |
| **TaskCluster index** `…pushdate.<Y>.<M>.<D>.<bid>.mobile.fenix-nightly` | 1 GET/build | 537/538 (99.81%) of buildids newer than 2025-11-01, covering 617,667 crashes **[measured ✓✓]** | **build existence + ordering** — the one thing nothing else answers |

### 2.5 What actually requires TaskCluster: the build **set**

Fenix does not build on every push desktop does. Over one 23-day span: **46
fenix-nightly builds vs 47 firefox-nightly buildids** **[measured ✓✓]**. The extra
desktop buildid (`20260805220233`) is a push with zero mobile tasks, so no APK.

Borrowing Firefox's build list for the *previous* build is therefore a trap:
`buildhub.get_two_last` returns the right revisions but the wrong predecessor for one
build in 45, and that one **collapsed the changeset window from 173 changesets to 2**
— a silent 99% truncation of the regressor search space **[measured]**. Resolve the
previous build from the `fenix-nightly` index leaf, never from the Firefox build list.

### 2.6 The enumeration recipe, with every gotcha that was actually hit

```
POST /api/index/v1/namespaces/gecko.v2.mozilla-central.pushdate.<Y>.<M>.<D>
       → children;  KEEP ONLY ^\d{14}$
GET  /api/index/v1/task/<ns>.<bid>.mobile.fenix-nightly    → 200 ⇒ a Fenix build exists
GET  /api/queue/v1/task/<taskId>                           → metadata.source → hg rev
```

Verified live for 2026-08-09: children `[20260809094648, 20260809214638, latest]` — i.e.
**two nightlies a day** **[verified]**.

* **`latest` is a real resolvable child.** `…pushdate.2026.08.10.latest.mobile.fenix-nightly`
  returns 200 with a real taskId, and in ASCII `'latest' > '20260810202013'`, so
  `max(children)` and `sorted(children)[-1]` both silently pick it. Filter on
  `^\d{14}$` — **not** on `!= 'latest'` **[measured ✓✓]**.
* **~30% of day children are not nightlies.** Over 23 days: 66 non-`latest` children,
  only 46 with a `fenix-nightly` leaf. 13 carry `fenix-nightly-simulation` +
  `fenix-debug` only; 6 have no `mobile.*` task at all. **Probe the leaf per candidate**;
  never take “the previous child” **[measured]**.
* **Multi-day gaps are real.** 2026-07-11 (Saturday) has an *empty* day namespace — no
  m-c pushes between 07-10 16:43 and 07-12 02:10, and no Fenix build. The walk-back
  resolved `cur=20260712113206 → prev=20260710095230` with `days_back=2`. Bound the
  walk and treat “no fenix-nightly within `max_back` days” as an **abstain**, not an
  exception **[measured]**.
* **Leaf choice matters.** `mobile.fenix-nightly` = `signing-apk-fenix-nightly`, the
  signed shipping APK. `fenix-nightly-simulation` is an on-push rehearsal (would resolve
  the *wrong* build); `android-*` is GeckoView, a different artifact; `focus-nightly` is
  Focus **[verified: all 20 leaves listed under one buildid's `.mobile` namespace]**.
* **hg vs git route.** Each task carries exactly two `revision.<40hex>` routes. hg is
  first in 46/46, but that is *insertion* order, not sorted — in 23 of 46 the first hash
  is lexically greater. **Never index `routes[]` positionally.** Read `metadata.source`
  (an `hg.mozilla.org/…/file/<rev>/…` URL) and cross-check the single
  `tc-treeherder.v2.*` route **[measured ✓✓]**.
* **`payload.env` is empty** on all 46 signing tasks **[verified]** — there is no
  revision in the payload. `GECKO_HEAD_REV` lives on the cron decision task
  (`extra.parent`) and on the same-push `android-*-opt` build, either of which works as
  a belt-and-braces cross-check **[measured]**.
* **No version in the index** (`data` is `{}`) **[verified]**, and `models.Build` needs
  one. Read `mobile/android/version.txt` at the build rev via hg-edge (returns
  `155.0a1`), or take it from the Socorro facet already in hand **[measured]**.
* **It is not a total function.** Over the full universe of 972 distinct Fenix nightly
  buildids the identity holds **971/972**. The exception is `20260202034059` (13-14
  reports, v149.0a1): the route 404s and its timestamp is not a push on m-c, autoland,
  try, beta, release, esr140 or unified — one device, one `install_time`, i.e. an APK
  **not built by Mozilla CI**, whose `MOZ_BUILD_DATE` is a wall clock (the in-tree
  analogue of `decision.py:372`'s `or int(time.time())` fallback). **Handle the 404 by
  abstaining** — do not walk back a day, do not guess, do not raise **[corrected;
  measured ✓✓]**.
* **`extra.index.rank` is unix seconds, not a buildid** (1786268808 for
  20260809094648). Code that reads `rank` expecting 14 digits breaks. The 14-digit form
  exists only in the route string and in `moz_build_date` **[corrected; measured ✓✓]**.
* **Retention cliffs at ~308 days** for the fenix family even though `expires`
  advertises 365. Irrelevant for nightly triage; fatal for a backfill — use the hg
  pushdate route there **[measured]**.
* **No rate limiting** across ~1,400 index/queue requests, 20-way concurrency fine,
  responses `no-store` (so cache `(buildid, revision, prev_buildid)` in `builds` as
  today) **[measured]**.
* **Respins are not a hazard** — checked because they looked like one. Push 44947 sat as
  tip for 18.5h and two `nightly-all` cron graphs ran on it, both with
  `moz_build_date: '20260722214024'`; the second decision task completed with
  `task-graph.json` = 0 entries, the whole graph optimised away. No second APK, index
  entry untouched **[measured ✓✓]**.
* **The buildid is a *push* clock, not a build clock.** `.cron.yml` schedules m-c
  `nightly-all` at 10:00 and 22:00, but decision tasks also appear at 04:22/13:24/16:02
  (catch-up runs), and a build can post-date its own buildid by up to ~6h. Harmless for
  the identity **[measured ✓✓]**.

### 2.7 Window size is a non-issue **[measured]**

The resulting Fenix nightly changeset window is median **95 changesets / 2 pushes**
(mean 105, p90 235, max 345; distinct files median 451) against a Firefox nightly
baseline of median **88** over the identical span — statistically indistinguishable,
because both products build off the same pushes. The agent reasons over the same volume
it does today. No candidate-set scaling problem.

### 2.8 Beta and release, for free **[measured]**

Same mechanism, different leaves — enumerated, not guessed:

| channel | repo | leaf |
|---|---|---|
| nightly | `mozilla-central` | `mobile.fenix-nightly` |
| beta | `mozilla-beta` | `mobile.fenix-beta` |
| release | `mozilla-release` | `mobile.fenix-release` |

Socorro's top beta and release buildids resolve 7/8 each, both misses being the
retention cliff. Note there is no `trunk.revision` route on those branches, and
`metadata.source` carries a `releases/` prefix plus a double slash.

---

## 3. The real blocker

Fenix produces **exactly zero rows** today, and the failure is silent **[code]**:

```
bin/schedule.py:13     every 20 min → update.update_all()
update.py:231          products = config.get_products()                     # ["Firefox"]
update.py:161          data = buildhub.get(date, channel, prods=product)    # {} for Fenix
update.py:162          if data: models.Build.put_data(data)                 # ← never runs
   …
update.py:184-186      bidid = models.Build.get_id(bid, channel, product)   # → None
update.py:186          errors.add(bid); continue                            # ← BEFORE UUID.add
```

One missing `builds` row drops 100% of the product, and the only trace is an info log:
`"No buildid in db for <bid>/Fenix/nightly"`. `builds → nodes` is where every downstream
revision comes from (`UUID.to_analyze`, `Build.get_two_last`, `Build.get_changeset`).

`update_builds` is the sole normal-operation writer of `builds`; `create.py:40` is the
only other caller (full DB recreate, and **`create.create()` has no live caller** — it
is not a Procfile entry point).

**Caller census** — only two of Buildhub's four functions are on the critical path
**[code]**:

| function | callers | criticality |
|---|---|---|
| `get` | `update.py:161`, `create.py:40` | **THE load-bearing call** |
| `get_rev_from` | `tools.py:11` (2nd leg of `tools.get_changeset`) | critical |
| `get_two_last` | `pushlog.py:104,118` | UI pushlog button; mirrored by `Build.get_two_last` for the agent window |
| `get_enclosing_builds` | `pushlog.py:128` | **unreachable from the shipped UI** — may return `[None, None]` |

`pushlog_for_buildid` is dead code (no caller in py/html/js).

---

## 4. Feasibility

### 4.1 Composition, Fenix nightly, 14 days **[verified]**

31,315 reports / 573 signatures:

| class | reports | distinct signatures |
|---|---|---|
| Java/Kotlin exceptions | **54.8%** | ~50% (284) |
| **native C++/Rust** | **24.7%** (~550/day) | **~45% (211)** |
| `EMPTY: no frame data available` | **20.5%** | 5 |

Top native signatures: `glean_sym … core::ptr::drop_in_place` (3,905),
`js::ArrayBufferObject::maxByteLengthGetterImpl` (1,815), `IPCError-browser | GPUProcessKill`
(270), `mozilla::dom::Navigation::UpdateEntriesForSameDocumentNavigation` (180),
`mozilla::extensions::StreamFilterParent::Init` (140).

> The first single-build sample I took showed 64% `EMPTY`, which read like a killer.
> That was a per-build artifact — over 14 days it is 20.5%.

**`proto_signature` and `java_stack_trace` are strictly mutually exclusive** (17,143
java / 7,764 proto / **zero** overlap) **[measured]**, and `uuids` rows are only ever
created from proto-signature facets. So a Java crash produces no uuid today and would
still produce none after this plan — which is what makes deferring JVM support a **no-op
rather than a regression**.

### 4.2 How much the selector would actually pick up — and the measurement trap **[corrected]**

This is the most important correction in the investigation.

A single **retrospective** replay of `get_new_signatures` + `utils.evaluate_days` over a
21-day window selects 104 (signature, build-day) pairs, of which only **36 are native**,
only 4 “fresh”, and 33 of 37 native build-days are one crash from one install. Read
straight, that says *there is no work here*, and one draft proposal built a `<1/week`
kill gate on it.

But `bin/schedule.py` runs `update_all()` **every 20 minutes** with `date=utcnow()`
(`update.py:170`), so every build-day is judged dozens of times **while still
immature** — where the bar is just the from-zero rule at `installs>=1`. The
`mature_after=5` / `mature_installs=4` gate and the untestable prefix, which produced
425 of the snapshot's declines, bite **only retrospectively**. Replaying the deployed
cadence over the same 21 build-days with the real `evaluate_days` and true per-report
arrival dates:

> **246 native (signature, build-day) pairs / 179 distinct native signatures
> ≈ 11-12/day, of which 58 pairs / 57 signatures are FRESH** (first seen within 7 days
> of the build), and 31 pairs carried `count>=3` at the moment of selection.

That is **6.5× the snapshot, and 14× on the fresh count.** Concrete fresh native spikes
the snapshot missed:

| signature | first seen | lifetime | why the snapshot declined it |
|---|---|---|---|
| `UiCompositorControllerParent::RecvRequestScreenPixels` | build 20260722214024 | 76 crashes / 13 install_times, SIGSEGV at 0x0, correct git path at the build rev | untestable prefix only |
| `MozSharedMap_Binding::CreateInterfaceObjects` | build 20260805092115 | 77 crashes, SIGILL | `mature_installs=4` > cardinality 2 |
| webrender `RenderBackend::process_transaction` | fresh | 4 crashes / 3 device models | — |

**Rule to carry forward: measure this selector at its deployed cadence, never
retrospectively.** Any go/no-go threshold derived the other way will kill the project on
an artifact.

What survives from the pessimistic read, and should temper expectations: native volume
is thin and singleton-heavy, multi-machine native pairs run ~7 signatures / 38
build-day pairs per 21 days versus **~1,000 pairs (48.7/day) for desktop**, and median
staleness is 273-288 days. **The problem is signal-to-noise and multi-machine
corroboration, not the absence of fresh native work.**

### 4.3 Native reports are fully usable **[measured]**

For the 24.8% of reports with a `proto_signature`, everything the seeding path needs is
present and correct: `json_dump` with `crash_info`/`crashing_thread`/`modules`/`threads`,
a top-level `crashing_thread` index, and frames with `module` (`libxul.so`), demangled
`function`, `file` as `git:github.com/mozilla-firefox/firefox:<path>:<40-char git sha>`,
and **accurate line numbers at the build revision**. Example: uuid
`0888c340-153c-4965-8306-9faa30260809`, frame 0
`mozilla::jni::Method<…>::Call<>` in `widget/android/jni/Accessors.h`.

### 4.4 Four upstream facts to write down

* **Socorro's stackwalker fails on Android at scale.** Counting only non-Java reports,
  8,059 of 21,328 (37.8%) have no `proto_signature` over 21 days, running **48-72% on
  recent crash days** and up to 83% on individual builds. Desktop control: 0.3-1.3%.
  Nothing Clouseau can do — worth filing against Socorro. It cuts the usable native
  sample to ~1/3 **[measured]**.
* **One device farm produces 48% of all reports.** The largest signature
  (`java.security.ProviderException … AndroidKeyStoreKeyGeneratorSpi.engineGenerateKey`)
  is 15,067 of 31,315 reports and resolves to **one** `android_model` — `A95XF4`, an
  Android TV box — at 99.7%, across 7 distinct install_times. The desktop “1 machine =
  81,843 of 86,196” trap reproduces harder. `mature_installs>=4` does gate these out of
  *selection*, but **any** volume metric, cost estimate or “is there work here” read
  must be distinct-install-per-day **[measured]**.
* **`install_time` does not mean the same thing on Android.** 14.6% of Fenix
  install_times span more than one buildid (max 11) vs 0.05% for Firefox, and **51.6% of
  native reports carry an install_time before 2010-01-01** (a clock reset). So
  `cardinality_install_time` — the axis `mature_installs` rests on — is a weak machine
  proxy, and `machine.py`'s calibration does not transfer **[measured ✓✓]**.
* **The single largest native family is not Gecko.** `glean_sym` / uniffi Rust ships
  into Fenix as a prebuilt from application-services / glean. **mozilla-central's
  pushlog cannot contain its regressor** — only a version-bump changeset. Either exclude
  that family explicitly or accept that those runs cannot succeed **[measured]**.

### 4.5 The build window needs no change **[measured]**

Report-weighted, Fenix looks like users never update: 1.4% of reports from builds ≤2
days old (Firefox 6.1%), 29.7% from builds >30 days old, with a 330-day-old buildid
contributing 1,998 reports. **Install-weighted, the two products are the same**
(≤7d: 29.5% Fenix vs 27.9% Firefox). The old-buildid distribution is a device-farm
artifact. **Explicitly resist widening `nightly_window_ndays`.**

---

## 5. Coupling inventory

| # | area | file:line | what happens |
|---|---|---|---|
| 1 | **`PRODUCT_TYPE` Postgres enum** | `models.py:18` | Native named enum, created once. `create_all()` only runs when the DB is fresh (`models.py:2729-2739`). Adding `"Fenix"` to config does **not** widen it, and both INSERTs **and SELECTs** on `product='Fenix'` then raise `DataError: invalid input value for enum`. Reads too — `Build.get_id`, `Build.get_two_last`, `Signature.get_reports`, `Verdict.map_for_build`, `UUID.get_uuids_from_buildid` all filter `Build.product ==`, so the UI/API 500s. **[measured ✓✓ against PG 16.14]** |
| 2 | ↳ and it is **one-way** | — | `ALTER TYPE … DROP VALUE` does not exist, and once a Fenix row exists, removing `"Fenix"` from config breaks every ORM read of it (`Enum.result_processor` raises `LookupError`). **`config.products` is NOT a kill switch.** |
| 3 | product-keyed config | `config.py:144-151`, `177-187` | All six blocks **silently default** for an unknown product — no exception, no warning. Measured: installs 1 (Firefox 1), **protos 1 (Firefox 50)**, floor 5 (Firefox 3), ratio 3/3, mature_after 5/5, mature_installs 4/4. Perverse incentive: that `protos` default is currently the *only* bound on Fenix uuid creation, so “fixing” it to 50 is what **un-bounds** the spend. **[measured]** |
| 4 | no `INGEST_PRODUCTS` hatch | `update.py:231` vs `:233` | Channels have `$INGEST_CHANNELS` *precisely because* config also defines the enum (docstring at `:226-230` says so). Products don't. So the enum widening and the ingestion switch are **the same edit**. **[code]** |
| 5 | no product gate on the agent | `orchestrator.py:2471-2489` | `enqueue_agent` gates on `agent_enabled` + `agent_channels` + proto-dedup only. No product check anywhere in `agent/**`, no product-keyed cost cap. Product *does* flow correctly through the seed (`orchestrator.py:606,626,684`, `second_opinion.py:125`). **[code]** |
| 6 | no product gate on autofile | `bugzilla_apply.py:510` | `AUTOFILE_BUGS=1` is armed in prod at rung 70 with needinfo. So ingestion, ~$2/crash triage and unattended Bugzilla filing are **one switch**. **[code]** |
| 7 | Java path gated on a literal | `datacollector.py:236-238`, `:385` | `if product == "Fennec"` → `get_uuids_fennec`, itself hardcoding `"product": "Fennec"`. That branch exists because Java crashes have no proto-signature (comment at `:237`). **[code]** |
| 8 | `.kt`/`.kts` not interesting | `interesting_extensions.json`, `utils.py:120-122` | Lists c/h/H/cpp/cc/cxx/hh/hpp/hxx/java/rs/mm/m — no `kt`. **0 of 29 files across three real Fenix changesets survive**; 341 of 4,734 window changesets (7%) touch `.kt` and nothing configured (median 6/window, p90 17). It is the default `file_filter` for `pushlog.pushlog` → `files`/`changesets` tables, so a Kotlin changeset gets a `Node` row with **zero** `Changeset` rows. **[measured]** |
| 9 | patch extraction misreads Kotlin | `patch_extract.py:261-265`, `:287-289` | No `kt` in `_LANG_BY_EXT`; `lang_for` falls back to `"cpp"`. Kotlin keywords leak into the identifier set, `change_tags` fires `deref` on **every lambda**, `_func_name` reports the **superclass** as the enclosing function. All silent. Measured on a real 19-file Kotlin changeset. **[measured]** |
| 10 | eval/corpus excludes Fenix | `eval/corpus.py:66-71`, `eval/study_corpus.py:45-53,156-158` | Hard-rejects any `java_stack_trace` and any product ≠ `"Firefox"`, and stamps `"product": "Firefox"`. So there is **no measurement path for Fenix quality**, and the calibration table `{25:.5, 50:.8, 70:.9714, 85:.9714}` was fit on corpora that exclude it. **[code]** |
| 11 | `parsepatch` comment stripping | `patch.py` | **Already correct for Kotlin** — verified, no work. **[measured]** |
| — | UI / API | `html.py:75-86,295-306`; `api.py`; `templates/` | **Fine.** Product dropdown is built from DB rows (`models.UUID.get_buildids()`); `"Firefox"` appears only as a request-arg default, immediately corrected. Only `html.pushlog:581,587` defaults to `"Firefox"` and would mis-link. Note `Build.get_products` orders `product.desc()` → on a native enum that is `enumsortorder`, so an appended `Fenix` sorts **after** `Firefox`. **[code]** |
| — | Bugzilla filing | `report_bug.py:634-665` | **Fine.** `resolve_product_component` is fully data-driven off the regressor bug's own product::component, with a fallback to the author's recent bugs. Nothing hardcoded to Firefox/Core. **[code]** |
| — | hg-edge source reads | `hgedge.py:79-98` | **Fine and language-agnostic** — verified working on a Fenix Kotlin path at a pinned build rev. **[measured]** |

---

## 6. Safety

This is the part neither draft proposal had, and it is the real precision argument.

* **The bit-flip gate is inert on Fenix.** `_apply_bit_flip_gate`
  (`orchestrator.py:1588-1663`) needs `possible_bit_flips_max_confidence >= 50`. Among
  native nightly crashes: **Fenix 1 of 4,091 carries the field at all, 0 at ≥50.**
  Firefox: 684 of 7,546 carry it, **326 (4.3%) at ≥50** **[measured]**.
* **The bad-machine gate is inert on Fenix.** `_apply_bad_machine_gate`
  (`orchestrator.py:1466+`) returns early when `cpus is None`. **`cpu_info` is NULL on
  95.7% of native Fenix crashes** (7,435 of 7,767). Where the diversity half *would*
  fire, it lands on colliding clock-reset install_times whose `cpu_info` facet is
  empty **[measured]**.
* Both gates exist for **exactly** the population Fenix consists of — singleton crashes
  from one install (33 of 37 native selected build-days). Knock-on:
  `models._INSTANCE_SUPPRESSED = ("bad_machine_suppressed", "possible_bit_flip_suppressed")`
  is the proto-dedup exemption, so that mechanism is moot on Fenix too.
* **The stale-signature gate is the one gate that works.** `sigage` is correctly
  product-scoped — verified: `Navigation::UpdateEntriesForSameDocumentNavigation`
  first-seen `20251110211328` on Fenix vs `20251202211324` on Firefox (22 days apart);
  `UiCompositorControllerParent::RecvRequestScreenPixels` first-seen on Fenix, **0 hits
  on Firefox** — **and it will fire on ~85% of Fenix leads** **[measured]**.
  *That asymmetry is the Fenix precision profile: the one working gate suppresses almost
  everything, and the two that would discriminate noise are dead.*
* **Proto-dedup is product-blind, so enabling Fenix can silently regress DESKTOP
  coverage.** `UUID.proto_already_analyzed` (`models.py:1207-1228`) filters
  `signatureid`, `protohash`, `Dossier.status == "done"` and the suppression flags —
  **no product, no channel clause** **[verified]**. Whichever product's uuid lands first
  closes the cluster for the other, forever. **61 of 280 Fenix native signatures
  (21.8%) also occur in Firefox nightly**, including **4 of the 7** chronic signatures
  that constitute Fenix's entire multi-machine ceiling, and at least one **byte-identical
  `protohash`** across products was confirmed (`ModuleLoaderBase::RegisterImportMap`)
  **[measured]**.
* **The desktop-fitted probability is published to Bugzilla, not just the UI badge.**
  `report_bug._worth_phrase` (`report_bug.py:766-777`) writes
  `", N% worth investigating — a calibrated estimate that this is worth someone's time"`
  into the filed comment, and `_apply_calibration` (`orchestrator.py:1080-1097`) is
  product-blind **[verified]**. Cheapest correct fix is the behaviour the function
  already implements for a missing number: **return `""`** for a product with no fitted
  table.
* **The autofile daily cap is global.** `Dossier.filed_bugs_since` counts every dossier
  with a `filed_bug` key regardless of product; `daily_cap` is a single `10`. So Fenix
  and Firefox compete for one cap **in both directions** **[measured]**.
* **The off-stack rescue is OFF.** `agent.offstack.enabled = false` **[verified]**, and
  `build_seed` does `if is_offstack and not offstack_cfg["enabled"]: return None`
  (`orchestrator.py:433-436`) — the run is skipped. The unfiltered
  `file_filter=lambda f: True` window is reachable **only** from `_offstack_candidates`.
  Since 29-32 of 36 Fenix native signatures are stale, expect the dossier rate to sit
  **far below** the selection rate — a free cost control, but it means “few Fenix
  dossiers” will **not** distinguish working from broken unless
  `"agent: no scored changesets"` is counted separately **[code]**.
* **No daily cost cap exists anywhere.** `max_cost_usd_per_crash` (2.0) and
  `_offstack` (4.0) are per-crash and **log-only** — `orchestrator.py:2165-2175` records
  `over_budget` *after* the run; nothing aborts **[code]**.
* **`Dossier`/`Verdict`/`Feedback`/`Archetype` have no product column**
  (`models.py:1689-1711`, `2304-2323`, `2588-2617`, `2449`), so `Feedback.scoreboard()`
  pools Fenix and Firefox into one “are we right?” number and scores desktop-learned
  archetypes against Fenix outcomes **[code]**.

---

## 7. Bugzilla

* **The BMO product is `Firefox for Android`** (37 components), **not `Fenix`** —
  `names=Fenix` returns 0 products. `GeckoView` (6 components) and `Focus` (3) also
  exist. `Trunk` is an active version in all, so `_bug_version("nightly")` is fine
  **[measured]**.
* **Socorro offers two destinations and `get_bz_query` silently takes the first.** A
  Fenix crash report page carries 4 `enter_bug.cgi` links, two with `keywords=crash`:
  `product=Firefox for Android` and `product=GeckoView`. `report_bug.get_bz_query`
  (`report_bug.py:28-43`) returns the **first**, so the GeckoView-vs-Fenix attribution
  question is resolved by accident. Meanwhile `autofile` → `resolve_product_component`
  inherits the **regressor bug's** product, which for a Gecko regressor is `Core`
  (arguably correct). **The two filing paths disagree and nobody chose** **[measured]**.
* **The mobile queue is live and the conventions fit.** `Firefox for Android` has 37
  crash-keyword bugs in 180 days (23 RESOLVED + 5 VERIFIED, 24 FIXED) and **181
  `regression`-keyword bugs** (105 RESOLVED + 53 VERIFIED). So `keywords="crash,regression"`
  and the needinfo workflow are idiomatic **[measured]**.
* **But at ~0.2 organic crash bugs/day vs Clouseau's ~3/day, Clouseau would outfile the
  organic Fenix crash-bug rate ~15× on day one** **[measured]**.
* **~40% of recent Fenix changeset authors cannot be needinfo'd**: 4 of 20 are
  `release+landoscript@mozilla.com` (the Lando bot — l10n imports and
  application-services version bumps, i.e. exactly what an off-stack window surfaces),
  4 of 20 are GitHub `noreply` addresses. Desktop control (`dom/**`): 2 of 20, no bots.
  **Good news:** this does **not** hit the known “unknown requestee kills the whole
  create (code 51)” trap — `report_bug._needinfo_account` (`report_bug.py:917-940`)
  verifies the address, falls back through the regressor bug's assignee/creator and the
  author's other bugs, then files **with no flag** rather than filing no bug. So the
  failure mode is a bug landing on a mobile queue **with nobody asked** **[measured]**.

---

## 8. Kotlin / JVM

### 8.1 “Is Kotlin indexed in searchfox?” has two different answers

**Kotlin *is* indexed on `firefox-main`** — files are browsable, blame works, full-text
search works. What `firefox-main` does **not** carry is **semantic analysis records**:
the definitions/callers/callees that `--define`, `--calls-from` and `--calls-to`
actually read. Measured on one file,
`mobile/android/android-components/components/browser/domains/.../CustomDomains.kt`
**[verified]**:

| tree | page | `data-symbols` records | `id:CustomDomains` |
|---|---|---|---|
| `firefox-main` | HTTP 200, browsable | **0** | **0 hits** |
| `firefox-beta` | HTTP 200 | **81** | 10 hits (7 by scip symbol) |
| `firefox-release` | HTTP 200 | **81** | — |
| `firefox-main`, `dom/base/nsINode.cpp` (C++ control) | HTTP 200 | **8,454** | — |

Straight through the tool the agent uses **[verified]**:

```
searchfox-cli -R mozilla-central --define 'mozilla::components::browser::domains::CustomDomains::load'
  → ERROR No potential definitions found
searchfox-cli -R mozilla-beta    --define 'mozilla::components::browser::domains::CustomDomains::load'
  → >>> 25:     fun load(context: Context): List<String> =
```

So: **searchfox has a live Java *and* Kotlin semantic indexer, serving on `firefox-beta`,
`firefox-release` and `firefox-esr140`** — trees the adapter can already target
(`Repo.BETA = "mozilla-beta"`, `searchfox.py:130`; every agent tool takes `repo=`). The
repo's **own unmodified client** returns a commit-pinned Kotlin definition permalink
plus a **138-edge call graph with per-edge citations reaching into `mozilla.components.*`**
**[measured ✓✓]**. Two working spellings: the `::`-joined pretty name
`org::mozilla::fenix::HomeActivity::onCreate` and the raw scip symbol
`S_jvm_org/mozilla/fenix/HomeActivity#onCreate().`.

**But the beta workaround has real drift, which is why it is not a free fix for a
nightly product** **[verified]**: the trees are pinned to their own branch tips —
`firefox-main` at `fca1efcadb9d` / `155.0a1`, `firefox-beta` at `cd001e124b15` /
`154.0b9`, a full release cycle behind. A Kotlin regressor that landed on nightly *this
week* is not in the beta index at all, which is exactly the population Fenix nightly
triage is about.

Two further caveats: `--calls-to` coverage for Kotlin is thinner than `--calls-from`
(interface dispatch yields no callers), and **`--field-layout` genuinely is C++-only**.
Also do not build on “main can never have this”: mozilla-central's own
`android-aarch64-searchfox-debug` task publishes a 43 MB
`target.mozsearch-java-index.zip` today, and mozsearch fetches it as an **optional**
artifact (`|| true`) — so main's JVM semantics could appear without notice
**[measured ✓✓]**.

### 8.2 The real blocker is R8 **[measured]**

Fenix nightly APKs are minified, and `java_stack_trace` line numbers are the remapped
ones. Verified at the exact build revision (buildid 20260724090143 → hg
`dc7f12a8cbce`): frame
`mozilla.components.lib.dataprotect.Keystore.generateKey(Keystore.kt:269)` — line 269 of
that file is a **KDoc close-comment `*/`**, while the real function is at line **232**.
The *file* is right (FQCN → in-tree path maps cleanly); the *line* is wrong. Every frame
checked was wrong. Line-granularity attribution needs the per-build R8/proguard mapping
artifact plus a deobfuscation step — a new subsystem.

### 8.3 And the Java path is structurally dead anyway **[measured / code]**

* `inspector.get_crash_info` (`inspector.py:93-121`) checks `java_stack_trace` **first**
  and never looks at `json_dump` if it is present; the off-stack escape hatch lives
  **only** in the native `else` branch.
* A real Fenix Java crash has **no `json_dump` at all** (verified on production crash
  `21cb28fc-8dcd-4d87-a70d-1c3540260804`).
* `java.inspect_java_stacktrace` marks a frame internal only when the FQCN starts with
  `org.mozilla.` (`java.py:57,95`) — but the dominant modern package is
  **`mozilla.components.*`** (7,840 of 20,253 sampled frames, **no `org.` prefix**),
  where the top real Fenix `IllegalStateException` lives; `mozilla.appservices.*` is
  also rejected. Only 42.9% of sampled Fenix java crashes contain *any* `org.mozilla.*`
  frame, while 97.9% contain a Mozilla frame.
* So the run attributes zero frames and ends `useless=True` — **the agent is never
  enqueued**.
* `java.py`'s file-discovery half walks `api.github.com/repos/mozilla/gecko-dev`, which
  is **archived** (`archived: True`, last push 2025-07-09), keeps only `.java`, roots at
  `mobile/android`, and **has no live caller**. `java.py:80` also passes the literal
  `"FennecAndroid"`, which is not a `PRODUCT_TYPE` label — so `/api/javast` raises
  `invalid input value for enum` on Postgres today.
* Good news: `FailureClass._missing_` (`schema.py:58-73`) is a **total function**, so a
  Kotlin exception dossier validates rather than being destroyed — the required-field
  trap that cost 41 verdicts does not reappear here. Every JVM crash lands in `other`.

### 8.4 Recommendation

**Cut JVM/Kotlin from the first landing**, on two independent grounds: the R8 line
remapping, and semantic coverage that is either absent (main) or a release cycle stale
(beta). Cutting it costs nothing measurable, because a Java crash produces no uuid today
either. Record the searchfox correction so the “Kotlin is capped at `lead` because
searchfox can't see it” claim is not recycled.

---

## 9. Pre-existing desktop defects

**None of these are Fenix work.** All are live in production for Firefox today, and the
Fenix measurement in the later phases is meaningless until §9.2 is fixed.

### 9.1 `_ensure_enum_values()` can never add an enum value — so `_ENUM_ADDITIONS` has NEVER fired **[verified]**

`models.py:2691-2726` opens `with engine.connect() as conn`, runs
`SELECT 1 FROM pg_enum …`, and only on the **miss** path does
`conn = conn.execution_options(isolation_level="AUTOCOMMIT")` before `ALTER TYPE`. The
SELECT has already autobegun a transaction, and SQLAlchemy raises **unconditionally** in
that state — the pinned 2.0.51, `sqlalchemy/engine/default.py:673-686`:

```python
if connection.in_transaction():
    trans_objs = [(name, obj) for name, obj, _ in characteristic_values if obj.transactional]
    if trans_objs:
        raise exc.InvalidRequestError(
            "This connection has already initialized a SQLAlchemy Transaction() object "
            "via begin() or autobegin; %s may not be altered unless rollback() or "
            "commit() is called first." % …)
```

The `except Exception` at `:2722` swallows it into a `logger.warning`. Reproduced against
PostgreSQL 16.14 through the real `models.create()` **[measured ✓✓]**.

The `exists` fast-path masked this forever: on a fresh DB `create_all` already installed
every label, so the DDL branch has **never once succeeded** — which means the existing
`{"VERDICT_TYPE": ("lead",)}` entry never migrated anything either, and prod got `lead`
from a fresh `create_all`. **`DEPLOY.md:8` claims `models.create()` “adds the `lead` enum
value”; that line is wrong** and must be corrected or the next person trusts it again.

Fix: issue the `ALTER` on a **separate** connection opened with
`.execution_options(isolation_level="AUTOCOMMIT")` (~4 lines). The sqlite suite
structurally cannot catch it (`:2701` returns early on non-Postgres), so this needs a
Postgres-backed test.

### 9.2 `pin_rev` is always `""`, and hg `tip` no longer contains source **[verified]**

`orchestrator.py:572-581` pins blame/history/source reads to the build rev only
`if inspector.git2hg(build_node)`. But `build_node` comes from `nodes.node`, which is an
**hg** short rev (Buildhub gives hg; `get_path_node` converts crash-frame git→hg — which
is precisely why `inspect_stacktrace`'s `node != build_node` check can ever pass).
`git2hg` maps git→hg only:

```
hg short  "b059f3919dbb"        → LandoMissingCommit → ""
hg full   "b059f3919dbbc623…"   → LandoMissingCommit → ""
git full  "cb6222f0865b6702…"   → "b059f3919dbbc623e378ecec37edbb05f3d4f70a"   (control)
```

So `pin_rev` is `""` on **every** run, and `pin_node`
(`agent/tools/__init__.py:18-20`) returns `"tip"`. And hg mozilla-central's `tip` is now
a **tags-only** commit — `03e0c921eb04`, *“No bug - Tagging … FIREFOX_153_0_4_RELEASE”*,
by `ffxbld@lando.moz.tools`, touching exactly one file, `.hgtags`:

```
raw-file/tip/dom/base/nsINode.cpp      → HTTP 404
raw-file/default/dom/base/nsINode.cpp  → HTTP 200
```

**Net effect: `mcp__source__raw_file` and `mcp__history__blame` return nothing on every
on-stack run today, desktop included.** Two independent one-liners: pin to `build_node`
directly (it is already the hg flavour hg-edge wants), and make the unpinned fallback
`default` rather than `tip`.

### 9.3 `datacollector.get_changeset` has returned `None` for every build since ~2025-11-10 **[measured ✓✓]**

Its filter is `'@"hg:hg.mozilla.org/".*:[0-9a-f]+'` (`datacollector.py:442`), a
pre-migration shape. Firefox nightly ≥2026-08-04: 322 hits of 7,651 with the hg filter
vs **4,656** with the git shape; faceting those 322 by build_id, the newest matching
buildid *anywhere* is `20251110211328`.

State it precisely: **zero for any buildid younger than ~2025-11-10, for both
products.** (An aggregate “61 Fenix / 355 Firefox hits” is entirely stale pre-migration
buildids and will mislead a fixer — two dimensions disagreed on this and both were
right.) This is the **third leg** of `tools.get_changeset`, i.e. the fallback that would
have covered a Buildhub-less product. Fix: pin the repo in the git shape and route the
captured sha through `inspector.git2hg` before `utils.short_rev`.

### 9.4 Two never-called entry points **[verified]**

* **`bin/feedback.py` has never run on a schedule.** `bin/schedule.py` has exactly two
  jobs (`update_all` /20min, `reap_stale_agent_jobs` /15min) and the Procfile has no
  entry — despite `bin/feedback.py` saying it is “safe to run on a schedule”. Any exit
  criterion phrased as “Bugzilla-verified outcome” depends on a loop that is not wired.
* **`archetypes.seed_quietly()` is called only from `bin/init.py`**, which is not a
  Procfile entry point — `bin/release.py` runs only `models.create()` +
  `HGAuthor.get_default_id()`. If it was never run by hand, `_matching_archetypes`
  returns `[]` on every prod run and the whole archetype layer is inert. Same
  never-called-entry-point class as `create.create()`.

> All four are the same diagnostic question that found the orphan reaper and the RQ
> retry: **“has this ever actually fired?”** asked of prod state rather than of the code.
> That question is now 5 for 5.

---

## 10. Refuted

* ~~“There is essentially no spiking native regression work in Fenix nightly, and no
  retuning can create it.”~~ **Instrument error** — measured retrospectively instead of
  at the deployed 20-minute cadence. Real figure: 246 native pairs / 179 signatures per
  21 days, 58 pairs fresh (not 4). A `<1/week` kill gate would have closed the project
  on this. §4.2.
* ~~“searchfox has no semantic index for Kotlin or Java; Fenix Kotlin is structurally
  capped at `lead`.”~~ A per-**tree** gap, not a language gap. §8.1. **Phrase it
  carefully:** Kotlin *is* indexed and browsable on `firefox-main`; what main lacks is
  the *semantic* records. Saying “Kotlin isn't indexed in main” is wrong and will be
  corrected by anyone who opens a `.kt` file there.
* ~~“The buildid→pushdate identity is total, so a day-bucket error is structurally
  impossible.”~~ Structural for **CI** builds (mechanism found at `decision.py:372`),
  but 971/972 in the wild — one non-CI APK exists in prod today. §2.6.
* ~~`extra.index.rank == buildid`~~ — `rank` is unix seconds equal to the push date.
* ~~Respins could produce a duplicate APK at the same buildid~~ — the exact
  configuration was found and is disarmed (second graph fully optimised away). §2.6.
* ~~“Deferring `.kt` is safe because the off-stack window already passes
  `file_filter=lambda f: True`.”~~ That path is behind `offstack.enabled=false`. The cut
  is still safe, but for a different reason: a Kotlin frame yields no uuid. §6.

---

## 11. The plan

Ordering principles: **the irreversible step ships alone**; the gates exist before the
data that could arm them; nothing costs money until a human has looked at free output;
and the desktop-value fixes land first, so the investigation pays for itself even if
Fenix is abandoned.

### Phase 0 — land the pre-existing defects (S) · *value is desktop; ships regardless*

* Fix `_ensure_enum_values` (separate AUTOCOMMIT connection) + a Postgres-backed test.
  Correct `DEPLOY.md:8`.
* Fix `pin_rev` (pin to `build_node` directly) and the `tip` → `default` fallback.
* Fix `datacollector.get_changeset`'s regex (repo-pinned git shape + `git2hg`).
* Fix `java.py:80`'s `"FennecAndroid"` literal.
* Add a scheduler entry for `bin/feedback.py`; call `archetypes.seed_quietly()` from
  `bin/release.py` (or confirm it was run by hand).

**Exit:** the Postgres test fails before / passes after; on the canary,
`"raw_file: not found or fetch failed"` goes from ~every source-reading run to 0;
`get_changeset` resolves a 2026 desktop nightly buildid.

### Phase 1 — product gates, before any Fenix row can exist (S)

* `INGEST_PRODUCTS` hatch at `update.py:231`, mirroring `INGEST_CHANNELS`.
* `agent.products` config key + gate in `enqueue_agent`.
* `autofile.products` gate in `autofile_bug`, and a **per-product** `daily_cap`.
* Decide and implement the proto-dedup product clause (`models.py:1207-1228`).
* `_worth_phrase`/`_apply_calibration` publish **no number** for a product with no
  fitted table.

**Exit:** tests prove each gate independently — Fenix `builds`+`uuid` rows with zero
agent enqueues; Fenix dossiers with zero Bugzilla writes.

### Phase 2 — widen the enum, alone, with no behaviour change (S)

* `_ENUM_ADDITIONS["PRODUCT_TYPE"] = ("Fenix",)`, run by the release phase, relying on
  the Phase 0 fix. Hand-run `ALTER TYPE "PRODUCT_TYPE" ADD VALUE 'Fenix'` on the canary
  first as belt-and-braces (outside a transaction).
* Set `INGEST_PRODUCTS="Firefox"` **before** the config-file deploy, not after.
* State the irreversibility in the commit message.

**Exit:** `pg_enum` shows `Firefox, Fenix`; **no** `could not ensure enum` warning in the
release log; `/reports.html` still 200.

### Phase 3 — the Fenix build source, stopping at `builds` rows (M)

* New `crashclouseau/tcindex.py` exposing Buildhub's exact interface so it drops into
  the two call sites that matter (`update.py:161`, `tools.py:11`):
  * `get(min_buildid, channel, prods, max_buildid=None) → {"Fenix": {"nightly": {tz-aware UTC datetime: {"revision": <12-char hg>, "version": str}}}}` — the product key must be the Socorro name, because `put_data` writes it straight into the `PRODUCT_TYPE` column.
  * `get_rev_from(buildid, channel, product)`.
  * `get_two_last` from the **fenix-nightly index leaf**, per §2.6.
  * `get_enclosing_builds` may return `[None, None]` — unreachable from the shipped UI.
* Revision from Buildhub-as-`firefox` or the hg pushdate route; **existence and ordering
  from the TC index**; version from `mobile/android/version.txt`.
* Dispatch on product; Firefox keeps its current path untouched.

**Exit:** on the canary — (1) `builds` rows for `product='Fenix'` are **set-equal** to
the TC index's `fenix-nightly` leaves for those days and contain **no desktop-only
buildid**; (2) every `nodes.node` equals the route's hg rev; (3) `"No buildid in db"` for
Fenix drops to ~0; (4) a regression test pins the 173→2 truncation case.

### Phase 4 — Fenix native ingestion, agent still OFF (M)

* Add `"Fenix"` sub-blocks to all six product-keyed config blocks with **deliberately
  chosen** values — state the `protos` value as a cost decision (§5 #3).
* Selector class filter: skip signatures with no `proto_signature` before
  `get_proto_small` (64% of Fenix selections yield zero protos — pure query savings).
* Keep `machine.py`'s suppressor off Fenix, or re-key it on `android_fingerprint`
  (cardinality 1,683) rather than `install_time`.
* Add `.kt`/`.kts` to `interesting_extensions.json` **and** `kt/kts → kotlin` to
  `patch_extract._LANG_BY_EXT` with a Kotlin keyword set — **these go together**;
  adding the extension without the language mapping is what produces the silent
  `deref`-on-every-lambda and superclass-as-function misreads.

**Exit:** Fenix uuids and scored changesets visible on `reports.html`; counts reconcile
(reports ≈ selections × protos); the `selection` table shows the EMPTY/JVM classes as
*declined* rather than silently absent; zero Bugzilla writes.

### Phase 5 — turn triage on, canary only, autofile held to Firefox (S)

* `agent.products: ["Firefox", "Fenix"]`. Nothing in `agent/**` is product-aware beyond
  values that already flow through the seed.
* **Re-price first.** Both draft cost estimates (~$4/day) were computed from the refuted
  snapshot; start from the cadence replay (~5×), then net off the proto-dedup skip and
  the `no scored changesets` skip — and **count that log line separately**, because it
  is what makes “few dossiers” ambiguous.

**Exit:** 3-5 nights of per-day Fenix dossier counts, $/day, verdict distribution, and
**which gate moved each verdict** — with the “stale gate fires on ~85%” prediction
checked explicitly.

### Phase 6 — arm or abandon (S)

* Blind second opinion (`agent/second_opinion.py` — the one calibrated instrument, sens
  ~0.93-1.00 / spec 1.00) over every Fenix verdict; plus the free Bugzilla
  `regressed_by` check where the crash bug has one.
* A Fenix variant of the corpus filters only if reporting is to be armed.

**Exit:** **arm Fenix reporting only if** the Fenix SO corroboration rate is no worse
than the desktop baseline (~26% of leads survive) **and** ≥3 Fenix verdicts were
measurable. Anything else → stay observe-only and say so.

---

## 12. Out of scope

* **JVM/Kotlin crash support** — on the R8 + index-drift argument (§8), not the
  searchfox one.
* **Fenix beta/release** — mechanism identical (§2.8), one request to add later; nightly
  is where the regression premise lives.
* **Pre-126 Fenix** (≤125) — source is in the archived `mozilla-mobile/firefox-android`
  git repo, no hg node.
* **The glean/uniffi Rust family** — the regressor is not in mozilla-central (§4.4).
* **A historical Fenix eval corpus** — possible via the hg pushdate route, but the TC
  index cliffs at ~308 days and the corpus filters exclude Fenix by construction.
* **A daily cost cap** — pre-existing gap, not Fenix's to fix, but nothing bounds spend
  today.
* **Socorro's Android stackwalk failure rate** — upstream; file it, don't work around it.

---

## 13. Decisions needed

1. **Proto-dedup across products** — free saving, or a **desktop** coverage loss? 21.8%
   of Fenix native signatures overlap Firefox, 4 of the 7 chronic ones, and ≥1
   byte-identical `protohash` is confirmed. This must be *decided*, not discovered in
   prod.
2. **`thresholds.protos` for Fenix** — the current silent default of 1 is the only thing
   bounding spend; 50 (Firefox parity) un-bounds it.
3. **Bugzilla destination** for a Fenix-attributed crash — `Firefox for Android` vs
   `GeckoView`, given Socorro offers both and `get_bz_query` takes the first.
4. **Autofile for Fenix at all**, given ~40% of authors cannot be needinfo'd and
   Clouseau would outfile the organic rate ~15×. *Recommendation: hold autofile to
   Firefox indefinitely and revisit only after Phase 6.*
5. **Whether “zero hardware-noise suppression” is acceptable** on a product whose
   crashes are overwhelmingly single-device singletons — or whether the
   `android_fingerprint` re-key is a prerequisite rather than a nice-to-have.
6. **Whether main's missing JVM semantic index is a deliberate exclusion or a broken
   ingest** — that determines whether Kotlin support is wait-for-upstream or a real
   build.

---

## 14. Operational notes

* Run `uv run python bin/predeploy.py` before every deploy — two deploys on 08-04 each
  killed 3-4 in-flight ~$3 runs because it wasn't used.
* Widening `config.products` ships **through the release phase**, so the irreversible
  enum DDL and the ingestion switch land in the **same deploy** unless
  `INGEST_PRODUCTS` exists first. Make that explicit in the runbook:
  `heroku config:set INGEST_PRODUCTS="Firefox"` **before** the config-file deploy.
* `config.products` is **not** a kill switch (§5 #2). `INGEST_PRODUCTS`,
  `agent.products` and `autofile.products` are.
* All hgmo/hg-edge requests must send the allowlisted `crash-clouseau` User-Agent;
  `hg.mozilla.org` 302-redirects to `hg-edge.mozilla.org`, so a client without redirect
  following gets an empty body and may wrongly conclude hg is dead.
* `spike/_fenix_scratch/` holds 118 untracked repro scripts for the fan-out numbers.

---

## 15. Appendix: reproduction commands

```bash
# Buildhub has no Fenix
curl -s -X POST https://buildhub.moz.tools/api/search -H 'Content-Type: application/json' \
  -d '{"size":0,"aggs":{"p":{"terms":{"field":"source.product","size":50}}}}'

# Fenix nightly composition
curl -s 'https://crash-stats.mozilla.org/api/SuperSearch/?product=Fenix&release_channel=nightly&date=%3E%3D2026-07-28&_facets=signature&_results_number=0&_facets_size=500'

# buildid -> Fenix build task -> hg revision
BID=20260809094648
curl -s "https://firefox-ci-tc.services.mozilla.com/api/index/v1/task/gecko.v2.mozilla-central.pushdate.2026.08.09.$BID.mobile.fenix-nightly"
curl -s https://firefox-ci-tc.services.mozilla.com/api/queue/v1/task/FD1yHxTKR3m4VL5zZyaGjQ  # .metadata.source

# enumerate a day's builds (note the 'latest' child, and probe the leaf per child)
curl -s -X POST https://firefox-ci-tc.services.mozilla.com/api/index/v1/namespaces/gecko.v2.mozilla-central.pushdate.2026.08.09 \
  -H 'Content-Type: application/json' -d '{"limit":1000}'
curl -s -X POST "https://firefox-ci-tc.services.mozilla.com/api/index/v1/tasks/gecko.v2.mozilla-central.pushdate.2026.08.09.$BID.mobile" \
  -H 'Content-Type: application/json' -d '{"limit":100}'

# the load-bearing identity: crash-frame git hash -> the build's hg rev
uv run python -c "from libmozdata.lando import LandoCommitMapAPI; \
  print(LandoCommitMapAPI().git2hg('cb6222f0865b6702e1cf166f57e4a0119c9c89d7').hg_hash)"
# -> b059f3919dbbc623e378ecec37edbb05f3d4f70a

# hg tip is a tags-only commit (ALWAYS send the UA; follow redirects)
curl -sL -A crash-clouseau https://hg.mozilla.org/mozilla-central/json-rev/tip
curl -sL -A crash-clouseau -o /dev/null -w '%{http_code}\n' \
  https://hg-edge.mozilla.org/mozilla-central/raw-file/tip/dom/base/nsINode.cpp      # 404
curl -sL -A crash-clouseau -o /dev/null -w '%{http_code}\n' \
  https://hg-edge.mozilla.org/mozilla-central/raw-file/default/dom/base/nsINode.cpp  # 200

# Kotlin semantics: indexed everywhere, semantically analysed only off main
P=mobile/android/android-components/components/browser/domains/src/main/java/mozilla/components/browser/domains/CustomDomains.kt
for T in firefox-main firefox-beta firefox-release; do
  echo -n "$T "; curl -sL -A crash-clouseau "https://searchfox.org/$T/source/$P" | grep -c 'data-symbols='
done
searchfox-cli -R mozilla-central --define 'mozilla::components::browser::domains::CustomDomains::load'
searchfox-cli -R mozilla-beta    --define 'mozilla::components::browser::domains::CustomDomains::load'
```
