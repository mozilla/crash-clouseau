# Plan #20 — Release channel: plumbing and containment, not triage

> **Status:** design proposal (2026-08-31), not implemented. Produced by a 27-agent
> measurement campaign (11 dimension reports, a resolver over 19 contradictions / 20
> unsupported claims / 19 missing-work items, a critic over 8 / 10 / 9 more, and 14
> adversarial verification verdicts). The machine-readable evidence is
> `spike/_release_recon/RECON.json` and `spike/_release_recon/VERIFY.json`; every scratch
> script is in `spike/_release_recon/` and `spike/_release_verify/`. **VERIFY overrides
> RECON wherever they disagree, and RECON's `resolved`/`critic` override its individual
> reports.** Two claims came back REFUTED and appear in §8 only, never as work.
>
> **The conclusion is not the one the request assumed. Plug release's PLUMBING and
> CONTAINMENT; do not arm its triage or its filing.** The reasons are in §1 and every one
> of them is a number with a denominator.
>
> **Prod facts to read before anything else.** `heroku config -a
> crash-clouseau-augmented`, re-read 2026-08-31: `INGEST_CHANNELS="nightly beta"`,
> `AGENT_CHANNELS="nightly beta"`, `AUTOFILE_BUGS=1`, `OFFSTACK_ENABLED=1`,
> `OFFSTACK_OBSERVE_ONLY=0`, `SECOND_OPINION_ENABLED=1`, `API_WRITE_TOKEN` set. `heroku ps`:
> `web` 1× Standard-1X (one gunicorn worker, no `-w`), `worker` 1× Standard-1X = **512 MB**
> on `QUEUES="high default low"`, `agentworker` 3× Standard-2X = **1 GB each** on
> `QUEUES="agent"`, `clock` 1. `heroku addons`: Postgres essential-1 (204 MB of 10 GB, PG
> 17.9, created 2026-07-06 10:19 UTC) + Redis mini. `heroku drains` is EMPTY, so
> `heroku logs -n 1500` is a **1h40m–2h window**. Deployed release is **v139 = `3a584d8`**.
>
> **MOST OF GROUPS A AND B LANDED WHILE THIS WAS BEING WRITTEN.** HEAD is **`beee69e`**, eleven
> commits ahead of prod, and **none of them is deployed**:
>
> | commit | items |
> |---|---|
> | `e65f801` | **1** — retrigger authentication, CORS scoped to `/api/javast`, `UUID.exists` |
> | `deef91a` | **11, 12** — both channel switches fail closed, at both readers |
> | `446a8b8` | **13, 14**, and half of **15** — `fit_channel`, release declared+held, the guard loop off the env var |
> | `bd92fcb` | **2** — the four fabricated-rate slots; also corrects plan #18 item 24 |
> | `c083308` | **3** — the dangling antecedent, by NAMING the sample on every channel |
> | `9463641` | **6, 7, 19** — `to_analyze` channel filter, prune on an empty window, the `put_filelog` clamp |
> | `cb45f8c` | *(out of scope here)* `/api/selection`'s validate-then-ignore |
> | `44d4331` | **9, 10** — the `_facets_size` lie, the venue rationale (3 copies), `sigtrend`'s reason, the inert floor |
> | `96801aa` | **4** — `payload["filing_declined"]` at the `_autofile` choke point |
> | `dfdc750` | **10 (docs)** — DEPLOY.md and `docs/architecture.md` |
> | `1c9b81e`, `beee69e` | `docker-compose` must now name its channels; one stale comment |
>
> **Still open:** item **5** (the four indexes — it needs an `_ADDED_INDEXES`/`_ensure_indexes()`
> hook, a *third* schema-evolution mechanism, and should be reviewed like a migration framework
> because `_ENUM_ADDITIONS` has never once fired on this DB), item **8** (deleting
> `datacollector.get_changeset`, `single.py` and `inspector.get_crash_by_uuid` — and note
> `crashclouseau/create.py` has no caller either, so it is a fourth orphan), items **18** (the
> prod residue transaction) and the whole of Groups **C** and **D**.
>
> The tree moved four times while this was written, so **check `git log` before starting
> anything, not this paragraph.** Test baseline at `beee69e`: **2,148 pass / 0 fail** on Postgres,
> 2,148 / 103 silent skips on sqlite.
>
> **LINE NUMBERS ARE AS OF `4b7ceb7`** — prod v139's tree plus the one commit after it, which
> is the tree every measurement in §1, §2 and §3 was taken on. The commits recorded as SHIPPED
> above moved many of them (`config.py:546-548` → `:570-572`, `api.py:173` → `:200`,
> `api.py:193` → `:220`, `orchestrator.py:4171` → `:4232`, `models.py:3718` → `:3741`,
> `triage.py:588-590` → `:594-596`), so **re-locate by symbol, not by line** — every citation
> here also names its symbol. A citation describing the tree *after* a fix says so explicitly;
> the shipped items cite the tree at `4b7ceb7`, **before** their own fix. The eleven commits
> above have moved line numbers further still — the anchor is deliberately the tree the
> measurements were taken on, not the current HEAD, because re-anchoring ~300 citations to a
> moving tree would make every one of them wrong again by the next commit.
>
> **Prod holds 7,267 `release` `nodes` and a `release` `lastdate` row frozen at
> 2026-07-06, and ZERO `release` `builds` rows.** Every switch-on statement in this plan is
> conditioned on that residue. §3 item 18 deletes it.

---

## 1. Summary — the eight things that decide this plan

**1. There is an addressable set, and it is ~0.6 genuinely-new-on-release signatures per
cycle.** The recon's headline "ZERO over 26 dot-release windows" is an **instrument
artefact**: `spike/_release_recon/v26_addressable.py:16-23` passes no `date` key and its
docstring calls that "(lifetime)". SuperSearch silently substitutes a rolling ~7 days —
measured on build 20260810162159: no `date` → 7,611 reports / 1,229 signatures;
`date=[">=2026-08-10"]` → 112,684 / 9,047. So 24 of the 26 windows compared the
**last-7-day straggler residue** of two builds, and a signature cannot reach 50 installs
inside a 7-day residue of a six-month-old build. The tree documents this trap at
`report_bug.py:418`. Re-run with `date=[">=2025-09-01"]`
(`spike/_release_verify/r3_verify_new.py`): 383 raw hits at ≥50 installs, 25 after
dropping respin pairs and zero-side pairs, **25/25 confirmed to have literally zero
reports on the baseline build by a direct `signature=` + `build_id=` query**, and **5 of
the 25 had never appeared on any earlier release build**. Corrected number: **5 over 26
windows, 1 of them non-boilerplate**. The named counter-example that kills the zero
outright: **`firefox_on_glean::factory::create_and_register_metric` — 105 reports / 97
installs, on release 147.0.1 and 147.0.2 only, 0 reports on nightly, 0 on beta,
`bugzilla_apply._open_bugs_for_signature` → `[]`.** (97 is a lifetime
`_cardinality.install_time`; per the project's own rule that is neither installations nor
install-days, only a ceiling.)

**2. 5-6 of 207 crash-signature regression bugs in fx148-155 have a regressor the release
window could contain, and the desktop-native residue is 2 bugs in 20 months.** Population:
BMO, `cf_crash_signature` non-empty + `regressed_by` non-empty + `cf_status_firefoxN` in
fixed/verified, fx148-155 → 217 distinct bugs, **207 with `regressed_by`**, 173 distinct
regressors (re-derived independently at `spike/_release_verify/a1_bmo_population.py`, an
exact match to the recon). Two instruments: hg pushlog over 2026-01..08 gives **5/207 =
2.4%**; BMO `flagtypes.name=approval-mozilla-release+`, time-unbounded, gives **6/207 =
2.9%**. Either way **~0.6-0.75 per cycle**. Then: 2 of the 6 are query artefacts (2006998
filed **45.2 days** and 2023331 filed **40.3 days** *before* their regressor's uplift was
approved — the uplift is the vehicle, not the cause) and 2 are Fenix/Java (unsupported), so
the desktop-native residue is **2 bugs in 20 months** (2036484, 2049845).

