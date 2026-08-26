# Plan #18 — Beta channel support

> **Status:** design proposal (2026-08-25), not implemented. Every number below is
> measured live against crash-stats SuperSearch, Buildhub, hg.mozilla.org
> (json-pushes / json-rev / raw-rev, always through `crashclouseau.net` or
> `libmozdata` for the allowlisted UA), BMO REST and `heroku config`, or read out of
> the tree with file:line. Produced by a 7-agent recon fan-out (ingestion/selection,
> agent pipeline, filing, cadence+population, regression window, filing funnel +
> prior art, tests/config/deploy) whose contradictions were then resolved against the
> tree and against each other — **six claims were corrected and one prod fact
> falsified two whole reports' premises.** §7 resolves every disagreement
> between the reports and says which instrument won; §8 is the kill list, so
> nothing here gets re-litigated.
>
> **The premise in the request holds, and the shape of the problem is not the one it
> suggests.** Beta is not a volume problem: at today's thresholds it is 1.3-3.4% of
> nightly's selection volume and would file roughly **one bug every one to four
> months**. The work is not tuning. It is that the ingestion half is *structurally
> switched off for part of every cycle*, that the LLM half reads the right repo and
> gets the wrong answer from it, and that the knob which looks like "never comment on
> an existing bug" skips the filing instead of filing a new one.
>
> **One prod fact to read before anything else.** `heroku config -a
> crash-clouseau-augmented` (re-verified 2026-08-25) has `OFFSTACK_ENABLED=1` and
> `OFFSTACK_OBSERVE_ONLY=0`, and `config.py:556,561` are `_env_bool` overrides. So
> **off-stack seeding is LIVE and action-emitting in production**, whatever
> `config/global.json` says. Two of the seven recon reports built their beta
> reasoning on the JSON value (`enabled: false`) and both are wrong where they did.
> Prod also has `INGEST_CHANNELS=nightly`, `AUTOFILE_BUGS=1`,
> `SECOND_OPINION_ENABLED=1`, `SHOW_ABSTAIN=1`.

---

## 1. Summary — the five things that decide this plan

**1. Beta selection is dead for two days per cycle, at the merge.**
`Build.get_last_versions` (`models.py:569-601`) applies `.limit(n)` *before* its
major-version break, so the day after a central→beta merge the three newest rows are
`155.0b1 / 154.0b10 / 154.0b9`, the break fires on row two, `len(res) >= 2` fails and
it returns `[]`. `get_builds` then returns `bids=[], search_date=""`;
`get_new_signatures` logs one warning and returns `({}, [])`, writing **no `Stats`, no
`uuids` and — because `Selection.record_many([])` returns early — no `Selection` rows
either.** The one table built to answer "why did you do nothing" is silent too, and the
only trace is one warning per 20-minute tick against a ~2h log window. Measured **12 of
127 run-days (9.4%) over 5 merges, 2/2/2/2/4 days each**, replayed over the Buildhub
build list with no Socorro needed. It falls exactly on the days a freshly uplifted
regression first reaches beta users.

**2. Every cycle ships two builds tagged `N.0b1`, and the first one has never had a
user.** Lifetime totals, all channels, no date bound: **8/4, 13/6, 7/7, 17/4, 1/1**
reports/installs for the merge-day builds of v151-v155, against **435-9,124 reports and
268-5,084 installs for all 54 other builds** since 2026-04-01 (median 2,850/1,974). Five
of five below 20, fifty-four of fifty-four above 435 — a 25x gap with no overlap. Sitting
in the 3-build window it is a **zero baseline**, so `is_spike`'s from-zero branch
(`utils.py:216-218`, which `floor` and `ratio` do *not* gate) fires on every signature
clearing the 6-install threshold: **4 of 30 replayed run-days carry 108 of 179 selections
(60%) and 104 of 160 from-zero fires (65%)**, and the top of the burst is boilerplate the
pipeline cannot act on (`OOM | small` 236, `OOM | unknown |
js::AutoEnterOOMUnsafeRegion::crash_impl` 125, `shutdownhang | RtlWaitOnAddress`,
`AsyncShutdownTimeout | profile-before-change`). Removing it takes distinct pairs from
105 to 40 per 30 days, max/run from 38 to 8, and spend from $16-68 to $4-17/day.

**These are two independent defects and one fix does not solve the other.** The
counterfactual was replayed: with the no-user build removed, the blackout *shifts to the
shipped b1 and shortens to 2 days each* (10 of 127 run-days). Fix both.

**3. Three windows read the `builds` table and the merge-day build row bounds two of
them.** This is the single most important thing in this document and no individual recon
report could see it, because it only appears when you put `Build.put_data`'s
`if rev in revs_c` guard (`models.py:523`) next to the three readers:

| reader | file:line | bounds | on the shipped b1, WITH the merge-day row | WITHOUT it |
|---|---|---|---|---|
| `get_last_versions(n=3)` | `datacollector.py:45` | spike SELECTION | 3 build-days, one a zero baseline | correct |
| `get_pushdate_before` | `update.py:60` | ON-STACK candidate `mindate` | `mindate` = merge pushdate + 1s → the merge's 5,130 changesets fall exactly 1 second below the boundary; window = the **46-122 uplifts** since the last build | window spans the merge: **5,144-6,952 changesets, 1,932-2,678 candidate-bearing** |
| `get_two_last` | `orchestrator.py:307` | OFF-STACK window (a LIVE `json-pushes?full=1`) | `[merge-day rev → b1 rev]` = the uplifts | **5,192 changesets, 7.5-16.2 MB, 3.4-4.1 s**, truncated to 150 with a meaningless recency tiebreak |

`Build.put_data` inserts a `builds` row only when a `nodes` row for that revision
already exists on that channel, and the merge-day build's revision (`22761955d964`,
"Update configs after merge day operations") **is a member of the merge push** (pushid
27990, pushdate 2026-08-13 14:15:59, which is also literally the buildid
`20260813141559` — the identity holds 3 of 4 measured cycles, and the push-timestamp ==
buildid identity holds 4 of 4). So *dropping the merge push at ingestion silently
deletes that row and widens both windows.* Implement item 8 in the **selector**, never
by refusing to write the build; implement item 30 by emitting merge-push members with
`files: []`, never by dropping them.

**4. The LLM half selects the right repo and gets the wrong answer.** `channel` is
threaded correctly through 36 of 37 `channel="nightly"` defaults (each traced to its
caller), libmozdata resolves it to `releases/mozilla-beta`, and hg-edge and json-rev both
serve it. The exception is `SearchfoxCtx`, which has no channel field at all. The beta
risk is that `releases/mozilla-beta` answers a *different question*: a merge push is one
push carrying a whole cycle, so **every landing date in the pipeline is the merge date** —
measured at a **23.2-day forward shift, identical across 4 sampled members of push
27990** (beta 2026-08-13T14:15:59 / central 2026-07-21T09:46:48), and **6.9 to 34.8 days**
across 6 sampled members of push 27533. Five consumers read that number, including the
`landed=` line in the prompt and `_bug_for_this_regression`'s 30-day venue window. On top
of that, `roles._COMPILED_OUT` asserts build flags to the skeptic that are the **exact
inverse** of beta's truth (read from beta's own `moz.configure`), and three population
constants presented as "the crash population" are nightly fits.

**5. The knob that looks like the requirement cannot express it.**
`agent.autofile.comment_on_existing = false` **skips** the filing —
`bugzilla_apply.py:951` returns `{"filed": False, "skipped": "open bug N exists"}` — and
two tests pin that meaning by name. `skip` satisfies the letter of "never comment on an
existing bug" but is *stricter* than "file only new bugs": it also forbids filing past an
older bug, which is **58-59% of beta signatures** (two independent instruments: 58/98 =
59.2% Wilson CI 49.3-68.4% of the top 100 beta signatures by install cardinality; 45/77 =
58% of the emulated selections; 43/67 = 64% at the selection level) against a **23%
nightly control**. And `autofile_bug` has no channel gate of its own: the only thing
keeping filing nightly-only is `get_agent_channels()` inside `enqueue_agent`, which
`retrigger_agent` bypasses with `force=True` by design. **So the day `INGEST_CHANNELS`
gains `beta` — a Heroku config var, no deploy — one retrigger click on tasks.html files a
beta bug under the nightly rules, i.e. a comment on somebody's existing bug.**

---

## 2. The measured picture of beta

### 2.1 Cadence (all Buildhub, `target.channel=beta` + `source.product=firefox` + the repo's own `N.0bM` regexp, `build.id >= 20260401`)

* **59 builds over 145.0 days.** Consecutive gaps (n=58): median **2.00 d**, mean 2.50,
  p25 2.00, p75 3.00, min 1.26, max 5.29. Histogram in 0.5 d bins: 2.0 d ×35, 3.0 d ×16,
  1.5 d ×2, 4.0 d ×1, 5.0 d ×2, 5.5 d ×2 — a Mon/Wed/Fri rhythm, with the 4-5.5 d gaps
  sitting exactly on the merge boundary. 3.2-3.5 builds/week in every cycle.
