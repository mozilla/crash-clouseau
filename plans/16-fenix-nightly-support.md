# Plan #16 — Fenix nightly support

> **Status:** design proposal (2026-08-11), not implemented. Every number below is
> measured live against crash-stats / buildhub / firefox-ci TaskCluster / hg-edge /
> lando / BMO, or read out of the tree with file:line. Produced by a 13-agent
> fan-out whose five riskiest claims were then adversarially re-verified —
> **three of the five were corrected**, and the corrections are folded in here
> rather than the original claims (§8 lists what was refuted, so it does not get
> re-litigated).
>
> **The premise in the request holds:** Buildhub genuinely has no Fenix data, so the
> `(product, channel, buildid) → revision` contract that the whole pipeline hangs on
> has to come from somewhere else. That turns out to be the *easy* part — and it is
> even easier than the TaskCluster recipe in `plans/`-adjacent notes suggested,
> because the Fenix nightly buildid **is** the mozilla-central push timestamp. The
> real work is everywhere else.

---

## 1. Summary (why, and what the actual shape of the problem is)

Fenix builds inside mozilla-central at `mobile/android/fenix` (monorepo since
2024-03), ships two nightlies a day off the same pushes as desktop nightly, and
produces ~2,200 crash reports a day that Socorro symbolicates properly. Clouseau
cannot see any of it: `config/global.json` `products` is `["Firefox"]`, and the one
writer of the `builds` table asks Buildhub, which has no Fenix.

Three things fall out of the measurement, and they reorder the work:

1. **buildid → hg revision is free and does not need TaskCluster.** The Fenix
   nightly buildid is the UTC push timestamp of the revision it was built from —
   structurally, not coincidentally (`taskcluster/gecko_taskgraph/decision.py:372`
   `build_date = parameters["pushdate"] or int(time.time())`, formatted into
   `moz_build_date` at :374; `.taskcluster.yml:279` passes `--pushdate` on hg-push,
   action *and* cron). So one hg pushdate query resolves it, and Buildhub asked for
   `source.product=firefox` at the *Fenix* buildid returns the same revision
   **114/114 over 62 days**.
2. **What genuinely needs TaskCluster is the build SET, not the revision.** Fenix
   does not build on every push desktop does. Over one 23-day span there were 46
   fenix-nightly builds against 47 firefox-nightly buildids, and borrowing Firefox's
   ordering for the *previous* build collapsed one changeset window from **173
   changesets to 2** — a silent 99% truncation of the regressor search space.
3. **The blocker is not source resolution, it is everything that assumes one
   product.** Including a Postgres enum that cannot be widened by the mechanism
   built to widen it, and two safety gates that are structurally dead on Android.

Two pre-existing **desktop** defects surfaced on the way (§4). Both are live in prod
today, neither is Fenix-specific, and one of them means the agent's pinned source and
blame reads have been returning nothing on every run.

---

## 2. The source-resolution contract, solved

### 2.1 Why Buildhub can never serve it

`source.product` buckets over all 1,792,717 Buildhub docs: `firefox` 1,295,137,
`thunderbird` 182,176, `devedition` 175,380, `fennec` 140,020, `flowstate` 4. A
full-text query for `fenix` returns **0 hits**. `buildhub.PRODS`
(`crashclouseau/buildhub.py:17`) has no Fenix entry, so `PRODS.get(x, x)` passes the
literal `"Fenix"` into an ES `terms: source.product` filter that matches nothing:
`buildhub.get(…, prods='Fenix')` → `{}` where `prods='Firefox'` → 17 builds.

### 2.2 The chain that replaces it — verified end to end

The load-bearing identity, because `inspector.inspect_stacktrace`
(`crashclouseau/inspector.py:172-173`) throws away **the entire stack** when a
frame's node ≠ the stored build node:

```
Fenix crash frame  git:github.com/mozilla-firefox/firefox:<path>:cb6222f0865b…
        │  inspector.get_path_node → GIT_PAT → inspector.git2hg (libmozdata Lando)
        ▼
hg rev  b059f3919dbbc623e378ecec37edbb05f3d4f70a          ← verified equal
        ▲
        │  metadata.source of task FD1yHxTKR3m4VL5zZyaGjQ = signing-apk-fenix-nightly
gecko.v2.mozilla-central.pushdate.2026.08.09.20260809094648.mobile.fenix-nightly
```

So the revision we persist in `nodes` must be **that hg hash**, and the existing
git→hg bridge already produces it. No new conversion layer is needed.

### 2.3 Three routes, ranked

| route | cost | coverage | use |
|---|---|---|---|
| **hg pushdate** — buildid as UTC timestamp → the one m-c push at that instant | 1 hg GET | ~100% back to at least 2024-07 (9/9 sampled expired-index buildids still resolve) | revision, and the only route that works for a historical corpus |
| **Buildhub as `source.product=firefox`** at the Fenix buildid | 1 POST, code already vendored | 114/114 over 62 days; 0 fenix-only buildids | revision (cheapest; reuses `buildhub.get_rev_from`) |
| **TaskCluster index** `…pushdate.<Y>.<M>.<D>.<bid>.mobile.fenix-nightly` | 1 GET/build | 537/538 (99.81%) of buildids newer than 2025-11-01, covering 617,667 crashes | **build existence + ordering** — the thing nothing else can answer |