**3. 73% of release selection PAIRS — and 65% of selection EVENTS, which is the share that
matters because spend is per run, not per pair — land on a build whose on-stack candidate set
is empty by construction.** 22 of 30 distinct (signature, buildid) pairs in the 31-run-day
replay are on a version-bump build; on the spend axis it is **65 of 100 selection events (62%
excluding the replay's first day — VERIFY does not record that sub-denominator)**.
Mechanism, confirmed in code not inferred:
`pushlog.collect` sets `files=[]` for cycle-merge members when `drop_merge_files`
(`pushlog.py:128-136`), which `pushlog()` defaults to
`suppresses_merge_extraction(channel)` = True on a release branch (`pushlog.py:194-195`,
`:39-64`); `Changeset.add` writes one row per interesting file, so merge members get none;
and `Changeset.find` filters `Node.channel == channel` (`models.py:502-524`, predicate at
`:516`). Say **on-stack**: the off-stack path is live in prod and does fill its 150 cap
there, **degenerately** — 150/150 `via_merge`, 150/150 `pref_flip`, all sharing one
pushdate, so `is_suspected_regression` is False for every candidate it can pick.

**4. Speed against humans is PARITY, not a win.** Ours: **8.24 d** median from build ship
to first selection, **uncensored n=17** — 13 of the 30 pairs are left-censored because the
build shipped before the replay window opened on 2026-07-27, and all 11 of the 153.0 pairs
read exactly 11.147 d because that is 07-27 minus 07-15, the replay's own start date.
Theirs, on the *same* ship clock over 91 release-tracked crash bugs
(`spike/_release_verify/d1_common_clock.py`): ship(earliest tracked major) → filing median
**−27.2 d**; **72 of 91 (79%) are filed BEFORE the version reaches release** and 33 of 43
(77%) have the regression range set before it does. For the 19 filed after ship: **+7.6 d**
to filing, **+10.4 d (n=10)** to regression range. The "21.7 h" the recon led with is a
*different clock* (filing → regression range, n=43 of 59 carrying `regressed_by`, because a
value set at creation leaves no history event). On the two desktop uplift-caused bugs
humans filed at **8.3 d** and **5.9 d** after the uplift approval and resolved in 13.2 d
and 7.3 d.

**5. Cost is unresolved between $207 and $4,050 a month and nobody has the UUIDs/day
number.** Both cost instruments used the wrong proto date bound: one passed
`date='>=2026-07-01'` (up to 61 days, past the run-day), the other passed **no `date` key
at all** (7-day default). The production shape is `search_date = '>=' + the oldest
build-DAY of the 3-build window, evaluated at the run-day`; measured on the same 30 pairs
(`spike/_release_recon/resolve_03_protos.py`): **11.7 kept protos/pair** (median 10, mean
25.8, max 140, 10/30 capped) against 13.0 and 6.3 for the two published shapes. Pairs/day
is a window question, not an instrument one: **2.47/run-day** over 133 run-days (329
distinct pairs, 2026-04-20..08-30) is the cost ceiling and **0.75-0.97/day** is the quiet
steady state (27/36 and 30/31 over the same month), the difference being four
ratio-branch bursts of 90-110 pairs whose configuration — a dot release arriving ≤4 days
after the previous build — occurs on **8 of 82 builds**. The multiplier nobody measured is
the per-tick lifetime union factor: **1.2-2.2x on n=4 pairs at DAILY granularity, applied
to a tick that runs 72x/day**. So **UUIDs/day is unresolved between 6 and 58 (9.4x)**.
For comparison, nightly's real spend is measured: **2,665-2,699 runs over 30-31 days,
$4,552.35-$4,598, mean $1.70-1.712, median $1.49, max $8.49**; beta 38 dossiers / $42.68 /
mean $1.123. The per-crash cost cap already fires on **683 of 2,665 nightly dossiers
(25.6%)** and 2 of 38 beta ones, so "the cap warns, it does not abort" is a quarter of
production runs.

**6. 98% of release LEADS are clamped below `min_confidence`, but a `culprit` is not, and
that path is unpriced.** 199 of 203 release SELECTED signatures (**98.0%**) clear
`min_age_days`=7 on the stale gate's own windowed clock (99.0% all-time), and
`_STALE_SIGNATURE_CLAMP` takes probable(70) → medium(50) < `min_confidence` 70
(`orchestrator.py:1972-1975`). But `_apply_signature_age_gate` returns early when
`v.decision != Decision.lead` (`orchestrator.py:2055-2062`) and the clamp map has no
`high` key (`:2061-2064`), so a `culprit`/strong verdict at 70 or 85 is **untouched** and
`culprit` is in `autofile.verdicts`. Nightly's measured mix over 30 days: **28 of 2,661
verdicts (1.05%) are `culprit`, all 28 at confidence ≥70.** The release culprit rate is
**unmeasured**, so the one filing path the gate leaves open has no price. ("Filing is
structurally blocked" is the wrong sentence; "every release LEAD is blocked, a culprit is
not" is the right one.)

**7. The strongest pro-release fact is real, and it is a different product.** At the
decision-relevant bar — signatures whose **90-day upstream distinct-`install_time` count**
sits below both upstream `spike.floor` values (`config/global.json`: nightly 3, beta 10),
i.e. crashes nightly and beta could **never** have selected — it is **10 of the top 200
release signatures (5.0%) and 37 of the top 350 (10.6%)**. **Unit caveat, because it is two
quantities:** production applies `spike.floor` to a day's REPORT count, not to installs
(§2.5), and a 90-day cardinality is neither installations nor install-days (§13.4), so this
bar is an approximation of "could never have selected", not the gate's own test.
About five are Firefox code:
`mozilla::MediaChangeMonitor::DecodeFirstSample` **1,493 release installs vs 1 nightly**;
`LdrLoadDll` 465 vs 1; `mozilla::gmp::GMPChild::RecvPreloadLibs` 398 vs 1;
`mozilla::Maybe<T>::value | mozilla::ContentSubtreeIterator::DetermineFirstContent` 416 vs
0; `@0x0 | neqo_udp::recv_inner` 274 vs 0. The weaker bar (zero upstream report in 90 d)
gives 0/100, 2/200, 8/350 — so the recon's "0 of the top 200" is 2, not 0. **Honestly: this
is not "find the regressor".** These are long-standing release-only crashes, not
regressions: they predate every build in any candidate window, the stale gate clamps them
by construction, and `regressed_by` is meaningless for them. §10 Q2 says what it would take
to serve them.
> And the "92.6% of install-hits" the top-200 claim rested on is a **top-500 panel share**.
> Same data, one facet depth at a time: 500 → 92.2%, 1,000 → 87.9%, 2,000 → 84.6%, 5,000 →
> 81.4%, **10,000 → 79.5%** — and the 10,000-deep facet saturates (626,158 reports, tail
> count 2), so even 79.5% is an upper bound. Quote 79.5%, never 92.6%.

**8. `update_all`'s empty `INGEST_CHANNELS` default already fired in production, and it is
what created the residue.** The DB was created 2026-07-06 10:19 UTC; heroku release **v8
(deploy) at 12:27:47 UTC**, **v9 "Set INGEST_CHANNELS" at 12:45:03 UTC**; the `release`
`lastdate.maxdate` is **2026-07-06 12:36:38.061871+00**, inside that 17-minute gap and
8m25s before the variable existed. The 7,267 release `nodes` occupy the perfectly
contiguous id block **12936..20202** (20202−12936+1 = 7267) with nothing interleaved — one
`Changeset.add` batch, one `put_filelog`, one tick, ever. The likeliest trigger is
DEPLOY.md step 4's `heroku run python bin/init.py` (`bin/init.py:19` = `update.update_all()`);
the clock's `IntervalTrigger(minutes=20)` first fires at boot+20min = 12:47:47 UTC, *after*
the config set, and `ps:scale` is not in the release audit trail so a clock scaled up
earlier cannot be excluded absolutely. The variable was **never CLEARED** — the audit trail
has "Set INGEST_CHANNELS" at v9 and v122 and no "Remove" entry at any point, and removals
*are* logged (`Remove BUGZILLA_TOKEN` v68, `Remove LIBMOZDATA_CFG_BUGZILLA` v77). The
hazard window is **app creation**, which is the one case DEPLOY.md's ordering does not
cover.

---

## 2. The measured picture of release

### 2.1 Cadence, and why the window is the binding constraint

Buildhub, `target.channel=release` + `source.product=firefox` + the repo's own regexp. **The
window choice is not neutral and this is where six reports drifted apart.**

| window | gaps | median gap | max gap |
|---|---|---|---|
| 2026-04+ | 26 | 5.25 d | 12.22 d |
| 2026-02+ | 32 builds | 6.90 d | **20.89 d** |
| 2025-10+ | 45 | 7.02 d | **20.89 d** |
| 2025-08+ / 2025-01+ | 53 / 83 builds | 7.07 d | **20.89 d** |

**Use 7.02-7.07 d as the cadence and 20.89 d as the maximum.** The 20.89-day gap falls
entirely outside 2026-04+, and it is what drives the retention result — so every
recommendation reasoned from a 2026-04+ slice is reasoning from a window that excludes its
own worst case (`spike/_release_recon/resolve_02_span_window.py`).

**Retention, replayed properly.** `datacollector.get_builds` / `Build.get_last_versions`
over **549 run-days** with `Node.clean` emulated at each run-day, against the same 83-build
list (2025-01-13..2026-08-26): build ROWS {1:1, 2:83, 3:465}; distinct build-**DAYS** 3 on
444 (**80.9%**), 2 on 104 (18.9%), 1 on 1 (0.2%); **short (<3) on 105/549 = 19.1%**.
No-pruning control: 3.8% short. Sweep: 30 → 80.9%, 35 → 90.2%, 40 → 94.7%, **45 → 96.0%**,
50-90 → 96.2%. The "0 of 33 triples exceed 30 buildid-days" kill uses the **wrong
statistic** — `Node.clean` prunes relative to *now* (`models.py:302-308`), not relative to
the newest build, and the newest release build is itself median **4.20** / p90 **10.47** /
max **20.35** days old at a run-day, so retention must cover (age of newest) + (3-build
span) = up to **51.6 d**, not 31.2 d. (Even on its own statistic, 2 of 81 triples exceed 30
d, max 31.24.)

**And the lookback alone cannot fix it.** `Build.put_data` inserts a `builds` row only
`if rev in revs_c` (`models.py:608`), i.e. only when a `nodes` row for that revision still
exists, and `put_filelog` only ever fetches FORWARD from `Node.get_max_date(channel)`. A
build whose node was pruned can never be re-inserted however far back Buildhub is asked.
**The retention is the binding constraint; `buildhub_lookback_ndays` is not.**

### 2.2 Supply — the addressable set, three ways

* **New on a dot release**: 5 over 26 windows at ≥50 installs, 1 non-boilerplate (§1.1).
* **Regressor landed on `releases/mozilla-release` outside a cycle merge**: 5-6 of 207
  (2.4-2.9%), 2 desktop-native in 20 months (§1.2).
* **New on release at all**: novelty arrives at the MAJOR, never at a dot. 9 of 188
  above-threshold signatures per cycle (4.8%) have zero release reports in the previous
  120 days; **8 of those 9 sit on buildid 20260812182057, the merge-day MAJOR**; 1 of 188 is
  new-on-release AND unseen upstream. On the dot windows it is 0. And "new on release" does
  not mean new anywhere: release clears `signature_age.other_channel_floor`=20 on ~100% of
  signatures (30/30 and 25/25, 80-93% of that on `esr*` labels alone), so `first_seen`
  degenerates to the unfloored all-channel value the config itself calls "not usable
  as-is".
* **Already covered**: 16 of the 30 release-selected signatures were already ingested AND
  carried a `status='done'` dossier inside one 30-day prod window; 24 of 30 appear in
  `selection`. The 14 without a dossier include 5 boilerplate (2 shutdownhang, 1
  AsyncShutdownTimeout, 2 stackoverflow) and ~8 genuine — but the prod window is only 30
  days, so **14/30 is an upper bound on release-unique supply**.

### 2.3 The sample — an interval, not a point

Release is ~10% sampled by antenna's `is_firefox_desktop` → `(10, ACCEPT, REJECT)`, but
`MOZILLA_RULES` puts the ACCEPT-100% rules **first**: `throttleable_0`, `has_comments`,
`has_phc`, `is_background{gpu,plugin,rdd,socket,utility}`. So the accept rate is
**class-dependent and the honest statement is an interval**:

* Background-process reports across the 30 selected pairs: **0 of 23,094 (0.00%)**.
* `throttleable=F`: **429 of 23,094 = 1.86%** on average but **0.00%-43.42% ACROSS pairs** —
  `std::_Reverse_vectorized` 43.4%, `mozilla::dom::OptionalServiceWorkerData::AssertSanity`
  23.9%, `recvfrom | std::sys::net…` 20.0%, `nsBlockFrame::RemoveFrame` 19.7%. **4 of 30
  pairs (13%) at 2-5x the nominal rate.**
* `has_comments` / `has_phc` are **UNMEASURABLE**: `user_comments` and `phc_kind` are
  `permissions_needed: ['protected']`, and a *filter* on them is silently ignored —
  `user_comments=!__null__` returned the full unfiltered 602,701 with `errors: []`.
* The 1% end of "1% to 100%" is **EMPTY on release**: `ipc_channel_error=ShutDownKill` in
  all of 2026-08 is release **1** report, esr 0, beta 0, nightly 1,068. Operative range is
  **10%↔100%**.

**Install-count understatement is a model, not a measurement, and it is an interval.** The
only DIRECT measurement is the per-(signature, build) reports-per-install `k` distribution
on the unsampled same-codebase esr153 population (14 signatures on build 20260716160951,
`spike/_release_recon/resolve_04_thinning.py`): median `k` **1.150** → a 10% per-report
Bernoulli leaves a median **11.33%** of installs visible → **8.83x**, range 4.27-9.67x. The
two published figures are models: 4.1x (antenna's rule over a channel-WIDE esr153
k-histogram, mean k 2.314, out-of-sample check predicted reports/install 1.26 vs release
153.x's actual 1.44) and 7.80x (a one-parameter shifted-geometric fit to an observed
reports/install of 1.036, bounded by its own author in [50, 500]). The channel-wide factor
is somewhere in **1.25-4.1x** depending on tail coverage; the per-signature factor is
**~8.8x**. Do not state either to two significant figures.

### 2.4 Prod's release residue, exactly

Read-only `heroku pg:psql`, app `crash-clouseau-augmented`, v139:

| | value |
|---|---|
| `nodes` release / beta / nightly | **7,267** / 9,184 / 6,329 = 22,780 (release **31.90%**) |
| release pushdate span | 2026-06-07 18:10:51 → 2026-07-06 12:00:35, 22 `merge=true` |
| release node id block | 12936..20202, contiguous — **one batch, ever** |
| `changesets` on release nodes | merge=f **19,527** (2,628 nodes) + merge=t **793** (7 nodes) = **20,320 of 33,147 = 61.30%** |
| `lastdate` release | mindate 2026-06-07 18:10:51, maxdate **2026-07-06 12:36:38.061871+00** |
| `builds` release | **0** (73 total = 61 nightly + 12 beta) |
| release rows in `selection` / `sigdaily` / `chandaily` / `uuids` / `scores` | 0 / 0 / 0 / 0 / 0 |

All seven release build revisions are present as nodes with `pushdate == build.id` to the
second (c3ced0930614=20260608154138/151.0.4 … d6222c84b8b1=20260706120035/152.0.5), and the
2 earlier and 9 later release build revisions return 0 rows — so the set is exactly the
30-day fresh-DB backfill window, complete.

**Why zero `builds` rows** — the recon's mechanism ("`if rev in revs_c` held") is FALSE:
`Build.put_data` was never called. `update.update_builds` guards it with `if data:`
(`update.py:222-223`) and `buildhub.get` returned `{}`, for two independent upstream
reasons: (a) in July the deployed line was `date -= relativedelta(days=config.get_ndays())`
= **3** days (it became `get_buildhub_lookback_ndays()` = 30 only at `4749e29`,
2026-08-26), so from maxdate 12:36:38 the request was `build.id >= 20260703123638` and **six
of the seven builds were never candidates**; (b) the one in-window build (20260706120035 /
d6222c84b8b1 / 152.0.5) had its earliest Buildhub `download.date` at **2026-07-06T16:30:14
UTC, 3h54m after the tick**. Seeded locally with a payload, `put_data` inserts all 7 rows
correctly. **`put_data` has no defect.**

**What the residue already cost.** All 19,527 non-merge release `changesets` are
`analyzed=true` and 10,838 carry real `added_lines` — so `update.analyze_one_patch` fetched
and parsed **2,628 mozilla-release raw-rev diffs** on the serial patch chain, for a channel
nobody had turned on. It got there because `analyze_one_patch` calls
`models.Changeset.to_analyze()` with no channel argument (`update.py:189`) and that branch
(`models.py:378-390`) has **no channel filter** — the one channel-blind path in the repo.
At the 3.45-6.51 s/fetch figure the repo itself quotes (`pushlog.py:96`) that is ~2.5-4.8
hours of the shared queue, already spent. The remaining 793 are permanently inert
(`Node.merge.is_(False)` at
`models.py:387`; nightly has 338 rows of the same shape).

**What a switch-on would cost, today.** `put_filelog('release')` reads
`Node.get_max_date` = 2026-07-06 12:00:35 (which wins over `LastDate.maxdate`, so branch 1
is the live one) and requests **55.89-56.0 days**, growing one day per day. Measured live
off `releases/mozilla-release` for exactly that window: **30,430,633 bytes = 30.43 MB**, 323
pushes / **20,094 changesets** / 175,570 file entries, fetch 9.8 s, `pushlog.collect` 0.1 s,
**total 10.0 s**; process peak RSS 139.7 → **215.2 MiB** (the increment attributable to
fetch+parse is ~106 MiB, on top of a *larger* baseline on the worker dyno). The 335 s is
real in magnitude and **misattributed**: the cost is `Changeset.add` — replaying the
measured 20,094-node payload against a LOCAL Postgres took **276.1 s / 80,491 SQL
statements / 20,099 commits = 4.01 statements per node**, because
`HGAuthor._get_or_create_id` (`models.py:804-822`) does INSERT..ON CONFLICT..RETURNING + a
SELECT + **`db.session.commit()` per node** and `Node.__init__` calls it per changeset. On
RDS, ~335 s is consistent. Patch extraction is *not* the cost: 19,727 of 20,094 (98.2%)
arrive `via_merge` across three cycle merges (6,888 / 7,448 / 5,391) and
`suppresses_merge_extraction("release")` is True, so only **110 `changesets` rows / 34
nodes** survive ≈ **~2 minutes** of `patch.parse`.

**"441 MB at a 183-day gap is an OOM" is REFUTED as stated** — see §8.

### 2.5 What is DEAD or INERT on release

* **`spike.floor.Firefox.release = 50` is structurally inert.** `evaluate_days`
  (`utils.py:299-347`) computes `spiked` first and `if not spiked: continue` *before* the
  install test; `numbers[day]["count"]` is the SUM over that day's buildids of each
  buildid's report count (`datacollector.py:277-279`), and for a given buildid reports ≥
  `cardinality_install_time` by construction (0 coerced to 1). So a day's `count` ≥ its max
  per-buildid installs, and **any floor ≤ the install threshold can never exclude a
  selectable day.** Confirmed empirically: floors 1/3/10/20/50 all give exactly **329 pairs**
  over 133 run-days; the first binding value is **100**. Its only live effects are the
  `UNTESTABLE_PREFIX` record and the `Selection` write-volume filter.
* **`spike.mature_after_days` / `mature_installs` release entries are DEAD.**
  `datacollector.get_maturity_bar("Firefox","release")` returns `(None, 1)` (probed live),
  so `mature` is always False off nightly. Beta's are dead the same way.
* **`thresholds.installs.Firefox.release = 50` is defensible and is NOT a volume
  calibration.** The per-(signature, build) install distribution on release is almost
  identical to beta's (p50/p75/p90/p95 = **1/2/5/12 on both**), so 50-vs-6 changes what an
  install *means*, not how loud it is. Its machine-equivalent is an **interval, ~205 to ~440
  machines**, from two models neither of which is measured (§2.3). Decision-irrelevant:
  nobody proposes changing it.
* **The 24 h build-axis attribution window is inert on release.** A release build's revision
  pushdate equals its buildid to the second on 27 of 27 builds, and only 2 of 26
  consecutive pairs are under 24 h apart, so `min(buildid−24h, prev_pushdate)` returns
  `prev_pushdate` on **92.3%** of builds and the widened window IS the deployed window.
* **`_incomplete_fix_bug` and `_fixed_after_build_bug`**: the second reason to file is
  effectively dead on release — that rests on `_incomplete_fix_bug`, which will essentially
  never fire (on the same top-100 panel, of 78 (signature, FIXED bug) pairs only 4 pass its
  ±7-day condition 3, and retention then drops the old ones). `_fixed_after_build_bug` fires
  on **4 of the top 100 release signatures by installs, at their modal build**
  (`filing_08_fixedafter.py`) — a **top-N PANEL, not the population the gate sees** (§13.5),
  and its 6.0% nightly comparison comes from a docstring on a different instrument, so **the
  difference is not interpretable** and the 4/100 is reach on a panel, not a death
  certificate. Release's build age inverts its stated DOMAIN, but that is a nightly-shared
  function and not this plan's business.

---

## 3. Work items

Tags: **[C]** correctness (silently wrong today, on desktop or on release) · **[N]** new
behaviour · **[T]** tuning. Ordering: the fixes whose value is *desktop* land first, so the
investigation pays for itself even though release is not being armed; the containment
lands before anything could arm release; the release-only material is documented and
**not shipped**.

**Groups A and B ship. Group C is written down and shipped only if §10 Q4 says yes. Group
D is documentation or deletion and must never be tuned.**

### Group A — live defects, value is desktop, ships regardless of release

**1. [C] SHIPPED — `/api/tasks/retrigger` had no authentication of any kind.**
Committed as **`e65f801`** (2026-08-31), *not* in prod v139. Recorded here because this plan
is also the record.
The route was `@cross_origin()` POST with no token read at all (`api.py:193-213`; the only
`_require_write_token` call site in the tree is `api.py:173`, and there are zero
`before_request` hooks). Measured exposure on the deployed app: one anonymous `GET
/tasks.html` → **200 / 569,567 bytes containing 500 distinct real uuids** with
`retriggerTask('<uuid>')` already wired; **3,188 of the 3,488 rows in `uuids`** have the
`crashstack` rows `build_seed` needs, so each was one unauthenticated POST from a full run
at a measured mean **$1.70** / max **$8.49** (3,188 × $1.70 ≈ **$5,400**, more than the
entire measured 30-day spend of $4,598). Five sequential anonymous POSTs → `[200 ×5]`, 5
jobs enqueued; the CORS preflight from `Origin: https://evil.example` returned **200 with
`Access-Control-Allow-Origin: https://evil.example`**. No rate limit, no per-caller cap, no
global spend cap (`max_cost_usd_per_crash` warns at `orchestrator.py:3776-3781`, it does not
abort). The job it starts is `run_crash_triage` with `permission_mode="bypassPermissions"`
and `_BUILTIN_TOOLS = ["Read","Grep","Glob","Bash"]` on a dyno holding `ANTHROPIC_API_KEY`,
`LIBMOZDATA_CFG_BUGZILLA_TOKEN`, `SOCORRO_TOKEN`, `DATABASE_URL` and `API_WRITE_TOKEN`.
What shipped: `_require_viewer()` as the first statement of `retrigger()` (the **viewer**
token, not the write token — `static/clouseau.js` sends no `X-Clouseau-Token` and
`VIEW_COOKIE` is `httponly`, so gating on the write token would have closed the hole and
silently broken the tasks-view button); CORS scoped to `resources={r"/api/javast": …}` and
`@cross_origin()` removed from this route; the `request.args` uuid fallback **deleted** (it
was the one shape a cross-site HTML form POST could use); and `models.UUID.exists` + a 404,
replacing a 500 from `UUID.get_id`'s `…first()[0]` on `None`. New test file
`tests/test_retrigger_auth.py` (187 lines) — there was previously **no** test asserting the
route's authorization, despite 20+ `retrigger` hits in `tests/`.
*Bounds worth keeping: per-uuid amplification is bounded by `Dossier.claim_running`
(`orchestrator.py:3725-3733`), Bugzilla writes are capped at `daily_cap` per 24h per channel
(comments count — `bugzilla_apply.py:1441` sets `filed: True`), and the third channel gate
at `bugzilla_apply.py:1059` (`autofile_channel_declared`) is NOT bypassed by `force` and
fails closed, so a retrigger can spend on any ingested channel but can only WRITE on
nightly.*
**Exit:** anonymous `POST /api/tasks/retrigger` → 403 and 0 jobs enqueued, asserted in CI.

**2. [C] SHIPPED as `bd92fcb` (not deployed) — the four fabricated-rate format slots in the
hardware-noise abstain reason.**
Built from droppable clauses: `None` drops the clause, a measured `0.0` stays. It also corrects
the false "reaches the filed bug and the UI" claim **in both places** — the comment here and
`plans/18-beta-channel-support.md` item 24 — and adds the `broken_cpu_rate=None` test arm that
did not exist (`tests/test_bit_flip_gate.py`, +55 lines). The record of why follows.
Files: `crashclouseau/agent/orchestrator.py:2844-2855`; comment to delete at `:2838-2839`;
model of the correct shape at `agent/triage.py:693-702` and `report_bug.py:748-753`.
All four slots fabricate. **The LIVE half is the two the recon did not look at:** `:2850`
`100 * (flip_rate or 0)` and `:2852` `100 * (cpu_rate or 0)` are the SIGNATURE's own rates,
and `sigage.hardware_noise` returns `broken_cpu_rate=None` **by design** (`sigage.py:651`
`_sum("cpu_info", …, if_empty=None)`, reasoned at `:638-641`: "an empty `cpu_info` facet is
an UNKNOWN") while `bit_flip_rate` gets `if_empty=0` at `:650`. So an unknown Raptor Lake share can
publish "**0% come from a known-defective Raptor Lake CPU (background 4%, meta bug
1975808)**" on **nightly and beta today**. `:2851`/`:2853` are the population slots and are
release-only (`sigage._POPULATION_RATES` has no release key), where the sentence would read
"(Firefox background 0%) … (background 0%)" — a 50% share against a population running at
0%.
**Latent, not an incident, and say so.** Prod: `hardware_noise_signature_suppressed` has
fired **4 times** in 2,706 dossiers (nightly 2026-08-22/23/25/27, 0 on beta) and all 4 quote
a correct 2%/4%; `payload LIKE '%background 0%%'` = **0 of 2,706**; the sibling denominator
is 1,144 nightly dossiers with `signature_bit_flip_rate` vs 1,131 with
`signature_broken_cpu_rate` = **13 (1.14%) flip-known/cpu-UNKNOWN**, all 13 reading flip=0.0
and 12 of 13 under the 5-report floor. So it has fired **0 times**.
**The comment at `:2838-2839` and plan #18 item 24 were FALSE and are corrected in `bd92fcb`
(which edits `plans/18-beta-channel-support.md` too):** "in a string that reaches the filed bug
and the UI" is impossible.
`hardware_noise_signature_suppressed` is in `corroborations.suppressions()`, and
`bugzilla_apply.py:1088-1093` returns `{"filed": False, "skipped": "suppressed by …"}`
**before any BMO request** and before `_incomplete_fix_bug`, the only path an abstain could
otherwise take; `_apply_eligible` (`bugzilla_apply.py:77-88`) refuses the manual apply; and
`report_bug.build_bug_comment` never reads `abstain_reason` (0 grep hits in
`report_bug.py`). Two tests lock it: `tests/test_bit_flip_gate.py:256-264` and
`tests/test_incomplete_fix.py:217-223`. The **real** surface is the anonymous page:
`orchestrator.py:1773` `rationale = verdict.abstain_reason or ""` → `verdicts.rationale`
(`models.py:2946`) → `bugzilla_apply.py:120-124` → `templates/crashstack.html:203`, verified
by an anonymous GET returning 200 with the sentence.
Fix: build the sentence from parts as `triage._hardware_noise_lines` does — emit a clause
only when its rate is not None, and append " (X background N%)" only when the population is
not None. **Traps:** do **not** replace `or 0` with `is not None else 0` on the signature
rates (flip legitimately IS 0.0, cpu legitimately IS None — the clause has to be
droppable); the "both unknown" case is unreachable because
`_signature_is_mostly_hardware` (`orchestrator.py:2635-2675`) is a positive test on one of
them, so it must not get a fallback either; and the prose must name **which arm fired**
(`flip_rate >= 0.2` OR `cpu_rate >= 0.7`) or a reader cannot see why it was suppressed. Do
**not** fix it by borrowing nightly's rates — `sigage._population`'s docstring
(`sigage.py:413-425`) makes the absent-vs-named-but-unmeasured split deliberate.
**Exit:** a unit test with `broken_cpu_rate=None` asserting no `0%` clause appears, and one
per channel asserting the population clause is present on nightly/beta and absent on
release. **Shipped in `bd92fcb`** as three arms in `tests/test_bit_flip_gate.py`:
`test_an_unmeasured_rate_drops_its_clause_instead_of_printing_zero`,
`test_a_rate_measured_at_zero_still_prints`, and
`test_an_unmeasured_population_drops_the_background_never_calls_it_zero` (population absent on
release, present on beta). Before that commit the file fixed `"channel": "nightly"` at `:64`
and asserted only `assertIn("mostly hardware error", …)` at `:594`, so nothing would have
caught a wrong fix.

**3. [C] SHIPPED as `c083308` (not deployed) — `_cpu_spread_line`'s closing parenthetical has
undefined denominators on every channel but nightly.**
Files: `crashclouseau/agent/triage.py:588-590` (inside the unconditional `.format()` tail
`:582-595`); the guarded branch that owns the numbers is `:571-576`.
The literal "**9 of the 26 at 100%, against 118 of the 200 overall**" prints for nightly,
beta, aurora, release, esr and None — **6 of 6**. "The 26" and "the 200" are introduced only
in the nightly-only branch at `:573` ("13% of them (26 of 200 sampled 2026-08-21)"), which
is skipped when `median is None`. So on beta the model reads bare "the 26"/"the 200" one
sentence after being told this channel's concentration "has NOT been measured".
**Live on beta today, in the model prompt only: 38 of 38 beta dossiers satisfy every print
condition** (vs 521 of 2,668 on nightly), reaching the principal `_user_prompt`
(`triage.py:717` → `:1297` → `:1326`) **and** the blind second opinion
(`second_opinion.py:92`) — two prompts per run. **163 bytes** of a ~6 KB prompt.
**Direction, honestly:** it is an *anti*-support caveat (9/26 = 34.6% vs 118/200 = 59%), so
it biases toward **abstaining**, not toward filing; it is byte-identical on nightly and beta
so it adds no differential skew; and on the real beta population it is largely off-topic (0
of the 38 are one-model, only 11 have top share ≥ 0.32, min 0.059 max 0.988). It cannot
reach a Bugzilla comment (`report_bug.py:812` returns "" while `median is None`) and cannot
reach the web UI (0 template hits for `top_cpu`/`cpu_spread`).
Fix: move the numbers into the `median is not None` branch, or restate the clause naming its
own population ("in the Firefox-nightly background sample, 9 of the 26 one-model signatures
carry a known Firefox bug against 118 of the 200 overall"). **Traps:** "in that same
sample" at `:588` must change with the numbers or the sentence dangles on nightly too; the
condition must stay `median is not None`, **never** `channel == "nightly"`, because an
absent channel deliberately falls back to nightly (`sigage._population`).
Two alleged sibling sites are **refuted**: `triage.py:541` is a **docstring** (`grep -rn
"__doc__" crashclouseau/` = 0 hits — no docstring reaches any prompt, comment or page), and
`report_bug.py:815` is guarded at `:812` (measured: `_cpu_spread_sentence(c,"beta")` and
`build_hardware_note(c,"beta")` both return `""`, also aurora and release).
**Exit:** the beta output contains no bare "that same sample" and no undefined "the 26"/"the
200"; wherever the 9/26 and 118/200 figures appear, the Firefox-nightly sample is named beside
them, **on every channel** — which is the branch `c083308` took, keeping the figures and naming
the sample (`triage.py:594-596` at `c083308`: "in the Firefox-nightly sample of 200 signatures
(2026-08-21), the 26 that sit at 100% … 9 of those 26, against 118 of the 200 overall"). Pinned
by `tests/test_bit_flip_gate.py:1022-1055`
(`test_every_denominator_in_the_spread_names_its_own_sample`, shipped in `c083308`), which
asserts `assertNotIn("that same sample")` and `assertIn("Firefox-nightly sample of 200
signatures")` on nightly/beta/release/None. `grep -rn "9 of the 26|118 of the 200|that same
sample" tests/` = **3 hits**, all in `tests/test_bit_flip_gate.py:1025-1044`. Before that
commit it was **0 hits** and `tests/test_shipped_channels.py:527-547` asserted only on strings
absent from beta.

**4. [N] Record a DECLINED filing.**
Files: new `models.Dossier.record_filing_skip(uuid, info)` beside `record_filing_error`
(`models.py:2726-2743`); single call site `crashclouseau/agent/orchestrator.py:3672-3675`.
Inside `autofile_bug` (`bugzilla_apply.py:1030-1540`) there are exactly **two** DB writes —
`record_filed_bug` (`:1536`, success only) and `record_filing_error` (`:1525`, BMO rejection
only). All ~25 `return {"filed": False, "skipped": …}` sites persist **nothing**, and
`_autofile` discards the returned dict except for a `logger.info` into an app with no log
drain and a ~2-hour log window. `feedback.py` cannot see a decline either (`_filed_bugs`
iterates `Dossier.filed_bug_rows()`, which skips rows without `filed`), `Selection.outcome`
has no filing state, and there is no dossier column.
**The measured cost is on NIGHTLY, not beta.** Prod, grouped by build channel: nightly
**reached the rung 117, filed 83, and 34 reached the rung and left no record of why they
were declined** — 27 of them post-arming and `status=done` (2026-08-05..08-26, ~1.2/day
over 22 days), 7 pre-arming. Beta is **0/0/0**: 37 abstain + 1 lead at confidence 25
against `min_confidence` 70 means 0 reached the rung, and re-deriving `_incomplete_fix_bug`
against live BMO for all 9 distinct (signature, product, buildid, first_seen) tuples behind
the 38 beta runs gives 7 with no FIXED bug at all and 2 failing condition 3 by years. So
the held-beta instrument has cost beta **exactly nothing**, and plan #18 item 20's last
sentence is unanswerable **for nightly**.
Fix: one new jsonb key, `payload["filing_declined"] = {at, channel, buildid, signature,
skipped, gate}`, written at the single choke point where `_autofile` already holds the uuid
and the full result dict. ~4 lines. Do **not** touch the ~25 return sites.
**TRAP 1, the important one:** it must **not** go under `filed_bug`. `already_filed`
(`models.py:2748-2755`) returns any truthy `filed_bug` with no `filed` test and `filed_bug`
IS in `_STICKY_PAYLOAD_KEYS`, so a skip recorded there survives every retrigger and reads
as "already filed" **forever** at `bugzilla_apply.py:1111-1113`, permanently closing a
crash whose bug was never created. Three more unhardened readers would mis-render it:
`list_tasks` plucks `filed_bug.bug` unconditionally (`models.py:2604`) and four skip paths
do carry a `bug` key (`:1183`, `:1295`, `:1311`, and `prior`); `html._task_view` increments
the "filed" counter on any truthy `filed_bug` (`html.py:250`); `retrigger_agent` would warn
"ALREADY went to bugzilla" (`orchestrator.py:4307-4314`). The comments at `models.py:2790`,
`:2876-2877` and `:2910` that say "a SKIP is recorded under that same key" describe an
intent the code is only half-hardened for.
**TRAP 2:** the record must **not** be sticky — mirror `filing_error`'s deliberate
exclusion (the stated rule at `models.py:2185-2190` is that stickiness is only correct for a
fact about the OUTSIDE WORLD) and pop it in `reset_for_retrigger` (`models.py:2319-2325`).
Keeping only the latest attempt's decline is the right trade for a counting instrument.
**Cheap partial needing no code, for the nightly 27:** `verdict in ('lead','culprit') AND
confidence >= 70 AND NOT (payload ? 'filed_bug')` already enumerates the rows whose reason
is missing — worth a one-off replay of the gates before `Node.clean` erases the 30-day
pushlog window they depend on.
**Exit:** a declined nightly run writes `filing_declined` and does **not** change
`already_filed`, `list_tasks`, the tasks-view filed counter, or `retrigger_agent`'s answer.

**5. [C] The missing indexes, in MEASURED benefit order — and the hook that stops them
shipping dead.**
Files: `crashclouseau/models.py` (`Changeset` `:336-350`, `Node` `:178-198`), plus a new
`_ADDED_INDEXES` + `_ensure_indexes()` called from `models.create()`.
Prod `pg_indexes`: `nodes` has **only** `nodes_pkey`, `changesets` **only**
`changesets_pkey`. A fresh local schema built by `models.create()` on postgres:16 is
**identical**, so this is missing in the MODEL, not drifted in prod.

| order | index | measured benefit |
|---|---|---|
| 1 | `changesets(fileid)` | owns ~**80%** of 3.37e9 seq tuple reads; `Changeset.find` **3.81 → 1.23 ms**/report, `get_scores` **2.88 → 0.77 ms**/frame |
| 2 | `changesets(nodeid)` **+** `builds(nodeid)` | the RI cascade: 5,130-node DELETE **4,847.7 → 37.0 ms** (131x); 6,000 nodes ×2 children 4,863.3 → 71.7 ms; 400 nodes 375.2 → 7.2 ms |
| 3 | `nodes(channel, node)`, preferably **UNIQUE** | lookup 1.10 → 0.32 ms, worth **~0.2 s/day alone** (172 scans/day) — not worth a deploy on its own, but it also closes the missing-upsert hole in `Changeset.add` (`models.py:419-420`); prod has **0** duplicate `(channel, node)` groups over 22,780 rows, so verify uniqueness and take it |

Denominators: `changesets` = **88,157 seq scans / 3,373,516,944 seq tuples in 56 days**
(~60M/day) on a 34k-row table ≈ 6-12 s/day of DB CPU **at an assumed 5-10M tuples/s on
essential-1 — a scan rate nothing in this campaign measures, so the seconds are a model, not a
reading of prod DB CPU**; ~20% of that is the RI cascade (**~659M of 3.37e9 seq tuples**,
19,340 of 88,157 scans, matching `n_tup_del`=19,340 on `nodes`), the rest is
`Changeset.find` (once per ingested report) and `get_scores` (once per frame). `nodes` =
9,609 seq scans / 201,978,500 seq tuples in the same window. `changesets(nodeid)` alone
barely moves the read path (2.64 / 2.67 ms) — **the claim's ordering is backwards and
`changesets(fileid)` is the one it omits.**
**One dated spike this fixes:** the beta merge blob (5,130 nodes at pushdate 2026-08-13
14:15:59) crosses the 30-day retention around **2026-09-12**, paying ~4.8 s measured locally
(likely 2-5x on essential-1) inside an RQ `update` job; another ~3.2 s around 2026-09-26
(3,408 nodes at 2026-08-27 13:34:59). The ~7,000-node blob the claim named is **release** at
2026-06-10 17:24:36 and `Node.clean` never runs on it — see item 7.
**THE TRAP: the naive fix ships dead.** `models.create()` (`models.py:3718`) runs
`db.create_all()` only when `not inspect(engine).has_table("lastdate")`. There is
`_ensure_tables()` for post-deploy TABLES (`_ADDED_TABLES`, `models.py:3919`) and
`_ensure_enum_values()` for enum VALUES (`_ENUM_ADDITIONS`, `models.py:3677`) and **nothing
at all for INDEXES**. Adding `db.Index(...)` to `__table_args__` creates it on a fresh DB
only; prod (created 2026-07-06) would never get it — **the same class of dead config as
`_ENUM_ADDITIONS`**. Needs `_ADDED_INDEXES` + `_ensure_indexes()` issuing `CREATE INDEX IF
NOT EXISTS`, idempotent, log-not-raise, exactly like `_ensure_tables`, so `bin/release.py`
applies it on every deploy. Plain `CREATE INDEX` is fine at 34k rows (<100 ms ACCESS
EXCLUSIVE).
Do **not** add an advisory lock to `put_filelog`: one `worker` dyno, RQ one job at a time,
0 duplicates in prod.
**Exit:** after a deploy, `pg_indexes` on prod shows all four; `EXPLAIN` of the
`Changeset.find` join uses `changesets(fileid)`; `pg_stat_user_tables.seq_tup_read` on
`changesets` stops growing at ~60M/day.

**6. [C] `Changeset.to_analyze()`'s no-channel branch has no channel filter.**
Files: `crashclouseau/models.py:378-390`; caller `crashclouseau/update.py:189`.
This is the path that spent **2,628 mozilla-release hg fetch+parse cycles, which marked
19,527 changeset rows analyzed**, on a channel nobody had turned on (§2.4) — one
`patch.parse` per NODE (`update.py:226-231` → `Changeset.add_analyzis`), not one per row.
It is the **one channel-blind path in the repo** —
every other `query(Node…)` in `models.py` is channel-filtered (lines 235, 265, 295, 304,
314, 324, 332), and `put_report`'s `to_analyze` call *is* channel-filtered.
Fix: give the no-channel form an `INGEST_CHANNELS` filter, or have `analyze_one_patch` pass
the ingested channels. While there, note the sibling quality item: that query has **no
`ORDER BY`** (`DISTINCT ON (nodes.id)` makes it behave as id-order on Postgres today) — the
exact defect its sibling `UUID.to_analyze` documents as load-bearing.
**Exit:** with `INGEST_CHANNELS="nightly beta"`, `to_analyze()` returns nothing for a seeded
release changeset. `EXPLAIN (ANALYZE)` on the current query is 3.373 ms with "Rows Removed
by Filter: 32016", so this is a correctness fix, not a performance one.

**7. [C] `Node.clean`'s retention is coupled to INGESTION, not to time.**
Files: `crashclouseau/models.py:302-308`; its only caller `Changeset.add`
(`models.py:434`).
`Node.clean(date, channel)` is channel-scoped by construction and reachable only via
`put_filelog` → `update()`, i.e. only for channels in `INGEST_CHANNELS`. It runs and is
exact to the second on both ingested channels (nightly min pushdate 2026-08-01 09:49:16 vs
its last cutoff 09:05:16; beta 09:03:15 vs 08:46:02). **The gap is that a channel dropped
from — or never added to — `INGEST_CHANNELS` keeps its rows for ever**, which is exactly how
31.9% of `nodes` came to be two months stale. Second, self-correcting gap: `Changeset.add`
returns early on an empty pushlog window (`models.py:413-414`) without calling `Node.clean`.
Fix: a time-driven, all-channel sweep — a clock job over every value of `CHANNEL_TYPE`, or
`Changeset.add` cleaning unconditionally including the empty-window early return.
*The claim's sharper inference — "`Node.clean` is not running or not doing what it claims, a
defect affecting all channels" — is **false**; do not write that.*
**Exit:** with `INGEST_CHANNELS="nightly beta"`, a seeded release node older than
`max_ndays` is gone after one clock tick.

**8. [C] Delete three dead paths.**
Files to delete: `crashclouseau/datacollector.py:566` (`get_changeset`);
`crashclouseau/single.py` (34 lines, whole file); `crashclouseau/inspector.py:73-85`
(`get_crash_by_uuid`, whose only consumer is `single.py:33`).
* `datacollector.get_changeset` returns **None on all three channels** when CALLED for real
  (nightly 20260823213825, beta 20260826090609, release 20260824154132), because its filter
  is `@"hg:hg.mozilla.org/".*:[0-9a-f]+` while every current `topmost_filenames` term is
  `git:github.com/mozilla-firefox/firefox:<path>:<40hex>`. **Death date is the hg→git
  migration, ~2025-11-11..18** (last hg build: nightly 20251110211328, beta 20251205151434;
  first git nightly 20251118090705) — *not* "every term is now git:", which is false as a
  statement about the field (the hg regex still matches 379,983 crashes over 2026-08, esr
  354,251 / release 20,035 / beta 1,710 / nightly 1,113 — pre-migration builds still
  crashing). **DELETE, do not repair:** repointing the filter at `git:` restores a facet vote
  with no minimum-ballot floor that already answered from **n=1** (release 20260810162159:
  hg=1 / git=100,210 of 112,687 reports). It is the third fallback in `tools.get_changeset`
  and the second (`buildhub.get_rev_from`) answers all three current channels
  (`949de4f3b957` / `490e9bad7f98` / `8b532c2140db`). Deleting it also retires
  `tests/test_beta_channel_wiring.py:199-209`, which asserts the query's channel param and
  never its answer, and the `get_search_channel` widening `b3d3d92` applied to a dead
  function. *If a third fallback is genuinely wanted, resolve buildid → revision from the
  buildid timestamp against the pushlog — that is free.*
* `crashclouseau/single.py` is referenced **nowhere** (no import in any `.py`, not in the
  Procfile's release/web/worker/agentworker/clock, not in `bin/`, no doc, no test), calls
  `models.Changeset.find(filenames, buildid, channel, ndays)` against the real signature
  `(filenames, mindate, maxdate, channel)`, and calls `pushlog.puhslog`, which **does not
  exist**. The typo dates from `b5d8749` ("First commit", 2018) — `git log -S puhslog --all`
  returns only that commit, so **the module has never worked**. Last touched 2021-12-08
  (`8ee58c3`, a black reformat).
**Exit:** `grep -rn "single\.\|get_crash_by_uuid\|datacollector.get_changeset"` over
`crashclouseau/ bin/ tests/ Procfile` is empty; suite green with zero behaviour change.
*Adjacent, deliberately NOT folded in: unauthenticated `POST /api/javast` 500s on any string
buildid inside `models.Build.get_changeset` (`psycopg2.errors.DatetimeFieldOverflow`) —
a separate endpoint defect that shadows the AttributeError this dead function would have
raised.*

**9. [C] `sigage.py:22` is a flat lie in the tree.**
File: `crashclouseau/sigage.py:22`.
It says "`_facets_size=10000` is rejected outright (HTTP 400)". Measured on **four** query
shapes (`collect_day`'s, `signature_history`'s, the historical `build_id`/364-day shape, and
`get_proto_small`'s): 2000/3000/4000/5000/**10000 all succeed and 10001 is the first 400**.
The tree already contradicts it: `config/global.json:12 "facets_limit": 10000` is sent by
`datacollector.py:244` and `:416` on every 20-minute tick, and `net.py:53-54` cites that same
10,000-term facet as a measured 0.4-0.5 s query. Worse, `signature_history` — the "ONE
Socorro lookup" that docstring documents — **passes no `_facets_size` at all** today
(`sigage.py:90-114`), so line 22 documents a query the module no longer makes. **Three recon
agents relied on this line.**
Fix: one docstring line — "`_facets_size` is capped at 10000; 10001 is a 400" — plus a note
that the remark belongs to the `build_id`-facet shape this function replaced. **Trap:** do
NOT "fix" it by raising anything in `datacollector` — `facets_limit: 10000` is already at
the ceiling, so a bump to 20000 would **400 every selector tick**.
**Exit:** no docstring in the tree asserts a 400 at 10000; the neighbouring 365-day clamp
note (which IS true — Socorro caps `date` ranges at 365 days and `>=2025-01-01` returns 400
"Date range is bigger than 365 days") stays.

**10. [C] `config.py`'s venue rationale — the conclusion stands, the figures and
the credited mechanism do not.**
Files: `crashclouseau/config.py:546-548` (**`:570-572` at `c083308`** — the block moved when
`446a8b8` inserted the release overlay, and this item's exit is that those exact lines change,
so check before editing), `tests/test_shipped_channels.py:174-179`,
`tests/test_beta_autofile.py:399-401`. **Three copies must change together, and
`test_shipped_channels` asserts the VALUE (`"skip"`) and never the prose, so nothing in CI
can catch a wrong number here.**
**THE CONCLUSION STANDS.** Re-measured with the repo's own chain
(`_open_bugs_for_signature` → `_split_by_application` → `_split_out_metas`) on the
signatures **beta's own selector picks**: **39/77 = 50.6%** (Wilson 39.7-61.5) carry an open
same-application non-meta bug, **36/67 = 53.7%** per selection, against a matched **nightly
selector** sample of **26/120 = 21.7%** (15.2-29.9), **z = 4.22, p = 2.4e-5**. Beta really is
~2.3x nightly on the population its own selector produces. So `skip` suppresses ~half of
beta's candidate filings and `file_new` restores them at **2.02x-2.16x**, not 2.4x.
What is wrong: (i) the **first** figure quoted (58/98 = 59.2%) is a **top-100-by-install
panel**, and on that instrument nightly is **63.0%** (53.2-71.8), beta **59.0%**
(49.2-68.1), **z = −0.58 p = 0.56**, release 64% — it measures signature volume, not
channel, and quoting it beside a selection-level "23% nightly control" mixes two
populations; (ii) two of the three beta figures (45/77, 43/67) are **raw
`_open_bugs_for_signature` counts that never went through `_split_out_metas`**
(`spike/_beta_recon/07_bugsplit.py:18-19` counts `B[s]["open"]` directly) — post-chain they
are 39/77 and 36/67; (iii) it credits **`_split_by_application`**, which moves **0**;
`_split_out_metas` is the split that moves beta's number (6 of 45, all trackers 1472062 ×5
and 1588498).
**Then add the statistic that actually decides `skip` vs `file_new`, because a venue RATE
decides nothing: of the venues the FILER sees, how many are the RIGHT venue.** Production,
83 real nightly filings: a venue existed for **22/83 = 26.5%** (Wilson 18.2-36.9 — a lower
bound, since skips write no row) and **15 of those 22 = 68%** (47-84) were accepted by
`_bug_for_this_regression`. On the beta SELECTION population it is **6/39 = 15.4%**
(7.2-29.7), median venue-bug age at candidate landing **995 d** (p25 290, p75 2,271, max
3,859; 69% >1 y, 46% >3 y). The function's own docstring gets **10/102 = 9.8%** on a nightly
panel. **That 4.4x swing across populations is the whole point** — and on the filer-visible
population it argues FOR `skip`.
**Traps:** any re-measurement must run `_split_by_application` **and** `_split_out_metas`, in
that order, on the raw list — that is the only thing `bugzilla_apply.py:1229` sees — and must
remember the lookup is unauthenticated, so a restricted bug is **absent, not an error**
(`:419-420`). And do not quote the stale-gate conditioning ("0 of the 9 beta selections that
pass the gate have a venue") as "the mode is a no-op": a `strong-evidence` decision returns
before the clamp and `Confidence.high` is absent from `_STALE_SIGNATURE_CLAMP`, and prod
proves venue-carrying runs reach the filer.
**Exit:** the three prose copies agree, name their instrument and their denominator, and a
new test asserts the *numbers* appear in `config.py`'s comment so a future edit cannot
silently drift them.

### Group B — containment, so release cannot arm itself

**11. [C] SHIPPED as `deef91a` (not deployed) — `update_all`'s empty `INGEST_CHANNELS` default
must fail CLOSED.**
File: `crashclouseau/update.py:299-312`; doc: DEPLOY.md's "One-time app setup" step.
It read `os.getenv("INGEST_CHANNELS", "").split() or config.get_channels()` — so an
**absent or empty** variable meant *every configured channel*, i.e. nightly + beta +
**release**. **This fired in production** and produced the whole residue (§1.8, §2.4).
Fix: read the variable, and if it is empty **log loudly and return**. Add
`INGEST_CHANNELS` to DEPLOY.md's config-vars step **above** the `bin/init.py` step — the
ordering is what produced this, and `INGEST_CHANNELS` is absent from that step today,
appearing only in the beta section *after* the first ingestion runs.
This inverts the usual reflex: `heroku config:unset INGEST_CHANNELS` used to be the most
dangerous command in the deployment and becomes a full stop. It costs nothing — prod sets
it (v9/v122), `docker-compose` sets it through `bin/init.py`, and the tests pass `channels=`
explicitly.
**Exit:** `INGEST_CHANNELS` unset or `""` → one warning per tick and **zero** `update_in_queue`
jobs; a test asserts both the empty and the unset case. **Verify on the deploy that follows:**
prod sets the variable, so the fail-closed branch must not fire — if the first tick after the
deploy logs "INGEST_CHANNELS is unset or empty", the variable was lost, not the fix.

**12. [C] SHIPPED as `deef91a` (not deployed) — `AGENT_CHANNELS=""` must mean "no channel", and
it takes TWO readers.**
Files: `crashclouseau/config.py:436-441` (the reader),
`crashclouseau/agent/orchestrator.py:4104-4105` (`enqueue_agent`),
`crashclouseau/models.py:1607` (`UUID.untriaged`); doc: DEPLOY.md:69-72's `AGENT_CHANNELS`
row.
Measured reader matrix: unset → `['nightly','beta']`; `""` / `" "` / `"   \t "` → `[]`;
`'bogus'` → `['bogus']`. And `[]` is **genuinely no filter** at both readers —
`if channel is not None and channels and channel not in channels` and `if channels:` —
verified by driving the real gate with a mocked queue: a `channel="release"` crash **IS**
enqueued with `AGENT_CHANNELS=""` and is **NOT** with it unset.
**Be precise about today's consequence.** `AGENT_CHANNELS=""` on today's prod does **not**
triage release at $1-3/crash, because triage cannot reach a channel that was never
INGESTED: `builds` holds 61 nightly + 12 beta + **0 release**, and `enqueue_agent` is only
ever called from the ingest loop (`update.py:138`), the sweep (DB rows) and a retrigger (a
uuid row). Release triage needs **both** variables broken. What *is* reachable today is
smaller and real: `orchestrator.py:4171` (`:4232` at `c083308`) computes the sweep's over-fetch as
`cfg["max_per_run"] * max(1, len(channels or ()))` = **3×1 instead of 3×2**, so
`UUID.untriaged` returns 3 candidates where the per-channel cap needs 6 in hand —
re-opening exactly the starvation `models.py:1576-1580` was written to close, at ~$1.28/crash
of misdirected spend (nightly, 14-day avg, n=1,317) and one lost nightly triage per affected
tick — **nobody has counted the affected ticks, so the total is unquantified** (§13.7).
**Trap:** invert it in the **reader**, not at the two call sites, and keep the
`channel is not None` half or every no-channel legacy caller breaks. Both readers have to
move together (`if channels:` → `if channels is not None:`) or the sweep stays wide open
after the enqueue gate is closed. **Asymmetry worth writing down:** `INGEST_CHANNELS` had
**two** dangerous states (unset AND empty) and it fired; `AGENT_CHANNELS` has one and it
takes a deliberate `=""`.
**Exit:** `AGENT_CHANNELS=""` → a warning, `[]`, and **nothing enqueued and nothing swept**;
a test for each reader. As shipped, the inversion is in the reader (`config.get_agent_channels`)
and `UUID.untriaged` distinguishes `None` ("caller did not ask") from `[]` ("no channel"), which
is the half that keeps the sweep closed.

**13. [C] SHIPPED as `446a8b8` (not deployed) — `get_agent_calibration` publishes nightly's
fitted table for any NAMED unfitted channel.**
Shipped exactly as specified: `agent.calibration.fit_channel: "nightly"` plus
`agent.calibration.channels.release: {}`. `get_agent_calibration("esr")` now returns `{}` too,
so the class is closed and not just its one member.
Files: `crashclouseau/config.py:1100-1103`; new config key
`agent.calibration.fit_channel`; reference implementation `crashclouseau/sigage.py:413-425`.
`config.get_agent_calibration` returns nightly's fit `{25:0.5, 50:0.5714, 70:0.7234,
85:0.7234}` for `"release"`, `"esr"`, `"aurora"`, `"NIGHTLY"`, `""` and `None`; only
`"beta"` returns `{}`. End to end at the real consumer
`orchestrator._apply_worth_investigating` (`orchestrator.py:1793`), a seed with
`channel="release"` at rung 70 or 85 gets **`p_worth = 0.7234`** and
`report_bug._worth_phrase` (`report_bug.py:1636-1647`) renders "**, 72% worth investigating
— a calibrated estimate that this is worth someone's time**". The rule it breaks is the
module's own (`config.py:1095`, also `:1005`): "a number a Bugzilla reviewer reads cannot be
fit on the wrong arm". Beta's `{}` demonstrably works: **38/38 beta dossiers carry NULL
p_worth**.
**Be precise about the surface: it is NOT a filed bug.** Release has no
`agent.autofile.channels` entry, so `autofile_channel_declared("release")` is False and
`bugzilla_apply.py:1059` returns `{"filed": False, "skipped": "channel 'release' has no
autofile configuration"}` before any BMO request, even with `AUTOFILE_BUGS=1`. The surfaces
are the **anonymous public crashstack badge** (`templates/crashstack.html:178-190`), the
on-page "bug we'd file" preview (`html.py:62`) and the stored payload.
**Nor is the fallback wrong in general.** It is right for `channel=None` (every no-arg caller
is a nightly path, pinned at `tests/test_shipped_channels.py:304`) and right for nightly,
which "must keep its table without naming itself" (`config.py:1098-1099`). It is wrong for
exactly one class — a NAMED channel nobody has fitted — and today that class has one
member.
Fix, following `sigage.py:413-425` **verbatim**: add `agent.calibration.fit_channel:
"nightly"` and fall back to the top-level fit only when `channel` is falsy **or** equals
`fit_channel`; a NAMED channel with no `channels` entry returns `{}`. Three lines plus one
config key. A one-line stopgap (`"release": {}` in `agent.calibration.channels`) is what
the existing guard test would demand anyway, but it does not fix the class.
**Traps:** do **not** require every channel to name itself in `channels`; do **not** delete
`"release"` from `config.get_channels()` — `models.py:18` builds the `CHANNEL_TYPE` Postgres
enum from that list and `_ensure_enum_values` can never ALTER it, so the DB would diverge
from the code.
**Exit:** `get_agent_calibration("release")` → `{}`; `get_agent_calibration()` and
`("nightly")` still return the shipped table; `get_agent_calibration("esr")` → `{}` too.

**14. [N] SHIPPED as `446a8b8` (not deployed) — declare `agent.autofile.channels.release` with
an EXPLICIT `enabled: false`.**
**Shipped shape: `{"enabled": false, "comment_on_existing": "skip", "daily_cap": 2}`**, i.e.
`skip`/2 where this item proposed `file_new`/3. Both are **inert while `enabled` is false**, and
`skip` is the shape the stronger evidence supports: the right-venue statistic on the
filer-visible population (15 of 22 = 68%, Wilson 47-84) argues **for** `skip`, and the
`file_new` rationale in the recon was inverted (§7.8, §8 note in §11). Recorded so the
difference is deliberate rather than a drift. The record of the mechanism follows.
File: `config/global.json`; semantics at `crashclouseau/config.py:638-651`
(`channel_veto = over.get("enabled") is False`); the only reader is
`bugzilla_apply.py:1059`.
Probed live under prod's `AUTOFILE_BUGS=1` (`spike/_release_recon/resolve_07_autofile.py`,
five overlay shapes):

| overlay | declared | held | effective |
|---|---|---|---|
| none (today) | False | False | dies at `:1059`, "no autofile configuration" |
| `{}` | True | **False** | **ARMED** — `enabled: True`, `daily_cap: 10`, `comment_on_existing: "comment"` |
| `{"comment_on_existing":"skip","daily_cap":3}` (no `enabled` key) | True | **False** | **ARMED** |
| `{"enabled": false}` | True | **True** | held, and it LOGS |
| any, with `AUTOFILE_BUGS=0` | — | — | off (global beats overlay) |

So **declare-and-hold works, and the only dangerous shape is an overlay without an explicit
`enabled` key.** The recon's kill ("adding `release: {enabled: false}` would be worse than
nothing") is **refuted by the code and by the probe** — see §8.
**Shipped** (`446a8b8`): `"release": {"enabled": false, "comment_on_existing": "skip",
"daily_cap": 2}` — **not** the `file_new`/3 this item originally proposed; the header says why
the `file_new` rationale was inverted. The `enabled: false` is load-bearing; the other two
keys are inert while it is false and are there so a future decision does not inherit
nightly's `comment` mode and cap 10 by accident. **Do not arm it in this plan or any adjacent one.**
Also worth the one line the recon asked for: make `autofile_channel_declared` require an
explicit `enabled` key for any channel other than `default_channel`, so an incomplete config
edit fails **closed** — the opposite direction from every other gate in `autofile_bug`.
**Exit:** `autofile_channel_declared("release")` True, `autofile_channel_held("release")`
True, and a test asserting a release dossier at rung 85 with `AUTOFILE_BUGS=1` still returns
`filed=False` with the **held** message (not the undeclared one) — including through
`retrigger_agent(force=True)`.

**15. [C] HALF SHIPPED as `446a8b8` (not deployed) — the three "every triaged channel must…"
guard loops are VACUOUS in exactly the dangerous state.**
**Done:** the filing-decision loop now iterates `config.get_channels()`
(`tests/test_shipped_channels.py:247`) with the reason in its docstring, and asserts release is
declared **and held**. **Still open:** the calibration loop (now
`tests/test_shipped_channels.py:336`) still iterates `config.get_agent_channels()`, so it is
still vacuous at `AGENT_CHANNELS=""` — move it to `config.get_channels()` minus nightly, which
is green now that item 13 has landed. **Correctly left alone:** the population-rates loop (now
`:519`) stays on triaged channels, because `sigage` deliberately gives release no rates.
File (pre-`446a8b8` line numbers): `tests/test_shipped_channels.py:223`, `:306`, `:489`.
All three iterate `config.get_agent_channels()`, **which reads the env var**. Measured:
* `AGENT_CHANNELS="nightly beta release"` → **5 failures**
  (`test_beta_publishes_no_calibrated_probability[release]`,
  `test_every_triaged_channel_has_a_filing_decision[release]`,
  `test_every_triaged_channel_has_its_own_two_rates[release]`, plus two shipped-value pins).
  The forcing function works when it is pointed at release.
* `AGENT_CHANNELS=""` — **the exact state of item 12's hazard, in which release IS
  triaged** — → **1 failure**, and all three per-channel loops iterate **zero channels**.

**Item 12 is what makes item 13's guard vacuous.** Fix: the calibration loop (`:306`) should
iterate `config.get_channels()` minus nightly — every label `Build.channel` can ever hold —
which goes green the moment item 13 lands. **Do NOT generalise the population-rates loop
(`:489`) the same way:** `sigage` deliberately gives release no rates, so that one must stay
on triaged channels. The filing-decision loop (`:223`) already ends with an explicit
`assertFalse(config.autofile_channel_declared("release"))`, which item 14 flips to
declared-and-held.
**Exit:** under `AGENT_CHANNELS=""` the calibration invariant iterates every
`config.get_channels()` label except nightly — **assert the iteration SET (beta, release), not a
failure** — and a new test asserts that no invariant loop can run zero iterations in that
state. **Do NOT use the failure count as the instrument:** `AGENT_CHANNELS="" python -m
unittest tests.test_shipped_channels` gives 16 tests and exactly **1** failure
(`test_the_shipped_agent_channels`, the `get_agent_channels() == ["nightly","beta"]` pin)
both before `446a8b8` and after the calibration loop moves, because an env-independent loop
is insensitive to the variable. What changes is 0 iterations → 2.

### Group C — what release would need IF it were ever armed. Documented and measured; NOT shipped.

**Ship nothing in this group unless §10 Q1 or Q4 says yes.** Items 18 and 19 are the
exception if an ingest-only cycle is approved: they are its prerequisites.

**16. [T] The `sigage` release population arm.**
Files: `crashclouseau/sigage.py:399-403` (`_POPULATION_RATES`), `:407-411`
(`POPULATION_LABEL`); consumers `triage.py:693-702`, `report_bug.py:748-753`,
`orchestrator.py:2844-2855` (item 2).
Measured on the shipped instrument: release **bit_flip 8.91%**, **broken_cpu 5.79%**, n =
**3,759,071 reports**, and **stable** across three sub-windows (**9.18 / 8.47 / 9.06%**). So
the numbers exist and are the most stable of any channel. `top_cpu_share` median **0.138**
from **196 measured of a random 200 SAMPLED** out of 2,612 qualifying signatures
(`sigage.hardware_noise` per signature, ≥5 reports, 14-day window — the same instrument
nightly's 0.32 came from; it is a sample, so the median carries a sampling error nobody has
computed); at-100% count
**2/196 (1%)** against nightly's **26/200 (13%)**.
**Shipping the median alone would be worse than shipping nothing**, because the prose
literals beside it ("13% of them (26 of 200 sampled 2026-08-21)" and "9 of the 26 at 100%,
against 118 of the 200 overall") are nightly's and would sit next to a correct release
median — a 13x-wrong count. So item 16 is **gated on item 3**, and it ships only with
per-channel literals.
`tests/test_shipped_channels.py:489` makes the two rates a **hard prerequisite** for any
triaged channel, which is the correct forcing function; leave it as-is.
*Separate nightly finding, recorded so it is not lost: nightly's shipped 0.025 reads
**0.0375** on the same instrument. That is a nightly drift question, not release's.*
**Exit (if ever shipped):** `population_bit_flip_rate("release")` is not None,
`population_top_cpu_share_median("release")` is 0.138, and no prompt on any channel quotes a
count from a channel it is not describing.

**17. [T] Per-channel retention: `retention_ndays.Firefox.release = 45`.**
Files: a new config key; `crashclouseau/config.py:83-85` (`get_ndays_of_data`) would need a
channel argument; consumer `models.Node.clean` (`models.py:302-308`).
Measured saturation (§2.1): 30 → 80.9% full 3-build windows, 45 → **96.0%**, 50-90 → 96.2%.
**50+ buys nothing.** Replay instrument: 549 run-days over an 83-build list spanning ≥12
months, because the 20.89-day gap that drives the result falls outside every 2026-04+ window.
**COUPLING, named explicitly.** 45 > `max_ndays` 30 and > `buildhub_lookback_ndays` 30, and
the docstring rests on the identity `buildhub_lookback_ndays == max_ndays`. Breaking it is
**deliberate** and must say so, with release's 7.02-7.07-day median cadence as the reason.
Two consequences to write down: (a) raising `buildhub_lookback_ndays` **alone changes
nothing**, because `Build.put_data` needs a surviving `nodes` row (`models.py:608`) and
`put_filelog` only fetches forward; (b) item 19's clamp reads the same retention value, so if
this ships, the clamp must read the **per-channel** function or the mismatch is recreated in
the other direction.
Peak cost: ~14k release nodes retained instead of ~7k, i.e. one extra cycle merge's worth.
**Exit (if ever shipped):** a seeded replay shows <3-build windows on ≤4% of run-days, and
a test pins the property (release's p90 3-build span), **not** beta's `3 × 2.0` cadence
literal — `test_the_buildhub_lookback_is_the_retention_window` hardcodes that literal today
and says nothing on release.

**18. [N] Delete the prod release residue. ONE TRANSACTION.**
Not a code change. Two statements, 27,588 rows:

```sql
BEGIN;
-- 20,320 `changesets` go with these via ON DELETE CASCADE (verified in
-- information_schema: changesets.nodeid->nodes CASCADE, builds.nodeid->nodes CASCADE,
-- scores.changesetid->changesets CASCADE). 0 `builds` and 0 `scores` rows are affected.
DELETE FROM nodes    WHERE channel = 'release';   -- 7,267
-- MUST be the same transaction. `put_filelog` falls back to `LastDate.get(channel)[1]`
-- (update.py:29-38), so deleting only the nodes still yields start_date = 2026-07-06 and
-- the same ~56-day first pushlog request.
DELETE FROM lastdate WHERE channel = 'release';   -- 1
COMMIT;
```

**THE TRAP: do not do this by calling `models.Node.clean(date, 'release')` from a console.**
That path ends in `LastDate.update(Node.get_min_date(channel), date, channel)`
(`models.py:308`) — it **REWRITES** the release `lastdate` row instead of deleting it, which
is **strictly worse than doing nothing**: `Node.get_max_date` then returns NULL,
`LastDate.get(...)[1]` returns that fresh timestamp, and the first real switch-on tick
backfills **~0 days instead of 30** — a silently under-filled channel, the same failure shape
as the `get_last_versions` break already documented in `models.py`.
**Do NOT garbage-collect `files`/`hgauthors`.** 6,986 `files` rows (29.0%) and 207
`hgauthors` rows (19.1%) are release-only, but **10,907 `files` rows (45.3%) and 182 authors
are ALREADY orphaned by ordinary nightly/beta retention** — `Node.clean` never sweeps those
two tables — so an "orphan sweep" deletes 17,893 file rows unrelated to this and churns
`files_name_key` for nothing. Both write paths are get-or-create, so the orphans are
harmless.
**Exit:** `SELECT count(*) FROM nodes WHERE channel='release'` = 0 and
`SELECT * FROM lastdate WHERE channel='release'` = 0 rows, in the same transaction;
`changesets` drops from 33,147 to ~12,827.

**19. [C] Clamp `put_filelog`'s derived start date.**
File: `crashclouseau/update.py:29-40`.
`put_filelog` has **no lower clamp**. With no explicit dates it sets `end_date = utcnow()`
and `start_date = Node.get_max_date(channel) + 1s`; only the third fallback branch (nodes
empty AND no `lastdate` row) has a floor. `pushlog()` applies no cap, `net.get`'s bound is
connect 10 s / read-gap 60 s (not a size or duration cap), and RQ's `default_timeout=6000`
does not kill it. Simulated on prod's exact release state: window = **55.89 days**.
Fix: inside the `if not start_date:` block, on the **two derived branches only**:
`start_date = max(start_date, end_date - relativedelta(days=config.get_ndays_of_data()))`.
**Trap:** placed *after* the whole block it also clamps an EXPLICIT `start_date` — harmless
for the only other caller today (`create.py:38` passes exactly
`date - get_ndays_of_data()`) but it would silently truncate a hand-run wider backfill, the
"fails by doing less, with no log line" shape. **Log the clamp when it fires** — that line is
the only thing that would ever tell you a channel had been dark for >30 days.
**It is lossless and it is not the performance fix.** `Changeset.add` calls
`Node.clean(end_date, channel)` which deletes `pushdate <= end_date − 30d` — the same value
the clamp would use — and it runs before `update_builds`, so in simulation **0** surviving
release nodes sit below the clamp start after one unclamped tick, and pushdate == buildid
held 7/7 on release so no build revision can be lost. It removes only **35%** of today's
payload (20,094 → 13,041 changesets, 30.43 → ~19.7 MB) and cuts `Changeset.add`
**276.1 → 121.8 s (−56%)** because the per-node cost is superlinear (3.85 ms/node at 2,000,
13.74 at 20,094). **Its real value is bounding growth forever.** Do NOT widen `Node.clean`'s
retention to match the fetch — that inverts the relationship the clamp relies on.
Fold in the cheap sibling the residue investigation asked for: **log a warning in
`update_builds` when `buildhub.get` returns falsy** (`update.py:222-223`). Today a channel
whose `builds` table stays empty produces "Update builds: finished." and nothing else, which
is exactly why the missing release rows went unexplained for two months — and it is the
instrument the §9 tripwires need.
**Exit:** the first `put_filelog` on a channel dark for >30 days logs the clamp and requests
≤30 days (or ≤ the per-channel retention if item 17 shipped).
*The real cost lever is separate and out of scope here: `HGAuthor._get_or_create_id`
(`models.py:804-822`) commits per call and `Node.__init__` calls it per changeset, so
`Changeset.add` costs 4.01 SQL statements and ~1 commit per node. Hoisting the author lookup
out of the loop — resolve the distinct (email, real, nick) triples once into a dict — would cut
it ~3x on every channel, including beta's routine 5,130-node merge tick. Only with
`tests/test_beta_merge_push.py` and the pushlog tests green: the per-call commit is currently
what makes `Node.hgauthor` FK-safe against the autoflush ordering.*

### Group D — measured DEAD or INERT config. Document or delete. Never tune.

**20. Document `spike.floor.Firefox.release = 50` as structurally inert.**
Proof and empirical confirmation in §2.5. Add the argument to `config.py`'s spike section and
to whichever test pins the release knobs. **Do not tune it, and do not remove it silently** —
`tests/test_selection_log.py`'s tripwire premise (floor(beta) > installs(beta)) is *equality*
on release, so a reader needs the reason written down. The recon's kill on the mature-installs
half of `config.py:361-364` is right for a **stronger** reason than it gives — see item 21.

**21. Document `spike.mature_after_days` / `mature_installs` release entries as DEAD.**
`datacollector.get_maturity_bar("Firefox","release")` returns `(None, 1)`, so `mature` is
always False off nightly and the **whole** maturity bar, not just its install half, is
unreachable on release. Beta's entries are dead the same way. Either delete both channels'
entries or comment them as dead; do not leave them looking like decisions.

**22. Document `thresholds.installs.Firefox.release = 50` as defensible and NOT a volume
calibration.**
Its machine-equivalent is an **interval, ~205 to ~440 machines**, from two models neither of
which is measured; the only direct measurement is the esr153 per-(signature, build)
k-distribution giving 8.83x (range 4.27-9.67x). Say "at most ~N", never "= N machines", and
never "release is 34x stricter than beta in machine terms" to two significant figures. It is
**decision-irrelevant**: nobody proposes changing it, and the per-(signature, build) install
distributions on release and beta are identical through the p95 (1/2/5/12), so no value makes
the two channels' numbers mean the same thing.

---

## 4. Numbers — every release knob, its measurement, its denominator, its decision

| knob | shipped | measured | instrument / denominator | decision |
|---|---|---|---|---|
| `thresholds.installs.Firefox.release` | 50 | ~205-440 machines (interval) | 2 unmeasured models + esr153 k-median 1.150 (14 sigs, 1 build) | **KEEP**, document as an interval (item 22) |
| `thresholds.protos.Firefox.release` | 20 | 11.7 kept protos/pair, prod-shaped query, n=30 pairs | `search_date` = oldest build-day of the window at the run-day | **out of scope** — matters only if armed |
| `spike.floor.Firefox.release` | 50 | 329 pairs at floor 1/3/10/20/50; first binds at 100 | 133 run-days, real selector | **INERT** (item 20) |
| `spike.ratio.Firefox.release` | 3 | 89% of 329 pairs come through this branch; 4 bursts of 90-110 | 133 run-days; burst config on 8 of 82 builds | **out of scope** — matters only if selected |
| `spike.min_build_installs.Firefox.release` | 15 | safe over [2, 833]; drops 9/9 respin days, 0/236 legitimate | max per-signature install cardinality per non-newest build-day AT the run-day, 245 observations | **KEEP**, 55x margin |
| `spike.mature_*.Firefox.release` | 5 / 4 | `get_maturity_bar` → `(None, 1)` | live probe | **DEAD** (item 21) |
| `max_ndays` (retention) | 30 | 30 → 80.9%, 45 → 96.0%, 50+ → 96.2% full 3-build windows | 549 run-days, 83 builds, ≥12 months | **per-channel 45 if ever armed** (item 17) |
| `buildhub_lookback_ndays` | 30 | inert alone: `put_data` needs a surviving node | static, `models.py:608` | **KEEP** |
| `facets_limit` | 10000 | 10000 accepted, **10001 = first 400** | 4 query shapes | **KEEP** (and fix the docstring, item 9) |
| `sigtrend.FACETS_SIZE` / `MAX_SIGNATURES` | 2000 / 1500 | release 2,403-3,686 signatures/day (91 days, median 3,112); nightly 276, beta 401 | live at the shipped settings | **KEEP the refusal**, rewrite the reason (§8.3) |
| `population.facets_size` | 1000 | error on every page prod can render today = **0%** | exhaustive 30-day census; 0 of 1,946 nightly and 0 of 8 beta ingested signatures over the cap | **footnote, not a defect** (§8.9) |
| `agent.calibration.channels.release` | **`{}`** (was absent) | absent inherited nightly's 90-row fit | `_apply_worth_investigating` end to end | **SHIPPED `446a8b8`** via `fit_channel: "nightly"` (item 13) |
| `agent.autofile.channels.release` | **`{enabled: false, comment_on_existing: "skip", daily_cap: 2}`** (was absent) | a `{}` overlay = ARMED at cap 10 / mode `comment` | live probe, 5 shapes, `AUTOFILE_BUGS=1` | **SHIPPED `446a8b8`** declared-and-held (item 14) |
| `sigage._POPULATION_RATES["release"]` | absent | 8.91% / 5.79%, n=3,759,071; top_cpu median 0.138 (196 measured of 200 sampled from 2,612 qualifying) | shipped instrument, 3 stable sub-windows | **not shipped** (item 16) |

---

## 5. Coupling inventory

1. **Item 17 ↔ item 19.** The clamp reads the retention value. If retention becomes
   per-channel, the clamp must read the per-channel function, or the 56-day window comes back
   for release and a 45-day fetch appears on nightly.
2. **Item 17 ↔ `buildhub_lookback_ndays` ↔ `Build.put_data`.** Raising the lookback alone is
   inert; raising retention alone is the fix; raising both breaks a documented identity, on
   purpose, and the docstring has to say so.
3. **Item 18 ↔ item 19.** The residue delete makes the clamp's first firing invisible (the
   fresh third branch backfills exactly 30 days). Do 18 **and** 19; do 19 first if only one
   ships, because it protects nightly and beta after any stall.
4. **Item 12 ↔ item 15.** The empty-`AGENT_CHANNELS` state is precisely the state in which
   the guards that would have caught items 13 and 14 iterate zero channels.
5. **Item 13 ↔ `config.get_channels()` ↔ `CHANNEL_TYPE`.** `models.py:18` builds the Postgres
   enum from that list and `_ensure_enum_values` can never ALTER it, so "release" cannot be
   removed from the list to solve a calibration problem.
6. **Item 2 ↔ item 3 ↔ item 16.** All three are the same disease (a nightly number printed
   against another channel) in three places. Item 16 must not land before item 3.
7. **Item 5 ↔ `_ADDED_TABLES` / `_ENUM_ADDITIONS`.** A third schema-evolution hook. Review it
   as one; `_ENUM_ADDITIONS` is the cautionary tale (it has never fired on this DB).
8. **Item 6 ↔ item 7 ↔ item 18.** The channel-blind patch chain is what turned dead nodes
   into ~2.5-4.8 hours of hg traffic; the ingestion-coupled retention is why they were still
   there to be read; the delete removes the instance. All three, or the next stale channel
   repeats it.
9. **Item 10 ↔ three copies.** `config.py:546-548` (`:570-572` at `c083308`),
   `tests/test_shipped_channels.py:174-179`, `tests/test_beta_autofile.py:399-401`. Nothing in
   CI asserts the prose.
10. **`Changeset.get_scores`' docstring ↔ the merge rule.** `models.py:541-546` says the
    two-rows-per-hash collision is latent "on the in-cycle beta window, which is the only one
    reachable today … It stops being latent the moment the `mindate` boundary that excludes the
    merge push moves." On release the merge push is **not** excluded — its pushdate IS the
    major build's buildid and `Changeset.find`'s `Node.pushdate <= maxdate` is inclusive. It
    stays latent for a **different reason than the docstring gives**: merge members get
    `files: []`. One comment.

---

## 6. Test plan

Commands. At `c083308`, `DATABASE_URL=sqlite://` gives **2,141 tests / 0 fail / 102 SILENT
SKIPS in 61 s**; it was **2,115 / 0 fail / 100 / 67 s** at prod v139's tree and the commits
recorded as SHIPPED added the difference (`tests/test_retrigger_auth.py` +187 lines,
`test_bit_flip_gate.py` +55 and +35). A real Postgres gave **2,115 / 0 fail / 0 skip in 95 s**
at that earlier count and has not been re-run. Use Postgres — 11 files are gated
and skip silently otherwise. `.taskcluster.yml`'s own command with no server gives **114
errors**.

**Existing tests that change**

* `tests/test_shipped_channels.py:223` → now `:247` — **DONE in `446a8b8`**: iterates
  `config.get_channels()` and asserts release declared-and-**held** (items 14, 15).
* `tests/test_shipped_channels.py:306` → now `:336` — **STILL OPEN**: loop over
  `config.get_channels()` minus nightly instead of `get_agent_channels()` (item 15's remaining
  half; green now that item 13 has landed).
* `tests/test_shipped_channels.py:489` → now `:519` — **leave on triaged channels** (item 16 is
  not shipping).
* `tests/test_shipped_channels.py:160-172` — **DONE in `446a8b8`**: the containment assertion is
  now `autofile_channel_held("release")` under a mocked `AUTOFILE_BUGS=1`, because asserting
  `enabled` with the variable unset passes trivially and says nothing about prod.
* `tests/test_beta_autofile.py` — **partly DONE in `446a8b8`**: `release` dropped from the
  undeclared-channel list. The venue prose at `:399-401` is still item 10's.
* `tests/test_shipped_channels.py:117-157` — its comment "Prod has INGEST_CHANNELS=nightly
  today" is stale (it is `nightly beta`), and its containment argument now includes item 11's
  fail-closed default and item 1's authenticated retrigger. The "an accidental release ingest
  is not an incident: no LLM spend" clause at `:153-156` was **false** until `e65f801` and must
  now cite it.
* `tests/test_shipped_channels.py:174-179`, `tests/test_beta_autofile.py:399-401` — the venue
  prose (item 10).
* `tests/test_bit_flip_gate.py:64`, `:594` — **DONE in `bd92fcb`**: a non-nightly channel arm, a
  `broken_cpu_rate=None` arm and a measured-zero arm (item 2).
* `tests/test_beta_channel_wiring.py:199-209` — deleted with `datacollector.get_changeset`
  (item 8).

**New tests**

* T1 — anonymous `POST /api/tasks/retrigger` → 403, **0 jobs enqueued**; unknown uuid → 404;
  no `Access-Control-Allow-Origin` echo. *(shipped as `tests/test_retrigger_auth.py`)*
* T2 — the hardware-noise abstain reason with `broken_cpu_rate=None` contains no `0%` clause,
  names which arm fired, and omits the population clause on a channel with no rates.
* T3 — `_cpu_spread_line(..., channel="beta")` contains no undefined "the 26"/"the 200";
  nightly still contains both, defined.
* T4 — a declined filing writes `payload["filing_declined"]`, is **not** in
  `_STICKY_PAYLOAD_KEYS`, is popped by `reset_for_retrigger`, and does not move
  `already_filed` / `list_tasks` / the tasks-view filed counter / `retrigger_agent`.
* T5 — `_ensure_indexes()` creates all four on an EXISTING DB (the `_ENUM_ADDITIONS` failure
  mode), is idempotent, and logs rather than raising on failure.
* T6 — `Changeset.to_analyze()` with `INGEST_CHANNELS="nightly beta"` never returns a release
  row; and it has a deterministic order.
* T7 — `INGEST_CHANNELS` unset **and** `""` → warning + zero jobs.
* T8 — `AGENT_CHANNELS=""` → `[]`, nothing enqueued (`enqueue_agent`) and nothing swept
  (`UUID.untriaged`); and the invariant loops do not silently iterate zero channels.
* T9 — `get_agent_calibration` returns `{}` for `release` and `esr`, the shipped table for
  `None`/`nightly`, `{}` for `beta`.
* T10 — a release dossier at rung 85 with `AUTOFILE_BUGS=1` returns `filed=False` with the
  **held** message, including through `retrigger_agent(force=True)`.
* T11 — `Node.clean` sweeps a channel absent from `INGEST_CHANNELS` (item 7).
* T12 — `put_filelog` on a >30-day-dark channel requests ≤ retention and logs the clamp.
* T13 — pin release's six selector knobs with an explicit **"INHERITED, NOT MEASURED"** note
  per value where that is true (`installs 50` and `protos 20` date from `3695614`,
  2018-02-24; `floor 50` from `9020f88`, 2026-07-08; `min_build_installs 15` was copied from
  beta by `4749e29`, 2026-08-26). A pinned unmeasured value is safer than an unpinned one,
  because the next person cannot quietly clean it up.
* T14 — `test_enum_values` gains `CHANNEL_TYPE`. It pins two enums today and not the one value
  a long-lived DB could be missing. (Release is **already** present — this is a guard, not a
  migration; see §8.4.)

**Note the sqlite blind spot:** sqlite accepts ANY channel string, so 2,039 of 2,141 tests
cannot catch a channel-label typo, and 42 of the **72** files under `tests/` at `c083308` (71
when the 42 was counted) pin `channel="nightly"` as their fixture. That is why T13/T14 exist.

---

## 7. Contradictions between the reports, resolved

Only the ones that change a decision. Full set in `RECON.json` `resolved` / `critic`.

1. **Does 30-day retention shorten the release selection window?** *Ingestion wins.* The kill
   used the wrong statistic — the retention clock runs from the RUN DAY and the newest release
   build is itself up to 20.35 d old then, so retention must cover up to 51.6 d, not 31.2 d.
   19.1% short, saturation at 45 days. Any re-measurement must span ≥12 months.
2. **Release selection volume: 2.47 or 0.97 pairs/day?** *They agree; the window differs.*
   2.47/day is a cost ceiling (133 run-days containing four bursts), 0.75-0.97/day is the quiet
   state. Burst mechanism confirmed: a baseline build current for only 3-4 days has a permanently
   small lifetime install base (151.0 21,850 vs 151.0.1 71,392 = 3.27x; 152.0.2 25,584 vs
   152.0.3 70,747 = 2.76x; 152.0 29,264 vs 152.0.1 83,575 = 2.86x). Configuration recurs on 8
   of 82 builds.
3. **Protos per pair, and the whole cost model?** *Both published figures are
   wrong-denominator.* Production shape = 11.7/pair. One passed 61 days, the other **no `date`
   key** (7-day default, reproducing its published median 3 / mean 11.0 / max 75 exactly).
   `date=""` is a hard **400**; an ABSENT `date` is a silent 7-day window.
4. **How much does the 10% sample understate machines?** *Per-signature ~8.83x (measured),
   channel-wide 1.25-4.1x (modelled).* The 4.1x figure is a channel-wide factor misapplied to a
   per-(signature, build-day) gate. Treat 10% as nominal, 6-11% as the band.
5. **Is the unsampled slice 0.00%, 7.4% or 6.63%?** *Three different things.* Background-process
   on the selected population is exactly 0.00%; `throttleable=F` on the same population is
   1.86% with a 0.00-43.42% per-signature range; channel-wide it is 6.60%. "A flat 10% is the
   correct model for everything release selection ever sees" is 98% right on average and
   materially wrong per signature.
6. **Is there a release-only class in the head?** *All three agreed once the rank was fixed*
   (0 in the top 100, 0 in the top 200, 4 in the top 300 at ranks 202/235/236/264) — and then
   **verification moved the bar**: at the below-both-upstream-floors bar it is 10/200 and
   37/350 (§1.7). Quote the bar, not just the number.
7. **Does `enabled: false` hold filing or arm it?** *It holds.* `channel_veto =
   over.get("enabled") is False` is honoured against a global `AUTOFILE_BUGS=1`, verified live
   for five shapes. A bare `{}` is the only dangerous shape.
8. **`skip` vs `file_new`, and the venue share?** *Every published release figure (64% / 70% /
   72.5% / 69.2%) is a top-by-volume panel.* At the SELECTION level release is **14/30 = 46.7%**
   (Wilson 30.2-63.9): significantly above nightly (z=2.77, p=0.006), indistinguishable from
   beta (z=−0.37, p=0.71) at n=30. And `skip` **discards the selections that HAVE a venue** and
   files the ones that do not — the recon's rationale for `file_new` was inverted.
9. **Does the stale gate block release filing?** *Leads only.* See §1.6.
10. **Is `spike.floor.release` inert?** *Structurally, yes.* See §2.5.
11. **Should release join `sigtrend`?** *No measurement disagreement, only a spend judgement —
    and the deciding number is neither.* See §8.3.
12. **Which dyno pays the off-stack cost?** *`agentworker`, 3× Standard-2X = 1 GB each.* The
    "512 MB dyno" framing names the wrong dyno; 512 MB is `worker`, which is the right dyno for
    the **ingestion** memory claims (215.0 / 234 / 251.3 MB, three instruments, all under 512 MB
    today).
13. **The nightly denominator every release projection scales from.** *Prod SQL settles it:*
    nightly **2,665 dossiers / 31 days = 86/day**, **83 filed = 2.68 filings/day = 3.1% of
    runs**, `total_cost_usd` **$4,552.35**, avg **$1.712**. The "0.69 bugs/day / 26.4 runs/day"
    figures came from parsing the deployed `tasks.html`'s last 500 rows and are 3.9x and 3.3x
    low. The per-run *rate* survives (2.6% vs 3.1%) because numerator and denominator scaled
    together; the absolute rates do not.
14. **Who receives a needinfo on a release filing?** *The patch author, not the release
    manager.* `pushlog.collect` reads `author = chgset["author"]` (`pushlog.py:138`) and never
    `push["user"]`. The counter-claim measured `pushuser`, which nothing in the tree reads.
    Residue: nobody sampled the `author` field of an **in-cycle** release uplift specifically —
    unmeasured, and it only matters if release is ever armed.
15. **Release's `top_cpu_share`.** *0.138 is measured on the shipped instrument* (196 measured
    of 200 sampled from 2,612 qualifying);
    the competing "0.060" is a channel-wide share of the top cpu model, a different quantity, and
    must not be cited as a `top_cpu_share`.

---

## 8. Kill list — measured wrong, or already refuted. Do not rebuild these.

1. ~~"Beta's `EARLY_BETA_OR_EARLIER` / `compiled_out` partition is wrong on an early-beta
   build."~~ **REFUTED.** Evaluated the real predicate at the OWN NODE of all **12 ingested beta
   builds**: `EARLY_BETA_OR_EARLIER` is **OFF at 12 of 12**. The five pre-alias ones (154.0b6..b10,
   2026-08-03..08-12) have `build/defines.sh` present but **EMPTY** — the 154-cycle late-beta unset
   landed 2026-07-31 15:48 (`3c31f793f850`), before our first beta build row; the other seven
   (155.0b1..b5, 156.0b1) have no `build/defines.sh` at all and carry the alias. Bug **2052050**
   landed on m-c 2026-07-29 01:10 (`71615c97760d`) and rode to `mozilla-beta` at the **2026-08-13**
   promotion — **13 days before beta ingestion was switched on** (`INGEST_CHANNELS` set 2026-08-26
   09:46, heroku v122). Crashes actually analysed on beta: 45 uuids / 38 dossiers, all on
   155.0b3/b4/b5, **100% post-alias**; `compiled_out_suppressed` has fired on beta **0** times (1
   on nightly over 2,668). Not reachable forward either: the alias is now on all three branches,
   bug **2052054** is still NEW, and when it lands the entry becomes **dead, not wrong** — and the
   non-nightly selector only sees `Build.get_last_versions(..., n=3)` (`datacollector.py:45`), so an
   old early-beta build could not be selected even if a row existed. Two smaller errors in the
   claim: the cited lines were the **release** rows (`_CHANNEL_MACROS["beta"]` is
   `compiled_out.py:154-159`, `:165` is release; `_CHANNEL_OFF_HOLLOW["beta"]` is `:189`, `:191` is
   release), and both hypothetical errors sit in the **OFF** half, i.e. the same direction.
   **What IS worth doing is documentary — and it is NOT an item and is in no phase:** date-stamp
   `compiled_out.py:123` and
   `tests/test_beta_compiled_out.py:8` — the read was at **155.0b4/b5, post-alias** — and record
   that pre-alias the predicate was `value AND any(x in app_version_display for x in "ab")`,
   TRUE for **39.3% of every beta cycle** (11.0-11.3 d of 23.9-35.2 d cycles: 39.3 / 39.1 / 39.7 /
   40.0 / 32.0 / 45.9%), and that `MOZ_DIAGNOSTIC_ASSERT_ENABLED` was gated on it too, so **both**
   of beta's OFF-half macros were ON in that window. **Do NOT** implement a guard by reading
   `build/defines.sh` at the build rev and calling non-empty ⇒ early beta: on release the file said
   `EARLY_BETA_OR_EARLIER=1` at times when the macro was off, so that inverts release. And if
   anyone still wants belt-and-braces, the check must go **inside** `_default_off_switch`'s channel
   branch (`compiled_out.py:543`), which fires before `rev` is used for anything.
2. ~~"`second_opinion._SYSTEM` is channel-blind, so the SO reads mozilla-central for a beta
   crash."~~ **REFUTED, and exactly inverted.** `second_opinion.py:123` reads
   `channel = crash.get("channel", …)` and threads it into **all five** channel-bearing MCP
   contexts — `SearchfoxCtx` `:130`, `PatchCtx` `:132`, `HistoryCtx` `:133`, `SourceCtx` `:135`,
   `SocorroCtx` `:138` — and `_user_prompt` `:90` emits a literal `Channel: beta` line as prompt
   line 2. `SearchfoxCtx.repo` → `repo_for_channel` (`searchfox.py:157-180`): beta and aurora →
   `mozilla-beta`, release → `mozilla-release`. `tests/test_beta_channel_wiring.py:405-411`
   (`test_the_blind_second_opinion_hands_every_context_the_beta_channel`) asserts it and 31/31
   tests pass. The failure mode would have been **loud** anyway: beta build nodes `490e9bad7f98` /
   `400be6de7377` return 200 on `releases/mozilla-beta` and **404 on mozilla-central**. The three
   surviving facts are inconsequential: `_SYSTEM` is 2,131 bytes and names no repo, and
   `triage._drift_paragraph` does not reach the SO. **Do NOT port `_drift_paragraph`** — `_SYSTEM`
   contains none of "drift"/"delta"/"downgrade"/"mismatch"/"EXPECTED", so there is nothing for it
   to correct, and pasting it in imports a line-delta amnesty the SO deliberately lacks, blunting
   the skepticism that produces the measured refute rate. Also corrected in passing: the SO
   refutes **241/389 = 62.0%** of nightly verify-mode runs (53.2% of all SO-ok runs; 0/64 in
   mechanism mode, where `corroborates` is null by construction) — neither 74% nor 34.6%; and SO
   spend is **$250.27 of $4,599.80 = 5.44%**, not ~4%. `SecondOpinion` carries **no citations**
   (`schema.py:574-594`), so a wrong tree could never have reached a cited permalink. The only
   defensible residue is cosmetic: `_SYSTEM` says "searchfox is tip-only" without naming which
   tip. If you close it — **not an item, in no phase** — make `_SYSTEM` a function of channel
   exactly as `triage._system_prompt(channel)` already is, `lru_cache(maxsize=8)` it, add a
   negative arm for nightly, and change **nothing else**.
3. ~~"Admit release to `sigtrend.SUPPORTED_CHANNELS`."~~ **KEEP THE REFUSAL — and rewrite its
   stated reason.** The docstring's sampling clause ("its install counts are not comparable with
   the other two anyway") is **not** the reason: `sigtrend.py:320,338` compute
   `ratio = w_ins/(b_ins × w_exp/b_exp)`, all four terms per-day sums for ONE channel (`:306`,
   `:309`), and none of the three consumers (`report_bug.py:691`, `triage.py:633/1505`,
   `orchestrator.py:859/2630`) compares channels — **so a uniform sample cancels to first order**
   inside its own within-channel statistic. (First order only: a simulated true 3.0x rise gives
   mean 2.98 / sd 0.37 at s=1.0 and mean 3.30 / sd 1.08 at s=0.1, a +10% median bias and 2.9x the
   spread, because install cardinality is sublinear in the sample rate.) **The two true reasons:**
   (a) the accept rate is **class-dependent** per `antenna/throttler.py`, so a signature's own
   class-MIX shift MANUFACTURES rises — on the one replayed release run-day (asof 2026-08-25),
   **2 of the top 3 risers of 129** correct from **3.942 → 1.483** (`EMPTY: no frame data
   available; EmptyMinidump`) and **3.867 → 1.373** (`OOM | large | EMPTY…`), **both under
   `MIN_INTERESTING_RATIO = 3.0`**; the **3 loudest hold 63% of the rising set's install
   mass**, and the **6 of 129 (4.7%) that lose the ≥3x sentence hold 47.7%** of it (across all
   129 the manufactured factor has median 0.93 and is >1.5x on 7/129 = 5.4%; the 3rd of the top
   3, `IPCError-browser | GPUProcessKill`, is unaffected at factor 0.93); and (b)
   `MIN_INSTALLS = 3` (`sigtrend.py:78`),
   `WORDING_MIN_INSTALLS = 5` (`:114`) and `MIN_INTERESTING_RATIO = 3` (`:113`) are single global
   constants explicitly calibrated on **1,052 active NIGHTLY signatures** (`:98-103`), and at a
   10% accept rate **3 observed installs is ~25 true** (P(install visible) = 0.118 at s=0.1). The
   VOLUME reason is also correct and load-bearing — release runs **2,403-3,686 distinct
   signatures/day** (91/91 cached days, median 3,112; live 2026-08-26 = 3,218) against nightly 276
   and beta 401 measured live at the shipped settings — but it must be stated **per day**: the
   "6,194" figure is a **3-DAY cardinality** (I measure 5,765) against a per-day constant, so the
   gap is 1.2-1.6x, not 4-8x. **Also record the invariant:** `MAX_SIGNATURES = 1500` sits **below**
   `FACETS_SIZE = 2000` (adjacent at `sigtrend.py:123-124`), so raising the facet alone still
   writes **ZERO** release rows — measured: at (2000, 1500) `collect_day("Firefox","release",
   2026-08-26)` returns 0 with "returned 2000 signatures (>= 1500)"; at (10000, 1500) it returns 0
   with "returned 3218 signatures (>= 1500)"; at (10000, 5000) it returns 3,218 and writes 3,218
   `sigdaily` rows. The pair needs an asserted invariant (`MAX_SIGNATURES <= FACETS_SIZE`), stated
   as a **margin** rather than a truncation guard — `len(rows) == FACETS_SIZE` is the only
   truncation signal Socorro gives, so the check must stay `>=` — and note the **1500-1999
   false-refusal band is occupied by no channel**. Cost, for the record and not as the deciding
   number: one 0.59-0.6 s query/day, 3,218 rows/day, **+271,538 rows / +105.7 MB** measured in real
   Postgres 16 against 204 MB of a 10 GB plan; the steady state is a **rolling 90-day window**
   because `SignatureDaily.prune(days=90)` runs every tick (`models.py:3900` via `update.py:250`),
   so ~268k release rows — **not** 1.13M/year, which is a throughput figure mislabelled as storage
   (the 4.7x ratio against nightly+beta's ~59k survives; prod `sigdaily` is 19,049 nightly +
   29,048 beta = 48,097 rows / 20 MB over 76 days). **Do ship one line:** an INFO log on
   `sigtrend.backfill`'s unsupported-channel return (`sigtrend.py:251`), so the absence is visible
   rather than a silent no-op. *If release is ever admitted, the only correct fix is a homogeneous
   accept class on BOTH sides (the signature series AND the `ChannelDaily` exposure), which
   collapses the per-signature distortion to a constant 0.917 exposure factor. Two traps:
   `throttleable=T` alone is insufficient (`has_comments` and `has_phc` are also 100%-accept and
   both fields are protected for our token, so they stay mixed in and must be declared as
   residual), you must also exclude `process_type in {gpu,plugin,rdd,socket,utility}` or 9 of 129
   risers get a zero denominator — and it must NOT be applied to nightly/beta, where the correction
   is the identity and the filter would silently delete ~3% of the denominator and all of the
   gpu/rdd population. The control that kills the naive test: restricting to a homogeneous class on
   NIGHTLY collapses `mozilla::webgpu::WebGPUParent::MapCallback` from 9.57 to 1.88 (factor 5.1)
   where no throttling exists at all.*
4. ~~"Release needs a `CHANNEL_TYPE` enum migration / an `_ENUM_ADDITIONS` entry."~~ **Verified
   dead twice.** Production's `pg_enum` reports `nightly | beta | release` with sortorder 1/2/3,
   and a fresh Postgres built by `models.create()` reports the same three. `release` has been in
   `config/global.json`'s `channels` since `3695614` (**2018**-02-24) and `models.py:18` builds the
   enum from `config.get_channels()` at table-creation time. `_ENUM_ADDITIONS` stays
   `{"VERDICT_TYPE": ("lead",)}`. **Do not touch `_ensure_enum_values`** — its `ALTER` can never
   fire on this DB and it is not needed here. (This is the one thing that genuinely separates
   release from ESR.) Also dead: `builds.version VARCHAR(10)` — the longest release
   `target.version` in Buildhub history is `134.0.1`, 7 characters.
5. ~~"Release needs an `aurora`-style search alias in `utils.get_search_channel`."~~ **Measured
   dead from both data sources.** Every one of the 26 Buildhub release buildids facets to exactly
   `[('release', n)]`. Socorro's `release_channel` facet over Firefox gives `release` (595,774-601,089)
   and `esr` (449,667-453,746) as **disjoint** terms; the junk tail is `release-localtest` 4,
   `releasexx` 1, `norelease` 1 out of 601,089. The only other label carrying release buildids is
   `default` at 0.3-6.2% of reports, and it comes from **2-12 machines** (6,567 reports from 4
   installs on 153.0.3; 1,398 from 2 on 154.0) — distro rebuilds or CI loops, which antenna accepts
   at **100%** while throttling real release traffic to 10%, so aliasing it in would import
   **unthrottled single-machine floods** into the install counts the whole selector is keyed on.
   `tests/test_beta_channel_wiring.py:113-122` already asserts release passes through unchanged.
   Do not touch it.
6. ~~"Change the merge-extraction rule for release."~~ **Keep the rule. But record that its
   stated justification is FALSE on release.** The docstring's reason — a release merge push's
   members already have `changesets` rows under nightly — does not hold: **4.9-10.2% of a
   beta→release merge push's members exist on `mozilla-beta` and NOT on `mozilla-central`**
   (155.0: 263/5,391 = 4.9%; 154.0: 533/7,448 = 7.2%; 153.0: 705/6,888 = 10.2%), and those grafts
   are exactly the cycle's `a=`-approved uplifts. Its supporting measurement (1,932 of 1,932
   merge-window candidates share a node hash with the m-c pushlog) was taken on the
   **central→beta** merge. **The rule is right anyway, for a different reason:** `Changeset.find`
   filters `Node.channel == channel` (`models.py:502-524`, predicate at `:516`), so an m-c row is
   **unreachable** for a release crash however many hashes overlap. Fix the prose, not the rule.
   `pushlog.suppresses_merge_extraction` returns True for beta, release AND esr (False only for
   nightly / None / ""), and `is_merge_push` fires on 8 of 8 release cycle merges measured (14-22
   merge-flagged members among 5,405-7,464), with total separation from ordinary release pushes
   (20 dot windows span 5-116 changesets).
7. ~~"Fix `population.for_crash`'s facet truncation."~~ **Real mechanism, zero live error — a
   footnote, not a defect.** `population.py:182-189` asks for `_facets: "install_time"` at
   `_facets_size: 1000` (`config/global.json:96`) with no cardinality, and `summarize` divides by
   `len(installs)` (`:117,124,128`). On beta's `OOM | small` over 30 d the facet returns exactly
   1000 terms against a true cardinality of **5,027 (5.03x)**, and the fix is genuinely free (one
   request with `_facets: ["install_time","_cardinality.install_time"]` returns both). **But the
   error on every page prod can render today is 0%:** an exhaustive census over the widest window
   the panel can use (30 d) finds the only nightly signature above 1000 installs is `OOM | small`
   (1,052, 1.05x), and beta has `OOM | small` (5,027) and one `AllocateTenuredCellInGC` OOM
   (2,588) — while **0 of 1,946 nightly and 0 of 8 beta INGESTED signatures exceed the cap**, the
   busiest sitting at 480 (48%). The census is complete because the 1000th faceted signature has
   count 1 (7 d) / 2-3 (30 d) and cardinality ≤ report count. **It reaches no prompt and no filed
   bug:** the only consumer is `html.py:72` → `crashstack.html`; the filed bug's install count comes
   from `report_bug.get_stats` (`report_bug.py:68-72`), which HAS the cardinality fallback
   (measured: truncated facets sum to 1,002 and `get_stats` correctly returns 3,607); prompt
   numbers come from `sigtrend.py:195` and `datacollector.py:272`, both `cardinality_install_time`.
   **And the UI already says so:** `crashstack.html:46` renders `1000&ge;` and `:94-96` prints "The
   facet list was capped, so the install count is a floor." The claimed direction is also inverted
   — `per_install` is **OVER**stated, not understated (beta/7d 1.51 vs true 1.42; beta/30d 2.82 vs
   1.36). The trigger is **distinct installs, not volume**: nightly's loudest signatures are
   `libc.so.6 | cuEGLApiInit` 2,824 reports / **17** installs, `abort | libMangoHud.so` 1,296 / 2,
   `GMPChild::RecvPreloadLibs` 1,075 / 1. *The real adjacent defect is the opposite one: the panel
   **OVER**-warns, printing "the facet list was capped" and a `≥` on an UNCAPPED facet for 2 of 9
   real ingested signatures, because ~1-4% of reports carry no `install_time` and that alone makes
   `faceted < total`.* If it is ever fixed, do **NOT** re-base `top_share`/`concentrated` onto
   `total` — the 0.5 threshold at `config/global.json:97` was fit on the `install_time` facet's own
   denominator (`config.py:285` says so explicitly).
8. ~~"`html.diff()`'s unvalidated `channel` is a security hole."~~ **No SSRF, no traversal off
   origin, no XSS.** `html.py:522-539` does interpolate a raw `channel` into
   `Mercurial.get_repo_url` → `https://hg.mozilla.org/releases/mozilla-<channel>`, but no
   server-side fetch happens anywhere in `diff()` (pure string concat + two `<iframe src>`); the
   scheme+host prefix is fixed and unnullifiable, so nothing escapes the origin
   (`channel=x/../../../../../../robots.txt%3f` resolves to a path on hg.mozilla.org, which answers
   302); and Jinja autoescaping renders `" onload="alert(1)` as `&#34; onload=&#34;`, so there is no
   attribute breakout. Worst achievable: two iframes pointing at an arbitrary path under
   hg.mozilla.org, on an unlinked legacy page. And **`style`, `node`, `filename`, `line` and
   `changeset` are equally raw in the same f-string**, with `style` closer to the front — so a
   channel-only fix is theatre. If you touch it at all, validate against `config.get_channels()`
   **and** the other five.
9. ~~"Release needs a bigger `buildhub_lookback_ndays` on switch-on, the way beta did."~~
   Measured dead: `update()` calls `put_filelog` first, which sets `LastDate.maxdate = now`, and
   `update_builds` then subtracts the already-widened 30 days — which for today's data returns a
   full 3-row window on tick one, all three revisions node-backed. Release's median cadence is
   7.02 d and the largest gap in 83 builds is 20.89 d, so a 30-day window is never empty. **The
   binding constraint is the retention** (item 17).
10. ~~"`target.channel=release` carries RC/candidate builds that pollute the window."~~ The RC and
    the shipped release are **ONE** Buildhub document listed under two channels: for
    155.0 / 154.0 / 154.0.1 / 153.0.1 / 151.0 the `build.id` sets and `source.revision`s on
    channel=release and channel=beta are identical, and there is exactly one `build.id` per
    (version, respin). Also: **0 of 35** release `build.id`s have more than one distinct
    `source.revision` — a respin is a NEW build.id (20260727110312 vs 20260727124451) — so
    `buildhub.get`'s `size: 1` revisions sub-aggregation loses nothing. What a respin actually does
    is add a `builds` row with 0 reports / 0 installs, which `min_build_installs = 15` already
    handles (it fires on 9 of 133 run-days in the replay).
11. ~~"A third channel needs a bigger `worker` dyno, or will break `_chain_is_running`, or will
    exhaust Redis."~~ All three measured dead. Memory: a release tick's `get_new_signatures` data
    dict retains **29.08 MB** (peak 36.59) against nightly's already-existing 28.31 MB, and wall
    time is 3.2 s vs nightly's 33.2 s — release is the **cheaper** tick on both axes, because
    nightly loops 81 builds where release loops 3. `_chain_is_running` (`update.py:157-177`) now
    counts only jobs whose `func_name` matches the chain function, and its own docstring names
    "three channels" as the case the old `len(queue) <= 1` check broke on. Redis: **8.80 MB of a
    25 MB maxmemory** (peak 10.62), 3,327 keys, **12 of 20 connections**, all four queues empty; a
    third channel adds ONE `update` job per 20-minute tick with a two-string payload and
    `result_ttl=0`. The number to watch is **connections, not memory** — a fourth agentworker
    would take it to ~14 of 20. (`Persistence: None` and `noeviction` remain true and are unchanged
    by the channel count.)
12. ~~"The extra serial patch parsing from release will starve nightly and beta."~~ **~30x
    cheaper than beta.** 152 `changesets` rows over 14 release build windows spanning 77 days =
    **1.97 patch.parse/day**, against beta's 45-122 per ~2-day window (~25-60/day). Merge windows
    add 0-2 rows because the suppression is already on for any channel ≠ nightly. And on the
    56-day catch-up: 19,727 of 20,094 changesets are `via_merge`, so only **110 rows / 34 nodes**
    survive ≈ ~2 minutes.
13. ~~"441 MB at a 183-day gap is an OOM, so the clamp is a memory fix."~~ **Refuted three ways.**
    Growth is **not per-day** — 19,727 of 20,094 changesets (98.2%) come from three cycle-merge
    pushes, so the payload steps every 2-4 weeks, not daily. 441 MB is **86% of a 512 MB quota**,
    not an OOM (Heroku R14 fires above quota, R15/SIGKILL far above). And 441 MB is not derivable
    from the measured numbers by the claim's own linear model (0.543 MB/day × 183 = 99 MB payload;
    the merge-driven estimate is ~76 MB → ~374 MiB peak in a bare process). Treat it as an
    **unverified extrapolation**. Also: "peak RSS 215 MB" conflates process total with cost — the
    process was already at 139.7 MiB holding the body, so the increment is ~106 MiB and the true
    peak on the worker dyno would exceed 215 MB.
14. ~~"`Build.get_last_versions`' removed major-version break needs a release-specific
    replacement."~~ Dead. The 31-run-day release replay had **zero** blackout days and the window
    correctly straddles a version bump (2026-07-27 → `['152.0.5','152.0.6','153.0']`, 13
    selections). The builds before the first release of a cycle ARE the previous cycle's dot
    releases, and "is this build crashier than the ones before it" is exactly the question.
    Re-adding a break would restore the 9.4%-of-run-days silent switch-off plan #18 just removed.
15. ~~"`min_build_installs` / `spike.ratio` / `protos` / the no-user window slot need tuning for
    release."~~ Out of scope while release is not selected (§11). `min_build_installs = 15` is
    correct with a **55x margin** and must not be touched. The window-membership fix was measured
    and **raises** the worst run (329 → 420 pairs but worst run 35 → 42, and it recovers only 2 of
    the 9/day lost in the specific 5-day 155.0 blackout).
16. ~~"`_bug_version` must become `Firefox NNN` for release", "the filer must set
    `cf_tracking_firefox<N>`", "`report_bug.get_stats` over-counts installs on release", "release
    needs a tighter poison rule", "release needs a module-only third-party signature filter",
    "`_OTHER_APP_PRODUCTS` needs release entries".~~ All measured dead. `_bug_version("release")` →
    `"unspecified"`, which is what humans and bots use (147 of 236 recent desktop crash bugs; the
    release-mgmt bot uses it on 58 of 58). `cf_tracking_firefox153/154` carry 19 and 13 bugs over
    200 days with values `+` (28) and `blocking` (4) — approvals set by hand, not nominations — and
    `cf_status_firefox154` is already populated on 24 of our own 72 filings by someone else.
    `get_stats`' install sum is right to within 1% on 12 real release pairs (sum/true = 0.99-1.01),
    because an installation contributes one `install_time` value regardless of how many builds it
    crashed on. Poison rate **per installation** is release **0.701%** against nightly 1.160% and
    beta 0.975% — release is **less** dense, and the poison-address predicate, the `groups` create
    parameter and the per-product security group are all channel-independent. Module-only
    signatures above the install threshold are **7/326 = 2.1%** on release against **22/494 = 4.5%**
    on nightly. `_OTHER_APP_PRODUCTS` is keyed on the crash's Socorro **product**, not the channel,
    and `_split_by_application` rescued **0 of 64** release venue signatures.
17. ~~"ESR needs adding."~~ Not a defect and not dead — a **scope question** (§10 Q3). ESR is a
    separate Socorro label, a separate Buildhub `target.channel` (199 docs vs release's 205; all 29
    esr `target.version`s carry a suffix that the anchored `VERSION_PATS["release"]` rejects), has
    **no `CHANNEL_TYPE` value**, and `Mercurial.get_repo_url('esr')` resolves to a **nonexistent
    repo**. But it is **39.4-40.9% of Firefox crash reports** across three instruments and windows
    (39.4% = 449,667 of 1,142,273 on the 2026-08 `release_channel` facet) — the largest
    population the product cannot represent. Do not quietly inherit "out of scope"; answer Q3.

---

## 9. Rollout

The levers are of three kinds and that decides the order. `INGEST_CHANNELS` and
`AGENT_CHANNELS` are **env vars** (`heroku config:set`, effective next 20-minute tick, no
deploy). `agent.channels`, `agent.autofile.channels` and `agent.calibration.channels` are
**config-file only**, so they need a **deploy**. The residue delete is a **one-off SQL
transaction** and it is the only irreversible step in this plan. **There is no Bugzilla write
anywhere in this plan, on purpose.**

### Phase 0 — Group A (items 1-10) · *ships regardless of release*
**Landed, not deployed: items 1 (`e65f801`), 2 (`bd92fcb`), 3 (`c083308`). Items 4-10 are open.**
Land, deploy, verify. **Exit:** item 2's `0%` clause cannot be produced with
`broken_cpu_rate=None`, and the false "reaches the filed bug" comment is gone from both
`orchestrator.py:2838-2839` and plan #18 item 24; item 3's beta prompt contains no undefined
denominator; item 4 writes `filing_declined` on a declined nightly run and the nightly count
of rung-reached-but-unrecorded stops growing past 34; item 5's four indexes appear in prod
`pg_indexes` **after a deploy on the existing DB** (this is the `_ENUM_ADDITIONS` failure mode
— check it, do not assume); item 6 returns nothing for a release changeset; item 7 sweeps a
non-ingested channel; item 8's greps are empty; items 9 and 10 have no wrong number left in
the tree.
*Honest note: item 5's `nodes(channel, node)` index is worth ~0.2 s/day on its own. It is in
this phase for the UNIQUE constraint, not for the speed.*

### Phase 1 — Group B (items 11-15) · *before any release row can exist*
**Landed, not deployed: items 11+12 (`deef91a`), 13+14 (`446a8b8`), and half of 15. The
calibration guard loop is the remaining half.**
One deploy, with `agent.channels` still `["nightly","beta"]`. **Exit:** `INGEST_CHANNELS=""`
logs and ingests nothing; `AGENT_CHANNELS=""` logs, enqueues nothing and **sweeps nothing**;
`get_agent_calibration("release")` is `{}`; `autofile_channel_declared("release")` is True and
`autofile_channel_held("release")` is True, with the **held** message reachable through
`retrigger_agent(force=True)`; and under `AGENT_CHANNELS=""` no invariant loop in
`tests/test_shipped_channels.py` iterates zero channels (the calibration loop iterates beta +
release) rather than passing vacuously — **the iteration set is the assertion, not a failure
count** (item 15).
**Tripwire:** the failure COUNT is the wrong instrument — it is **1** under `AGENT_CHANNELS=""`
both before and after item 15 (measured: 16 tests, one failure, `test_the_shipped_agent_channels`),
because a loop moved off the env var is insensitive to it. Assert the ITERATION count: if the
invariant loops still iterate **zero** channels under `AGENT_CHANNELS=""`, **item 15 did not
land**.

### Phase 2 — the residue and the clamp (items 18, 19) · *only if Phase 3 is approved*
Deploy item 19 **first**, then run item 18's transaction. Item 19 protects nightly and beta
after any stall and is worth shipping on its own merits; item 18 is pointless without it and
dangerous done halfway.
**Exit:** `nodes WHERE channel='release'` = 0 and `lastdate WHERE channel='release'` = 0 rows,
committed together; `changesets` drops 33,147 → ~12,827; `update_builds` logs a warning when
Buildhub answers falsy.

### Phase 3 — **OPTIONAL** ingest-only measurement cycle · zero LLM spend, zero Bugzilla risk, no deploy
**Decision, not a default: §10 Q4.** If approved:
```
heroku config:set INGEST_CHANNELS="nightly beta release" -a crash-clouseau-augmented
```
`agent.channels` stays `["nightly","beta"]` and `AGENT_CHANNELS` stays `"nightly beta"`, so
`enqueue_agent` drops every release uuid and `UUID.untriaged` never sweeps them. **Set it
explicitly. Never clear it** — even with item 11 landed, clearing it now means "ingest
nothing", which is a different silent failure.
**Watch list, all free:**
* The **two-column per-channel `last_node` vs `last_uuid_row` SQL** from
  `ingestion-stall-has-no-alarm`. The uuid clock lies by hours; the node clock is the honest
  one. Baseline for reference: nightly runs **86 dossiers/day** (2,665 over 31 days,
  prod SQL — §7.13); the `~85-120/day` in `ingestion-stall-has-no-alarm` is the older
  eyeballed range, not a second measurement.
* The `"No buildids for Firefox-release"` warning count — **must be ~0**.
* `builds` rows for `product='Firefox', channel='release'` after the first tick — **expected
  ~5** (30-day lookback at a 7.02-day median cadence, all node-backed by the fresh 30-day
  backfill), **zero today**.
* The `selection` outcome mix **per channel** (`selected` / `below_install_threshold` /
  `untestable_prefix` / `not_spiking`). Expect a large write volume, from **two
  instruments**: **~3,310 rows** in ONE real end-to-end tick (`esr_16_selector.py`, one run on a
  seeded Postgres) and a median **3,679 per run** (max 9,830) over the 133-run-day replay
  (`sel_25_floorlog.py`), ~93% of them saying only "fewer than 50 machines".
* `useless=True` and `"agent: no scored changesets"` **counted per channel** — the free
  measurement of whether the off-stack path would be carrying release at all.
* The `patch.parse` backlog around the first cycle merge — **expected ~110 `changesets` rows /
  34 nodes ≈ ~2 minutes**, not the 19,527 the July residue produced.
* The first tick's `put_filelog` duration, the pushlog window it logs, and the `worker` dyno's
  RSS (512 MB, and it also carries nightly and beta report scoring).
* `pg_stat_user_tables.seq_tup_read` on `changesets` before and after Phase 0's indexes.
* **New `uuids` rows with `channel='release'` per day, days 2-8.** This is the whole point of
  the phase.

**Tripwires — "if you see X, item N did not land":**

| observation | conclusion |
|---|---|
| first release tick logs a pushlog window **> 31 days** | item 19 did not land, or item 18 deleted only the nodes |
| first release tick logs a window of **~0 days** | someone ran `models.Node.clean(date,'release')` from a console instead of item 18's transaction — **stop, the channel is now silently under-filled** |
| `builds` rows for channel='release' still **0 after two ticks** | `if data:` swallowed an empty Buildhub answer; item 19's warning is missing or Buildhub latency exceeded the tick |
| `changesets` on release nodes climbing past **~150 in a non-merge window** | item 6 did not land, or `suppresses_merge_extraction` is not firing |
| `Changeset.add` on the first tick **> 400 s** | expected — the `HGAuthor` per-node commit is out of scope here (§11). Not a regression |
| any dossier with `channel='release'` | `AGENT_CHANNELS` gained release or was emptied — **item 12 did not land**; `heroku config:set AGENT_CHANNELS="nightly beta"` |
| a release `p_worth` badge on crashstack.html | **item 13 did not land** |
| `autofile_channel_declared("release")` True **and** `held` False | item 14's overlay is missing its explicit `enabled: false` — **the one dangerous shape** |
| **the UUID rate** | **≤10/day ⇒ the $207-740/month projection was right; ≥40/day ⇒ the $960-4,050 one; anything between ⇒ neither, and re-measure before triage is discussed at all.** Do **not** read day 1 — the replay shows a cold start |

**The last row is the point of the phase.** The two cost instruments disagree **9.4x** for
reasons that are now understood (a 61-day proto window vs a silent 7-day one, and a 1.2-2.2x
per-tick union factor measured on n=4 pairs at daily granularity against a tick that runs
72x/day) but **not resolved**. Nothing in the repo, and nothing offline, tells an operator in
flight which projection was right. **A tripwire is the only instrument that does.**
**Measure for at least two weeks INCLUDING one cycle merge.**
**Kill switch:** `heroku config:set INGEST_CHANNELS="nightly beta"`. Release rows already
written stay and are harmless (nothing reads them without `agent.channels`) — but they then
become residue again, so re-run item 18's transaction if the phase is abandoned.

### Phase 4 — triage · **NOT PLANNED. Documented so nobody starts it by accident.**
The arming criterion does not exist yet. It needs **both** numbers §10 Q1 names: the UUIDs/day
figure Phase 3 would produce, and the release **culprit** rate (the one filing path the stale
gate leaves open, unmeasured, against nightly's 28 of 2,661 verdicts = 1.05%). If it is ever
attempted, it needs Group C shipped first (items 16 and 17), the six shipped tests that fail
the moment release joins `agent.channels`, and `agent.autofile.channels.release.enabled` left
**false** for a full cycle so item 4's `filing_declined` record can answer "how much would it
have filed".

### Irreversible steps, called out
* **Item 18's `DELETE`.** Recoverable only by a 30-day re-ingest — which is exactly what it
  triggers, so the recovery is the intended behaviour. **One transaction.** Never
  `models.Node.clean` from a console.
* **Item 5's `_ADDED_INDEXES` hook** is a third schema-evolution mechanism. The indexes
  themselves are reversible (`DROP INDEX`); the hook should be reviewed like a migration
  framework, because `_ENUM_ADDITIONS` has never fired on this DB and nobody noticed for
  months.
* **`_ENUM_ADDITIONS` / `ALTER TYPE`** is one-way and **is not needed here** (§8.4). Do not
  touch it.
* **No Bugzilla write.** Everything in this plan is recoverable.

---

## 10. Open questions — decisions the user has to make

**Q1. Arm release triage at all?**
**RECOMMENDATION: NO.** The case against is §1.1-1.6: ~0.6 genuinely-new signatures per cycle,
2 desktop-native uplift-caused crash regressions in 20 months, 73% of selections on a build
whose on-stack window cannot contain the answer, parity rather than speed against humans (8.2 d
vs 7.6-10.4 d, and 79% of release-tracked crash bugs are filed **before** the version reaches
release), and a cost unresolved across a 9.4x band against nightly's measured
$4,552-4,600/month.
**The two measurements that would change it:** (a) **the UUIDs/day number nobody has** — if
Phase 3 shows ≤10/day, release is a ~$200-400/month experiment and the answer becomes
arguable; if it shows ≥40/day it is a second nightly-sized bill and the answer is settled;
(b) **the release culprit rate** — the stale gate clamps every release LEAD (199/203 = 98.0%)
but not a `culprit` at ≥70, and nightly runs 1.05% culprit (28 of 2,661, all at ≥70). If a
held release cycle produced ≥5 culprit verdicts a month, the "filing is blocked" argument
collapses and this needs re-deciding on precision, not on supply.

**Q2. Serve the below-both-upstream-floors release-only population as a separate feature?**
**RECOMMENDATION: measure first, build nothing yet.** The supply is real and larger than
anything else in this plan: **10 of the top 200 (5.0%) and 37 of the top 350 (10.6%)** release
signatures sit below both upstream spike floors over 90 days, ~5 of them Firefox code with
1,493 / 465 / 416 / 398 / 274 release installs against 0-1 nightly (§1.7). **But it is a
different product and nothing in the regressor pipeline serves it:** these crashes predate every
build in any candidate window, so there is no window to attribute; `_apply_signature_age_gate`
clamps them by construction; `regressed_by` is meaningless; and an LLM run would have nothing
to reason over.
**What it would take:** a signature-level alarm, no LLM run at all — a 90-day three-channel
install-cardinality cross (release vs nightly vs beta, remembering that
`utils.get_search_channel` widens beta to include `aurora`, which is ~36% of beta and whose
omission undercounts it), a below-both-floors predicate, and an output that is a **list a human
triages**, not a filed bug.
**The number that would change the recommendation:** how many of the 37 have **no open bug**.
That is ~37 `_open_bugs_for_signature` reads, minutes of work, and **unmeasured**. If most are
un-bugged it is a real product; if most are already tracked it is redundant with `topcrash`
triage (97 `topcrash` bugs in 2026 = 12.1/month, plus ~20 crash bugs/month from
`release-mgmt-account-bot` and 109 crash-signature bugs from aryx).

**Q3. ESR?**
**RECOMMENDATION: answer it explicitly as a scope question, and the honest default is NO for
now.** ESR is **39.4% of Firefox crash reports** (449,667 of 1,142,273, `release_channel` facet over
product=Firefox, 2026-08-01..08-31) — 39.4-40.9% across three instruments and windows, the
largest population the product cannot represent — and it is a genuinely separate label,
Buildhub channel and branch, so no release query picks it up and no release change touches it.
Against: it needs a `CHANNEL_TYPE` enum value, which is precisely the case
`_ensure_enum_values`' `ALTER` can never satisfy on this DB; `Mercurial.get_repo_url('esr')`
resolves to a nonexistent repo; and every argument in §1 gets **worse**, because ESR code is
older and its uplift windows are the same dot-release shape. For: ESR is **unsampled**, which
makes it the only high-resolution instrument for release-codebase behaviour — it is how the
only *direct* thinning measurement in this campaign was taken (esr153, median k 1.150).
**The number that would change it:** ESR's own addressable set, measured the way §2.2 measures
release's. **Nobody has it.**

**Q4. Run an ingest-only measurement cycle (Phase 3)?**
**RECOMMENDATION: YES, once — after items 11-15, 18 and 19, for one cycle, and only for the
UUID tripwire.** It is the one instrument that resolves the 9.4x cost disagreement without
spending on LLM runs, and it also exercises `put_filelog` against `releases/mozilla-release`,
the release Buildhub regexp, the merge suppression on a real beta→release merge, and
`Build.put_data`'s node dependency — all of which have only ever been measured offline.
**Against, honestly:** the age clock **already reads release without ingestion**
(`sigage.py:160` builds `any_params` with a `release_channel` FACET and no filter, so
`first_seen_any` and `total_other_channels` see release and esr today); `sigtrend` refuses
release; each cycle merge writes 5,400-7,500 `nodes` rows plus author round-trips into a
Postgres whose `builds`/`uuids` tables are already pruned to 30 days; and the `selection` rows
it produces can be replayed offline for free.
**The number that would change it to NO:** if the UUID rate can be settled offline instead — by
replaying `_cardinality.proto_signature` per pair over the full 329-pair set at the **deployed**
cadence, and reporting both the per-tick capped count and the per-pair LIFETIME union
(`UUID.add` dedups on `(signatureid, protohash, buildid)`, verified at `models.py:1454-1481`, so
a new protohash entering the count-ordered top-N on a later tick is a new row). That is free and
it is the measurement Phase 3 would buy. **Try it first.**

---

## 11. Explicitly out of scope

* **Arming release filing**, and therefore `skip` vs `file_new` for release. Recorded numbers
  so it is not re-litigated: selection-level venue 14/30 = 46.7% (Wilson 30.2-63.9),
  indistinguishable from beta at n=30 and significantly above nightly; and the right-venue
  statistic on the **filer-visible** population argues **for** `skip` (15 of 22 = 68%, Wilson
  47-84).
* `severity: "S3"` on release filings, `cf_tracking_firefox<N>`, `_bug_version` mapping,
  `_fixed_after_build_bug`'s inverted meaning on release — all moot while filing is off (§8.16).
* **All burst fixes**: `spike.ratio.Firefox.release` 3→5 (329 → 117 pairs, worst run 35 → 11),
  rate-normalising `is_spike`'s second branch (118 pairs, worst run 7), and the equal-days-since-ship
  maturity correction. Measured equivalent on boilerplate share (46.2% vs 44.9%), and the
  arrival-curve measurement shows the maturity fix does **not** remove the bursts. They matter
  only if release is selected.
* `thresholds.protos.Firefox.release` 20 → 5 or 3.
* Admitting the cycle merge as a first-class release window (the beta-only graft subset,
  263-705 changesets per merge, 15-76 min of serial queue every 14-35 days).
* The off-stack ranking tier order (`pref_flip` ranks above token overlap in the tuple sort at
  `orchestrator.py:422-433`; tier(non-backout AND pref_flip) = 294 against `max_candidates` =
  150 on a release merge window, dropping the two best token-overlap changesets — counts
  unreplicated). And note there is **no per-channel off-stack switch**: `config.get_agent_offstack()`
  takes no channel, so nothing can put release's off-stack path into observe-only without
  disarming nightly's.
* `HGAuthor._get_or_create_id`'s per-node commit — a real ~3x lever on **every** channel, with
  its own test surface. Item 19 names it; it is not in this plan.
* `/api/selection`'s validate-then-ignore `channel`. Real and live: three byte-identical bodies
  (same sha256 over `rows`, 274,827 bytes) for `?channel=nightly|beta|release`, 500 rows of 450
  nightly + 50 beta, while the 14-day window holds 2,930 rows (1,902 / 1,028); `?channel=bogus`
  correctly 400s; `html.selection()` (`html.py:301-305`) never reads the parameter, while the
  API's *signature* branch does filter — so the two surfaces disagree about the same row. Beta is
  starved by `build_day DESC`, not by `number DESC`. **Worth shipping, in its own change**, and
  the fix is two lines (pass `product`/`channel` through to `Selection.summary`/`recent`,
  `models.py:1202`/`:1217`, exactly as `for_signature` at `:1191-1198` already does) — do **not**
  raise the 500 limit instead, on a single-worker web dyno. **Say in the commit that
  `?channel=release` will then correctly return `{"rows": []}`**, or the first empty response
  reads as an outage. There is no test on `/api/selection` at all.
* `POST /api/javast`'s 500 on any string buildid (`models.Build.get_changeset`,
  `psycopg2.errors.DatetimeFieldOverflow`) — a separate unauthenticated-endpoint defect.
* `report_bug.get_stats:70-71`'s hardcoded `it == 100` against a `_facets_size: 100` set 80 lines
  away in two different callers: correct today (verified firing), silently wrong the day either
  number moves.
* **The memory-safety backfill.** Verified on prod v139: `/api/evidence?uuid=41bb8c8a-3458-4803-90f8-a7a850260819`
  returns 200 with `withheld: null` and empty `corroborations` — the canonical poison dossier is
  still served **anonymously** and `bin/backfill_memory_unsafe.py --apply` has **not** run. Not
  release's problem, but it is the largest open item adjacent to this one.
* **The docs diff.** Every line plan #18 §12 flagged is still wrong and a release rollout makes
  more wrong: DEPLOY.md:4 ("nightly-only, observe-only canary"), DEPLOY.md:40-42 ("do NOT set a
  Bugzilla token / strictly read-only" — **flatly false**, prod has the token and
  `AUTOFILE_BUGS=1`, and it is the line someone rolling release out will read), DEPLOY.md:45
  (`agentworker=1` vs the actual 3), docs/architecture.md:100-101 (the `INGEST_CHANNELS`
  default, which item 11 changes), README.md:9 (the wrong app — `clouseau.moz.tools` 404s on
  `/tasks.html`, `/selection.html` and `/api/selection`; the live app is
  `crash-clouseau-augmented-2d0335d53b8d.herokuapp.com`). Item 11 carries the one DEPLOY.md
  sentence this plan needs; the rest should ride whichever change actually deploys.
* CI's `DATABASE_URL` (`.taskcluster.yml:48` points at a Postgres the image does not provide:
  **114 errors** with the exact CI command).

---

## 12. Already true — verified live, do not rebuild

1. **`compiled_out._CHANNEL_MACROS["release"]` (`compiled_out.py:165`) and
   `_CHANNEL_OFF_HOLLOW["release"]` (`:191`) are correct.** Checked against
   `releases/mozilla-release`'s **own** source, through `hgedge.raw_file(..., channel="release")`,
   at tip **and** at a real 154.0.1 build rev: `init.configure` has
   `is_early_beta_or_earlier = is_nightly` at `:1140`, `set_define("NIGHTLY_BUILD",
   milestone.is_nightly)` at `:1166`, `set_define("RELEASE_OR_BETA", milestone.is_release_or_beta)`
   at `:1168`; `moz.configure` has `MOZ_DIAGNOSTIC_ASSERT_ENABLED … when = moz_debug |
   milestone.is_nightly | moz_dev_edition` at `:170`. **All three ON macros and all five OFF
   macros are right.** Release is additionally **structurally immune** to the pre-alias early-beta
   predicate, because of the `any(x in app_version_display for x in "ab")` conjunct on
   `browser/config/version_display.txt` — confirmed at three pre-alias release revs
   (400562fcbb16, 5aeb361e6f23, e3f2d501dc29). Release's partition is byte-identical to beta's and
   that is correct for all 8 macros; the most worth adding is **one assertion that they are equal,
   with the reason**, so a future divergence is deliberate. **Do NOT add `MOZ_ESR`** to release's
   hollow set: 5 C++ hits on mozilla-release against `NIGHTLY_BUILD`'s 368 — a searchfox lookup per
   candidate symbol for essentially no detection, the same argument `compiled_out.py:182-187`
   already makes for leaving `RELEASE_OR_BETA` out of nightly's arm.
2. **`searchfox._CHANNEL_REPO["release"] = Repo.RELEASE` (`searchfox.py:131`, `:162`) works, and
   `firefox-release` is genuinely indexed.** Tree `firefox-release` answers HTTP 200 with 78,113
   bytes of index at rev `d065a04bc561` (version 155.0). The obvious objection — it is indexed a
   whole cycle ahead of the crashing population, 72% of release reports being on 154.0/154.0.1 —
   was **measured false**: diffed against the actual 154.0.1 build source, `firefox-release` is
   **closer than `firefox-main` on 6 of 8 crash-relevant files and never worse** (max matching-block
   line shift 4 vs 35 on `nsDisplayList.cpp`, 14 vs 31 on `Cell.h`, 151 vs 251 on `nsINode.cpp`, 54
   vs 58 on `GC.cpp`, ties on the rest). Already asserted at `tests/test_product_wiring.py:1160`
   and `:1169-1179`, `tests/test_beta_channel_wiring.py:487-488` and `:506-511`,
   `tests/test_searchfox.py:263`. *Incidental finding worth keeping: the **beta** arm's value is not
   constant — at 155.0b5 against a `firefox-beta` tree that had just merged to 156.0b1,
   `firefox-beta` and `firefox-main` gave IDENTICAL content on 5 of 8 files. The beta tree's
   advantage peaks just before merge day and decays to zero just after.*
3. **`buildhub.VERSION_PATS["release"]` (`buildhub.py:24`) is anchored and correct.**
   `[0-9]+\.[0-9]+(\.[0-9]+)?` excludes **0 of 32** real release versions and rejects `155.0b9`,
   `140.8.0esr` and `155.0a1`. Buildhub's firefox channel buckets for 2026 — nightly 3,002 / beta
   751 / aurora 546 / **release 205** / **esr 199** / nightly-maple 69 — confirm ESR is its own
   channel, and Elasticsearch anchors `regexp`, so pointing the release pattern at channel=esr
   returns `[]`.

Also already correct and worth not re-deriving: `pushlog.suppresses_merge_extraction` covers
beta, release and esr for free; `is_merge_push` fires on 8 of 8 release cycle merges;
`roles._BUILD_NAME`, `triage._CHANNEL_LABEL`, `report_bug._PROVENANCE_SCOPE` and the per-channel
`_system_prompt` cache all have release arms that resolve; `tasks.html:102` renders three
distinct channel letters (N / B / R) with no collision; `reports.html:41-50` builds its dropdown
from the DB so release appears automatically once release uuids exist; `sigage.first_seen_ever`
needs no product or channel mapping and answered **300 of 300** randomly sampled release
signatures (100%, against beta's 77/77 and nightly's 7.6%-of-dossiers blind spot); and
`agent.sweep`'s caps do **not** need raising for a third channel (`orchestrator.py:4215` caps the
tick at `max_per_run` = 3 regardless of channel count) — what a third channel adds is a
**starvation** coverage gap, not spend, and item 12's fix is what closes the version of it that
is reachable today.

---

## 13. Instrument rules this campaign had to learn twice

These are the project's own rules, and every one of them was violated by at least one report in
this campaign. They belong in the plan because the next measurement will be taken by someone who
has not read RECON.json.

1. **A SuperSearch query with no `date` key silently means ~7 days.** `date=""` is a hard **400**.
   This broke the headline supply number (§1.1) **and** the proto cost model (§7.3) — the same bug,
   in two dimensions, in one campaign.
2. **Socorro caps `date` ranges at 365 days** (`>=2025-01-01` → 400 "Date range is bigger than 365
   days"), so a true "lifetime" query is not expressible for a build older than a year, and the
   instrument silently changes meaning as it ages.
3. **`_facets_size` is capped at 10000; 10001 is the first 400.** `sigage.py:22` says the opposite
   and three agents believed it (item 9).
4. **"Installations" means a PER-DAY distinct `install_time` count.** Crash REPORTS are not a
   volume metric; `install_time` resets on update, so a multi-day cardinality is neither
   installations nor install-days. Six reports quoted the same build's "installations" at four
   window lengths differing **12x** (build 20260812182057: 5,031 at 3 d, 35,589-36,050 at 7 d,
   67,067 lifetime, maximum per-day distinct **11,888**) and **none of them was a machine count**.
5. **A top-N-by-volume PANEL is not the population a gate sees.** It flattens every channel
   difference (nightly 63.0% / release 64% / beta 59.0% on venue rate, p=0.56) while the SELECTION
   instrument separates all three (nightly 21.7% / release 46.7% / beta 50.6%). Say which you
   measured.
6. **An all-channel rate is not a channel's rate**, and a channel-wide factor is not a
   per-signature one (4.1x vs 8.83x on the same quantity, §2.3).
7. **Where two instruments disagree and neither won, say so.** UUIDs/day on release is
   **unresolved between 6 and 58 and nobody has the number**. `installs = 50` is **~205 to ~440
   machines**, an interval from two unmeasured models.
8. **Release's accept rate is class-dependent (10% to 100%)**, so the honest statement is an
   interval, and `has_comments`/`has_phc` are **unmeasurable with our token** — a filter on a
   protected field is silently ignored, returning the full unfiltered total with `errors: []`.
9. **`utils.get_search_channel` widens beta to include `aurora`** (~36% of beta). Forgetting it
   undercounts beta in any cross-channel visibility check.
10. **`heroku logs -n 1500` is ~2 hours** and there is no drain, so any measurement that depends on
    a log line has to be taken **while it is happening**. That is why §9's tripwires are SQL and
    counters, not greps.