* **~10-11 shipped builds per cycle over its first ~21-23 days, then a 5-7 day RC phase
  with no `N.0bM` build at all** (v151's builds span 22.8 d of a 28 d cycle).
* **Merge-to-merge is still 4 weeks.** First build tagged `N.0b1`: v151 20260420143330
  (Mon), v152 20260518152802 (Mon), v153 20260615115325 (Mon), v154 20260720195614 (Mon),
  v155 20260813141559 (Thu) → **28.04, 27.85, 35.34, 23.76 days** (mean 28.7, median
  28.0). Using the SHIPPED b1 instead: 28, 28, 35, 26. **No 2-week cadence appears
  anywhere in 2026-04-01..08-24** — every 2-week figure in this plan is arithmetic on the
  observed 3.2-3.5 builds/week, not an observation.
* **A build stays testable 3.9-5.0 days** (from shipping until it falls to window index
  0) and holds 77-96% of its eventual crashes by then: 154.0b6 4.03 d / 77.1%, 155.0b1
  3.86 d / 90.2%, 155.0b2 5.0 d / 96.2%. On its own ship day a build holds only
  **0.2-2.7%** (154.0b10 1.1%, 154.0b6 2.7%, 155.0b1 0.2%, 155.0b2 2.5%).

### 2.2 Population

| | beta+aurora | nightly | ratio |
|---|---|---|---|
| reports / 30 d | 44,488 (beta 28,213 = 63.4%, **aurora 16,275 = 36.6%**) | 28,762 | 1.55x |
| crashing installations / day (median, per-day distinct `install_time`) | 1,158 (min 666, max 1,514) | 462 | 2.51x |
| reports per BUILD (lifetime, shipped builds only) | median 2,674 | median 315 | **8.49x** |
| installs per BUILD (lifetime, shipped builds only) | median 2,000 | median 166.5 | **12.01x** |
| `cpu_arch=x86` share of reports (30 d) | **41.9%** (19,161/45,700) | 1.6% (463/29,322) | 26x |
| Windows share | 83.5% | 64.0% | — |
| `OOM \| *` share of reports | **41.75%** (19,089/45,726) | 18.03% | 2.3x |

**The selector's axis is the build-day, so the 8.49x per-build figure is the one that
prices it, not the 1.55x report figure.** Per-(signature, buildid) distributions over
official builds *built inside* the 30-day window (beta 13 builds / 5,527 pairs; nightly 62
builds / 6,915 pairs): the count body is near-identical (both p50 1, p75 2; beta p90 7 /
p95 16 / p99 67 / max 941 vs nightly 5 / 9 / 25 / 210) and **the INSTALL distribution is
where the channels really differ** (beta p50/p75/p90/p95/p99 = 1/2/5/14/58, max 583;
nightly 1/1/3/5/11, max 33; installs ≥20 on 4.00% of beta pairs vs 0.14% of nightly's).

**Three population traps that fired during this recon and will fire again.**
(a) `aurora` IS beta (Developer Edition) and is 36-41% of the channel — three independent
measurements agree (36.6% over 30 d; 38.5% over 08-18..25; 41% over 364 d in
`sigage.hardware_noise`'s own docstring). (b) Report *counts* are not volume: use per-day
`_cardinality.install_time`. (c) The 32-bit x86 share is the reason `OOM | large`
escalates at merge nearly every cycle — **64.6% of beta's OOM is x86 against 3.7% of
nightly's** — and it is a population fact with no regressor to name.

### 2.3 The funnel, end to end (deployed-cadence replay, 29-30 run-days, 2026-07-26..08-24)

Two independent emulators of `datacollector.get_new_signatures` agree on the beta
volume and the nightly control reproduces the recorded prod baseline of 85-120
dossiers/day, which is what calibrates the whole chain.

| stage | beta today | beta with item 8 | nightly control |
|---|---|---|---|
| new distinct (signature, buildid) pairs / run-day | 3.5 (105/30, incl. cold start) — **2.31 steady state** (67/29, after dropping day 1's 37) | **1.33** (40/30) | **102.6-144.6** (two instruments) |
| protos per selected pair | mean 8.4 | median **3.0**, p90 14, max 22, sum 229/40 | mean **1.07**, median 1, max 6 (n=80) |
| ingested UUIDs / day | 29.4 | **7.6** (4.9% of nightly) | 155 |
| dossiers / day (×0.55-0.77 yield) | 16-23 | **4.2-5.8** | 85-120 (recorded) |
| $ / day (×$1-3/dossier) | $16-68 | **$4-17** | — |
| file-eligible (no open same-app bug) | 36-42% of selections | same | 77% |
| filings per eligible selection | (imported) | (imported) | **3.63%** (60 bugs/21 d ÷ 102.6/day ÷ 0.77) |
| population adjustment (stale-pass 12% vs 29%, hw-pass 83% vs 86%) | ×0.388 | ×0.388 | 1.0 |
| **filed bugs / day** | 0.014-0.035 | **0.008-0.020** | 2.86 |
| **one bug every…** | 29-74 days | **50-125 days** | 8 hours |

At `file_new` instead of `skip` the eligible fraction goes 36-42% → ~100%, so ×2.4:
**0.019-0.048 bugs/day, one every 21-53 days.** Inverting: 0.25 bugs/day needs 43 beta
selections/run-day (42% of nightly's volume, ~43 extra LLM runs/day); 1.0/day needs 171
(166% of nightly's).

**Read the funnel as an order-of-magnitude bound, not a forecast.** Nobody ran the LLM
pipeline on a beta crash. The 3.63% filing rate is imported from nightly, and three
things could move it hard, in different directions: the ordinary beta candidate window is
**13x tighter** than nightly's (61 vs ~790 changesets, 21 vs 266 candidate-bearing) which
should raise precision; **off-stack is live in prod**, which two recon reports did not
know, so more beta crashes reach the model than an on-stack-only estimate assumes; and
the UUID→dossier yield of 0.55-0.77 is a single-window calibration (nightly's measured 155
UUIDs/day divided into the *remembered* 85-120 dossiers/day, not re-measured) with
`$1-3/dossier` likewise a nightly figure.

### 2.4 Novelty — the honest headline

Of the 77 signatures behind the emulated beta selections, with `sigage.signature_history`
on beta and nightly plus `first_seen_ever` (`SignatureFirstDate`, which answered **77/77**):

* **new on beta AND new/absent on nightly: 9/77 = 12%**
* **new on beta but long-lived on nightly: 3/77 = 4%** — the class the merge-window
  regression story is about (`nsBlockFrame::RemoveFrame` 0 d beta / 14 d nightly;
  `mozilla::net::DocumentLoadListener::Open` 0/766; `logging::CheckLogMessage::~CheckLogMessage` 0/31)
* **long-lived on beta: 65/77 = 84%** — beta first-seen median **393 d** (p25 113, p75
  724, max 862); TRUE unbounded first-seen median **1,194 d** (p75 2,727, max 6,033), only
  6 of 76 ≤ 7 d

So beta's crash population is overwhelmingly *old signatures at high volume*, not new
regressions. The stale-signature gate fires on **68/77 = 88%** of beta selections (nightly
control 85/120 = 71%) and, because `_STALE_SIGNATURE_CLAMP` maps `probable(70) →
medium(50)` which is *below* `autofile.min_confidence` 70, on beta it is a **filing
blocker rather than a downweight**. That is where ~61% of the projected beta filings are
lost — **and it is mostly correct** (see §7 contradiction 4 and kill #11).

### 2.5 The candidate window — two different animals

| | changesets | non-merge with an interesting file | distinct interesting files | on-stack candidate set per real crash stack |
|---|---|---|---|---|
| beta, between consecutive in-cycle builds (n=47) | median **61**, p90 80, min 16, max 116 | median **21**, p90 34, max 42 | median 43, max 111 | median **0**, max 4; zero on 299/333 stack×window pairs (90%) and 60/71 mid-cycle stacks (85%) |
| beta, spanning the merge (n=4) | median **6,335**, min 5,144, max 6,952 | 1,932 / 2,421 / 2,430 / 2,678 | 5,041-10,981 | median **28.5**, max 69; zero on 3/36 |
| nightly, its real 3-day production window (n=35) | median **672**, p90 1,058 | median **266**, p90 401 | median 875 | median **3**, max 13; zero on 10/35 (29%) |

The in-cycle beta window is **genuinely high-signal**: classifying all 2,875 in-cycle
non-merge changesets on the first line of the description, **89.7% of the 1,009
candidate-bearing ones are approved bug-fix uplifts (`a=<release manager>`) and 3.5% are
backouts = 93.2% plausible crash regressors**, median 1 interesting file each; 6.3% is
generated-data-header noise (see item 26) and test-path touches are 67 of 2,407 = 2.8%.

The merge window is *not* a reasoning blocker either — file intersection alone narrows
5,000+ changesets to median 28.5 candidates and line-proximity scoring narrows that to
median 3.5 scoring >0, median 3.0 scoring ≥5, median 1.0 at the max score of 10 (n=6
crashes, 330 patches fetched and scored with the production rule, 0 fetch failures). It is
simply **unreachable today** for three independent measured reasons, so nothing in this
plan depends on it: the merge window belongs only to the merge-day build (item 9), that
build ships to 0-7 installs against an install threshold of 6, and its build-day is at
index 0 of the evaluated series in 4/4 cycles so `get_spike_indices` never tests it.

### 2.6 What is DEAD or INERT on beta

* **`get_maturity_bar` → `(None, 1)`** off nightly, so the floor half can never fire and
  `needed = max(threshold, mature_installs)` degenerates to `threshold` = 6. Correct here
  — nightly's bar prices a 21-DAY window while beta's is 3 BUILDS = 4-7 days, almost
  exactly beta's arrival interval (§2.1). See kill #16.
* **Bit-flip triggers (1) and (2)** require `singleton = reports <= max_reports (1)`
  (`orchestrator.py:2524-2540`), and a beta signature almost never has exactly one report
  in a year (min 6, median 445, n=77). Two of the three gates written for bug 2061961 do
  not exist on beta. Direction is toward reporting, so nothing is lost silently.
* **`signature_age.other_channel_floor = 20`**: 87% of beta selections clear it with a
  **median of 2,892** off-beta reports (p75 23,908, max 1,041,241) against nightly's 52%
  and median 33, so no count-based value can discriminate and the gate always uses the
  all-channel clock.
* **`machine._install_history` and the whole bad-machine gate** are off for the 36-41% of
  beta that is DevEdition until item 4 lands (the query filters `release_channel=beta`
  while every report is `aurora`, so `machine` returns its all-None `empty` dict).
* **`_split_by_application`** rescues 0 of 58 and 0 of 45 beta venue decisions.
* **`agent.offstack.max_candidates = 150`** is 3.6x above the measured in-cycle maximum
  of 42 and never binds in-cycle.
* **`min_signature_reports = 5`** is inert on beta (0 of 77 below it) where it mutes the
  hardware gate on 69 of 120 = 58% of nightly selections (median 3 reports).
* **`spike.floor` and `spike.ratio` reach only 22% of beta selections** — 78-89% come
  through `is_spike`'s from-zero branch, which neither knob gates. **`installs` is the
  whole gate.**
* Inverted from nightly: **`thresholds.protos` binds on beta** (median 3.0, p90 14, max
  22 protos per pair) where nightly's cap of 50 has never bound (mean 1.07).

---

## 3. Work items

Ordering principles, inherited from plan #16: **the irreversible step ships alone**; the
gates exist before the data that would arm them; nothing costs money until a human has
looked at free output; and the fixes whose value is *desktop* land first so the
investigation pays for itself even if beta is abandoned.

Each item is independently landable in the order given. Tags: **[C]** correctness
(silently wrong on beta, or on nightly, today) · **[N]** new behaviour · **[T]** tuning ·
**CANARY** = required before the phase named in §9 · **FOLLOW** = can land after.

### Group A — correctness, value is desktop, ships regardless of beta

**1. [C] CANARY(0) — `patch.py`: fetch `raw-rev` through `crashclouseau.net`, and refuse
to mark an empty parse analysed.**
Files: `crashclouseau/patch.py` (the whole `parse`), `crashclouseau/models.py:404`
(`Changeset.add_analyzis`).
`parsepatch.Patch.parse_changeset` does a bare `requests.get(url, stream=True)` with **no
User-Agent, no timeout and no `raise_for_status`** — the only HTTP client in the tree off
the allowlisted UA. Verified on one URL, three variants:
`releases/mozilla-beta/raw-rev/0535872fe489` → bare requests **406 / 0 bytes**; explicit
`python-requests/2.32` UA **406 / 0 bytes**; `crashclouseau.net.get` **200 / 8,881
bytes**. A 406 yields `{}`, and `add_analyzis({}, nodeid, channel)` **still sets
`analyzed=True`** with no added/deleted/touched lines — so that candidate scores 0
forever, unrecoverable without `Changeset.reset`, and nothing logs an error. Reproduced
live: 12 serial parses succeeded, a 189-fetch burst tripped hg's throttle, then 100%
returned `{}` while `net.get` stayed at 200 — and 330 patches then parsed cleanly through
`net.get` with 0 failures. **This is live on nightly today.** Fix: fetch with `net.get`,
raise on non-200, hand `r.text` to `Patch.parse_patch(file_filter=…,
skip_comments=True)`; and make `add_analyzis` leave `analyzed=False` on an empty parse.
Beta multiplies the trigger (~+12% in-cycle volume, and any merge that slips item 30
dumps 1,932-2,678 calls at once).
*Caveat to keep in the diff: "a merge burst will trip the throttle" is inference from two
observations, not a measured per-IP/per-UA budget. The UA discrimination is deterministic.*

**2. [C] CANARY(0) — `UUID.to_analyze` must exclude `error` rows and have a deterministic
order.**
Files: `crashclouseau/models.py:1466-1479`; alternatively `models.py:1288-1294`
(`set_error`).
`set_error` sets only `error=True`; `to_analyze` filters `UUID.analyzed.is_(False)` with
no `error` predicate and no `ORDER BY`, so one persistently-failing report is handed back
to the single serial analysis chain forever and livelocks it — **on any channel**. Adding
beta doubles the traffic on that chain and makes an arbitrary interleave with nightly
possible. Add `UUID.error.is_(False)` plus `ORDER BY UUID.id`.

**3. [C] CANARY(0) — `Build.get_pushdate_before` must handle "no earlier build".**
Files: `crashclouseau/models.py:604-617`, `crashclouseau/update.py:55-61`.
`qs = … .first()` is `None` when there is no earlier build row, and line 617 is
`return qs.pushdate` → `AttributeError: 'NoneType' object has no attribute 'pushdate'`,
proven by executing the real method with a session whose `.first()` returns `None`. **Zero
test coverage** (`grep -rn 'get_pushdate_before' tests/` → 0 hits). Unreachable through
the selector (a picked build-day is always at index ≥1, so an older build row exists by
construction) but live via `redo.reset()` on a UUID whose older beta build rows were
cascade-deleted (`Node.clean` prunes at `max_ndays` = 30 and `builds.nodeid` is
`ON DELETE CASCADE`; on beta a whole merge push's 5,130 nodes age out in one instant) and
via any un-analysed backlog that outlives that pruning. Nightly cannot hit it: its
`mindate` is arithmetic. Fix: return `None`, and in `put_report` fall back to the nightly
rule (`buildid - config.get_ndays()`) with a warning rather than raising.

**4. [C] CANARY(0) — route every Socorro query keyed on OUR channel label through
`utils.get_search_channel`.**
Six one-line sites: `crashclouseau/machine.py:81`, `crashclouseau/population.py:191`,
`crashclouseau/agent/tools/socorro.py:142`, `crashclouseau/datacollector.py:439`,
`crashclouseau/report_bug.py:136`, `crashclouseau/report_bug.py:465`.
`utils.get_search_channel` (`utils.py:35-37`) maps `beta → ["beta","aurora"]` and is
applied at only 6 of 13 `release_channel` sites today (grep-verified: `datacollector`
×4 at :167/:273/:331/:386, `sigage` ×2 at :172/:541). Socorro files Developer Edition
under `aurora`, which is **36-41% of the beta population** by three independent
measurements. Per-site cost:
* `report_bug.py:465` (`fetch_signature_stats`) — **every filed beta bug's crash count is
  low.** On build 20260819090452 (155.0b2), the top 12 beta signatures by distinct
  install_time: raw-`beta` 1,600 crashes vs `["beta","aurora"]` 2,149 = **25.5% loss
  overall, 0.0% to 72.4% per signature** (e.g. `OOM | unknown |
  js::AutoEnterOOMUnsafeRegion::crash_imp` 40/33 → 145/97; `shutdownhang |
  RtlWaitOnAddress | WaitOnAddress` 35/32 → 81/75). `report_bug.py:136` is the hand-draft
  twin and must move with it or the two comments quote different numbers.
* `machine.py:81` — a DevEdition crash's install history queries `release_channel=beta`
  while every one of its reports is `aurora`, so it returns no hits, `machine` returns its
  all-None `empty` dict, and **the bad-machine gate cannot fire at all.** Also pass the
  crash's DB channel (`"beta"`), not the processed crash's `release_channel` (`"aurora"`) —
  an installation is one channel, so widening costs nothing.
* `agent/tools/socorro.py:142` — the only crash-stats instrument the blind second opinion
  has under-counts beta and prints a truncated channel facet and total.
* `population.py:191` — the `crashstack.html` population panel.
* `datacollector.py:439` — `get_changeset`'s fallback.
While there: `report_bug.build_stats_sentence` (`report_bug.py:275-282`) prints the bare
version (`155.0b2`), which after this fix covers beta AND DevEdition — say
"beta/DevEdition 155.0b2" or the sentence claims a narrower population than the number
beside it. **There is no test anywhere for `get_search_channel`** (item T1 in §6).

**5. [C] CANARY(0) — `_PROVENANCE` must name the crash's channel.**
File: `crashclouseau/report_bug.py:866-868` — the literal "which analyses **nightly**
crashes with an LLM". False on a beta filing, in the one paragraph whose job is to tell
the reader what wrote the analysis and how to discount it. `report_bug._crash_where`
(:275-282) and `_bug_version` (:1717-1724) already branch on channel; this line was
missed. `grep "analyses nightly crashes\|_PROVENANCE" tests/` → 0 hits.

### Group B — correctness on beta: the three windows and the selector

**6. [C] CANARY(2) — `update_builds`' Buildhub lookback must cover the retention window,
not `get_ndays()`.**
Files: `crashclouseau/update.py:153-164`; new top-level config `buildhub_lookback_ndays`
(default = `max_ndays` = 30) in `config/global.json` + `config.py`.
`update()` runs `put_filelog(channel)` first, which sets `LastDate.maxdate = now`;
`update_builds` reads it back and subtracts `config.get_ndays()` = **3 days**, so
`buildhub.get()` only offers 3 days of builds. Beta ships every ~2.00 days (median of 58
gaps), so over the 196 days of Buildhub history a rolling 3-day window holds **0 builds on
23 days (12%) and 1 build on 117 days (60%) — 71% of possible switch-on moments give
`get_last_versions` fewer than 2 rows and therefore `[]`**, and the table only grows one
build at a time so it takes ~5 days to reach a 3-deep window. `create.py` is fine (it
passes `start_date + 1 day` ≈ 29 days); **the hazard is specific to flipping
`INGEST_CHANNELS` on a live DB, which is the documented canary mechanism.**
`Build.put_data` is `on_conflict_do_nothing` (`models.py:532`), so a 30-day fetch is
idempotent on nightly; the cost is one larger Buildhub POST payload per tick. Without this
item the canary is silently dead 71% of the time.
Related foot-gun in the same area: `update_all`'s `os.getenv("INGEST_CHANNELS",
"").split() or config.get_channels()` means **clearing the variable to "turn beta on" also
turns RELEASE on.** Always set it explicitly.

**7. [C] CANARY(2) — `Build.get_last_versions` must not return `[]` at a major-version
rollover.**
File: `crashclouseau/models.py:569-601`.
Mechanism and blast radius in §1.1. Measured by three instruments that agree: **12 of 127
run-days (9.4%) over 5 merges, 2/2/2/2/4 days each** (replayed over the Buildhub build
list, no Socorro needed); **1.36-4.01 days per cycle, mean 2.05, median 1.83** across
cycles 149-155 (continuous time, from the first build of a new major to the second);
**10 blind run-days over 4 merges** by a third emulator. 9.4% of beta run-days today;
**14-18% at a 2-week cadence** (2 days / 14 = 14.3%; 2.5 average run-days / 14 = 18%).
Fix: drop the major-version break for non-nightly channels and take the newest `n` build
rows; **`len(res) >= 2` must not be able to turn ingestion off silently** — if the window
is short, run with what there is and record it. A replay of the whole selector with the
break disabled produces a working window on 2026-08-14..08-17.
*What the break looked like it was protecting, and is not:*
`buildhub.VERSION_PATS["beta"]` (`buildhub.py:21-25`, `[0-9]+".0b"[0-9]+`) already removes
the 26-30 RC/dot-release builds that Buildhub also carries on `target.channel=beta`
(154.0, 154.0.1, 153.0.4, 150.0.2 …), which interleave with the betas
(20260727110312/124451 = 153.0.1 sit between 154.0b3 and 154.0b4) and report
`release_channel=release` in Socorro, **never beta/aurora** (20260812182057 = 154.0:
72,019 release / 0 beta; 20260715202819 = 153.0: 105,097 release / 0 beta). They never
reach `builds`, so the break is not protecting against them. **Do not "fix" the version
regexp** — kill #3. Add the reason to its comment, because it reads like a cosmetic
version filter and it is actually protecting the window.

**8. [C] CANARY(2) — the selection window must drop a build-day with no users.**
Files: `crashclouseau/datacollector.py:115-200` (`get_new_signatures`),
`crashclouseau/utils.py:290-341` (`evaluate_days` — a new outcome for the log),
`crashclouseau/models.py` (`Selection`'s outcome vocabulary).
Mechanism and measured cost in §1.2. **Not a fitted threshold:** 5 of 5 merge-day builds
below 20 lifetime reports (8, 13, 7, 17, 1) and 54 of 54 other builds above 435, a 25x
gap with no overlap, so **any cut in [20, 400] behaves identically**. Two other recon
instruments measured the same object independently over cycles 149-155 (17/12, 9/7, 11/5,
13/6, 7/7, 17/4, 1/1 reports/installs) and over 4 cycles (13/6, 7/7, 17/4, 0/0).
*How to implement, and the two traps:*
* `get_new_signatures` already fetches a per-(signature, `build_id`) facet for every bid,
  so the per-buildid TOTAL is **free** — sum `numbers[day]["bids"][bid]` across signatures
  before the `evaluate_days` loop. Use report COUNT, not summed install cardinality
  (summing per-signature cardinalities double-counts a machine that crashes on several
  signatures). Suggested floor **100**, with the [20, 400] equivalence stated in the code
  comment.
* Apply it **only to a build-day that is not the newest in the window**, because a build
  on its own ship day holds 0.2-2.7% of its eventual crashes while a 2-4-day-old build
  holds 67-96%. The merge-day build only ever *hurts* as a baseline, i.e. once it is no
  longer newest.
* **Never implement this by refusing to write the build row** (`update.update_builds`) or
  by dropping the merge push at ingestion — item 9 and kill #2.
* Record the drop in the `Selection` log (a new outcome, e.g. `dropped_no_users`) so it
  is never silent. The whole point of that table is that a declined build-day leaves a
  trace.

**9. [C] CANARY(2) — pin the three windows with a test; never remove the merge-day
`builds` row.**
File: `tests/test_beta_windows.py` (new). Assertions only, no production change.
Table and mechanism in §1.3. Supporting measurements: push 27990 = 5,130 changesets at a
single pushdate `2026-08-13 14:15:59`, of which **only 20 are `merge`-flagged** so 5,110
pass `Changeset.find`'s `Node.merge.is_(False)` (`models.py:451`); the merge-day build's
revision `22761955d964` is a member of that push, so
`get_pushdate_before(20260817142839)` = 14:15:59 and `mindate` = 14:16:00, one second
above every merge changeset. The identity holds 4/4 cycles (the merge arrives as ONE push
of 5,130 / 6,185 / 6,413 / 6,917 changesets, and the first-b1 build revision is inside it
in 3 of 4, its push timestamp equalling the buildid exactly). Per-build candidate windows
on beta: 155.0b1 **47**, 155.0b2 46, 155.0b3 76, 155.0b4 61, 154.0b7 75, 154.0b9 122,
154.0b10 61 — versus **5,144** for the merge-day build. **The exclusion is accidental and
nothing in the tree records it.** Pin it.

**10. [C] CANARY(2) — add the channel to the proto-signature cluster key.**
Files: `crashclouseau/models.py:1142-1167` (`_cluster_dossiers` — join `Build`, filter
`Build.channel`), `:1326-1372` (`proto_already_analyzed`), `:1393-1440` (`untriaged`,
which already takes `channels` but must correlate the sibling's channel too).
`_cluster_dossiers` filters `sib.signatureid` and `sib.protohash` only — `uuids` has no
channel column and the query never joins `Build` — and `protohash =
utils.hash(proto_signature)` (`models.py:1298`) is the same string on any channel. So
whichever channel lands first closes the cluster for the other, **forever**. The dangerous
direction is **beta closing a nightly cluster**: same shape as plan #16 §6.2's Fenix note,
and it means enabling beta can silently regress desktop coverage.
**Measured blast radius: 16.5%, not "most".** Of the 224 beta (signature, proto) clusters
behind the 40 selected pairs, only **37 (16.5%)** also occur verbatim on nightly over 60
days; 187 (6.2/day) are beta-only. And the real gate additionally requires the nightly
cluster to hold a `status=done` dossier, so 16.5% is an upper bound (kill #12). Decide
explicitly: a nightly dossier must NOT satisfy a beta crash, because the beta filing is a
different bug on a different repo from a different build. `models._INSTANCE_SUPPRESSED`
reasoning is unaffected — this is a scoping bug, not a suppression policy. `models.py`
already anticipates the interaction at :1415-1420 ("with beta ingestion on, an unfiltered
`limit` of 3 could return three beta crashes, which `enqueue_agent` drops on the floor").

**11. [C] FOLLOW — `Changeset.get_scores` and `Changeset.reset` must filter on channel.**
Files: `crashclouseau/models.py:463-467`, `:329-332`; thread the channel from
`put_frames`/`put_report`, which already has it.
`find` (`models.py:438-453`) IS channel-filtered; `get_scores` is not (it filters
`Node.node.in_(chgsets)`, `File.name ==`, `Changeset.analyzed.is_(True)` and nothing else)
and `Score.set` inserts without dedup, so a hash present under both channels yields two
Score rows per frame and `CrashStack.get_by_uuid` renders the same changeset twice with
two different push dates. **Latent today for two unrelated reasons**, both measured: a
beta uplift is an hg graft with a NEW hash — of 40 sampled changesets pushed to
mozilla-beta after the 08-13 merge only 7 also exist on mozilla-central, and all 7 are
`.hgtags` "Tagging …" commits with no interesting file and therefore no `Changeset` row;
and the complement, **0 of 1,009 candidate-bearing in-cycle changesets exist on m-c under
the same hash** while **1,932 of 1,932 merge-window ones do** (5,116 of 5,124 non-merge =
99.8% overall, with both repos serving byte-identical `raw-rev`). The mass collision is
therefore the merge push, which item 9's `mindate` boundary excludes. It stops being
latent the moment that boundary is disturbed or item 30 lands.

### Group C — correctness on beta: the LLM half

**12. [C] CANARY(4) — `sigage.pushdate_for_node` must be able to answer "when did this
code first exist" on central.**
Files: `crashclouseau/sigage.py:605-648` (`pushdate_for_node`, `json_rev`),
`crashclouseau/agent/orchestrator.py:1834-1838` (`candidate_pushdates`),
`crashclouseau/bugzilla_apply.py:505-540` (`_candidate_landed`, called at :957).
Every landing date in the pipeline comes from the push record in the crash's OWN repo
(`pushlog.collect` stamps `pushdate = push["date"]` on every changeset,
`pushlog.py:36-37`; `pushdate_for_node` reads `json-rev/<node>` on
`Mercurial.get_repo_url(channel)`). A merge push is one push containing a whole cycle.
**Measured: 4 sampled non-merge members of push 27990 (`f44045181a24`, `7f4d7e8c27d6`,
`2c98bfc534ef`, `02297ff55cd2`) all report beta pushdate `2026-08-13T14:15:59` and
central pushdate `2026-07-21T09:46:48` — a 23.2-day forward shift, identical across the
push. Six sampled members of push 27533 (6,917 changesets, all at
`2026-07-20T17:14:53`) give central pushdates of 06-15, 06-19, 06-25, 07-01, 07-07,
07-13 — drift 6.9 to 34.8 days.** Together: 0 to ~35 days, a function of where in the
cycle the changeset landed.
Five silent consumers: (a) `candidate_pushdates` in the seed; (b) `triage._user_prompt`'s
`landed=<date>` line (`triage.py:1242`); (c) `_apply_signature_age_gate`; (d)
`bugzilla_apply._candidate_landed` → `_bug_for_this_regression`'s
`comment_max_bug_age_days` = 30 window, which with a clock up to 35 days late **errs
toward accepting an OLD bug as this crash's venue — the exact direction beta's requirement
forbids** (latent today because the `comment_on_existing` skip at :951 fires first, live
the moment item 21's `file_new` lands); (e) `_offstack_candidates`' recency tiebreak.
Fix: give `pushdate_for_node` an explicit origin channel, resolve the node on central
first and fall back to the channel repo when central does not know it — **that fallback is
the genuine uplift case, where the beta pushdate IS the right answer.** Store BOTH on
`Node`/`candidate_pushdates` so the prompt can say "landed on central `<date>`, reached
beta `<date>`". **Do not switch the whole `json_rev` call to central**:
`backedout_by_for_node` and `same_push_backout_target` genuinely want the channel repo's
answer. Better still, per `build_related_bugs_note`'s own KNOWN GAP paragraph, hand the
filer the pushdate the run already computed instead of re-asking hg.
*What this does NOT change: the stale gate's firing rate. See §7 contradiction 4.*

**13. [C] CANARY(4) — `SearchfoxCtx` must carry the channel.**
Files: `crashclouseau/agent/tools/searchfox_cg.py:39-43` (add `channel`), `:119` (the tool
description), `crashclouseau/agent/triage.py:1385` and
`crashclouseau/agent/second_opinion.py:129` (both construct it),
`crashclouseau/searchfox.py:94,119-150` (`_coerce_repo`; `Repo.BETA = "mozilla-beta"` with
tree `firefox-beta` already exists), `crashclouseau/agent/orchestrator.py:2705-2732`
(`_resolve_struct_layout` → `client.field_layout`),
`crashclouseau/compiled_out.py:545-552` (`_default_off_switch` → `client.search` /
`client.define`), `crashclouseau/html.py:337-346` (`_SF_TREE` — fold into one shared
channel→`Repo` map so there is a single table).
`SearchfoxCtx` is a dataclass with ONE field, `client` — unlike `PatchCtx` / `HistoryCtx`
/ `SourceCtx` / `SocorroCtx`, which all get `channel=channel`. Every `SearchfoxClient`
method takes `repo=None` and `_coerce_repo(None)` falls through to
`agent.searchfox.default_repo` = `"mozilla-central"`. So for a beta crash every
`calls_from` / `calls_to` / `calls_between` / `define` / `search` / `field_layout` reads
firefox-main tip, every emitted permalink points at firefox-main, and
`SearchfoxCitation.repo` honestly records `"mozilla-central"` for code that may never have
existed in the beta build. **Two deterministic gates read the same wrong tree**, and
`_resolve_struct_layout` is fail-closed, so a beta layout differing from central's
silently costs the lead→probable promotion. The tool description actively points the model
the wrong way ("searchfox repo token, e.g. mozilla-central") and nothing in `system.md` or
`roles.py` tells the model its crash is not on central.
Verify searchfox indexes `firefox-beta` at the required freshness before relying on it,
and fall back to central **with a prompt note** rather than silently. Prior art already
measured (plan #16 §7): `firefox-beta` is pinned to its own branch tip
(`cd001e124b15` / 154.0b9 while `firefox-main` was at 155.0a1) — which for a BETA crash is
the *correct* tree, unlike the Fenix-nightly case where it was a cycle behind. `tools/history.py:16-17`
is the right affordance to copy: "`channel` defaults to the crash's channel; pass
`nightly` to query mozilla-central (where regressors usually land first) even for a
beta/release crash."

**14. [C] CANARY(4) — make `compiled_out`'s three deny lists, and the skeptic prompt that
renders them, a function of the crash's channel.**
Files: `crashclouseau/compiled_out.py:47-95` (`CHANNEL_ON_DENY`, `BUILD_TYPE_DENY`,
`GUARD_DENY`), `:80-95` / `:202-212` / `:580` / `:695-706` (`is_build_flag_ground`,
`guard_macros`, `hollow_symbols`), `crashclouseau/agent/roles.py:91-115`
(`_COMPILED_OUT`, rendered into the skeptic prompt).
Read from mozilla-beta's OWN source on 2026-08-25 via `hgedge.raw_file(...,
channel="beta")`: `build/moz.configure/init.configure` — "if we have 'a1' in
GRE_MILESTONE, we're building Nightly (define NIGHTLY_BUILD) - otherwise, we're building
Release/Beta"; `set_define("NIGHTLY_BUILD", milestone.is_nightly)`,
`set_define("RELEASE_OR_BETA", milestone.is_release_or_beta)`, and
`is_early_beta_or_earlier = is_nightly` with the comment "EARLY_BETA_OR_EARLIER is an
alias for NIGHTLY_BUILD, pending its removal"; `moz.configure`:
`set_define("MOZ_DIAGNOSTIC_ASSERT_ENABLED", True, when=moz_debug | milestone.is_nightly
| moz_dev_edition)`.
So on beta **three of the five "ON, never conclude off" macros are OFF** (`NIGHTLY_BUILD`,
`EARLY_BETA_OR_EARLIER`, and `MOZ_DIAGNOSTIC_ASSERT_ENABLED` for non-DevEdition) **and one
of the three "genuinely off" macros is ON** (`RELEASE_OR_BETA`). The prompt currently tells
a beta skeptic to refuse the correct conclusion about `NIGHTLY_BUILD`-guarded code,
asserts that 9-11% of the crashes are `MOZ_DIAGNOSTIC_ASSERT` crashes when on beta they
cannot be, and grants a free "this `RELEASE_OR_BETA` code isn't in the build" veto that is
wrong by construction.
`is_build_flag_ground` **inverts in both directions, so no single global relaxation fixes
it**: a beta note "behind `#ifdef NIGHTLY_BUILD`, not compiled into this build" matches
`CHANNEL_ON_DENY` → returns True → the correct noise-kill is discarded and the lead is
reported; a note "this `#ifdef RELEASE_OR_BETA` code is not in the build" matches
`_BUILD_TYPE_GROUND` → returns False → the claim BINDS and abstains a good lead. And
`guard_macros` subtracts `GUARD_DENY`, so `hollow_symbols` can never detect a symbol whose
whole body is `#ifdef NIGHTLY_BUILD` — **on beta the single most common way a symbol is
genuinely hollow** (and see the `compiled-out-mechanism-not-citation` note: the guarded
thing in 3 of 4 confirmed nightly cases was a hollow symbol).
Key `MOZ_DIAGNOSTIC_ASSERT_ENABLED` on the **raw `release_channel`** (`aurora` =
DevEdition = ON; `beta` = OFF), not on our stored channel label. Keep
`compiled_out.py:63`'s sentence ("Same for RELEASE_OR_BETA, which really is off on
nightly") — it is correct, scoped to nightly. **Measure the new firing rate on beta before
trusting it**: the nightly replay (5 of 1,765 fails, 0 of 216 vetoes changed) says nothing
about beta's distribution, and the v109 audit recorded all five `compiled_out_*`
corroborations firing **0/197** on nightly.

**15. [C] FOLLOW — the crash brief must state TWO signature ages on a non-nightly
channel.**
Files: `crashclouseau/agent/triage.py:625-724` (`_signature_age_lines`,
`_NEW_SIGNATURE_GUIDANCE`, `_OLD_SIGNATURE_GUIDANCE`), `crashclouseau/sigage.py:888`
(`NEW_SIGNATURE_DAYS = 7`).
`signature_first_seen_ever` is Socorro's `SignatureFirstDate` and takes no channel or
product argument (`sigage.py:265`), so for a regressor that landed on central during the
previous cycle the all-time first-seen is the **nightly** debut. By the time the first beta
build ships, the stated age is the cycle offset plus build lag: 7-35 days at 4 weeks, 7-21
at 2 weeks. `_OLD_SIGNATURE_GUIDANCE` therefore fires and tells the model "a changeset
that landed long after the signature was already crashing cannot be what CREATED this
crash, and saying it did is the error a module owner rejected ten of our filings for" —
while the candidate list beside it prints `landed=<merge day>`, i.e. *after* the stated
first-seen. **Both halves of the framing point the model away from the one changeset set
that CAN contain the origin.**
Fix: on a non-nightly channel print both ages and say which is which ("first seen anywhere
in build X, N days ago, on nightly; first seen on beta in build Y, M days ago, this cycle's
merge"), plus a third guidance variant — the crash is new TO THIS CHANNEL, so the
merge/uplift window is the right place to look even though the signature is not new.
**Do not widen `NEW_SIGNATURE_DAYS`**; that would also relabel genuinely old nightly
signatures. Scale of the population this helps: **3 of 77 = 4%** of emulated beta
selections are "new on beta, long-lived on nightly". State that in the diff — this is a
correctness fix for a small, high-value class, not a volume lever. (Good news for beta:
`SignatureFirstDate` answered **77/77** beta signatures, so the
`signature-first-date-blind-to-new-signatures` blind spot — 7.6% of nightly dossiers — is
nearly absent here, because beta's signatures are old.)

**16. [C] FOLLOW — `system.md`'s revision-drift section and the four
`"repo": "mozilla-central"` literals must be rendered from the resolved repo.**
Files: `crashclouseau/agent/prompts/system.md:65-76` and `:135-137`,
`crashclouseau/agent/roles.py:155` and `:217`, `crashclouseau/agent/schema.py:258`
(`StructLayoutCitation.repo` default).
The prompt says "The searchfox tools read ~tip of mozilla-central, which is NEWER than the
crash build" and then "A small line delta between a crash frame and the call site you
found at tip … is EXPECTED revision drift, NOT evidence against your hypothesis. Do not
downgrade strong-evidence to lead over a line-number mismatch alone." For a beta crash
central tip is one to two full trains ahead **and on a diverged branch**: it contains code
that was never in the beta build, and the beta build contains uplifts not on central at
that rev. So the drift is not small, and the instruction removes the model's only signal
that it is reading the wrong tree. The prompt does carry `Channel: {channel}`
(`triage.py:1150`) but nothing connects that to which tree the tools read. Depends on item
13. For beta, name the tree the tools read and tell the model to prefer
`mcp__source__raw_file` (which already reads the crash's own repo at the build rev)
whenever the exact build-time code matters.

### Group D — new behaviour: per-channel filing, and the file-only-new-bugs rule

**17. [N] CANARY(1) — `config.get_agent_autofile(channel=None)` with a `channels.<ch>`
overlay.**
Files: `crashclouseau/config.py:459-495`; the **one** production call site
`crashclouseau/bugzilla_apply.py:886`; `config/global.json` `agent.autofile`.
Every gate in `autofile_bug` reads the resulting dict (`:887, :889, :891-893, :920-923,
:951, :961, :1089`), so one channel argument covers `enabled`, `min_confidence`,
`verdicts`, `needinfo`, `daily_cap`, `comment_on_existing` and
`comment_max_bug_age_days` at once. Verified safe: `grep` gives exactly one production
caller; the four test mocks (`tests/test_autofile.py:86,125,1004,1457`,
`tests/test_bit_flip_gate.py:235`) all use `return_value`, i.e. **argument-insensitive**,
and `tests/test_bad_machine_gate.py:180,185` call it with no argument — so a defaulted
parameter keeps every one of them green. **No test asserts the signature.**
*Exact shape, because the merge order is load-bearing:* read
`a = get_agent().get("autofile", {})`, then
`over = (a.get("channels") or {}).get((channel or "").lower()) or {}` and merge
`a = {**a, **{k: v for k, v in over.items() if k != "channels"}}` **BEFORE** the existing
per-key `a.get(...)` reads, so `channel=None` returns today's dict byte-identically. Apply
the two `_env_bool` kill switches (`AUTOFILE_BUGS`, `AUTOFILE_NEEDINFO`) **AFTER** the
merge so they stay global — *a kill switch a JSON overlay can defeat is not one.*
Call site: `cfg = config.get_agent_autofile(uuid_info.get("channel"))`.
`config/global.json` gains `agent.autofile.channels = {"beta": {"enabled": false,
"comment_on_existing": "skip", "daily_cap": 3}}`. **Do not add a per-channel env var**
unless beta genuinely needs stopping without a deploy — the house rule is that an
improvement ships live and flags are for real kill switches. (`AGENT_CHANNELS` in item 28
is a real kill switch and does need one.)

**18. [N] CANARY(1) — `autofile_bug` needs its own channel gate, failing closed on an
unknown channel.**
Files: `crashclouseau/bugzilla_apply.py:886-923`; `config/global.json`
`agent.autofile.channels.beta.enabled: false`.
Nothing in the filing half knows about channels: `autofile_bug`'s twelve documented gates
(docstring at :853-885) contain none, and the only `channel` read in the whole function is
`_candidate_landed(dossier, uuid_info.get("channel"))` at :957. The **only** thing keeping
filing nightly-only is `get_agent_channels()` inside `enqueue_agent`
(`orchestrator.py:3771-3774`) — and `enqueue_agent(uuid, channel, force=True)` bypasses
the channel and proto-dedup gates **by design**, which is exactly what `retrigger_agent`
calls (`orchestrator.py:3938`). `AUTOFILE_BUGS=1` is live in prod. So the day
`INGEST_CHANNELS` gains `beta` — no deploy, no code change — **one retrigger click on
tasks.html runs the pipeline on a beta crash and files a beta bug under the nightly rules,
`comment_on_existing: true`, i.e. a comment on somebody's existing bug.** And a bulk
retrigger is a Bugzilla WRITE (`retrigger-destroys-the-filing-record`).
Fail closed on a channel with no autofile config — the same direction as every other gate
in that function. **This item must land before `INGEST_CHANNELS` changes.**

**19. [N] CANARY(6), recommended at (1) — `Dossier.already_filed_for_signature(signature)`:
a channel-blind, BMO-free self-duplication guard.**
Files: `crashclouseau/models.py` (next to `already_commented`, `:2542`); consulted in
`bugzilla_apply.autofile_bug` right after `already_filed(uuid)`.
`_open_bugs_for_signature` filters `resolution: "---"` (`bugzilla_apply.py:429`), so a bug
**we** filed from nightly and a human then closed is INVISIBLE to the beta run. `existing`
is empty, the `comment_on_existing` branch does not fire, `_bug_for_this_regression` is
never asked, and `Dossier.already_commented(bug_id, signature)` is only consulted when a
venue was CHOSEN (`if bug_id is not None`, `:1008`), so it is **dead on this path**.
`_fixed_after_build_bug` catches only the subset RESOLVED FIXED after the beta build was
produced.
**Measured on our own filings.** Panel: the 58 parseable signatures behind the 60 bugs the
canary filed (BMO `creator=cdenizet@mozilla.com`, `creation_ts >= 2026-08-05`, summary
`Crash in [@`). 18 of those signatures also crash on Firefox beta+aurora in the last 21
days. Of the 18: **11 our bug is still OPEN** (the beta run sees it and skips — dedup
works); **7 our bug is CLOSED with no other open bug covering the signature.**
Re-running `_fixed_after_build_bug` against the latest beta build carrying each signature
catches 3 (2061960, 2062119, 2066113 — all FIXED after the beta build) and **MISSES 4:
2060922 (DUPLICATE), 2061726 (INVALID), 2063364 (INVALID), 2064066 (WORKSFORME)** — "ONLY
FIXED COUNTS" is deliberate (`:725-728`). So **4 of 18 = 22.2% (95% CI 9.0-45.2%) of
nightly-filed signatures that also crash on beta would get a second Clouseau bug, and the
four are exactly the resolutions where a duplicate is worst.**
The same guard closes a disclosure case for free: the venue lookup is deliberately
unauthenticated (`:396-445`, no `X-Bugzilla-API-Key` on the `net.get` at `:432` — "we must
not reason about a security bug we can only see because the filing account can"), so a
**restricted** bug we filed from nightly is invisible, and a beta run whose own dossier
does not trip `sensitive.is_withheld` (different report, possibly different fault address)
would file a PUBLIC bug on that signature. Frequency bound: the deterministic
poison-address gate fires on 1 of 57 filings, so ~1-2 restricted bugs exist in the panel of
60. **Do not authenticate the venue lookup** — that trade was made deliberately. The DB
guard never asks BMO.
Shape: the same JSONB predicate as `already_commented` minus the `bug` term
(`fb["filed"].astext == "true" AND fb["signature"].astext == signature`). One indexed DB
query, zero BMO requests, and it is the only guard that survives the target bug being
closed. On beta it SKIPS, returning the prior bug id. On nightly it is the unshipped half
of plan #17 defect A ("5 of the 7 duplicate targets are our OWN earlier filings"), so
introduce it **channel-scoped** if nightly behaviour is to stay byte-identical.

**20. [N] CANARY(1) — record the channel (and buildid) on every filing result; make
`daily_cap` per-channel; put the channel in the ops view.**
Files: `crashclouseau/bugzilla_apply.py:1089-1092` (`result`),
`crashclouseau/models.py:2588-2595` (`filed_bugs_since`),
`crashclouseau/feedback.py:79-108` (`_filed_bugs`), `crashclouseau/models.py:2394-2440`
(`Dossier.list_tasks`) + `templates/tasks.html`.
`result = {"filed": …, "uuid": …, "signature": …, "at": …}` carries **no channel, product
or buildid**, so nothing downstream can answer "how is beta doing".
`filed_bugs_since(when)` counts `Dossier.payload.has_key("filed_bug")` with no channel
predicate against a global cap of 10, so beta filings consume nightly's budget and vice
versa — and beta's selections are **48% concentrated in a 4-day post-merge burst**, exactly
when a merge regression is freshest. It also counts rows whose `filed_bug` records a SKIP,
whereas `filed_bug_rows` deliberately filters on the `filed` flag (`models.py:2604-2606`):
**so the moment beta skips start being recorded, the cap silently tightens for everyone.**
Gate the cap on `filed` first.
`feedback._filed_bugs` builds its row from exactly those keys, and `_NOTE_MODES =
("new_bug",)` (`feedback.py:71`) is precisely beta's mode, so **beta filings WILL enter the
ReviewNote corpus pooled with nightly's** — and retuning beta against a pooled denominator
is the failure mode the `hardware-noise-denominator` note calls out ("the denominator is
the whole rule"). Add `"channel"` and `"buildid"` to `result` and surface both in
`_filed_bugs`'s row. Write the channel predicate so pre-existing rows (no channel key) do
not vanish from the nightly count; backfill is possible from `UUID → Build.channel` for
the existing 60 rows.
`tasks.html`: `list_tasks` selects no channel column and the template has no channel
filter, so beta and nightly runs are indistinguishable in the ops view — "is that stalled
run beta or nightly?" (`builds.version` is `String(10)`, which fits `156.0b10`.) Also
record the *skips*: today an `open bug N exists` skip writes nothing to
`payload['filed_bug']`, so nobody can answer "how many beta crashes did we decline to
report, and were any of them worth it?"

**21. [N] CANARY(5) for `skip`, FOLLOW for `file_new` — make `comment_on_existing`
three-valued and give beta `file_new`.**
Files: `crashclouseau/config.py:477-479`, `crashclouseau/bugzilla_apply.py:951-952`,
`config/global.json`.
Today: `if existing and not cfg["comment_on_existing"]: return {"filed": False,
"skipped": "open bug {} exists"}` — the check sits **before** `_candidate_landed` /
`_bug_for_this_regression`, so it does not matter whether that bug could be about this
regression: no comment AND no new bug. Its own config comment says so ("Turning this off
does NOT file anyway — it skips") and two tests pin the meaning by name
(`test_comment_on_existing_off_skips_it_does_not_file_anyway`,
`test_comment_on_existing_off_still_skips_a_bug_that_predates_the_cause`,
`tests/test_autofile.py:261,268`), one stating the rule outright: "Someone who turns it
off is asking for no writes, not for a different kind of write."
`skip` satisfies the letter of the requirement but is **stricter** than "file only new
bugs": it also forbids filing past an older bug, which on nightly is **92 of the 102
open-bug signatures** (`bugzilla_apply.py:630-641`).
**Measured cost on beta, two independent instruments.** Top 100 Firefox beta+aurora
signatures of 2026-08-15..25 by `_cardinality.install_time` (10,488 reports covered; 2
lookups 502'd): **58 of 98 = 59.2% (Wilson CI 49.3-68.4%)** have ≥1 open same-application
non-meta bug. The 77 signatures behind the emulated selections: **45 = 58%**, and at the
selection level **43 of 67 = 64%**. Nightly controls: **28 of 120 = 23%** on a matched
sample and 102 of 200 on the repo's own panel. So beta selections are ~2.5x more likely
than nightly's to sit on an existing open bug. `_split_by_application` rescues nothing:
**0 of 58 and 0 of 45** signatures have a foreign-application bug as their ONLY venue
(venue products across the 95 bugs behind the 58: Core 76, Toolkit 11, Firefox 4, Firefox
for Android 2, Socorro 1, Firefox Build System 1).
*Design that satisfies both the requirement and the existing tests:* accept
`"comment" | "skip" | "file_new"` **and** legacy `true`/`false` (→ `comment` / `skip`).
The two tests set it `False` → `skip` → **they must stay green byte-for-byte; if they
change, the design is wrong.** `file_new` is a new value nothing can reach by accident.
**Ship beta at `skip` for the first armed phase and move to `file_new` only after item
19.** In `file_new` mode the new bug MUST reference the open bugs it deliberately did not
comment on, or it reads as a duplicate.

**22. [N] CANARY(5) — move the memory-safety withhold computation ABOVE the skip.**
Files: `crashclouseau/bugzilla_apply.py:951` vs `:1044-1087`.
`sensitive.is_withheld` is only consulted at `:1044`, ~100 lines **after** the
`comment_on_existing` skip. On nightly, a memory-safety crash whose signature has an open
PUBLIC bug takes the branch at `:1059`: it declines the public venue, files a NEW
restricted bug, and names the public one as a probable duplicate in comment 0 (never
`see_also`, `:1071-1078`). **On beta at `skip` that crash hits `:951` first and produces
nothing — no restricted bug, no comment, no record.** The one class :mccr8 said must always
be filed (bug 2065051: "Bugs on poison crashes like that should always be filed initially a
security issue") is the class the beta skip silently drops.
Reach: the deterministic poison-address gate fires on 1 of 57 filings
(`report_bug.py:1791-1796`) and 59.2% of beta signatures have an open venue, so ~1% of
beta rung-70 verdicts. Small, and the highest-value 1%.
Fix: `if existing and not comment_allowed and not sensitive.is_withheld(...)`, so a
withheld dossier continues down the existing nightly path. **This is a security regression
introduced by item 21, not a pre-existing one — the two must land together.** Remember the
group is per product: Core `core-security`, Firefox/Toolkit/DevTools
`firefox-core-security`; `is_security_issue` is not a BMO param.

**23. [N] CANARY(2), with item 8 — don't let a merge-window candidate assert `regression` /
`regressed_by`.**
Files: `crashclouseau/report_bug.py:1183-1213` (`is_suspected_regression`), `:1786, :1828,
:1841` (the keyword, the field, the "Suspected regressor:" vs "Starting point — NOT a
suspected cause" wording), `crashclouseau/corroborations.py:56` (a new **literal-key**
flag).
`is_suspected_regression` reads `corroborations['candidate_in_pushlog_window']` and gates
three structured claims: the `regression` KEYWORD release management triages on, the
`regressed_by` field, and the wording. It was narrowed precisely because a 2022 changeset
got all three on bug 2062119. On beta, "in this build's window" degenerates on any build
whose window spans the merge to "landed on trunk some time in the last month" —
**5,192 changesets** for `20260812080401 → 20260817142839` against **45 / 76 / 61** for
155.0b1→b2→b3→b4. (For scale, mozilla-central pushed 6,209 changesets in 30 days total.)
**With item 9 held this is reachable only on the merge-day build, which ships to 0-7
installs against an install threshold of 6 — so it is near-unreachable today.** But it is
one absent `builds` row away from being live on every b1 filing, and item 8's
implementation is exactly the change that could remove that row. Ship the guard with item 8.
The merge push is trivially recognisable from data `pushlog.collect` already holds — one
push, thousands of changesets, `pushuser=ffxbld@lando.moz.tools`, and `collect` already
records `merge: len(chgset["parents"]) > 1` (`pushlog.py:36`). Record a second
corroboration `candidate_arrived_by_merge` and treat a merge-window candidate as OUT of
window for `keywords` / `regressed_by`, dropping it onto the existing "Starting point"
prose. **An uplift-window candidate (the 45-122 bucket) is a stronger regression claim
than anything nightly produces and keeps the keyword.**
*Registry checklist the new flag must satisfy, verified by injection:* the key must be a
**LITERAL** (a computed key like `"beta_" + name` slips the scanner entirely — proven);
declared in `corroborations.REGISTRY` with a kind from `corroborations.KINDS`; every
non-`policy:` reader must literally contain `get("flag")` or `["flag"]`; a
`promotion`/`clamp` must be read by `report_bug.py` or listed in
`corroborations.UNPUBLISHED` with an argument; a `suppression` that should close the
proto-cluster forever goes in `models._INSTANCE_SUPPRESSED` **and** declares
`policy:_INSTANCE_SUPPRESSED`; if the SO fold may re-inflate past it, it goes in
`orchestrator._SO_BOOST_POLICY` **and** declares `policy:_SO_BOOST_POLICY`; a reader-less
flag must be added to `TestWriteOnlyFlagsAreADecision.EXPECTED`
(`tests/test_corroboration_registry.py:283`) with a reason.

### Group E — tuning, ops and cost

**24. [T] CANARY(4) — per-channel `POPULATION_BIT_FLIP_RATE` /
`POPULATION_BROKEN_CPU_RATE`; measure `POPULATION_TOP_CPU_SHARE_MEDIAN`.**
Files: `crashclouseau/sigage.py:379-391`; consumers `agent/triage.py:585-606`
(`_hardware_noise_lines`), `:526-540` (`_cpu_spread_line`),
`agent/orchestrator.py:2547-2557` (the abstain reason, which reaches the **filed bug** and
the UI, and hardcodes the word "nightly" at `:2553`).
The shipped 0.025 / 0.041 **reproduce exactly** on Firefox nightly over 364 days (2.55% /
4.15%, n=692,770, against the docstring's 696,901 over the same shape), and **beta over
364 days reads 0.0675 / 0.0582 (n=269,501)** — 2.6x and 1.4x. Shipping them unchanged
tells a beta prompt that a 6% flip rate is 2.6x the population **when 6.75% IS the beta
population**, under the label "crash population: 2.5%", immediately followed by "the higher
these are, the likelier it is that this signature is a failing-hardware artefact … any
mechanism you can construct for it will be fiction that fits."
**`POPULATION_TOP_CPU_SHARE_MEDIAN = 0.32` was measured over 200 nightly signatures and
nobody measured the beta equivalent.** Measurement to run before item 15/24 lands: the same
shape as `sigage.hardware_noise` (Firefox, `get_search_channel("beta")`, 364 d) over the
beta signatures the selector actually picks, n ≥ 100, reporting the median top-`cpu_info`
share. Until then `_cpu_spread_line` must not quote "the median Firefox-nightly signature"
to a model reasoning about a beta crash.
*Separate nightly finding, recorded so it is not lost:* the last 30 days of nightly read
**8.86% / 8.10%** (n=29,322), so the 364-day constants now understate the CURRENT nightly
population ~3x and ~2x — the annotation's coverage is climbing. That is a nightly retune
question, not beta's.

**25. [T] CANARY(4) — beta fixture in the prompt-byte ledger.**
Files: `tests/test_prompt_budget.py:40` (`_PLAIN` has `"channel": "nightly"`), `:80-86`
(`_MEASURED`: system.md 16157±400, plain user prompt 970±120, hang user prompt 2675±300).
`_user_prompt` prints `Channel: {channel}` (`triage.py:1134,1150`) and the whole prompt is
assembled from channel-aware helpers, but every ledger fixture is nightly, so a beta-only
sentence adds **zero** bytes to the four measured numbers and the ledger stays green —
reproducing exactly the v109 failure the file was written to prevent (+2,552 unreviewed
bytes; candidate-naming 42.2% → 23.4%). The file's own docstring: "the failure this
prevents is not 'the prompt got big', it is 'the prompt got bigger and nobody wrote it
down'." **Any of items 14/15/16 must land WITH a beta fixture and its measured bytes in
the same diff.**

**26. [T] CANARY(2) — add the two generated cert-data headers to
`agent.filters.ubiquitous_paths`.**
File: `config/global.json` `agent.filters.ubiquitous_paths`.
Add `security/ct/CTKnownLogs.h` and `security/manager/ssl/StaticHPKPins.h`. **64 of the
1,009 candidate-bearing in-cycle beta changesets (6.3%)** are automated "No Bug,
mozilla-beta repo-update remote-settings mobile-experiments ct-logs" commits, and **100% of
their interesting file touches are those two generated data headers** (49 and 15
occurrences — the two most-touched interesting files in the whole 3-cycle beta pushlog,
ahead of `dom/ipc/ContentParent.cpp` at 15). No crash frame is ever in a generated
cert-log table. Cheap, and it is the only *noise* class in a window that is otherwise
93.2% plausible regressors.

**27. [T] CANARY(1) — `agent.calibration.table` must leave beta's `p_worth` EMPTY.**
Files: `crashclouseau/config.py:803-820`, `config/global.json` `agent.calibration.table`,
`crashclouseau/agent/orchestrator.py:1587-1606` (`_apply_worth_investigating`),
`crashclouseau/report_bug.py` (`_worth_phrase`).
`{25: 0.5, 50: 0.5714, 70: 0.7234, 85: 0.7234}` is the fit over **all 90 rows of
`corpus_ship`** (n per rung, reported rows only: 25 → 1/2, 50 → 4/7, 70 → 21/27, 85 →
13/20), which is entirely Firefox nightly — and the number reaches a human:
`_worth_phrase` puts "N% worth investigating" **in the filed bug** and the crashstack badge
shows it. `_apply_worth_investigating` has no channel key. Fix: key the table by channel
and leave beta's entry empty, so `p_worth` stays `None` and the comment simply omits the
sentence, which is what it already does pre-calibration. `config.py`'s own rule is that a
number a Bugzilla reviewer reads cannot be fit on the wrong arm; plan #16 §6.2 took the
same decision for Fenix.

**28. [T] CANARY(1) — per-channel sweep cap, an `AGENT_CHANNELS` kill switch, and a
decision about the per-crash cost cap.**
Files: `crashclouseau/agent/orchestrator.py:3798-3860` (`sweep_untriaged_crashes`),
`crashclouseau/config.py:400-427` (`_SWEEP_DEFAULTS`, `max_per_run: 3`),
`config.py:369` (`get_agent_channels` — add an `AGENT_CHANNELS` env override),
`orchestrator.py:3441-3452` (`over_budget`), `config.py:570-578`.
The sweep passes one global `max_per_run = 3` per 6-hourly tick with
`channels=config.get_agent_channels()` and one `SweepMark` cursor advancing by uuid id.
Once beta is an agent channel, beta candidates compete with nightly for the same 3 slots
and share one monotonic cursor — the scenario
`tests/test_sweep_untriaged.py:302-313`'s own comment describes ("three beta candidates
would otherwise fill a tick … and starve the nightly ones"), except that today the channel
filter is what prevents it. That test still passes with beta enabled, **so it is not a
tripwire for this.**
`get_agent_channels` is the ONE flag of the ~14 canary flags with no env override, so
enabling and disabling beta triage each require a deploy — and a deploy kills in-flight
~20-min runs at ~$3 each. Add `AGENT_CHANNELS` (space-separated, same shape as
`INGEST_CHANNELS`) so phases 4 and 5 stop requiring a deploy and **beta gets a kill switch
that does not take nightly down with it** (`AUTOFILE_BUGS=0` is global).
Also: **the only per-crash cost "cap" is reactive and log-only** —
`over_budget = cap is not None and cost > cap` sets `payload["over_budget"]` and logs a
warning *after* the run has been paid for; nothing aborts, and `config.py:570-578` says
so. The real bounds are `agent.job_timeout` 1800 s (RQ SIGKILL) and
`principal.max_turns` 60, both channel-blind. Either make it abort (or at least skip the
second opinion) or rename it, so nobody plans beta capacity against a number that
enforces nothing. Expected beta load is 1.33 new pairs/day → 7.6 UUIDs/day → 4.2-5.8
dossiers/day, so `max_per_run` (3 × 4 ticks = 12/day) is **not** the binding constraint on
beta volume — nightly starvation is what matters.

**29. [T] CANARY(3) — separate the ingestion tick from the analysis chain.**
Files: `crashclouseau/update.py:127-131` (`analyze_reports`), `:146-150`
(`analyze_patches`), `:219-236` (`update_all` / `update_in_queue`),
`crashclouseau/worker.py:47` (`get_queue` default `"low"`), `Procfile` (already lists
`high default low`).
`update_all` enqueues one `update` job per (product, channel) onto the `"low"` queue every
20 minutes (`bin/schedule.py:13-15`), and the report/patch analysis chain lives on the SAME
queue behind `if len(queue) <= 1`. With nightly only, an executing `update` sees an empty
queue and the chain is seeded. **With nightly+beta it can see 2 and `analyze_reports()`
becomes a silent no-op for that tick; at three channels or two products it always does.**
It fails by doing nothing, with no log line — the shape of `done-is-not-triaged`'s four
silent no-ops, and `ingestion-stall-has-no-alarm` says there is no alarm for it. Fix:
count only `analyze_one_report`/`analyze_one_patch` jobs, or move the chain to its own
queue. Item 2 supplies the deterministic order so beta cannot indefinitely delay nightly
inside that single serial chain.

**30. [T] CANARY(3) if the canary spans a merge — skip patch EXTRACTION for a merge push,
keep its `nodes` rows.**
Files: `crashclouseau/pushlog.py:34-58` (`collect`), `crashclouseau/models.py:379-401`
(`Changeset.add`).
Measured per merge: the push is 5,130-6,952 changesets of which **1,932-2,678 touch an
interesting file**, so `analyze_patches` owes that many serial self-re-enqueuing
`patch.parse` jobs at 3.45-6.51 s each (median 3.76 s in-cycle, 6.14 s on merge revs) =
**3.3-4.5 HOURS of the shared queue per merge**, delivering 24-33 days of nightly's
~80-interesting-changesets/day rate in one burst. **100% of it is work already done under
nightly**: 1,932 of 1,932 merge candidates have a node hash that also appears in the m-c
pushlog (5,116 of 5,124 non-merge = 99.8% overall) and both repos serve byte-identical
`raw-rev`. Meanwhile `Changeset.add` fires **~32,155 SQL round-trips** (27,006 statements +
5,149 COMMITs, because `Node.__init__ → HGAuthor.get_id → _get_or_create_id` commits PER
CHANGESET) = 31.6 s on a local Postgres 16 container, ~48-130 s networked at 0.5-3 ms RTT.
The HTTP half survives fine (3.43-4.09 s, 7.5-16.2 MB, RSS 122-160 MB, well inside a 512 MB
dyno).
**The trap:** `Build.put_data` inserts a `builds` row only `if rev in revs_c`
(`models.py:523`), i.e. only when a `nodes` row for that revision already exists — and the
merge-day build's revision IS a member of the merge push. **Dropping the push therefore
deletes the merge-day `builds` row and with it the bounds of the on-stack and off-stack
windows (item 9).** So: for a push containing a `merge`-flagged changeset, **emit every
member with `files: []`.** Node rows are still created (`Build.put_data` finds the
revision; all three windows keep today's bounds), no `Changeset`/`File` rows are created
(no patch parses, no 10,090 changeset rows), and `Changeset.find` could never have returned
them anyway because it needs a `Changeset` row.
Selection rule, **not fitted: drop a push that contains a changeset with
`len(parents) > 1`. No size threshold.** Exactly **5 of 2,356 beta pushes over 126 days**
qualify: 4 are the cycle merges and the 5th (push 26875, 11 changesets) carries 0
candidate-bearing changesets. Median ordinary beta push = 1 changeset, p99 = 6, so the
other 2,351 are untouched. Record what was dropped; do not silently discard.
The residue is the duplicated `nodes` rows and the HGAuthor round-trips — bound the
round-trips in the same diff if it is cheap (batch the lookup), otherwise measure them on
the canary's first merge.
*Timing: at a 4-week cadence any canary longer than ~4 weeks spans a merge; at 2 weeks,
any longer than ~2. Plan for it.*

**31. [T] FOLLOW — unique index on `nodes (channel, node)` + `Changeset.add` upsert.**
Files: `crashclouseau/models.py:178-190` (`Node`), `:379-401` (`Changeset.add`),
`crashclouseau/update.py:25-52` (`put_filelog`); DDL in `bin/release.py`.
`nodes` declares no `UniqueConstraint` and **no index at all** — the only unique
constraints in `models.py` are on `builds (buildid, product, channel)`, `hgauthors`,
`selection`, `files.name`, `uuids.uuid`, `dossiers` and `verdicts`. A second row per
(hash, channel) is REQUIRED, not accidental: the same changeset has two different
pushdates (its central landing vs the merge/uplift push) and every read path filters on
channel (`get_ids:287`, `get_id:298`, `find:447-451`, `to_analyze:344`,
`authors_for:213`). The gap is on the WRITE side: `Changeset.add` does
`db.session.add(Node(channel, chgset))` unconditionally — no `on_conflict`, no existence
check — and `put_filelog` derives its start from `Node.get_max_date(channel) + 1s` with no
lock, so two concurrent `update` jobs for the same channel compute the same start and both
insert. On beta the blast radius is one merge push: **5,130 nodes + 10,090 changeset rows
duplicated in one go**, and with no index every later `get_ids`/`find` scans them.
**Migration hazard:** `models.create()` runs `create_all()` only on a FRESH DB
(`_ADDED_TABLES` is the mechanism for a new TABLE; there is no equivalent for an index on
an existing table), and `_ensure_enum_values` can never `ALTER` — reproduced live
(`isolation_level may not be altered unless rollback() or commit() is called first`). So
this needs explicit idempotent DDL (`CREATE UNIQUE INDEX IF NOT EXISTS` on a **separate**
connection opened with `isolation_level="AUTOCOMMIT"`) in `bin/release.py`, a duplicate
sweep first, and a Postgres-gated test. **Do not assume `create_all` will do it.** Item 30
removes the worst case, which is why this is FOLLOW.

**32. [T] FOLLOW — measure the bad-machine gate on beta before trusting it there.**
Files: `crashclouseau/machine.py:14-29,50-81`, `crashclouseau/config.py:734-760`,
`crashclouseau/agent/orchestrator.py:2151-2236`.
**Not a code change yet — a measurement.** `install_time` is a (machine, BUILD) id reset by
the updater, and the module's numbers rest on nightly's daily churn (its own docstring:
"56,012 of 56,111 nightly install_times map to exactly one buildid and the median install
is 1.1 days old at crash time. So a machine's visible history is short."). Beta ships every
~2.00 days, so a beta `install_time` survives days-to-a-week and accumulates more crashes,
more distinct signatures and a longer span inside the same 14-day lookback.
`min_signatures >= 10`, `max_cpu_infos <= 1` and `min_span_seconds >= 1800` will therefore
be met by healthy beta installs a nightly install could never reach, and **the gate
ABSTAINS outright, with no downweight.** The −7.0 pp effect (z = −4.4) behind
`min_signatures = 10` was measured over 141k nightly crashes.
Measurement to run: the same scatter study on beta with the same outcome definition (later
reproduction on different hardware), then make `min_signatures`/`min_span_seconds`
per-channel. Candidate reformulation to test in the same pass: **normalise by install AGE
— signatures per DAY rather than per install** — which is the quantity the mechanism
actually predicts and which does not move with the update cadence. **Item 4 is a
prerequisite:** until `machine._install_history` goes through `get_search_channel` the gate
cannot fire at all for the 36-41% of beta that is DevEdition, so any measurement before
item 4 is measuring the wrong thing. Until it is measured, leave the gate on (it fails
toward reporting on beta, since `machine` returns `empty` when it cannot see the history)
and count its beta firing rate on the canary.

---

## 4. Numbers — every threshold, its measurement, its denominator, its decision

House rule: a number fitted on one motivating case is overfitting by construction. Where
the recon did not measure something, this table says so and names the measurement instead
of inventing the value.

| knob / constant | shipped | decision | measurement and denominator |
|---|---|---|---|
| `thresholds.installs.Firefox.beta` | 6 | **KEEP** | It is the whole gate (78-89% of beta selections come through `is_spike`'s from-zero branch, which `floor`/`ratio` do not touch). With item 8 it lands at **40 distinct (sig,bid) pairs / 30 as-of run-days = 1.33/day → 7.6 UUIDs/day → 4.2-5.8 dossiers/day → $4-17/day**. Priced alternatives: 3 → 4.60 pairs/day / $9-38; 2 → 13.3/day / $21-89; 1 → 96.6/day / $71-299; 10 → 0.57/day; 20 → ONE pair in 30 days. Independent non-fitted argument: 6 sits at the **90.01st percentile of 5,527 beta (sig,bid) pairs**, the same place nightly's only non-degenerate install bar (`mature_installs` = 4) sits in nightly's own distribution (**91.97th of 6,915**). |
| `spike.floor.Firefox.beta` | 10 | **KEEP** | Gates only the nonzero-baseline branch. Branch split over 30 run-days at installs=6 / ratio=3 with item 8 applied: floor 3 or 5 → 61 of 122 selections through that branch (50%, 65 pairs/30 d); **10 → 17 of 78 (22%, 40 pairs)**; 15 → 7 (10%, 31); 20 → 4 (6%, 28); **25 and 50 → 0 (0%, 26)**. The population-rate transfer is self-refuting: a beta build carries **8.49x** a nightly build's reports (lifetime medians 2,674 vs 315 over 18 and 84 shipped builds), so nightly's 3 "scales" to 25.5 — and at 25 the branch dies, making "a live beta signature suddenly getting worse" structurally undetectable. Also: `tests/test_selection_log.py:32-47` fails below 6 (probe: floor=5 → `AssertionError: 5 not greater than 6`). |
| `spike.ratio.Firefox.beta` | 3 | **KEEP** | A ratio is dimensionless, so the 8.49x per-build scale factor does not apply; nightly's 3 transfers unchanged. Measured cost of moving it (30 run-days, installs=6, floor=10, item 8 applied): 2 → 56 pairs/30 d (+40%, $7.7-32/day); **3 → 40 ($4-17/day)**; 5 → 29 (−27%, $2.5-10/day). Reaches only the 22% of selections on the floor branch, so it cannot fix a spend problem. |
| `thresholds.protos.Firefox.beta` | 20 | **20 → 5, but only after the canary prices it** (open question 4 — deliberately not a work item) | The 40 pairs item 8 leaves carry median **3.0** / p90 14 / max 22 distinct protos (sum 229), against nightly's mean **1.07** / median 1 / max 6 (n=80) — **beta is the first channel where this cap binds.** Cap → UUIDs/day: 20 → 7.57 ($4-17, truncates 1 of 40); 10 → 6.50 ($4-15, 7/40); **5 → 4.40 ($2-10, 16/40)**; 3 → 3.17 ($2-7, 19/40); 1 → 1.30 ($1-3, 30/40); 50 → 7.63 (0/40). 5 halves per-pair spend while still analysing the five loudest clusters (the facet is count-ordered). Not canary-required: $4-17/day at 20 is affordable, and the UUID→dossier yield is a single-window calibration. |
| `spike.mature_after_days` / `mature_installs` beta | 5 / 4 | **KEEP, and keep them inert** | `get_maturity_bar` returns `(None, 1)` off nightly (verified by execution), so neither fires. Correct here: nightly's bar prices a 21-DAY window while beta's is 3 BUILDS = 4-7 days, almost exactly beta's arrival interval (a build stays testable 3.86-5.0 d and holds 77-96% of its crashes by then). **If the 3-build window is ever widened, the bar must come back with it.** |
| `backward_lookup_ndays` used as beta's `shift` | 1 (`datacollector.py:182`) | **KEEP for the canary; re-measure after item 8** | With `shift=1` and 3 build-days, index 0 is `untestable_prefix` and indices 1-2 are testable — but the newest holds 0.2-2.7% of its crashes and can never clear `3 × max(before)`, so **all 135 of 135 selections across 14 replayed run-days over two cycles landed at index 1** (index 2 selected 0 times; index 0 cannot). The measured alternative is `shift 1→2` **with** `n_builds 3→6`: post-merge burst 32 selections in 4 days → **0**, total volume 2.31 → 1.28/run-day — essentially the same volume as item 8's 1.33. `shift` must be raised WITH the window (at n=3, shift=2 leaves one testable day and shift=3 leaves none). Nothing in the suite exercises `shift` today. **The two fixes overlap: re-measure after item 8, do not stack them blind.** |
| `Build.get_last_versions(n)` for beta | 3 | **KEEP for the canary** | Coupled to `shift` (above). At a 2-week cadence there are only ~4-5 shipped builds per cycle, so n=3 covers most of the cycle and the major-boundary effect gets relatively worse — which item 7 is what fixes, not `n`. |
| `agent.autofile.min_confidence` beta | 70 | **KEEP** | The config docstring's own measurement: ~3 filings/day at rung 70 vs ~7.6/day at 50, a 2.53x multiplier. On beta the extra volume lands almost entirely on the **84% of selections whose signature is long-lived on beta** (median beta first-seen 393 d; median TRUE first-seen 1,194 d; only 6 of 76 ≤ 7 d) — exactly the population the stale gate exists to hold back. Buy volume with the selector knobs, which are measured. |
| `agent.autofile.daily_cap` | 10, global | **per-channel; propose nightly 10 / beta 3** | Nightly runs at ~3 filings/day (60 bugs in 21 days) against a cap of 10. Beta's projected rate is 0.008-0.048/day, so a cap of 3 is not a throughput constraint — it bounds the blast radius of the **48%-of-selections-in-4-days** post-merge burst, which is the only way beta could eat nightly's budget. Item 20. |
| `agent.autofile.comment_on_existing` beta | `true` (global) | **`skip` for the canary, `file_new` after item 19** | 58-59% of beta signatures have an open same-app non-meta bug (58/98 = 59.2% Wilson CI 49.3-68.4%; 45/77 = 58%; 43/67 = 64% at selection level) vs a **23% nightly control** (28/120) — and `_split_by_application` rescues **0 of 58** and **0 of 45**. `skip` therefore suppresses ~60% of beta filings; `file_new` restores them at ~2.4x the volume. Items 21, 22. |
| `agent.signature_age.min_age_days` beta | 7 | **KEEP** | Do NOT retune — see contradiction 4 and kill #11. The gate fires on 68/77 = 88% of beta selections (nightly 85/120 = 71%) and is **mostly correct**: 84% of beta selections sit on signatures long-lived on beta. The class where it is genuinely wrong is 3 of 77 = 4%, fixed by items 12 and 15, not by a threshold. |
| `agent.signature_age.other_channel_floor` beta | 20 | **INERT — needs a channel-relative rule, not a count. Not designed here.** | 87% of beta selections (67/77) clear a floor of 20 with a **median of 2,892** off-beta reports (p75 23,908, max 1,041,241), against nightly's 52% and median 33 — the floor was calibrated on ten nightly points whose off-channel counts ran 1-80. **No count-based value can discriminate on beta**, so the gate always uses the all-channel clock. Raising it is worse than useless. A channel-relative reformulation (off-channel share, or off-channel-per-day) is a measurement nobody has run. |
| `sigage.POPULATION_BIT_FLIP_RATE` | 0.025 | **per-channel; beta 0.0675** | Nightly 364 d: 2.55%, n=692,770 (reproduces the shipped constant). Beta 364 d (`get_search_channel("beta")`): **6.75%, n=269,501.** Item 24. |
| `sigage.POPULATION_BROKEN_CPU_RATE` | 0.041 | **per-channel; beta 0.0582** | Nightly 364 d: 4.15%, same n. Beta 364 d: **5.82%**, same n. Item 24. |
| `sigage.POPULATION_TOP_CPU_SHARE_MEDIAN` | 0.32 | **NOT MEASURED for beta — do not port it.** | Nightly value measured over 200 nightly signatures. Measurement to run: `hardware_noise`'s shape (Firefox, beta+aurora, 364 d) over ≥100 selector-picked beta signatures, reporting the median top-`cpu_info` share. Until then `_cpu_spread_line` must not quote "the median Firefox-nightly signature" to a beta run. Item 24. |
| `agent.bit_flip.max_bit_flip_rate` | 0.2 | **KEEP** | On the 77 beta selections with their own product+channel denominator, `bit_flip_rate` runs median 0.000 / p75 0.038 / **p90 0.502** and clears 0.2 on **10/77**. The flip-value histogram on beta clusters as the docstring describes with a gap between 43 and 62 (25:1136, 43:292, 62:284, 92:405, 96:205), so `min_confidence=50` is not on a knife edge on beta either. |
| `agent.bit_flip.max_broken_cpu_rate` | 0.7 | **KEEP** | `broken_cpu_rate` median 0.014 / p75 0.038 / p90 0.078 / max 0.982, clears 0.7 on **3/77**. Empty space, not a knife edge. |
| `agent.bit_flip.min_signature_reports` | 5 | **KEEP** | **INERT on beta** (0 of 77 below it; min 6, median 445 reports/signature over 364 d) where it mutes the gate on **69 of 120 = 58%** of nightly selections (median 3 reports). The docstring's "the margin is ONE report" hazard is nightly-only. |
| `agent.bit_flip.max_reports` (the singleton test) | 1 | **KEEP — do not raise it** | Triggers (1) and (2) require `reports <= 1` and a beta signature almost never has exactly one report in a year, so two of the three gates written for bug 2061961 do not exist on beta. The direction is toward reporting, so nothing is lost silently, and **the reason it is 1 is precisely the volume argument beta makes worse** (config.py:723-725). A per-report-share test on the same channel would be the honest replacement; nobody measured it. |
| net effect of the hardware gates | — | **transfers; no retune** | `_signature_is_mostly_hardware` suppresses **13/77 = 17%** of beta selections vs **17/120 = 14%** of nightly's. Note the denominator rule still bites: 4 signatures (`js::IsProxy`, `js::Nursery::Space::isInside`, `js::gc::HeaderWord::get`, `wcslen \| RtlInitUnicodeStringEx \| LoadLibraryExW`) are suppressed on an all-channel denominator and NOT on beta's own — the bug-2062219 failure mode. |
| the shipped `cpu_model` normalisation | — | **changes 0 beta suppressions** | Exact-string `BROKEN_CPUS` finds **0** Raptor Lake reports on beta/x86 where normalised finds **154** (0.8% of x86 cpu'd reports); channel-wide 1,473 vs 1,627, i.e. blind to **9.5%**. On the 77 selections, **22 (29%) have their Raptor Lake count understated but 0 cross `max_broken_cpu_rate` 0.7.** Same shape as bug 2065969: it stops us printing a false hardware clean bill, nothing more. |
| `agent.calibration.table` | one global fit | **beta gets NO table** | Fit over all 90 rows of `corpus_ship` (25 → 1/2, 50 → 4/7, 70 → 21/27, 85 → 13/20), entirely Firefox nightly, and the number is published to BMO. Item 27. |
| `agent.offstack.max_candidates` | 150 | **KEEP; no beta value** | Max in-cycle candidate-bearing changesets over 47 beta windows = **42**; p90 34; median 21. The cap is 3.6x above the measured maximum and never binds in-cycle. (It WOULD bind at 1,932-2,678 on a merge window — a second reason to bound the merge rather than cap it.) |
| `agent.sweep.max_per_run` | 3, global | **per-channel** | Not a throughput constraint on beta (3 × 4 ticks = 12/day vs a projected 4.2-5.8 dossiers/day); the risk is nightly starvation. Item 28. |
| `max_cost_usd_per_crash` / `job_timeout` / `principal.max_turns` | 1800 s / 60 | **no per-channel value; decide abort-vs-log** | The cost cap is log-only after the fact; the real bounds are channel-blind. Item 28. |
| `_fixed_after_build_bug` | unchanged | **KEEP; paste the beta numbers into its DOMAIN paragraph** | Its docstring asks to be re-measured before beta. Done, two instruments. Same control shape as the nightly measurement (60 reports/day × 14 d, beta+aurora, 509 (sig, build) pairs found, first 200 evaluated): **2/200 = 1.0% overall; 1/97 = 1.0% (CI 0.2-5.6%) for builds ≤14 d old; 0/62 for 15-90 d; 1/13 for >90 d**, against the nightly control's 47/599 = 7.8% and **25/414 = 6.0% (CI 4.1-8.8%) in domain**. Second instrument: 2/77 = 3% beta vs 2/120 = 2% nightly. Also measured: the uplift-shaped complement (a same-app FIXED bug resolved within 30 days BEFORE the beta build) is **3/97 = 3.1% (CI 1.1-8.7%) and all three are one bug (1906769) on one signature** — so "never comment" does not systematically cost us uplift requests. |
| `report_bug._bug_version("beta")` | `"unspecified"` | **KEEP** | Kill #6 — measured against 273 human filings. |
| `cf_status_firefoxNNN`, whiteboard | not set | **KEEP not set** | Kill #7 — `release-mgmt-account-bot` is the first setter in 5/5 sampled histories. |
| the "no-user build" floor (item 8) | new | **100 reports, any value in [20, 400]** | 5 of 5 merge-day builds ≤ 20 lifetime reports (8, 13, 7, 17, 1); 54 of 54 other builds ≥ 435 (median 2,850). **25x gap, no overlap — not fitted.** |
| the merge-push rule (item 30) | new | **`len(parents) > 1` anywhere in the push; no size threshold** | 5 of 2,356 beta pushes over 126 days qualify; 4 are the cycle merges, the 5th carries 0 candidate-bearing changesets. Median ordinary push = 1 changeset, p99 = 6. **Not fitted.** |
| `buildhub_lookback_ndays` (item 6) | new, 30 | **= `max_ndays`, the node-retention window** | A 3-day window holds 0 builds on 23 of 196 days and 1 on 117 — **71% of switch-on moments are dead.** 30 is not a tuned number; it is the retention window the rest of the pipeline already uses. |

---

## 5. Coupling inventory

| # | area | file:line | what happens on beta |
|---|---|---|---|
| 1 | `CHANNEL_TYPE` Postgres enum | `models.py:18` | **Nothing. No migration.** Generated from `config.get_channels()`, which has listed nightly/beta/release since commit `3695614` (2018-02-24); a fresh Postgres built by `models.create()` reports `['nightly','beta','release']` in `pg_enum`. The `_ensure_enum_values` ALTER defect (reproduced live) is irrelevant here — it would only bite for a channel added AFTER a DB was created, e.g. `esr`. |
| 2 | `INGEST_CHANNELS` | `update.py:233` (the only reader) | Env var, no deploy, takes effect on the next 20-min tick. **Its empty default is all configured channels, so clearing it turns RELEASE on too.** Set it explicitly. |
| 3 | `agent.channels` | `config.py:369`, read at `orchestrator.py:3771` and `:3833` | Config-file only — **the one canary flag of ~14 with no env override.** Item 28 adds `AGENT_CHANNELS`. |
| 4 | `enqueue_agent`'s channel gate | `orchestrator.py:3770-3774` | `force=True` bypasses it by design, and `retrigger_agent` (`:3938`) uses `force=True`. Item 18. |
| 5 | `autofile_bug` | `bugzilla_apply.py:886-923` | No channel dimension at all. `AUTOFILE_BUGS=1` is live. Items 17, 18. |
| 6 | proto-signature dedup | `models.py:1142-1167` | Cross-channel, both directions. 16.5% upper bound. Item 10. |
| 7 | the three `builds`-table windows | `datacollector.py:45`, `update.py:60`, `orchestrator.py:307` | §1.3. Item 9. |
| 8 | `Build.put_data`'s `if rev in revs_c` | `models.py:523` | A `builds` row exists only if a `nodes` row for its revision does — the coupling that turns item 30 into a window change. |
| 9 | the single `"low"` queue | `update.py:127-131`, `worker.py:47`, `bin/schedule.py:13` | `len(queue) <= 1` measures the clock, not the chain. Item 29. |
| 10 | `SearchfoxCtx` | `agent/tools/searchfox_cg.py:39-43` | The one MCP context with no channel. Item 13. `Repo.BETA` and `html._SF_TREE`'s beta→`firefox-beta` mapping already exist and the latter is already pinned by `tests/test_product_wiring.py:1157`. |
| 11 | the other four MCP contexts | `agent/triage.py:1382-1389`, `agent/second_opinion.py:123-138` | **Fine.** `PatchCtx`/`HistoryCtx`/`SourceCtx`/`SocorroCtx` all receive `channel=channel`; all 37 `channel="nightly"` defaults were traced to their callers and 36 pass the crash's channel. Harden `build_seed` to FAIL rather than default to `"nightly"` on an unknown channel — a silent fallback to mozilla-central for a beta crash is the one way a default could still bite. |
| 12 | web UI | `html.py:86`, `:306`, `:22-23`, `:337-346`; `templates/reports.html:41-50` | **Fine.** `reports.html` builds its channel `<select>` from `UUID.get_buildids()`, which groups by (product, channel), so beta appears as soon as beta rows exist and `?channel=beta` works (verified against Postgres with one nightly and one beta build+uuid). `crashstack.html` takes its channel from `uuid_info["channel"]`, so a beta crash gets `releases/mozilla-beta` hg links and the `firefox-beta` searchfox tree automatically. `selection.html` already shows `row.channel`. **`tasks.html` is the one gap** — item 20. |
| 13 | needinfo + product/component | `report_bug.py:1551-1690`, `:1135-1180`; `models.py:200-246` | **Fine, and it rests on one operational fact.** `Node.authors_for(nodes, channel)` and `recent_bugs_by_author` filter `Node.channel == channel`, which sounds like it starves the ladder and does not: the cycle merge pushes the whole of mozilla-central onto mozilla-beta with authorship preserved. Measured 2026-07-26..08-25: beta 5,862 changesets / **559 distinct authors** vs central 6,209 / 589; nodes per author median 4 vs 4 (p75 11 vs 10); distinct bugs per author median 3 vs 3; authors with exactly one node 28% vs 25%. `pushlog.collect` reads `chgset["author"]`, not the pusher, so the `dsmith`/`rvandermeulen`/`ffxbld` push users do not matter. **Caveat: if the merge push is truncated or fails to ingest, `authors_for` returns `{}` for every merged candidate and BOTH the needinfo and the product/component resolution go quiet — and `autofile_bug` refuses to file without a product/component pair (`:1032-1037`), so the failure mode is a silent global beta filing stop.** Assert the merge-push node count after the first canary cycle rather than discovering it from an empty filing log. |
| 14 | Bugzilla filing shape | `report_bug.py:1717-1724`, `:1798-1861`; `bugzilla_apply.py:1120-1127` | **Fine.** `version: unspecified` (kill #6); no tracking flags, no whiteboard (kill #7); the create-payload allowlist forwards only product/component/version/type/keywords/`cf_crash_signature`/groups/cc + summary/description/flags. |
| 15 | eval / corpus | `eval/corpus.py`, `eval/study_corpus.py` | **No beta arm exists**, so there is no measurement path for beta quality and no beta calibration table can be fitted. Out of scope (§11), but it is the reason item 27 leaves `p_worth` empty. |

---

## 6. Test plan

**The suite says almost nothing about channels today, and this was probed empirically.**
In a symlinked scratch tree, flipping `agent.channels` to `["nightly","beta"]` leaves the
**whole suite GREEN** (1,713 tests, OK); so does retuning beta's `installs` / `ratio` /
`protos` / `mature_after_days`; so does injecting brand-new config keys
(`beta_window_ndays`, `agent.beta.{...}`) — there is no config-schema validation. And
because **no test anywhere uses `autospec=True` / `create_autospec`**, every mocked
`config.get_*` accepts any call signature, so changing `get_threshold(typ, product,
channel)` to take a fourth argument would not fail a single mocked call site. **Exactly one
test trips on a beta retune**: `tests/test_selection_log.py:32-47`'s
`floor(beta) > installs(beta)` tripwire.

### 6.1 Commands

```
# full suite, sqlite — 1713 tests, ~13 s, 62 SILENT SKIPS
DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
  uv run python -m unittest discover tests/

# the one to actually use for this plan — 1713 tests, 0 skips
docker run -d --rm --name clouseau_test_pg \
  -e POSTGRES_USER=clouseau -e POSTGRES_PASSWORD=passwd -e POSTGRES_DB=clouseau_test \
  -p 55432:5432 postgres
DATABASE_URL=postgresql://clouseau:passwd@localhost:55432/clouseau_test \
  REDIS_URL=redis://localhost:6379/0 uv run python -m unittest discover tests/
```

Without a Postgres `DATABASE_URL`, **62 tests skip silently — exactly the tables this plan
touches**: 12 `filed_bug` JSONB (autofile), 14 round-trip/cascade (persistence), 17
round-trip (heartbeat + reaper), 11 candidate selection + 3 `SweepMark` (sweep), 5
selection-log round-trip. The repo documents two conflicting recipes (port 55432 /
`postgres` / db `clouseau_test`; port 5433 / `postgres:16`); either works for all of them —
**normalise to one line in `README.md`.** And `README.md:29`'s documented command omits the
env, which makes three modules fail at IMPORT (`sqlalchemy.exc.ArgumentError: Could not
parse SQLAlchemy URL`) and **135 tests never load**.

### 6.2 Existing tests that change

| test | change |
|---|---|
| `tests/test_agent.py:5`, `test_agent_schema.py:6`, `test_agent_tools.py:5` | add `os.environ.setdefault("DATABASE_URL", "sqlite://")` like the other 30 modules; they document the env in a comment and never set it. Fix `README.md:29`. **Land this first — a beta contributor's first `uv run` otherwise reports 3 errors and silently skips 135 tests.** |
| `tests/test_selection_log.py:26-30` `test_shipped_values` | **add the beta knobs** (`installs` 6, `floor` 10, `ratio` 3, `protos` 20) so a retune is visible in a test, as it already is for nightly. |
| `tests/test_selection_log.py:32-47` `test_maturity_bar_applies_to_nightly_only` | unchanged while `floor` stays 10. **If the floor is ever lowered below 6, delete the tripwire in the SAME diff and replace it with a positive test of whatever the new relation guarantees** — do not weaken it in place. |
| `tests/test_autofile.py:261,268` | must stay **GREEN byte-for-byte** under item 21 (legacy `False` → `skip`). If they change, the design is wrong. |
| `tests/test_autofile.py:86,125,1004,1457`; `test_bit_flip_gate.py:235`; `test_bad_machine_gate.py:180,185` | no change needed (all `return_value` or no-arg, hence argument-insensitive). Add `autospec=True` to the ones items 17/20 touch. |
| `tests/test_compiled_out_guard.py` | currently pins that all 20 macros appear in the prompt; **extend to pin the per-channel PARTITION** (item 14). |
| `tests/test_prompt_budget.py:40,80-86` | add a beta `_PLAIN` and `_HANG` fixture and their measured bytes to `_MEASURED` (item 25). |
| `tests/test_product_wiring.py:1901-1908` | `_bug_version("beta") == "unspecified"` stays (kill #6); **add an assertion that `_PROVENANCE` names the channel** (item 5), which nothing asserts today. |
| `tests/test_product_wiring.py:1157` `test_searchfox_tree_by_channel` | already asserts beta→`firefox-beta`; **extend to the agent-side map** after item 13 so `html._SF_TREE` and `SearchfoxCtx` cannot drift. |
| `tests/test_sweep_untriaged.py:302-313` | still passes with beta enabled, so it is **not** a tripwire for starvation; add T8 below. |
| `tests/test_corroboration_registry.py:283` | add `candidate_arrived_by_merge` to the registry (item 23); if it ends up reader-less, add it to `TestWriteOnlyFlagsAreADecision.EXPECTED` with a reason. |

### 6.3 New tests

**T1 · `tests/test_beta_channel_wiring.py`** (sqlite).
`test_every_socorro_query_uses_get_search_channel` — for each of the six sites in item 4,
patch `socorro.SuperSearch` and assert the `release_channel` param is `["beta","aurora"]`
for channel `"beta"`. **There is no test anywhere for `get_search_channel` today.**
`test_every_mcp_context_gets_the_seed_channel` — all FIVE contexts (`PatchCtx`,
`HistoryCtx`, `SourceCtx`, `SocorroCtx`, `SearchfoxCtx`) receive `seed["channel"]` in both
`triage.build_options` and `second_opinion.build_options`, so a future context added
without it is caught.
`test_build_seed_refuses_an_unknown_channel` — `build_seed` must not silently default to
`"nightly"`.

**T2 · `tests/test_beta_selection.py`** (sqlite).
`TestGetLastVersionsAcrossTheMerge` — table-driven over the three real row sets: day after
merge (`155.0b1 / 154.0b10 / 154.0b9`), after the respin (`155.0b1 / 155.0b1`), mid-cycle
(`155.0b4 / b3 / b2`). Asserts a **non-empty** window in all three (item 7). The first row
set is the one that returns `[]` today.
`TestNoUserBuildIsNotABaseline` — the `evaluate_days` shapes measured on the real data: a
3-build window whose index-1 predecessor has count 0 and a per-build total below the floor
must NOT select at `count >= 1`; the same window with a real predecessor must; and the
newest-day-fresh shape (`idx=2 count=8 baseline=[280] → not_spiking`) must still decline.
Asserts the `Selection` log records the drop (item 8).
`TestShiftPerChannel` — `shift` is 3 on nightly and 1 on beta. Nothing exercises `shift`
today (`grep -rn shift tests/` finds only an unrelated comment at
`test_tasks_view.py:336`).

**T3 · `tests/test_beta_windows.py`** (**Postgres-gated**).
Fixture: `154.0b9`, `154.0b10`, the merge-day build and the shipped b1, with real revisions
and pushdates, plus one `nodes` row carrying the merge push's pushdate.
`test_selection_window_is_non_empty_after_the_merge` (item 7);
`test_on_stack_mindate_excludes_the_merge_push` — `get_pushdate_before(shipped_b1)` equals
the merge-day build's pushdate, so `Changeset.find`'s `mindate` is one second above every
merge changeset (item 9);
`test_off_stack_window_is_the_uplifts` — `get_two_last(shipped_b1)` == `[merge-day build,
shipped b1]` (item 9);
`test_removing_the_merge_day_build_widens_both_windows` — **the test that documents WHY the
row must stay**: delete it and assert the previous two now span the previous cycle's last
build.

**T4 · `tests/test_beta_merge_push.py`** (**Postgres-gated**).
`test_a_merge_push_creates_nodes_but_no_changesets` — a synthetic push with one
`parents>1` changeset plus 500 members yields 501 `nodes` rows, 0 `changesets` rows, and
`Build.put_data` still finds the merge-day revision (item 30).
`test_an_ordinary_push_is_untouched` — a 6-changeset push keeps its `Changeset` rows.
Docstring carries the denominator: 5 of 2,356 beta pushes over 126 days contain a merge
changeset.

**T5 · `tests/test_patch_fetch.py`** (sqlite).
`test_raw_rev_goes_through_net_get` — `patch.parse` must not call `requests.get`; assert
the allowlisted UA is present.
`test_empty_parse_does_not_mark_analysed` — `Changeset.add_analyzis({}, nodeid, channel)`
leaves `analyzed=False` (item 1). Asserts the client and the guard, not the network.

**T6 · `tests/test_beta_autofile.py`** (**Postgres-gated** for the `filed_bug` JSONB half).
`test_default_and_nightly_are_identical` — `get_agent_autofile() ==
get_agent_autofile("nightly")` and both equal the shipped defaults (item 17).
`test_every_overlay_key_is_a_real_key` — every key in every `channels.<ch>` overlay is a
key the returned dict actually has, so a typo like `comment_on_exisitng` **FAILS** rather
than becoming a silent no-op. This is the `_ENUM_ADDITIONS` / `blocks`-discarded-on-create
failure mode.
`test_env_kill_switch_beats_the_overlay` — `AUTOFILE_BUGS=0` wins over
`channels.beta.enabled: true`.
`test_unknown_channel_files_nothing` — `autofile_bug` refuses a channel with no autofile
config (item 18).
`test_a_forced_retrigger_on_beta_files_nothing_while_beta_is_unarmed` — the
`force=True` path (item 18); this is the one that would have caught the tasks.html hole.
`test_beta_never_comments` — a beta dossier at rung 70 with an open venue bug produces
`comments == []` in **every** mode (item 21).
`test_beta_withheld_files_restricted_past_an_open_public_bug` — beta + `is_withheld` + an
open public bug ⇒ a NEW restricted bug, not a skip (item 22).
`test_already_filed_for_signature_blocks_the_second_bug` — a nightly `filed_bug` row for
signature S whose bug is now RESOLVED INVALID ⇒ a beta run on S files nothing. Fixture
shape from the four measured cases (2060922 DUPLICATE, 2061726 INVALID, 2063364 INVALID,
2064066 WORKSFORME) (item 19).
`test_daily_cap_is_per_channel` — 10 nightly filings do not block a beta filing and vice
versa, and `filed_bugs_since` counts only rows whose `filed_bug.filed` is true (item 20).
`test_result_carries_channel_and_buildid` (item 20).

**T7 · `tests/test_beta_compiled_out.py`** (sqlite).
`test_channel_partition` — on beta, `NIGHTLY_BUILD` / `EARLY_BETA_OR_EARLIER` sit on the
"genuinely off" side and `RELEASE_OR_BETA` on the "ON, never conclude off" side; the
inverse on nightly.
`test_is_build_flag_ground_inverts_per_channel` — the two real note strings: "behind
`#ifdef NIGHTLY_BUILD`, not compiled into this build" must BIND on beta and UNBIND on
nightly; "this `#ifdef RELEASE_OR_BETA` code is not in the build" the reverse.
`test_diagnostic_assert_keys_on_the_raw_channel` — `aurora` ⇒ ON, `beta` ⇒ OFF.
`test_guard_macros_can_find_a_nightly_build_hollow_symbol_on_beta` (item 14).

**T8 · `tests/test_beta_sweep_and_dedup.py`** (**Postgres-gated**).
`test_a_nightly_dossier_does_not_close_a_beta_cluster` and its mirror
`test_a_beta_dossier_does_not_close_a_nightly_cluster` — the second is the one that
protects desktop coverage (item 10).
`test_sweep_cap_is_per_channel` — three beta candidates in a tick do not starve the
nightly ones (item 28).

**T9 · `tests/test_beta_pushdate.py`** (sqlite, network mocked).
`test_pushdate_prefers_central_then_falls_back` — a node present on central returns the
central pushdate; a graft present only on beta returns the beta pushdate; **both are
stored.** Fixture from the measured pair (`f44045181a24`: beta `2026-08-13T14:15:59` /
central `2026-07-21T09:46:48`).
`test_candidate_landed_uses_the_central_date` — `_bug_for_this_regression`'s 30-day window
is computed from the central date (item 12).

**T10 · `tests/test_shipped_channels.py`** (sqlite).
`test_shipped_agent_channels` — `config.get_agent_channels()` equals the value we mean to
run; `config.get_agent_autofile("beta")["enabled"]` equals what we mean to run. **This is
the forcing function the suite lacks**: today a beta extension can land with no test in
the diff, and no test tells a reader what channels prod actually triages. Model it on
`test_the_shipped_table_is_the_full_arm` (`tests/test_phase2_calibration.py:441`) and
`test_shipped_values`.

---

## 7. Contradictions between the recon reports, resolved

**1. Is off-stack seeding live? — CORRECTED, and it changes two reports' conclusions.**
Two reports asserted "`agent.offstack.enabled` is false today, so none of this is live" and
built beta conclusions on it. They read `config/global.json`. Prod has
**`OFFSTACK_ENABLED=1` and `OFFSTACK_OBSERVE_ONLY=0`** (`heroku config -a
crash-clouseau-augmented`, re-verified 2026-08-25 for this document) and `config.py:556,561`
are `_env_bool` overrides. **Believe the env.** Consequences: (a) a beta crash with 0
on-stack candidates does NOT vanish — `build_seed` seeds it off-stack, so the claim "beta
would analyse only 10-15% of its crashes and silently drop the rest" is refuted for prod;
(b) `_offstack_candidates` WILL run on beta the moment beta is an agent channel, and it can
emit actions; (c) **the beta in-cycle window is the best off-stack window in the project**
(median 21 / max 42 candidate-bearing changesets, 93.2% approved uplifts + backouts, median
1 file each, every one carrying a description the agent can rank on — against nightly's
3-day off-stack window of 266, already over the 150 cap), so beta may be where off-stack
finally pays off; (d) the off-stack share of beta runs is **the largest unmeasured cost on
the canary.**

**2. Does removing the merge-day build fix the merge blackout? — NO. Two defects.**
One report proposed excluding zero-crash builds and said "that single rule fixes both the
dead window and the amputation". Another **replayed the counterfactual**: with the no-user
build removed, the blackout shifts to the shipped b1 and shortens to 2 days each (10 of 127
run-days instead of 12). **Believe the replay** — `len(res) >= 2` still needs two builds of
the new major, which after the merge means waiting for b1 *and* b2. Items 7 and 8 are
independent and both are required.

**3. Is the merge push in the candidate window? — It depends on which window, and one
report used the wrong predecessor.**
Report claims: (a) "the merge push is excluded from every candidate window a beta crash can
reach" (`mindate` = prev build pushdate + 1s); (b) "on the first beta build of a cycle the
pushlog window is 5,192 changesets"; (c) "`Build.get_two_last` bounds a window spanning the
whole merge push, so `pushlog_for_revs` returns ~5,000-7,000 changesets".
Resolution: **(a) is right and (b)/(c) are computed against the wrong predecessor.** (b)
measured `154.0b10 → 20260817142839`, i.e. it *skipped* the merge-day build — but the
merge-day build IS in `builds` (Buildhub carries it, and it is the row that flips
`get_last_versions` to `[]`). (c) reads `get_two_last` as if it had a major-version break;
it does not (`models.py:542-566` is a plain `limit(2)`), so for the shipped b1 it returns
`[merge-day build, b1]` and the window is the 46-122 uplifts. **Both wrong claims become
TRUE if the merge-day `builds` row goes missing** — which is exactly what naively
implementing item 30 or item 8 would do. Hence items 9 and 30's `files: []` design. This is
the resolution that matters most in the whole document.

**4. Is the stale-signature gate wrong on beta? — Its mechanism is real; its impact is 4%
of selections; its 88% firing rate is mostly CORRECT. Do not retune it.**
One report argued the gate mis-fires on beta merge regressions "and only on the ones with
the most nightly evidence", because `landed_after = pushdate(beta) − first_seen` uses the
merge date. Another **measured** the population: 84% of beta selections sit on signatures
long-lived on beta (median beta first-seen 393 d; median TRUE first-seen 1,194 d), so the
gate's 88% firing rate is largely right.
Both are true and they compose, and the arithmetic settles it. For the **84% long-lived**
class, `landed_after` is ~372 days with the central clock and ~393 with the beta clock —
**both fire, the clock fix changes nothing.** For the **12% new-anywhere** class,
`landed_after` is ~0 with the beta clock and negative with the central one — **both silent,
the clock fix changes nothing.** The clock fix only changes the **4% (3 of 77) "new on
beta, long-lived on nightly"** class, where the beta clock gives +14 to +766 days (fires)
and the central clock gives a negative (silent). **So: fix the clock as a correctness item
(item 12 — the printed `landed=` date is simply false and it also feeds
`_bug_for_this_regression`'s venue window), and do NOT touch `min_age_days` or
`other_channel_floor`** (kill #11).

**5. How much does the cross-channel proto dedup cost beta? — 16.5%, not "most".**
One report called it the top finding ("most beta crashes will be skipped before
`build_seed` ever runs"). Another measured it: of the 224 beta (signature, proto) clusters
behind the 40 selected pairs, **37 = 16.5%** also occur verbatim on nightly over 60 days,
and the real gate additionally requires a `status=done` dossier, so that is an upper bound.
**Believe the measurement.** The fix is still required — for the *other* direction, beta
closing a nightly cluster (item 10).

**6. Beta selection volume: 3.5/day or 2.31/day? — Both, and they agree.**
Two independent emulators produced 105 and 104 distinct (sig,bid) pairs over the same 30
run-days. One reported 105/30 = 3.5/day; the other dropped day 1's 37 selections as a
cold-start artefact and reported 67/29 = 2.31/day. **Use 2.31 as the steady-state figure
and 3.5 as the including-cold-start figure — and budget for the cold start, because a real
prod switch-on genuinely produces ~37 selections at once on its first run-day.**

**7. The nightly control: 102.6 or 144.6 new pairs/run-day? — Report the range.**
Two emulators, different windows (29 run-days vs 7) and slightly different
reimplementations, both calibrated against the same remembered prod baseline of 85-120
dossiers/day. **102.6-144.6.** The beta:nightly ratio is 0.9-1.3% of pairs with item 8, or
1.6-3.4% without.

**8. `_fixed_after_build_bug` on beta: "fires less" or "no difference"? — Both refute the
worry; report both.**
Instrument A (200 (sig, build) pairs, the same control shape as the nightly measurement):
**1.0% beta vs 6.0% nightly in domain.** Instrument B (77 emulated selections): **3% beta
vs 2% nightly.** They disagree on direction and agree on the conclusion that matters: it is
**not** a mass suppressor on beta. Keep the gate; paste both numbers into its DOMAIN
paragraph (kill #9).

**9. The beta cycle length: 17-35 days, or 24-35? — 24-35.**
One report listed cycle lengths "17/28/35/24 d". Two others measured merge-to-merge from
the first `N.0b1` build (28.04 / 27.85 / 35.34 / 23.76) and from central cycles (24-35).
**Believe the two that agree**; the 17 is a window-truncation artefact. No 2-week cadence
exists in the data.

**10. Is the beta on-stack candidate set really median 0? — Directionally yes, but the rate
is measured on the wrong population.**
The 85-90% zero-candidate rate comes from top-volume signatures by install cardinality
(OOM / shutdownhang / GC, whose stacks sit in ubiquitous paths), **not** from the
spike-selected population. The selected population is 36 code / 21 OOM / 6
AsyncShutdownTimeout / 4 shutdownhang out of 67, i.e. ~54% code signatures. **Treat "the
in-cycle window is starved" as strongly indicated, not as a rate.** The canary measures it
for free: count `useless=True` and "agent: no scored changesets" per channel (§9 phase 3).

**11. Hardware gates: "needs tuning" or "transfers"? — Different gates.**
One report reasoned about triggers (1) and (2) (the per-report singleton path, which is
inert on beta because `max_reports = 1`); another measured trigger (3)
(`_signature_is_mostly_hardware`, 17% beta vs 14% nightly). No conflict — both conclude
"do not retune". Recorded as facts in §2.6 and §4, not as work.

---

## 8. Kill list — measured wrong, or already refuted. Do not rebuild these.

1. ~~"Exclude the zero-crash merge-day build and the merge blackout is fixed too."~~
   Replayed counterfactual: the blackout shifts to the shipped b1 and shortens from
   2/2/2/2/4 to 2 days each (10 of 127 run-days). Two independent defects, items 7 and 8.
2. ~~"Drop the merge push at ingestion — don't create nodes/changesets for it."~~
   `Build.put_data` inserts a `builds` row only `if rev in revs_c` (`models.py:523`) and the
   merge-day build's revision is a member of the merge push, so this **deletes the row that
   bounds the on-stack and off-stack windows.** Do the `files: []` variant (item 30).
3. ~~"Fix `buildhub.VERSION_PATS['beta']` — it drops legitimate builds."~~
   The 26-30 RC/dot-release builds it drops report `release_channel=release` in Socorro and
   **never** beta/aurora (20260812182057 = 154.0: 72,019 release / 0 beta; 20260715202819 =
   153.0: 105,097 release / 0 beta), and they cost 701 of 44,488 reports = **1.6%**. If they
   DID get `builds` rows their major would trip `get_last_versions`' break mid-cycle and add
   more dead days. The regexp is protecting the window. Add the reason to its comment;
   change nothing.
4. ~~"Beta needs a `devedition` product in Buildhub, or a separate build source."~~
   There is **no `source.product: devedition` document at all** (0 for beta, aurora and
   nightly). DevEdition is `source.product=firefox` + `target.channel=aurora` with the
   **same `build.id` and `source.revision`**: 58 of 59 buildids shared with identical
   revisions since 2026-04-01 (one beta-only, one aurora-only respin), 76 of 77 over 200
   days, and 90.75-95.34% of aurora reports sit on a buildid the `builds` table holds. So
   fetching `target.channel=beta` covers DevEdition and `put_crashes` can resolve an aurora
   crash. The only residue is the aurora-only respin buildid.
5. ~~"`git2hg` will map a beta frame to central, and `inspect_stacktrace`'s
   `node != build_node` guard will throw the whole stack away."~~
   Verified on two real 155.0b2 crashes (one `beta`, one `aurora`): **every in-tree frame
   carries `git:github.com/mozilla-firefox/firefox:<path>:7335c569dfad…` which resolves to
   `485039f30ace`, exactly Buildhub's revision for 20260819090452**, so the guard passes and
   beta stacks are not silently discarded. **This is the one thing that would have killed
   beta outright and it is invisible from the code.** Record it durably.
6. ~~"`_bug_version` must become `Firefox 155` for beta."~~
   `unspecified` is right and matches human practice: of 273 human-filed bugs with summary
   `Crash in [@` since 2026-05-01, version is `unspecified` on **184 (67%)**, `Trunk` on 63
   (23%), `Firefox NNN` on 14 (5%); of the 8 Firefox-side crash bugs whose comments name a
   beta build string, **7 of 8 are `unspecified`** (the eighth is Thunderbird). BMO would
   *accept* `Firefox 155` (`Firefox 141`..`156`, `Trunk` and `unspecified` are all
   `is_active` in all five products `resolve_product_component` can return) — it is just not
   the convention, and **Socorro's own file-a-bug link sets no `version` at all** (the
   scraped `enter_bug.cgi` query for a beta crash carries bug_type / keywords / product /
   op_sys / rep_platform / `cf_crash_signature` / short_desc / comment and no `version`).
   `tests/test_product_wiring.py:1901-1908` already pins it. If the beta major should be
   visible, put it in the prose (`build_stats_sentence` already prints `155.0b2`).
7. ~~"Set `cf_status_firefoxNNN: affected` on a beta filing, or use a `[beta]`
   whiteboard."~~
   `release-mgmt-account-bot@mozilla.tld` is the **first setter in 5 of 5 sampled histories**
   of our own filings, 12 minutes to a few hours after filing (2060920 and 2060922 at
   12:30, 2060924 at 23:44 setting firefox155=affected plus esr140/esr153/153/154=unaffected,
   2061124, 2061691) — 30 of our 60 filings already carry one. It fired on a human beta crash
   bug (2062639, created 14:45, flags set 15:44) **with `version: unspecified`, so it does not
   need a version.** Asserting it would duplicate a bot and be wrong whenever the crash is not
   actually a 155 regression. Whiteboard is empty on all 8 sampled human beta crash bugs and on
   57 of our 60 (the 3 exceptions are team tags a human added).
8. ~~"Beta has more third-party-injected-DLL crashes than nightly."~~
   Refuted by its own denominator, twice. The apparent **3.0x** (276/45,703 = 0.60% beta vs
   60/29,341 = 0.20% nightly) was **facet truncation** — nightly's 3,000-term
   `modules_in_stack` facet is dominated by Linux libs that push blocklisted DLLs out. The
   exact `~`-operator union over all **165** in-tree blocklist names (parsed from
   `toolkit/xre/dllservices/mozglue/WindowsDllBlocklistDefs.in`) reads beta 333 reports =
   **0.87% of Windows** vs nightly 153 = **0.81% of Windows**; the whole-channel gap is beta
   being 83.5% Windows against nightly's 64.0%. Third instrument, signature-head class,
   Windows-only, **install**-denominated: beta 0.97% of installs vs **nightly 7.11%**,
   nightly's excess being GPU drivers (6.45% vs 0.22%, over 227 signatures). **If anything
   wants a third-party gate it is nightly's GPU-driver class, not beta.**
9. ~~"`_fixed_after_build_bug` will fire much harder on beta because a beta build lags m-c
   by weeks."~~ 1.0% vs 6.0% in domain (n=200 vs 414) by one instrument, 3% vs 2% (n=77 vs
   120) by another. Not a mass suppressor. Keep the gate unchanged.
10. ~~"The hardware-noise thresholds need retuning for beta."~~ 17% vs 14% suppression;
    both thresholds sit in empty space (`bit_flip_rate` p90 0.502 against a 0.2 bar clears
    10/77; `broken_cpu_rate` p75 0.038 against a 0.7 bar clears 3/77);
    `min_signature_reports = 5` is inert on beta. §4.
11. ~~"Disable the stale-signature gate on beta, or raise `other_channel_floor`."~~
    84% of beta selections sit on signatures long-lived ON BETA (median beta first-seen 393
    d; median TRUE first-seen 1,194 d; only 6 of 76 ≤ 7 d) and only 12% are new anywhere, so
    the 88% firing rate is **mostly correct**. Raising the floor is worse than useless: 87%
    of beta selections clear 20 with a **median of 2,892** off-beta reports (max 1,041,241)
    against nightly's 52% and median 33, so no count can discriminate. The class where the
    gate is genuinely wrong is **3 of 77 = 4%**, and the fix is the pushdate clock (item 12)
    plus the two-age brief (item 15).
12. ~~"Most beta crashes will be skipped by the cross-channel proto dedup."~~ 16.5% upper
    bound (37 of 224 clusters), and the real gate also needs a `status=done` dossier. The fix
    is still needed for the opposite direction.
13. ~~"On beta the off-stack path hands the model 150 of 5,129 changesets."~~
    `Build.get_two_last` has no major-version break, so for a shipped build it returns
    `[merge-day build, this build]` and the window is the 45-122 uplifts. The
    5,000-changeset window belongs to the merge-day build alone, which ships to 0-7
    installs against an install threshold of 6. `max_candidates` 150 is 3.6x above the
    measured in-cycle maximum of 42 and never binds. (It DOES become real if the merge-day
    `builds` row goes missing — hence item 9.)
14. ~~"`_split_by_application` needs an Android/GeckoView exclusion before beta."~~
    **0 of 58** and **0 of 45** beta signatures have an Android/GeckoView product as their
    ONLY venue; both Android bugs sit on signatures that also have a desktop venue, so the
    exclusion would change **0 skips**. Revisit with plan #16, where the same map is the
    actual blocker.
15. ~~"Beta needs a `CHANNEL_TYPE` migration / an `_ENUM_ADDITIONS` entry."~~
    `CHANNEL_TYPE = db.Enum(*config.get_channels(), name="CHANNEL_TYPE")` (`models.py:18`)
    and config has listed nightly/beta/release since commit `3695614` (2018-02-24), so every
    DB ever built by `models.create()` already accepts `'beta'` in `lastdate.channel`,
    `nodes.channel` and `builds.channel` — verified against a disposable Postgres (`pg_enum`
    = `['nightly','beta','release']`). **No migration.** The `_ensure_enum_values` ALTER
    defect (reproduced live) is irrelevant here and `DEPLOY.md:8`'s claim about it is still
    wrong, but that is plan #16's item, not this one.
16. ~~"Maturity has to come back for beta."~~ `get_maturity_bar` returns `(None, 1)` off
    nightly (verified by execution) and that is right for a reason the docstring does not
    give: nightly's bar prices a 21-DAY window while beta's is 3 BUILDS = 4-7 days, almost
    exactly beta's arrival interval (154.0b6 4.03 d / 77.1%, 155.0b1 3.86 d / 90.2%,
    155.0b2 5.0 d / 96.2%). The one apparent exception (154.0b10: 1.25 d / 11.0%) is item
    7's blackout, not a maturity bug. **If the 3-build window is ever widened, the bar must
    come back with it**, and `mature_installs` must stay inert against beta's higher install
    threshold.
17. ~~"The 2-week beta cycle is already here."~~ Not in the data as of 2026-08-24:
    merge-to-merge **28.04 / 27.85 / 35.34 / 23.76 days** (mean 28.7, median 28.0), or
    28/28/35/26 using the shipped b1; central cycles 24-35 days. **Every 2-week figure in
    this plan is arithmetic on the observed 3.2-3.5 builds/week.**
18. ~~"`agent.offstack.enabled` is false, so off-stack is not a beta concern."~~ Prod has
    `OFFSTACK_ENABLED=1` and `OFFSTACK_OBSERVE_ONLY=0`. Contradiction 1.
19. ~~"A 2-week cycle needs a different in-cycle candidate window."~~ The in-cycle window is
    driven by BUILD CADENCE, not cycle length: the compressed v155 cycle's windows are
    47/46/76/61 changesets, indistinguishable from the 4-week cycles' median of 61 (n=47).
    What a 2-week cycle changes is that **the merge's share of beta builds roughly doubles
    (4 of 51 pairs = 7.8% → ~1 in 6 = ~17%) and each merge halves** (~3,050 changesets /
    ~1,170 candidate-bearing, projected from the measured 218 changesets/day and 83
    interesting/day; the empirical half-window gives candidate median 11.5 vs 28.5). **So
    item 30 gets MORE load-bearing, not less.**
20. ~~"Build the merge-window candidate set now."~~ It is reachable in principle and not a
    reasoning blocker — median 28.5 file-intersected candidates, median 3.5 scoring >0,
    median 3.0 scoring ≥5, median 1.0 at the max score of 10, and ~12-14 candidates under a
    2-week cycle. But it is unreachable today for **three independent measured reasons** and
    nothing about the beta launch depends on it, so building it now would be speculative.
    §11.
21. ~~"Widen `nightly_window_ndays` / the beta window because beta users are on older
    builds."~~ Not measured for beta and explicitly resisted for Fenix on the analogous
    argument (plan #16 §5). Beta's window self-limits: a build stays testable 3.9-5.0 days
    and holds 77-96% of its crashes by then.

---

## 9. Rollout

The two levers are of different kinds and that decides the order. `INGEST_CHANNELS` is an
**env var** (`heroku config:set`, effective on the next 20-minute tick, no deploy).
`agent.channels` is **config-file only** — the one canary flag of ~14 with no env override —
so enabling beta triage requires a **deploy** until item 28 lands. `AUTOFILE_BUGS=1` is
already live and channel-blind, so **beta triage and beta filing cannot be separated
without items 17 and 18.**

### Phase 0 — the desktop fixes (items 1-5) · *ships regardless of beta*
Land, deploy, verify. **Exit:** `patch.parse` 406s go to zero (count `add_analyzis`
refusals of an empty parse); no report is re-picked after `set_error`; the three test
modules load without a manual env.
*Note honestly: item 4 has no visible nightly effect (nightly has no `aurora`), so its value
is beta-only even though the change is channel-agnostic. Items 1-3 are desktop value.*

### Phase 1 — the gates, before any beta row can exist (items 17, 18, 19, 20, 27, 28)
Deploy with `agent.autofile.channels.beta.enabled: false` and `agent.channels` still
`["nightly"]`. **Exit:** T6 proves a beta dossier at rung 70 files nothing while beta is
unarmed, *including through the `force=True` retrigger path*; `heroku config` still has
`INGEST_CHANNELS=nightly`; `AGENT_CHANNELS` exists as a kill switch.

### Phase 2 — the windows and the selector, still no beta ingestion (items 6-11, 23, 26)
**Free smoke test, no Heroku change:** `docker-compose up` sets no `INGEST_CHANNELS` and its
entrypoint runs `bin/init.py`, which calls `update.update_all()` — so a local run already
ingests nightly+beta+release and exercises the beta branch of `get_builds`, `put_filelog`
against `releases/mozilla-beta`, and the beta Buildhub regexp. **Exit:** T2/T3/T4 pass; the
local run produces beta `builds` rows across a synthetic rollover; no `"No buildids for
Firefox-beta"` in the local log.

### Phase 3 — ingest-only canary · **zero LLM spend, zero Bugzilla risk, no deploy**
```
heroku config:set INGEST_CHANNELS="nightly beta" -a crash-clouseau-augmented
```
`agent.channels` stays `["nightly"]`, so `enqueue_agent` drops every beta uuid
(`orchestrator.py:3771-3774`) and `UUID.untriaged(channels=["nightly"])` never sweeps them.
**Set it explicitly to `"nightly beta"` — never clear the variable, because
`os.getenv("INGEST_CHANNELS","").split() or config.get_channels()` turns RELEASE on too.**
Watch, all free:
* `reports.html?channel=beta` — scored beta crashes appear. (Trailing the newest build by
  1-2 days is normal; see `scored-page-lag-is-normal`.)
* `selection.html` — the beta outcome mix: `selected` / `below_install_threshold` /
  `untestable_prefix` / `not_spiking` / the new `dropped_no_users`. Expected shape on a
  typical mid-cycle day: selected ~3, below_install_threshold ~258, untestable_prefix ~33,
  not_spiking ~1,650.
* The `"No buildids for Firefox-beta"` warning count — **must be ~0** after item 7.
* The two-column `last_node` vs `last_uuid_row` SQL from `ingestion-stall-has-no-alarm`,
  **per channel** (the uuid clock lies by hours; the node clock is the honest one).
* `useless=True` and `"agent: no scored changesets"` **counted per channel** — this is the
  free measurement of contradiction 10, and it is the number that decides whether the
  off-stack path is carrying beta or not.
* The `patch.parse` backlog depth around the first merge — item 30's exit criterion.
* `builds` row count for `product='Firefox', channel='beta'` after the first tick — item 6's
  exit criterion (should be ~13-15 builds, not 1-2).
**Measure for at least two weeks INCLUDING one merge.** Expected: **1.33 new (sig,bid)
pairs/day, 7.6 UUIDs/day, ~40 pairs per 30 days**, plus a ~37-selection cold start on day
1. **If the observed rate is 3.5 pairs/day, item 8 did not land.** If it is 0 for two
consecutive days mid-cycle, item 7 did not land.
**Kill switch:** `heroku config:set INGEST_CHANNELS=nightly`. Beta rows already written stay
(harmless — nothing reads them without `agent.channels`).

### Phase 4 — triage on, filing still held (items 12-16, 24, 25, 29; then `agent.channels`)
Deploy the LLM-half items first, then flip `agent.channels` (or set `AGENT_CHANNELS="nightly
beta"` once item 28 has landed, so this stops needing a deploy).
`agent.autofile.channels.beta.enabled` stays **false**.
Watch: dossiers/day and $/day **per channel**; the verdict distribution; **which gate moved
each verdict**, with the stale-gate-fires-on-88% prediction checked explicitly; the
off-stack share of beta runs (the largest unmeasured cost); the SO corroboration rate on
beta; the `compiled_out_*` firing rate on beta (it was 0/197 on nightly — do not assume).
Expected: 4.2-5.8 dossiers/day, $4-17/day. **Budget for higher**, because beta's off-stack
share is unknown and off-stack has its own (higher) cost cap.
**Kill switches at this phase:** `AGENT_CHANNELS=nightly` (new), `AUTOFILE_BUGS=0`
(global), `OFFSTACK_OBSERVE_ONLY=1` (global), `SECOND_OPINION_ENABLED=0` (global),
`AGENT_SWEEP=0`.

### Phase 5 — arm beta filing at `skip` (items 21 at `skip`, 22)
`agent.autofile.channels.beta = {"enabled": true, "comment_on_existing": "skip",
"daily_cap": 3}`.
**Arm only if** Phase 4's beta second-opinion corroboration rate is no worse than nightly's
baseline **and** at least 3 verdicts were measurable — plan #16's Phase-6 rule.
Expected **one bug every 50-125 days**, so **the first filing is an event to inspect by
hand, not a rate to monitor.** Read it end to end: the crash count (item 4), the provenance
sentence (item 5), the absence of a `p_worth` sentence (item 27), the `regression` keyword
and `regressed_by` (item 23), the `landed=` date in the analysis (item 12).

### Phase 6 — decide `file_new` (item 21 at `file_new`)
Requires item 19 shipped and exercised. Expected ×2.4 volume. Before flipping, replay the
existing filings against the new venue logic and hand-inspect every filing that *changes*
— this is a precision-for-recall trade and the failure mode is a near-duplicate on BMO.

### Irreversible steps, called out
* **The first beta Bugzilla write** (Phase 5). Everything before it is recoverable.
* **`nodes` rows for beta** are prunable (`Node.clean` at `max_ndays` = 30) but the
  duplicated merge-push rows are not worth a manual sweep — item 30 prevents the next one.
* **`_ENUM_ADDITIONS` / `ALTER TYPE`** is one-way, and **is not needed here** (kill #15).
  Do not touch it.

---

## 10. Open questions — decisions the user has to make

1. **`skip` or `file_new`, and when?** `skip` is what beta ships with in Phase 5 and it
   suppresses **58-59% of beta signatures** — including, until item 22, the poison crashes
   :mccr8 said must always be filed. `file_new` is the literal requirement and needs item 19
   first. Measured: `skip` → ~1 bug every 50-125 days; `file_new` → ~1 every 21-53 days.
   **Recommendation: `skip` for Phase 5, `file_new` at Phase 6 once item 19 has been
   exercised.**
2. **Which burst fix — item 8, or `shift 1→2` with `n 3→6`?** Both are measured at the
   deployed cadence and both land at ~1.3 pairs/day (item 8: 1.33; shift/window: 1.28), and
   the shift/window option needs no new plumbing. **Recommendation: item 8, because it
   removes the cause (a build with no users acting as a baseline in a users-based detector)
   rather than diluting it, and because raising `shift` reduces the number of testable
   build-days.** But they overlap, so if item 8's measured effect on the canary is smaller
   than the replay predicts, the shift/window change is the priced fallback — **re-measure,
   do not stack them blind.**
3. **Is `OOM | *` in scope on beta at all?** This, not third-party DLLs, is what is
   genuinely beta-specific: `OOM | *` is **41.75% of beta reports (19,089/45,726) vs 18.03%
   of nightly's, and 64.6% of beta's OOM is 32-bit x86 vs 3.7% of nightly's** (beta is 41.9%
   x86 against nightly's 1.6%). It is **21 of the 67 emulated selections (31%)** and 6 of
   the 10 loudest, and nothing in the repo suppresses it. The class is identified by the
   signature prefix, so no threshold is fitted. **But measure the share AFTER item 8** — the
   top of the post-merge burst it removes is `OOM | small` (236) and `OOM | unknown |
   js::AutoEnterOOMUnsafeRegion::crash_impl` (125), so much of the 31% may go with it. If a
   residual remains, the choice is suppress / observe-only / accept.
4. **`thresholds.protos.Firefox.beta` 20 → 5?** Fully priced (§4). Not needed for the
   canary at $4-17/day. **Recommendation: decide after Phase 3 measures the real beta
   UUID→dossier yield**, since the 0.55-0.77 figure is a nightly single-window calibration.
5. **Beta's `daily_cap`.** Proposal 3. It is not a throughput constraint (projected
   0.008-0.048 filings/day); it bounds the 48%-in-4-days post-merge burst.
6. **Off-stack on beta: intended?** It is already live in prod (`OFFSTACK_ENABLED=1`,
   `OFFSTACK_OBSERVE_ONLY=0`) and it is what stops a 0-on-stack-candidate beta crash from
   vanishing. It is also the largest unmeasured cost, and the guards are global not
   per-channel. **Decision: leave it on for Phase 3-4 and measure, or set
   `OFFSTACK_OBSERVE_ONLY=1` for beta's first weeks — noting that the flag is GLOBAL, so
   that also disarms nightly's off-stack path.**
7. **Second opinion on beta.** `SECOND_OPINION_ENABLED=1` is global. Beta's SO cost and
   corroboration rate are unmeasured, and the SO is the one calibrated instrument the
   arming decision in Phase 5 rests on. Leave it on and measure it, or accept that Phase 5's
   exit criterion has no instrument.
8. **Fix `nodes`' missing index now or later (item 31)?** It needs explicit idempotent DDL
   in `bin/release.py` on an AUTOCOMMIT connection plus a duplicate sweep — the mechanism
   built to do this kind of thing (`_ensure_enum_values`) provably cannot. Item 30 removes
   the worst case, so **later** is defensible.
9. **`severity` on filed bugs.** 28 of our 60 filings still sit at `--`, and in **12 of 12
   sampled histories a human component owner set it** (jmathies, jteh, valentin.gosu, …),
   never a bot. Channel-independent, but it bites harder on beta where release management
   filters on S1/S2. **A separate, measurable product decision — not part of this change.**
10. **Does beta justify itself at ~1 bug/month?** The honest read of §2.3 and §2.4: beta's
    crash population is overwhelmingly old signatures at high volume, and the gates that
    hold those back are mostly correct. The upside arguments are the ones the recon supports:
    the in-cycle uplift window is the highest-signal candidate window in the project (93.2%
    plausible regressors, median 21 candidates), an uplift-window regression claim is
    stronger than anything nightly produces, and beta is where off-stack seeding is most
    likely to pay. **This is the decision to make before Phase 4 spends money.**

---

## 11. Explicitly out of scope

* **The `release` channel.** Thresholds exist (`installs` 50, `floor` 50) and nothing about
  them is measured; release is 10% sampled; the same `get_last_versions` blackout applies;
  and `update_all`'s empty default already turns release on if `INGEST_CHANNELS` is cleared
  (item 6). Nothing here should be read as release support.
* **ESR.** Would need a real `CHANNEL_TYPE` enum addition, which needs `_ensure_enum_values`
  fixed first (plan #16 §4.1, reproduced live).
* **The merge-window candidate set as a first-class regression window.** Kill #20 — it is
  reachable in principle and unreachable today for three independent measured reasons, so
  nothing about the launch depends on it. If it is ever wanted, attach it to the **shipped**
  b1, not the merge-day build, and fix the selector first.
* **Batching `Node`/`HGAuthor` inserts** beyond bounding the merge case (item 30). The
  ~32,155 round-trips per merge are a pre-existing shape, not beta's to fix.
* **A beta eval corpus / a `corpus_ship` beta arm.** Required before any beta-specific
  calibration table can be fitted, which is why item 27 leaves `p_worth` empty.
* **Fenix beta** — plan #16 §10 already scopes it out; the mechanism there is different.
* **A channel-relative reformulation of `signature_age.other_channel_floor`.** Measured
  dead as a count on beta (§4); designing the replacement is its own measurement.
* **Making the per-crash cost cap actually abort.** Item 28 names the decision; the
  implementation is a pre-existing gap, not beta's.
* **`severity` on filed bugs** — open question 9.

---

## 12. Operational notes

* **`uv run python bin/predeploy.py` with `DATABASE_URL` set before every deploy.** A
  deploy kills in-flight ~20-minute runs at ~$3 each; two deploys on 08-04 each killed 3-4.
* **Grab logs first.** The web dyno has one gunicorn worker and there is no log drain, so
  `heroku logs -n 1500` is about **2 hours** of history — and the beta blackout's only trace
  today is one warning per 20-minute tick.
* **Redis is Mini / `Persistence: None`.** A restart drops the queue and a lost job leaves
  **no trace**. Beta roughly doubles the traffic on the one serial analysis chain (item 29).
* **A bulk retrigger is a Bugzilla WRITE**, and `enqueue_agent(..., force=True)` bypasses
  the channel gate — which is precisely why item 18 must land before `INGEST_CHANNELS`
  changes. Also remember `_STICKY_PAYLOAD_KEYS`: a re-run that succeeds used to drop
  `payload['filed_bug']` and blind both Bugzilla idempotence keys.
* **`bin/feedback.py` has never run on a schedule** (`bin/schedule.py` has exactly two jobs
  — `update_all` /20 min and `reap_stale_agent_jobs` /15 min — and the Procfile has no
  entry). Any exit criterion phrased as "Bugzilla-verified outcome" depends on a loop that
  is not wired.
* **`archetypes.seed_quietly()` is only called from `bin/init.py`**, which is not a Procfile
  entry point, so `_matching_archetypes` may be returning `[]` on every prod run.
* **Docs to correct in the beta diff.** `docs/architecture.md:98-101` says `update_all` is
  "scoped to the configured channels (`$INGEST_CHANNELS`, default `nightly`)" — the CODE
  default is **all** configured channels (`update.py:233`); nightly-only is a prod config
  var, not a default. `DEPLOY.md:4,15,65,69` describes the app as a "nightly-only,
  observe-only canary", lists "nightly-only (`agent.channels`)" under Cost controls, and
  verifies with `reports.html?channel=nightly`. Both are now actively misleading.
* **Scratch/repro for every number in this document:** `spike/_beta_recon/` (gitignored,
  written by seven agents; the `00`-`31`, `q*`, `C_`, `a_`/`b_`/`c_` prefixes are different
  agents' scripts for the same measurements, which is why several numbers appear twice from
  independent instruments — that redundancy is what resolved §7).

---

## 13. Prior art: the `nightly_vs_beta` escalation study

Lives in a **separate checkout** — `~/dev/mozilla/clouseau-nightly-beta-study`, branch
`nightly_vs_beta`, with `study/` and `reports/` **uncommitted**. The `nightly_vs_beta`
branch in *this* repo is an ancestor of `augmented` and contains no beta study at all.
Read `reports/NIGHTLY_VS_BETA.md` before proposing anything about nightly→beta escalation.

**Surviving findings relevant here.** The crash was usually already in nightly (median 23 d
before the bug existed, 32/40) but only 6 of 57 at ≥3 machines/day for ≥7 days, so the gap
is spent at 1-2 machines/day and is unactionable. The best alarm under 10 alerts/week
(level ≥3 for 2 d) gives 9.3 alerts/week at 12% precision and +2.5 d median lead (1.5 after
data latency), catching 18/40. Windows updates are NOT a cause; third-party software is
(~12%, 7/57). **Filing coverage is COMPLETE at this scale: 0 of 25 genuine nightly→beta
escalations had no bug on file** — which is the strongest independent check on this plan's
~1-bug-per-month projection. **"Nightly is not a smaller beta":** beta 31.0% 32-bit x86 vs
nightly 2.22% (DevEdition 3.3%), 6 of 19 escalating signatures >60% x86 on beta and 3 at
100%, which is why `OOM | large` escalates at merge nearly every cycle (open question 3).
`MOZ_DIAGNOSTIC_ASSERT` is 7.04% nightly / 4.59% beta / 0.00% release — **6.6x louder on
nightly and HALF as likely to escalate** (relevant to item 14). No nightly
metadata/stack needle exists (412 hypotheses, 6 hunters): "quiet on nightly" is
**anti**-predictive, breadth beats height (`nightly_machine_days` AUC 0.914 vs
`nightly_peak` 0.850), and the strongest predictor is beta_peak of cycle V−1 (rho 0.73).
Uplift is not a severity proxy (AUC 0.565 vs crash volume).

**Its five refuted headline claims, each for a measurement reason worth internalising.**
(a) "a different code path in beta" was proto-signature *string equality* defeated by JIT
`@0x` frames, mangling and inlining — compare the **top 10 frames**. (b) "alarms can't beat
humans" was a backtest bug (`seen_before |= set(today)` daily made every `sustained>=2` rule
unfirable) plus scoring over a corpus containing 2004 bugs. (c) "external causes are rare
(3/54)" dated arrivals against the **study window** instead of the retention edge,
fabricating an arrival at the window start for every pre-existing value. (d) "N crashes were
visible in nightly and ignored" used a floor-of-ONE onset and a peak taken over days
**after** filing — windowed to pre-filing only, 16 → 6. (e) "14 escalations were never
filed" searched BMO for `[@ sig]` while BMO also stores `[@ sig ]` **with a trailing space**,
which is what crash-stats' file-a-bug link writes — biased against exactly the bugs filed
FROM Socorro. **This repo had already fixed that same bug in commit `14019f0` and the study
did not inherit it.**

**The durable rules that came out of it, and that this plan tried to obey.** Never publish a
lag computed from data that postdates the decision. Never publish stage medians without
denominators. A fall-through class in a precedence chain inherits everything. And **an
ABSENCE ("nobody filed this") is the one finding that grows MORE confident as the query
grows more broken — verify it with a second, independently-maintained instrument.** That
last rule is why §7 exists: every claim in this document that would have changed a decision
was checked against a second instrument or against the tree.

---

## 14. Implemented 2026-08-25 — and what the live run changed

Items 1-30 are implemented in the working tree (31 and 32 are not: 31 needs idempotent DDL on an
AUTOCOMMIT connection and item 30 removes its worst case; 32 is a measurement, not a change).
While implementing, the real ingestion path was run **against live Socorro / Buildhub /
hg.mozilla.org** on a scratch Postgres (`spike/_beta_recon/e2e_ingest.py`). Three things it
settled:

**1. The merge rule fires exactly as designed, on real data.** `put_filelog("beta")` over the
30-day window logged `merge push at 2026-08-13 14:15:59+00:00: keeping 5130 node(s), extracting 0
patches` — the same 5,130 §1.3 measured, at the same pushdate — and created 0 `changesets` rows
for them. `Build.get_last_versions(n=3)` returned a full window (155.0b4 / b3 / b2), and
`update_builds` with the 30-day lookback (item 6) populated builds back to 154.0b4.

**1b. Items 7 and 8 were both verified on the REAL merge boundary, and item 8 is worth more than
§1.2 projected.** Replaying the selector against the scratch DB's real build list
(`spike/_beta_recon/replay_merge_day.py`):

| run date | 3-build window | old behaviour | now |
|---|---|---|---|
| 2026-08-14, 08-15 | `155.0b1`(merge-day) / `154.0b10` / `154.0b9` | `get_last_versions` → `[]`, **ingestion dead** | working window, 22 signatures selected (154.0b10 evaluated against 154.0b9 — the question the window is for) |
| 2026-08-18, 08-19 | `155.0b1`(shipped) / `155.0b1`(merge-day) / `154.0b10` | merge-day build is a ZERO BASELINE | `dropped_no_users: 1`, and the from-zero flood is gone |

The counterfactual on 08-18, same window, floor on vs off
(`spike/_beta_recon/counterfactual_item8.py`): **35 signatures / 142 LLM runs → 3 signatures / 15
runs.** At $1-3 a run that one tick goes from $142-426 to $15-45 — an 89% reduction, and the 32
signatures it drops are all from-zero fires against a build nobody ran. Note also that the drop
correctly does NOT fire on 08-14/15, when the merge-day build is still the NEWEST day in the
window: it only ever hurts as a baseline.

**2. `thresholds.protos` for beta moves 20 → 5, and the reason is bigger than §4 thought.**
§4 priced this as optional at $4-17/day. The live run says the proto multiplier is the DOMINANT
cost term on beta, not a refinement: **4 selected pairs carried 37 distinct proto-signatures**,
i.e. 37 paid LLM runs from 4 selections. Per pair: 19 crashes → 12 protos, 10 → 10, 10 → 10,
6 → 5. Beta crash stacks are nearly all DISTINCT, so the dedup that makes nightly cheap (mean
1.07 protos/pair, max 6, cap 50 never binds) does almost nothing here. Sweep on that same live
selection: cap 1 → 4 runs, 3 → 12, **5 → 20**, 10 → 35, 20 → 37, 50 → 37. At $1-3 a run that is
~$20-60 rather than ~$37-111 for one tick, and the facet is count-ordered so the five kept are
the five loudest clusters. **Cap 3 (12 runs) is the priced fallback** if beta's real dossier
yield lands above the nightly-calibrated 0.55-0.77 the arithmetic assumes. This supersedes open
question 4: it is shipped, not deferred.

**3. Every one of the four live beta selections is a JS-engine signature.**
`js::gc::UnmarkGrayTracer<T>::unmark`, `TraceStackRoots`, `js::frontend::ParseNode::getKind`,
`OOM | unknown | js::AutoEnterOOMUnsafeRegion::crash_impl | js::Nursery::setForwardingPoint` —
three of them GC/marking, which is precisely the class Jon Coppeard refuted three times on
nightly (all three compiled-out, all three concurrent marking). The recorded nightly finding is
`Core::JavaScript*` = 0 FIXED / 4 INVALID / 4 owner-refuted of 10 (p=0.02), with a blind second
opinion corroborating 11/11 — and a flat ban on the category is REFUTED (human non-fuzzer JS
crash bugs run 30% FIXED; `corpus_ship` shows 6/6 exact-regressor hits). So this is **a watch
item, not a change**: n=1 run, and the survivor from that whole analysis was signature NOVELTY,
which on beta is what §2.4 measures at 12-16%. If the first beta week is dominated by GC/marking
signatures that the module owner then refutes, the lever is novelty, not a component filter.

Also settled while implementing:

* **`_chain_is_running` replaces `len(queue) <= 1`** (item 29) and was verified against a real
  RQ queue: with two `update` jobs queued the chain is still correctly detected as present, and
  correctly detected as absent when only `update` jobs are there. That is the exact case the old
  depth test got wrong.
* **The prompt-byte ledger was already stale at HEAD** — `user prompt, plain deref` measured 988
  against a recorded 970, and the hang fixture 2678 against 2675, both before any of this work.
  The beta rows added in item 25 are measured at 16,697 (system.md, +540 over nightly's, all of
  it the revision-drift rewrite), 985 (plain user prompt, −3: "beta" is shorter than "nightly")
  and 2,108 / 1,342 for the two-age block.
* **`already_filed_for_signature` fails toward SKIPPING** (outer join, unknown-build rows match
  every channel): it is a dedup guard, so a missed match is a duplicate bug on BMO and a spurious
  one costs a filing the next crash makes again.
* **`config.comment_mode` is applied at BOTH ends** — in `get_agent_autofile` and again at the
  read site — because every filer test mocks `get_agent_autofile` with a literal dict, so the
  value the filer sees is a raw `True` that would otherwise compare unequal to `"comment"` and
  silently select the strictest mode.

### 14.1 What the test suite found in the implementation

Nine new test files (`tests/test_beta_*.py`, `tests/test_shipped_channels.py`, ~300 KB) were
written against the implementation rather than with it, and they found **eleven defects in it**.
Every one is now fixed and the test that found it is the regression test. Suite: **1,907 tests,
0 skips on Postgres, 0 expected failures.**

The two that mattered most were both mine, and both were the same mistake — a rule measured on
mozilla-beta applied to every channel:

1. **`pushlog.collect`'s merge rule would have deleted over half of NIGHTLY's candidate supply.**
   Item 30's "5 of 2,356 pushes contain a merge changeset, so nothing else is touched" is a
   mozilla-BETA count. On mozilla-central, sheriffs land autoland in merge pushes several times a
   day: measured live, **26 of 191 pushes over 28 days (13.6%) carry 1,193 of 2,186
   candidate-bearing changesets = 54.6%** (73.7% over 7 days; 92.7% on 2026-08-24). Applied there
   the rule creates no `changesets` rows, so nothing scores onto a frame and every affected crash
   falls through to the off-stack path, which is live and action-emitting in prod. The fix is a
   channel scope (`pushlog.suppresses_merge_extraction`) plus a split between the two consequences
   of a cycle merge: `via_merge` is a FACT and is always recorded, while dropping the FILES is an
   ingestion optimisation that only `pushlog.pushlog` asks for — so the off-stack enumeration keeps
   the file lists its pref-flip ranker reads.
2. **Item 8's floor was reading the wrong quantity.** The "any value in [20, 400]" equivalence is a
   gap between LIFETIME report totals, but the code can only see what has arrived by now, and a
   1.25-day-old build holds ~11% of its eventual crashes. Window index 1 — where **135 of 135**
   replayed selections landed — is one cadence gap old, so the quietest real build shows ~48
   reports there and was dropped as having "no users", costing the whole run-day silently. Now it
   discriminates on **distinct installations** (max per signature, never the sum — cardinalities do
   not add), where a build nobody runs stays at 4-7 forever against 268+ for a real one:
   `min_build_installs` 15, defensible over [8, 24], a narrower margin than the report gap and
   stated as such.

The other nine, in the order a reader would care:

3. `already_filed_for_signature` was called with the crash's own channel, making the guard blind to
   the entirely CROSS-channel population it was measured on (4 of 18 = 22.2%). Now channel-blind;
   nightly is protected by the MODE test instead.
4. `AUTOFILE_BUGS=1` overrode a per-channel `enabled: false`, so "triage this channel but hold its
   filing" could not be expressed in prod. Now the strictest of the two wins in both directions.
5. The per-channel sweep cap fired with only ONE channel in play (cutting nightly's drain from
   12/day to 8/day), could not reassign the slot it took (the SQL `LIMIT` had already fixed the
   candidate list), and DROPPED rather than deferred (the cursor advanced past the capped row).
   Now: over-fetch `max_per_run * channels`, bind only when the fetched rows span more than one
   channel, and stop the cursor at the first row the tick did not consume.
6. `is_build_flag_ground` called `_partition(channel)` directly, and `_partition` maps a falsy
   channel to nightly — so the ContextVar's single documented consumer, `Dossier._skeptic_veto`,
   never saw the beta partition at all. One line: `_partition(channel or build_channel())`.
7. Developer Edition got plain beta's build-flag partition, so `MOZ_DIAGNOSTIC_ASSERT_ENABLED` was
   treated as OFF where `moz_dev_edition` turns it ON — on the 36-41% of the channel where those
   crashes actually exist. Keyed on the raw `release_channel` now
   (`orchestrator._build_flag_channel`).
8. `_install_history` passed the processed crash's label, so a DevEdition crash asked Socorro about
   `aurora` alone and `get_search_channel` had nothing to widen. Normalised to our DB channel.
9. `utils.make_url_for_signature` was the 13th `release_channel` site and item 4's list of six
   missed it — so the "this signature on crash-stats" link CONTRADICTED the population panel
   beside it on the same page.
10. The stale-signature gate read the seeded ARRIVAL date, so item 12's origin-first clock never
    reached its main consumer. It now resolves the origin date for the one chosen candidate (one
    cached hg lookup). The prompt's `landed=` is fixed by LABELLING rather than by lookup —
    resolving 150 off-stack candidates is not affordable, and `arrived-with-the-cycle-merge=` is
    both honest and strictly more informative.
11. The filed bug's "Starting point" prose said "did not land in this build's pushlog window" to a
    merge member, which is exactly backwards — the window is why it was seeded. It has a merge
    variant now.

One PRE-EXISTING defect surfaced on the way and is also fixed: `get_info_helper`'s crash-stats
query carried no `date` anchor, while its twin `fetch_signature_stats` does. SuperSearch with no
`date` returns only the last ~8 days of report dates, so on any build older than that the
hand-drafted enter_bug comment said "There are 0 crashes (from 0 installations)" for a crash the
autofiled comment counted in the thousands. Nothing in the tree said so until the new test diffed
the two parameter dicts.