### 2.4 The enumeration recipe, with every gotcha that was actually hit

```
POST /api/index/v1/namespaces/gecko.v2.mozilla-central.pushdate.<Y>.<M>.<D>
     → children;  KEEP ONLY ^\d{14}$
GET  /api/index/v1/task/<ns>.<bid>.mobile.fenix-nightly   → 200 means a Fenix build exists
GET  /api/queue/v1/task/<taskId>  → metadata.source → hg rev
```

* **`latest` is a real resolvable child.** `…pushdate.2026.08.10.latest.mobile.fenix-nightly`
  returns 200, and in ASCII `'latest' > '20260810202013'`, so `max(children)` and
  `sorted(children)[-1]` both silently pick it. Filter on `^\d{14}$`, **not** on
  `!= 'latest'`.
* **30% of day children are not nightlies.** Over 23 days: 66 non-`latest` children,
  only 46 with a `fenix-nightly` leaf — 13 carry `fenix-nightly-simulation` +
  `fenix-debug` only, 6 have no `mobile.*` task at all. The walk-back **must probe the
  leaf per candidate**, never take "the previous child".
* **Multi-day gaps are real.** 2026-07-11 (Saturday) has an empty day namespace — no
  m-c pushes between 07-10 16:43 and 07-12 02:10. Walk back with a bound and treat
  "no fenix-nightly within `max_back` days" as an abstain, not an exception.
* **Leaf choice matters.** `mobile.fenix-nightly` = `signing-apk-fenix-nightly`, the
  shipped APK. `fenix-nightly-simulation` is an on-push rehearsal (would resolve the
  *wrong* build); `android-*` is GeckoView, a different artifact.
* **hg vs git route.** Each task carries exactly two `revision.<40hex>` routes. hg is
  first in 46/46, but that is insertion order, not sorted (in 23 of 46 the first hash
  is lexically greater) — **never index `routes[]` positionally.** Read
  `metadata.source` (an `hg.mozilla.org/…/file/<rev>/…` URL) and cross-check the single
  `tc-treeherder.v2.*` route. `payload.env` is empty on all 46 signing tasks;
  `GECKO_HEAD_REV` lives on the cron decision task and the same-push `android-*-opt`
  build if a belt-and-braces check is wanted.
* **No version in the index** (`data` is `{}`). `models.Build` needs one: read
  `mobile/android/version.txt` at the build rev via hg-edge (returns `155.0a1`), or
  take it from the Socorro facet already in hand.
* **Retention cliffs at ~308 days** for the fenix family even though `expires`
  advertises 365. Irrelevant for nightly triage; fatal for a backfill — use the hg
  pushdate route there.
* **It is not a total function.** Over the full universe of 972 distinct Fenix nightly
  buildids, the identity holds 971/972. The exception is `20260202034059` (13-14
  reports, v149.0a1): the route 404s and the timestamp is not a push on m-c, autoland,
  try, beta, release, esr140 or unified — one device, one `install_time`, i.e. an APK
  not built by Mozilla CI, whose `MOZ_BUILD_DATE` is a wall clock (the in-tree analogue
  of decision.py:372's `or int(time.time())` fallback). **Handle the 404 by abstaining**
  — do not walk back a day, do not guess, do not raise.
* No rate limiting observed across ~1,400 requests; responses are `no-store`, so cache
  `(buildid, revision, prev_buildid)` in the `builds` table as today.
* Beta/release use the same mechanism with different leaves (enumerated, not guessed):
  `nightly → (mozilla-central, fenix-nightly)`, `beta → (mozilla-beta, fenix-beta)`,
  `release → (mozilla-release, fenix-release)`.

### 2.5 Window size is a non-issue

The resulting Fenix nightly changeset window is median **95 changesets / 2 pushes**
(p90 235) against a Firefox nightly baseline of median 88 over the identical span —
because the two products build off the same pushes. The agent reasons over the same
volume it does today.

---

## 3. The hard gate: why Fenix produces exactly zero rows today

```
bin/schedule.py:13     every 20 min → update.update_all()
update.py:231          products = config.get_products()            # ["Firefox"]
update.py:161          data = buildhub.get(date, channel, prods=product)   # {} for Fenix
update.py:162          if data: models.Build.put_data(data)        # ← never runs
update.py:184-186      bidid = models.Build.get_id(bid, channel, product)  # → None
update.py:186          errors.add(bid); continue                  # ← BEFORE UUID.add
```

So a Fenix crash never becomes a `uuids` row, and the only trace is an info log,
`"No buildid in db for <bid>/Fenix/nightly"`. One missing `builds` row silently drops
100% of the product. `builds → nodes` is where every downstream revision comes from
(`UUID.to_analyze`, `Build.get_two_last`, `Build.get_changeset`).

---

## 4. Two pre-existing desktop defects found on the way

**These are not Fenix work.** They are live in production for Firefox today, and the
Fenix measurement is meaningless until the second one is fixed.

### 4.1 `_ensure_enum_values()` can never add an enum value — so `_ENUM_ADDITIONS` has never once fired

`models.py:2691-2726` opens `with engine.connect() as conn`, runs
`SELECT 1 FROM pg_enum …`, and only on the miss path does
`conn = conn.execution_options(isolation_level="AUTOCOMMIT")` before `ALTER TYPE`. The
SELECT has already autobegun a transaction on that connection, and SQLAlchemy raises
**unconditionally** in that state — verified in the pinned 2.0.51 at
`sqlalchemy/engine/default.py:673-686`:

```python
if connection.in_transaction():
    trans_objs = [... if obj.transactional]
    if trans_objs:
        raise exc.InvalidRequestError("This connection has already initialized a
            SQLAlchemy Transaction() object via begin() or autobegin; …")
```

The `except Exception` at :2722 swallows it into a `logger.warning`. Reproduced against
PostgreSQL 16.14 through the real `models.create()`. The `exists` fast-path has masked
it forever: on a fresh DB `create_all` already installed every label, so the DDL branch
has never succeeded — which means the existing `{"VERDICT_TYPE": ("lead",)}` entry has
never migrated anything either, and prod got `lead` from a fresh `create_all`.

Fix: issue the `ALTER` on a **separate** connection opened with
`.execution_options(isolation_level="AUTOCOMMIT")` (~4 lines). It cannot be caught by
the suite, which runs on sqlite where the function returns early at :2701 — so this
needs a Postgres-backed test. **`DEPLOY.md:8` currently claims `models.create()` "adds
the `lead` enum value"; that line is wrong and must be corrected or the next person
trusts it again.**

### 4.2 `pin_rev` is always `""`, the tools fall back to `tip`, and `tip` no longer contains source

`orchestrator.py:572-581` pins blame/history/source reads to the build rev only
`if inspector.git2hg(build_node)`. But `build_node` comes from `nodes.node`, which is
an **hg** short rev (Buildhub gives hg; `get_path_node` converts crash-frame git → hg,
which is precisely why `inspect_stacktrace`'s `node != build_node` comparison can ever
succeed). `git2hg` maps git→hg only, so it returns `""` for hg input. Measured:

```
hg short  "b059f3919dbb"                       → LandoMissingCommit → ""
hg full   "b059f3919dbbc623…"                  → LandoMissingCommit → ""
git full  "cb6222f0865b6702…"  (control)       → "b059f3919dbbc623e378ecec37edbb05f3d4f70a"
```

So `pin_rev` is `""` on every run, `pin_node()` (`agent/tools/__init__.py:18-20`)
returns `"tip"` — and hg mozilla-central's `tip` is now a **tags-only** commit
(`03e0c921eb04`, `No bug - Tagging … FIREFOX_153_0_4_RELEASE`, by
`ffxbld@lando.moz.tools`, touching exactly one file: `.hgtags`):

```
raw-file/tip/dom/base/nsINode.cpp      → HTTP 404
raw-file/default/dom/base/nsINode.cpp  → HTTP 200
```

**Net effect: `mcp__source__raw_file` and `mcp__history__blame` return nothing on every
on-stack run today, desktop included.** Fix is two independent one-liners: pin to
`build_node` directly (it is already the hg flavour hg-edge wants), and make the
unpinned fallback `default` rather than `tip`.

### 4.3 Two smaller ones

* **`datacollector.get_changeset` has returned `None` for every build since ~2025-11-10.**
  Its filter is `'@"hg:hg.mozilla.org/".*:[0-9a-f]+'` (`datacollector.py:442`),
  pre-migration shape. Firefox nightly ≥2026-08-04: 322 hits of 7,651 with the hg
  filter vs 4,656 with the git shape — and faceting those 322 by build_id, the newest
  matching buildid *anywhere* is `20251110211328`. State it precisely: **zero for any
  buildid younger than ~2025-11-10, for both products** (the aggregate "61 Fenix / 355
  Firefox hits" is entirely stale pre-migration buildids and will mislead a fixer).
  Fix: pin the repo in the git shape and route the captured sha through
  `inspector.git2hg` before `utils.short_rev`.
* **`java.py:80` passes the literal `"FennecAndroid"`** into
  `models.Build.get_changeset` → `filter(Build.product == …)`. That is not a
  `PRODUCT_TYPE` label, so on Postgres `/api/javast` raises
  `invalid input value for enum` today.

---

## 5. Is there anything worth triaging? (feasibility, measured)

**Composition, Fenix nightly, 14 days (31,315 reports / 573 signatures):**

| class | reports | distinct signatures |
|---|---|---|
| Java/Kotlin exceptions | 54.8% | ~50% |
| **native C++/Rust** | **24.7%** (~550/day) | **~45% (211)** |
| `EMPTY: no frame data available` | 20.5% | 5 |

`proto_signature` and `java_stack_trace` are **strictly mutually exclusive** (17,143
java / 7,764 proto / zero overlap), and `uuids` rows are only ever created from
proto-signature facets — so today a Java crash produces no uuid, and would still
produce none after this plan. That is what makes deferring JVM support a no-op rather
than a regression.

**How much the selector would actually pick up.** This is where the first measurement
was wrong and the correction matters: a single *retrospective* replay of
`get_new_signatures` + `evaluate_days` over a 21-day window selects only 36 native
signatures, 4 of them fresh — which reads like "there is no work here". But
`bin/schedule.py` runs `update_all()` **every 20 minutes** with `date=utcnow()`
(`update.py:170`), so every build-day is judged dozens of times while still
*immature*, where the bar is just the from-zero rule at `installs>=1`. The
`mature_after=5` / `mature_installs=4` gate and the untestable prefix — 425 of the
snapshot's declines — bite only retrospectively. Replaying the **deployed cadence**
over the same 21 build-days with the real `evaluate_days`:

> **246 native (signature, build-day) pairs / 179 distinct native signatures ≈ 11-12/day,
> of which 58 pairs / 57 signatures are FRESH** (first seen within 7 days of the build)
> — 6.5× the retrospective snapshot, and 14× on the fresh count.

with real fresh spikes as counterexamples: `UiCompositorControllerParent::RecvRequestScreenPixels`
(first seen at build 20260722214024, 76 crashes, 13 distinct install_times, SIGSEGV at
0x0, correct git path at the build rev), `MozSharedMap_Binding::CreateInterfaceObjects`
(77 crashes, SIGILL), `RenderBackend::process_transaction` (4 crashes / 3 device
models). **Any go/no-go threshold must be derived from the cadence replay, not a
retrospective pass** — a `<1/week` kill gate would have closed the project on a
measurement artifact.

What survives from the pessimistic read, and should temper expectations: native volume
is thin and singleton-heavy (33 of 37 native selected build-days are one crash from one
install), multi-machine native pairs run ~7 signatures / 38 build-day pairs per 21 days
versus ~1,000 for desktop, and median staleness is 273 days. **The problem is
signal-to-noise and multi-machine corroboration, not absence of work.**

**Three upstream facts to write down:**

* **Socorro's stackwalker fails on Android at scale.** Counting only non-Java reports,
  8,059 of 21,328 (37.8%) have no proto_signature over 21 days, running 48-72% on
  recent crash days and up to 83% on individual builds (desktop control: 0.3-1.3%).
  Nothing Clouseau can do; worth filing against Socorro. It cuts the usable native
  sample to ~1/3.
* **One device farm produces 48% of all reports.** The largest signature
  (`java.security.ProviderException … AndroidKeyStoreKeyGeneratorSpi.engineGenerateKey`)
  is 15,067 of 31,315 reports and resolves to **one** android_model (`A95XF4`, an
  Android TV box) at 99.7%. The desktop "1 machine = 81,843 of 86,196" trap reproduces
  harder. `mature_installs>=4` does gate these out of selection, but any volume read
  must be distinct-install-per-day.
* **`install_time` does not mean the same thing on Android.** 14.6% of Fenix
  install_times span more than one buildid (max 11) vs 0.05% for Firefox, and **51.6%
  of native reports carry an install_time before 2010-01-01** (a clock reset). So
  `cardinality_install_time` — the axis `mature_installs` rests on — is a weak machine
  proxy, and `machine.py`'s calibration does not transfer.
* **The single largest native family is not Gecko.** `glean_sym` / uniffi Rust
  (`core::ptr::drop_in_place` … 3,905 reports) ships into Fenix as a prebuilt from
  application-services / glean. **mozilla-central's pushlog cannot contain its
  regressor** — only a version-bump changeset. Either exclude that family explicitly or
  accept that those runs cannot succeed.

**Build window needs no change.** Report-weighted, Fenix looks like users never update
(1.4% of reports from builds ≤2 days old vs 6.1% desktop). Install-weighted, the two
products are the same (29.5% vs 27.9% on builds ≤7d). The old-buildid distribution is
a device-farm artifact. **Explicitly resist widening `nightly_window_ndays`.**

---

## 6. What breaks that has nothing to do with buildids

### 6.1 Coupling inventory

| # | area | file:line | what happens |
|---|---|---|---|
| 1 | **`PRODUCT_TYPE` Postgres enum** | `models.py:18` | Native named enum, created once. `create_all()` only runs when the DB is fresh (`models.py:2729-2739`). Adding `"Fenix"` to config does **not** widen it; both INSERTs **and SELECTs** on `product='Fenix'` then raise `DataError: invalid input value for enum`. Reads too — `Build.get_id`, `Build.get_two_last`, `Signature.get_reports`, `Verdict.map_for_build`, `UUID.get_uuids_from_buildid` all filter `Build.product ==`, so the UI/API 500s. Widening is **one-way**: `ALTER TYPE … DROP VALUE` does not exist, and once a Fenix row exists, removing `"Fenix"` from config breaks every ORM read of it. **`config.products` is NOT a kill switch.** |
| 2 | product-keyed config | `config.py:144-151`, `177-187` | All six blocks **silently default** for an unknown product — no exception, no warning. The consequential one: `thresholds.protos` Firefox/nightly is 50, unknown product gets **1**. Note the perverse incentive — that default bug is currently the *only* bound on Fenix uuid creation, so "fixing" it to 50 is what un-bounds the spend. Make it a deliberate cost decision. |
| 3 | no `INGEST_PRODUCTS` hatch | `update.py:231` vs `:233` | Channels have `$INGEST_CHANNELS` precisely because config also defines the enum (docstring at `:226-230`). Products don't. So the enum widening and the ingestion switch are **the same edit**. |
| 4 | no product gate on the agent | `orchestrator.py:2471-2489` | `enqueue_agent` gates on `agent_enabled` + `agent_channels` + proto-dedup only. No product check anywhere in `agent/**`, and no product-keyed cost cap. |
| 5 | no product gate on autofile | `bugzilla_apply.py:510` | `AUTOFILE_BUGS=1` is armed in prod at rung 70 with needinfo. So ingestion, ~$2/crash triage and unattended Bugzilla filing are **one switch**. |
| 6 | Java path gated on a literal | `datacollector.py:236-238`, `:385` | `if product == "Fennec"` → `get_uuids_fennec`, itself hardcoding `"product": "Fennec"`. That branch exists because Java crashes have no proto-signature. |
| 7 | `.kt`/`.kts` not interesting | `config/interesting_extensions.json`, `utils.py:120-122` | 0 of 29 files across three real Fenix changesets survive. 341 of 4,734 window changesets (7%) touch `.kt` and nothing configured. Default `file_filter` for `pushlog.pushlog` → `files`/`changesets` tables. |
| 8 | patch extraction misreads Kotlin | `patch_extract.py:261-265`, `:287-289` | No `kt` in `_LANG_BY_EXT`; `lang_for` falls back to `"cpp"`. Kotlin keywords leak into the identifier set, `change_tags` fires `deref` on every lambda, `_func_name` reports the **superclass** as the enclosing function. All silent. |
| 9 | eval/corpus excludes Fenix | `eval/corpus.py:66-71`, `eval/study_corpus.py:45-53,156-158` | Hard-rejects any `java_stack_trace` and any product ≠ `"Firefox"`, and stamps `"product": "Firefox"`. So there is **no measurement path for Fenix quality**, and the calibration table was fit on corpora that exclude it. |
| — | UI / API | `html.py:75-86`, `295-306`; `api.py`; `templates/` | **Fine.** Product dropdown is built from DB rows. Only `html.pushlog:581,587` defaults to `"Firefox"`. Note `Build.get_products` orders `product.desc()` → on a native enum that is `enumsortorder`, so an appended `Fenix` sorts *after* `Firefox`. |
| — | Bugzilla filing | `report_bug.py:634-665` | **Fine.** `resolve_product_component` is fully data-driven off the regressor bug. |

### 6.2 The safety story — what is dead on Fenix

This is the part neither draft proposal had, and it is the real precision argument:

* **Bit-flip gate is inert.** `_apply_bit_flip_gate` (`orchestrator.py:1588-1663`)
  needs `possible_bit_flips_max_confidence >= 50`. Among native nightly crashes:
  **Fenix 1 of 4,091 carries the field at all, 0 at ≥50.** Firefox: 684 of 7,546
  carry it, 326 (4.3%) at ≥50.
* **Bad-machine gate is inert.** `_apply_bad_machine_gate` returns early when
  `cpus is None`. **`cpu_info` is NULL on 95.7% of native Fenix crashes** (7,435 of
  7,767). Where the diversity half would fire, it lands on colliding clock-reset ids
  with empty `cpu_info`.
* Both gates exist for exactly the population Fenix consists of (singleton crashes
  from one install). Knock-on: `models._INSTANCE_SUPPRESSED` is the proto-dedup
  exemption, so that mechanism is moot on Fenix too.
* **The stale-signature gate is the one gate that works** — `sigage` is correctly
  product-scoped (verified: `Navigation::UpdateEntriesForSameDocumentNavigation`
  first-seen 20251110211328 on Fenix vs 20251202211324 on Firefox, 22 days apart) —
  **and it will fire on ~85% of Fenix leads.** That asymmetry *is* the Fenix precision
  profile: the one working gate suppresses almost everything, and the two that would
  discriminate are dead.
* **Proto-dedup is product-blind.** `UUID.proto_already_analyzed`
  (`models.py:1208-1227`) has no product/channel clause, so whichever product lands
  first closes the cluster for the other **forever**. 61 of 280 Fenix native
  signatures (21.8%) also occur in Firefox nightly, including 4 of the 7 chronic
  signatures that make up Fenix's multi-machine ceiling, and at least one
  byte-identical `protohash` across products was confirmed. Enabling Fenix can
  therefore **silently regress desktop coverage**. This must be a decided behaviour.
* **The desktop-fitted probability is published to Bugzilla, not just the UI.**
  `report_bug._worth_phrase` (`report_bug.py:765-777`) writes "N% worth investigating
  — a calibrated estimate" into the filed comment; `_apply_calibration`
  (`orchestrator.py:1080-1097`) is product-blind; the table was fit on corpora that
  exclude Fenix. Cheapest correct fix: return `""` for a product with no fitted table.
* **The autofile cap is global.** `Firefox for Android` has 37 crash-keyword bugs in
  180 days (~0.2/day) — a live queue, 24 FIXED. Clouseau autofiles ~3/day. So Clouseau
  would become the dominant filer of Fenix crash bugs on day one, **and** the two
  products would compete for one cap in both directions.
* **Off-stack rescue is off.** `agent.offstack.enabled=false`, and `build_seed` returns
  `None` for an off-stack crash (`orchestrator.py:433-436`). Since 29-32 of 36 Fenix
  native signatures are stale, expect the dossier rate to sit **far below** the
  selection rate — a free cost control, but it means "few Fenix dossiers" will not
  distinguish working from broken unless `"agent: no scored changesets"` is counted
  separately.
* **No daily cost cap exists at all.** `max_cost_usd_per_crash` is per-crash and
  log-only (`orchestrator.py:2165-2175` records `over_budget` *after* the run).

### 6.3 Bugzilla specifics

* The BMO product is **`Firefox for Android`** (37 components), not `Fenix` — `Fenix`
  returns 0 products. `GeckoView` (6 components) and `Focus` (3) also exist. `Trunk` is
  an active version in all, so `_bug_version("nightly")` is fine.
* **Socorro offers two destinations and `get_bz_query` takes the first.** A Fenix crash
  page carries `enter_bug.cgi` links for both `Firefox for Android` and `GeckoView`;
  `report_bug.get_bz_query` (`report_bug.py:28-43`) returns the first
  `keywords=crash` link, so the GeckoView-vs-Fenix attribution question is resolved by
  accident. Meanwhile `autofile` inherits the *regressor bug's* product, which for a
  Gecko regressor is `Core` (arguably correct). **The two paths disagree and nobody
  chose.**
* `keywords=regression` is idiomatic on mobile (181 bugs / 180 days, 105 RESOLVED).
* **~40% of recent Fenix changeset authors cannot be needinfo'd**: 4 of 20 are
  `release+landoscript@mozilla.com` (the Lando bot — l10n imports and
  application-services version bumps, i.e. exactly what an off-stack window surfaces),
  4 of 20 are GitHub `noreply` addresses. Desktop control: 2 of 20, no bots. Good news
  — this does **not** hit the "unknown requestee kills the whole create (code 51)"
  trap: `report_bug._needinfo_account` (`report_bug.py:917-940`) verifies the address,
  falls back through the regressor bug's assignee/creator, then files **with no flag**
  rather than filing no bug. So the failure mode is a bug landing on a mobile queue
  with nobody asked.

---

## 7. Kotlin / JVM — recommended OUT of scope, but not for the reason you'd expect

The tempting argument ("searchfox has no semantic index for Kotlin, so the call-path
gate can never fire, so Kotlin is capped at `lead`") is **false**, and it was the most
load-bearing wrong claim in the whole investigation:

**First, the distinction that matters, because "is Kotlin indexed?" has two different
answers.** Kotlin **is** indexed on `firefox-main`: the files are browsable, blame
works, and full-text search works. What `firefox-main` does not carry is **semantic
analysis records** — the definitions/callers/callees that `--define`, `--calls-from`
and `--calls-to` actually read. Measured on one file, `mobile/android/android-components/
components/browser/domains/.../CustomDomains.kt`:

| tree | page | `data-symbols` records | `id:CustomDomains` |
|---|---|---|---|
| `firefox-main` | HTTP 200, browsable | **0** | **0 hits** |
| `firefox-beta` | HTTP 200 | **81** | 10 hits (7 by scip symbol) |
| `firefox-release` | HTTP 200 | **81** | — |
| `firefox-main`, `dom/base/nsINode.cpp` (C++ control) | HTTP 200 | 8,454 | — |

And straight through the tool the agent uses:

```
searchfox-cli -R mozilla-central --define 'mozilla::components::browser::domains::CustomDomains::load'
  → ERROR No potential definitions found
searchfox-cli -R mozilla-beta    --define 'mozilla::components::browser::domains::CustomDomains::load'
  → >>> 25:     fun load(context: Context): List<String> =
```

* **So searchfox has a live Java *and* Kotlin semantic indexer, serving right now on
  `firefox-beta`, `firefox-release` and `firefox-esr140`** — trees the adapter can
  already target (`Repo.BETA = "mozilla-beta"`, `searchfox.py:130`; every agent tool
  takes `repo=`). The repo's **own unmodified client** returns a commit-pinned Kotlin
  definition permalink plus a **138-edge call graph with per-edge citations reaching
  into `mozilla.components.*`**. Two working spellings: the `::`-joined pretty name
  `org::mozilla::fenix::HomeActivity::onCreate` and the raw scip symbol
  `S_jvm_org/mozilla/fenix/HomeActivity#onCreate().`.
* **But the beta workaround has real drift, which is why it is not a free fix for a
  nightly product.** The trees are pinned to their own branch tips:
  `firefox-main` is at `fca1efcadb9d` / `155.0a1`, `firefox-beta` at `cd001e124b15` /
  `154.0b9` — a full release cycle behind. A Kotlin regressor that landed on nightly
  *this week* is not in the beta index at all, which is the exact population Fenix
  nightly triage is about. Use the existing `queried_tip`/`rev_label` bookkeeping and
  expect misses on new code.
* Do not build on "firefox-main can never have this" either: m-c's own
  `android-aarch64-searchfox-debug` task publishes a 43 MB
  `target.mozsearch-java-index.zip` today, which mozsearch fetches as an **optional**
  artifact (`|| true`) — so tip-tree JVM semantics can reappear without notice.
* Only `--field-layout` is genuinely C++-only.

**The real blocker is R8.** Fenix nightly APKs are minified, and `java_stack_trace`
line numbers are the remapped ones. Verified at the exact build revision: frame
`mozilla.components.lib.dataprotect.Keystore.generateKey(Keystore.kt:269)` — at rev
`dc7f12a8cbce`, line 269 of that file is a KDoc close-comment `*/`, while the real
function is at line **232**. The *file* is right (FQCN → in-tree path maps cleanly);
the *line* is wrong. Line-granularity attribution needs the per-build R8 mapping
artifact plus a deobfuscation step — a new subsystem.

Add to that: `inspector.get_crash_info` (`inspector.py:93-121`) checks
`java_stack_trace` **first** and never looks at `json_dump` if it is present, and the
off-stack escape hatch lives only in the native `else` branch — so a Java crash reaches
`java.inspect_java_stacktrace`, whose `org.mozilla.` prefix filter rejects
`mozilla.components.*` (where the top real Fenix `IllegalStateException` lives),
attributes zero frames, and the run ends `useless=True` with the agent never enqueued.
`java.py`'s file-discovery half walks `api.github.com/repos/mozilla/gecko-dev`, which is
**archived** (`archived: True`, last push 2025-07-09), keeps only `.java`, and has no
live caller.

**Recommendation:** cut JVM support from the first landing on the **R8 line-number**
argument (and the fact that a Java crash produces no uuid today, so cutting it is a
no-op). Record the searchfox correction in the plan so the `lead`-cap claim does not get
recycled at the next review.

---

## 8. Refuted — do not spend time here again

* ~~"There is essentially no spiking native regression work in Fenix nightly, and no
  retuning can create it."~~ Instrument error: measured retrospectively instead of at
  the deployed 20-minute cadence. Real figure is 246 native pairs / 179 signatures per
  21 days, 58 pairs fresh. §5.
* ~~"searchfox has no semantic index for Kotlin or Java; Fenix Kotlin is structurally
  capped at `lead`."~~ Per-*tree* gap, not a language gap. §7. Phrase it carefully:
  Kotlin **is** indexed and browsable on `firefox-main`; what main lacks is the
  *semantic* records. Saying "Kotlin isn't indexed in main" is wrong and will be
  corrected by anyone who opens a `.kt` file there.
* ~~"The buildid→pushdate identity is total, so a day-bucket error is structurally
  impossible."~~ Structural for CI builds (mechanism found in `decision.py:372`), but
  971/972 in the wild — one non-CI APK exists in prod. §2.4.
* ~~`extra.index.rank == buildid`~~ — `rank` is unix seconds (1786268808), equal to the
  push date. The 14-digit form exists only in the route string and `moz_build_date`.
* **Respins are not a hazard** (checked because they looked like one): push 44947 sat as
  tip for 18.5h and two `nightly-all` cron graphs ran on it, both with
  `moz_build_date: '20260722214024'` — but the second decision task's `task-graph.json`
  has 0 entries, fully optimised away. No second APK, index entry untouched.

---

## 9. Phased plan

Ordering principles: **the irreversible step ships alone**; the gates exist before the
data that would arm them; nothing costs money until a human has looked at free output;
and the desktop-value fixes land first so the investigation pays for itself even if
Fenix is abandoned.

### Phase 0 — land the pre-existing defects (S) · *value is desktop; ships regardless*
* Fix `_ensure_enum_values` (separate AUTOCOMMIT connection) + a Postgres-backed test.
  Correct `DEPLOY.md:8`.
* Fix `pin_rev` (pin to `build_node` directly) and the `tip` → `default` fallback.
* Fix `datacollector.get_changeset`'s regex (repo-pinned git shape + `git2hg`).
* Fix `java.py:80`'s `"FennecAndroid"` literal.
* **Exit:** the Postgres test fails before / passes after; on the canary,
  `"raw_file: not found or fetch failed"` goes from ~every source-reading run to 0;
  `get_changeset` resolves a 2026 desktop nightly buildid.

### Phase 1 — product gates, before any Fenix row can exist (S)
* `INGEST_PRODUCTS` hatch in `update.py:231`, mirroring `INGEST_CHANNELS`.
* `agent.products` config key + gate in `enqueue_agent`.
* `autofile.products` gate in `autofile_bug`, and a **per-product** `daily_cap`.
* Decide and implement the proto-dedup product clause (`models.py:1208-1227`).
* `_worth_phrase`/`_apply_calibration` return no number for a product with no fitted
  table.
* **Exit:** tests prove each gate independently — Fenix `builds`+`uuid` rows with zero
  agent enqueues; Fenix dossiers with zero Bugzilla writes.

### Phase 2 — widen the enum, alone, with no behaviour change (S)
* `_ENUM_ADDITIONS["PRODUCT_TYPE"] = ("Fenix",)`, run by the release phase. Hand-run
  `ALTER TYPE` on the canary first as belt-and-braces.
* Set `INGEST_PRODUCTS="Firefox"` **before** the config-file deploy, not after.
* **Exit:** `pg_enum` shows `Firefox, Fenix`; no `could not ensure enum` warning in the
  release log; `/reports.html` still 200.

### Phase 3 — the Fenix build source, stopping at `builds` rows (M)
* New `crashclouseau/tcindex.py` exposing buildhub's exact interface so it drops into
  the two call sites that matter (`update.py:161`, `tools.py:11`): revision from
  Buildhub-as-firefox / hg pushdate, **existence and ordering from the TC index** per
  §2.4, version from `mobile/android/version.txt`. `get_enclosing_builds` may return
  `[None, None]` — it is unreachable from the shipped UI.
* Dispatch on product; Firefox keeps its current path untouched.
* **Exit:** on the canary, `builds` rows for `product='Fenix'` are **set-equal** to the
  TC index's `fenix-nightly` leaves for those days and contain **no desktop-only
  buildid**; every `nodes.node` equals the route's hg rev; `"No buildid in db"` for
  Fenix drops to ~0; a regression test pins the 173→2 truncation case.

### Phase 4 — Fenix native ingestion, agent still OFF (M)
* Add `"Fenix"` sub-blocks to all six product-keyed config blocks with **deliberately
  chosen** values (state the `protos` value as a cost decision, §6.1 #2).
* Selector class filter: skip signatures with no `proto_signature` before
  `get_proto_small` — 64% of Fenix selections yield zero protos, so this is pure query
  savings.
* Keep `machine.py`'s suppressor off Fenix, or re-key it on `android_fingerprint`.
* Add `.kt`/`.kts` to `interesting_extensions.json` **and** `kt/kts → kotlin` to
  `patch_extract._LANG_BY_EXT` with a Kotlin keyword set — these two go together; adding
  the extension without the language mapping is what produces the silent
  `deref`-on-every-lambda misreads (§6.1 #8).
* **Exit:** Fenix uuids and scored changesets visible on `reports.html`; counts
  reconcile (reports ≈ selections × protos); `selection` table shows EMPTY/JVM classes
  as *declined* rather than silently absent; zero Bugzilla writes.

### Phase 5 — turn triage on, canary only, autofile held to Firefox (S)
* `agent.products: ["Firefox", "Fenix"]`. Nothing in `agent/**` is product-aware
  beyond values that already flow through the seed.
* **Re-price first:** both draft cost estimates (~$4/day) were computed from the refuted
  snapshot. Start from the cadence replay (~5×), then net off the proto-dedup skip and
  the `no scored changesets` skip — and count that log line separately.
* **Exit:** 3-5 nights of per-day Fenix dossier counts, $/day, verdict distribution,
  and **which gate moved each verdict** — with the stale-gate-fires-on-85% prediction
  checked explicitly.

### Phase 6 — decide (S)
* Blind second opinion (`agent/second_opinion.py`, the one calibrated instrument, sens
  ~0.93-1.00 / spec 1.00) over every Fenix verdict; plus the free Bugzilla
  `regressed_by` check where available.
* A Fenix variant of the corpus filters only if reporting is to be armed.
* **Arm Fenix reporting only if** the Fenix SO corroboration rate is no worse than the
  desktop baseline **and** ≥3 verdicts were measurable. Otherwise stay observe-only and
  say so.

---

## 10. Explicitly out of scope

* **JVM/Kotlin crash support** — on the R8 argument (§7), not the searchfox one.
* **Fenix beta/release** — the mechanism is identical (§2.4) and costs one request to
  add later; nightly is where the regression premise lives.
* **Pre-126 Fenix** (≤125) — source is in the archived `mozilla-mobile/firefox-android`
  git repo with no hg node.
* **The glean/uniffi Rust family** — the regressor is not in mozilla-central (§5).
* **A historical Fenix eval corpus** — possible via the hg pushdate route, but the TC
  index cliffs at ~308 days and the corpus filters exclude Fenix by construction.
* **A daily cost cap** — pre-existing gap, not Fenix's to fix, but it bounds nothing
  today.

---

## 11. Decisions needed before Phase 1

1. **Proto-dedup across products** — free saving, or a desktop coverage loss? 21.8% of
   Fenix native signatures overlap Firefox and the dedup has no product clause. This
   must be decided, not discovered in prod.
2. **`thresholds.protos` for Fenix** — the current silent default of 1 is the only
   thing bounding spend; 50 (Firefox parity) un-bounds it.
3. **Bugzilla destination** — `Firefox for Android` vs `GeckoView` for a
   Fenix-attributed crash, given Socorro offers both and `get_bz_query` takes the first.
4. **Autofile for Fenix at all**, given ~40% of authors cannot be needinfo'd and
   Clouseau would outfile the organic Fenix crash-bug rate ~15×. Recommendation: hold
   autofile to Firefox indefinitely and revisit only after Phase 6.
5. **Whether "zero hardware-noise suppression" is acceptable** on a product whose
   crashes are overwhelmingly single-device singletons — or whether the
   `android_fingerprint` re-key is a prerequisite rather than a nice-to-have.

---

## 12. Operational notes

* `bin/predeploy.py` before every deploy (§ `next_session.md`); two deploys on 08-04
  each killed 3-4 in-flight ~$3 runs.
* Widening `config.products` ships **through the release phase**, so the enum DDL and
  the ingestion switch land in the same deploy unless `INGEST_PRODUCTS` exists first.
* **`bin/feedback.py` has never run on a schedule** — `bin/schedule.py` has exactly two
  jobs (`update_all` /20min, `reap_stale_agent_jobs` /15min) and the Procfile has no
  entry. Any exit criterion phrased as "Bugzilla-verified outcome" depends on a loop
  that is not wired.
* **`archetypes.seed_quietly()` is called only from `bin/init.py`**, which is not a
  Procfile entry point (`bin/release.py` runs `models.create()` +
  `HGAuthor.get_default_id()` only). If it was never run by hand, `_matching_archetypes`
  returns `[]` on every prod run and the archetype layer is inert. Same
  never-called-entry-point class as `create.create()`.
* Scratch/repro for every number in this document: `spike/_fenix_scratch/` (118 files,
  untracked).
