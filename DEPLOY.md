# Deploying the agent-based Clouseau (Heroku)

Checklist for standing up a fresh Heroku app running the evidence agent.

**"Nightly-only, observe-only" is out of date and has been for weeks.** The deployment this
document describes runs **nightly, beta AND release** triage (`AGENT_CHANNELS="nightly beta
release"`) and **files bugs unattended on all three** (`AUTOFILE_BUGS=1`). **Every
`daily_cap` is `null` = unbounded since 2026-09-17** (the knob is kept; a number re-arms it) --
the caps below are what each channel shipped with: nightly at
`daily_cap` 10 with `comment_on_existing: "comment"` (measured at 2.68 filings/day over 30
days); beta at cap 3 with `skip`, held from 2026-08-26 and **armed 2026-09-07** after a
fortnight of 40 held runs, 0 filed and 2 at the rung (both the QuotaManager spike a human had
already filed as bug 2069097); release at cap 2 with `skip`, the `[new in release]` title and a
tracking nomination, held 08-31 and armed 09-07 (`plans/20-release-channel-support.md`). The
**ESR channel** is declared on release's model since 2026-09-07 as one channel label for the
current ESR line (`esr153`; esr115 and esr140 ran for two hours that day and were retired), with
the family's filing policy (`channels.esr`: `skip`, cap 2, `[new in esr]`,
`cf_tracking_firefox_esr<major>` nominated) -- **and only runs where the two env vars name it**
(see "Turning the ESR channel on"). A
channel can still be held with `agent.autofile.channels.<ch>.enabled: false`, which beats the
global arm. Since 2026-09-15 the deployment also ingests, triages **and files** **Fenix nightly**
-- a second PRODUCT on the `nightly` label, not a channel -- at cap 2 with `skip`
(`agent.autofile.products.Fenix`; shipped held that afternoon, armed the same evening on the
first tick's first culprit; see "Turning Fenix on"). Read "Cost controls" below as what bounds
the spend, not as evidence that there is none.

Several things are automated by the repo now; the rest are one-time app setup.

## Automated by the repo (no action needed)
- **DB schema** — the `release:` phase runs `bin/release.py` on every deploy
  (`models.create()` is idempotent and adds any enum value the long-lived DB is missing: the
  `lead` verdict, the ESR channel label on `CHANNEL_TYPE`, the `Fenix` product label on
  `PRODUCT_TYPE` -- `models._ENUM_ADDITIONS`; no ingestion is run).
  Until 2026-09-07 that ALTER had never actually run (it re-used a connection already in a
  transaction and the failure was logged, not raised); the first deploy carrying the ESR labels
  is the first time it matters. The same phase widens `builds.version` from VARCHAR(10) to 24
  (`153.10.0esr` will be 11 characters; `models._WIDENED_COLUMNS`). Check the release log for
  `enum CHANNEL_TYPE: added value 'esr153'` / `enum PRODUCT_TYPE: added value 'Fenix'` and
  `widened builds.version`, or `psql` for
  `SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname =
  'CHANNEL_TYPE'` (or `'PRODUCT_TYPE'`) and `\d builds`, before setting `INGEST_CHANNELS`. A
  Postgres enum label is never dropped again: adding one is the irreversible half of a switch-on.
- **`searchfox-cli`** — `bin/post_compile` fetches the pinned static-musl binary at
  build time and exports `$SEARCHFOX_CLI` via `.profile.d` (the agent needs it for
  call-graph grounding; it queries searchfox.org over the network).
- **Agent isolation** — the Procfile runs a dedicated `agentworker` (queue `agent`) so
  ~20-min triage runs never block the ingestion `worker` (queues `high default low`).
- **Cost controls** — the analysed channels (`agent.channels`, overridable per deploy-free
  env var `AGENT_CHANNELS`), one run per proto-signature cluster **per channel** (dedup),
  a per-channel `autofile.daily_cap` (all `null` = no cap since 2026-09-17: at 2 on release it
  dropped a culprit at 85 behind two lesser filings, and a capped finding is never retried; a
  number re-arms it), and a **sonnet** principal tier are all in
  `config/global.json`.
- **Ignored signatures** — `ignored_signatures` in `config/global.json`: deliberate test
  crashes (about:crashparent / about:crashcontent both sign as
  `CrashChannel::OpenContentStream`) are never selected, never rate-picked, never escalated
  and never run, whatever their numbers. The selection log records them as `ignored`.
  `ignored_signature_patterns` beside it holds start-anchored regexes for a test crash whose
  exact signature moves per build: Fenix's debug-drawer `ArithmeticException` signs with an
  R8 lambda index and line that took three spellings in 90 days, so one pattern on its
  `org.mozilla.fenix.debugsettings.crashtools.` package covers every spelling. Both lists are
  for deliberate crashes only, never a denylist for a noisy real signature.

## One-time app setup (required)
1. **Add-ons:** Heroku Postgres + Heroku Redis. (`DATABASE_URL`/`REDIS_URL` are set
   automatically; the worker handles the `rediss://` SSL params.)
2. **Config vars:**
   - `heroku config:set ANTHROPIC_API_KEY=…` — **required**; the SDK reads it from the
     env and nothing in code sets it. Without it every agent run errors (silently, since
     runs are failure-isolated) and crashstack panels stay empty.
   - `heroku config:set SOCORRO_TOKEN=…` — **required for reports.html to populate**; the
     scoring/ingestion path needs a crash-stats token. Copy it from the existing app.
     **It does NOT carry protected-data scope**, whatever this line used to claim. Measured
     2026-08-26 against the live API with the prod token: SuperSearch returns byte-identical
     columns with and without the header, and every `protected` field is *omitted* rather
     than nulled — asked for `moz_crash_reason_raw` on two crashes whose PUBLIC
     `moz_crash_reason` was populated, and it came back absent both times.
     `SuperSearchUnredacted` answers `403 … requires the 'View Personal Identifiable
     Information' permission`. So `remote_type`, `url`, `user_comments` and the `phc_*`
     fields are all unreadable here. That matters beyond provisioning: `remote_type` is the
     only field that tells a WebExtensions process from a web content process (`process_type`
     is `content` for both, and has no `extension` value), which is how bug 2066201 was
     filed against an extension API for a `webIsolated` crash. Tracked in bug 2066600.
   - `heroku config:set GITHUB_TOKEN=…` — **optional, recommended with Fenix**: a read-only
     personal token for the one GitHub request per new Fenix build that indexes
     `mobile/android/**.{kt,java}` (`java.refresh_file_index`, the tree of
     `mozilla-firefox/firefox` at the build's git sha). Unauthenticated the limit is 60 requests
     an hour PER ORIGINATING IP, and Heroku dynos share egress IPs, so the budget may be spent by
     other tenants; a token gets 5,000 an hour of its own. Without it a 403 is retried on the
     next 20-minute tick (3 requests an hour at worst) and the Kotlin frames of a Java crash keep
     resolving through the paths the pushlog already recorded (~500 `.kt` files over 8 days).
     **Lifetime at most 366 days**: Mozilla's GitHub enterprise refuses a fine-grained token
     with a longer (or no) expiry on every endpoint with a 403 that names the rule, while
     `/rate_limit` still answers 5,000 -- measured 2026-09-15 on the first token set here. A
     refused token is logged with GitHub's message and the request is retried anonymously, so
     a bad token is never worse than none; the fix is the token's expiry on GitHub's side.
   - `heroku config:set LIBMOZDATA_CFG_BUGZILLA_TOKEN=…` (or `BUGZILLA_TOKEN`) — the filer's
     API key (`clouseau-bot`). Read from the environment first because libmozdata cannot
     (`config.get_bugzilla_token`). Without it every Bugzilla write hard-fails safe and Clouseau
     is read-only whatever `AUTOFILE_BUGS` says; with it AND `AUTOFILE_BUGS=1` it files
     unattended as the header describes. "Do NOT set a Bugzilla token (observe-only)" stood here
     until 2026-09-15 and had been false since the first filing; the product-level way to
     observe without writing is a per-product hold (Fenix, below), not a missing token.
     The account is in `canconfirm` (granted 2026-09-17), so a bug it files is created `NEW`
     / ever-confirmed instead of waiting on BugBot's crash-signature rule (5 h to 3 days).
     Should the group ever be lost nothing fails: BMO silently files UNCONFIRMED and BugBot
     confirms later, as before.
3. **Scale every dyno** (only `web` auto-starts; the rest default to 0):
   ```
   heroku ps:scale web=1 worker=1 agentworker=1 clock=1
   ```
   Keep exactly **one** `clock` (multiple would double-enqueue ingestion). Both `worker`
   and `agentworker` are needed — `worker` alone never runs agent jobs; `agentworker`
   alone never ingests.
4. **First ingestion:** the clock's first tick fires ~20 min after it starts. For an
   immediate kick, run once: `heroku run python bin/init.py` (creates schema if needed +
   runs one ingestion pass).

## Turning the beta channel on (plan #18)

Beta support is in the code and wired in `config/global.json` (`agent.channels` includes
`beta`; `agent.autofile.channels.beta` sets `comment_on_existing: "skip"` and a
`daily_cap` of 3). **Nothing beta happens until `INGEST_CHANNELS` says so**, and that is
one env var, no deploy, effective on the next 20-minute tick:

```sh
# ingest-only canary: beta rows appear, nothing is analysed and nothing is filed
heroku config:set INGEST_CHANNELS="nightly beta" AGENT_CHANNELS=nightly

# then, when the free output looks right: analyse beta too
heroku config:set AGENT_CHANNELS="nightly beta"
```

**Set `INGEST_CHANNELS` and `AGENT_CHANNELS` explicitly, at app creation, BEFORE the first
`bin/init.py`.** Both now FAIL CLOSED — absent or empty means *nothing*, and each logs a
warning saying so — but the ordering still matters, because `bin/init.py` calls
`update.update_all()` directly and does not wait for the clock.

This used to be the most dangerous pair of variables in the deployment, and one of them fired.
`update_all` read `os.getenv("INGEST_CHANNELS", "").split() or config.get_channels()`, so
*clearing* it — or never having set it — meant every configured channel, **including
release**. On 2026-07-06 the app was created at 10:19 UTC, deployed at 12:27:47, and
`INGEST_CHANNELS` was set at 12:45:03; the `release` `lastdate.maxdate` is 12:36:38, inside
that 17-minute gap. It cost 7,267 `nodes` rows, 20,320 `changesets` rows (61% of the table)
and 2,628 `releases/mozilla-release` patch fetches for a channel nothing could read. See
`plans/20-release-channel-support.md` §1.8. `AGENT_CHANNELS=""` was the same shape on the
money switch: it meant "no filter", i.e. triage every channel at ~$1-3 a crash, on the one
variable an operator reaches for to stop spending.

To restore the config-file value, **unset** `AGENT_CHANNELS`; do not empty it.

The two levers are different kinds of thing, which is why they are separate:

| lever | what it costs | how to change it |
|---|---|---|
| `INGEST_CHANNELS` | free (Socorro + hg reads) | env var, next tick. Absent or empty = **ingest nothing** (logged) |
| `AGENT_CHANNELS` | ~$1-3 per crash | env var, next tick (it used to need a deploy, and a deploy kills in-flight runs at ~$3 each). Absent = the config file's value; **empty = triage nothing** (logged) |
| `INGEST_PRODUCTS` | free | env var, next tick. Absent = the config's `ingest_products` (`Firefox Fenix`); **set-but-empty = ingest nothing** (logged). See "Turning Fenix on" for why it does not fail closed |
| `AGENT_PRODUCTS` | ~$1-3 per crash | env var, next tick. Absent = `agent.products` (`Firefox Fenix`); **set-but-empty = triage nothing** (logged). `AGENT_PRODUCTS=Firefox` stops the Fenix spend without a deploy |
| `AUTOFILE_BUGS` | Bugzilla writes, **global** | env var, immediate |

`AUTOFILE_BUGS` is global on purpose (a kill switch that only stops one channel is not a
kill switch). To stop beta *without* stopping nightly, drop it from `AGENT_CHANNELS`:
beta then costs nothing and files nothing.

### What to watch on the first beta days
- `tasks.html` now has a channel column — beta runs should be a small minority
  (projected ~4-6 dossiers/day against nightly's 85-120).
- `selection.html`: a new `dropped_no_users` outcome appears around each merge. That is
  the merge-day `N.0b1` build being kept out of the baseline, not a lost signature.
- The worker log at each cycle merge: `merge push at <date>: keeping N node(s),
  extracting 0 patches` (N ≈ 5,000-7,000). If that line is *absent* on a merge day, the
  merge push was patch-extracted after all — expect 3-4 hours of queue and check
  `pushlog.is_merge_push`.
- Beta filings carry `comment_on_existing: skip`, so a beta crash whose signature already
  has an open bug files **nothing** and logs `open bug N exists`. That is the requested
  behaviour ("only crashes without a bug in Bugzilla"), and it suppresses roughly 58-59%
  of beta signatures — the alternative, `file_new`, is measured at ~2.4x the volume.

## Turning the ESR channel on (plan #23)

Socorro has ONE `esr` channel; Mozilla ships several ESR lines at once (115, 140 and 153 on
2026-09-07), each from its own repository, searchfox tree and build lineage. **Only the current
line is a channel**: `esr153`, a label of its own exactly the way `release` is one label for one
repo, with the policy shared by the family `esr` (`config.channel_family`). All three lines were
switched on that evening; esr115 (Windows 7, 32-bit, security-only uplifts) spent its first tick
on 17 runs of one `OOM | large` signature and the two older lines were retired within two hours.
**Nothing ESR happens where the env vars do not name the line**:

```sh
# 1. deploy first: the release phase adds the enum label to the long-lived DB (above)
# 2. ingest-only canary: pushlog + builds + selection rows appear, nothing analysed or filed
heroku config:set INGEST_CHANNELS="nightly beta release esr153"
# 3. then analyse (and, with AUTOFILE_BUGS=1 already live, FILE)
heroku config:set AGENT_CHANNELS="nightly beta release esr153"
```

Two guards make the variables authoritative even for work already queued: `update.update`
ignores a job for a channel the deployment does not ingest, and `run_evidence_agent` does not
start a non-forced run for a channel `AGENT_CHANNELS` no longer names (both 2026-09-07, after
dropping esr115/esr140 left 36 of their jobs in the `agent` queue). Selection knobs are release's
(installs 50, protos 20, floor 50, rate path off); filing is `channels.esr`: `enabled: true`,
`skip`, `daily_cap: 2`, `[new in esr] Crash in [@ ...]`, and the crash's own line nominated for
tracking (`cf_tracking_firefox_esr153 = ?`, its own best-effort PUT). To hold it:
`channels.esr.enabled: false`.

**Rotating to the next ESR line** (153 -> 166, mid-2027): add `esr166` to `config.channels` and a
`searchfox.Repo` member, deploy, add it to both env vars. **Retiring a line**: drop it from both
env vars (the two guards above turn its queued jobs into no-ops), purge its rows, then drop the
label from `config.channels`. A Postgres enum label cannot be dropped and stays in the type; a
stored label the config no longer lists still READS (`models.CHANNEL_TYPE` is lenient on the way
out), so the order is for tidiness, not survival. The purge is one command -- a dry run without
`--yes`; it refuses a label either env var still names -- and one transaction (`builds`,
`changesets`, `uuids`, `crashstack`, `dossiers` and `verdicts` cascade from `nodes`):

```sh
heroku run -a crash-clouseau-augmented -- python bin/retire_channel.py esr115 esr140        # report
heroku run -a crash-clouseau-augmented -- python bin/retire_channel.py esr115 esr140 --yes  # delete
```

The same thing by hand, if `heroku run` is not at hand:

```sql
BEGIN;
DELETE FROM selection WHERE channel IN ('esr115', 'esr140');
DELETE FROM lastdate  WHERE channel::text IN ('esr115', 'esr140');
DELETE FROM nodes     WHERE channel::text IN ('esr115', 'esr140');
COMMIT;
```

What to watch on the first ESR days:
- `selection.html?channel=esr153`: the window is the line's own two or three builds; the first
  tick after switch-on carries the two builds of the last 30 days.
- The worker log: `Get pushlog data for esr153` against `releases/mozilla-esr153` (small: an ESR
  repo takes a handful of uplifts a cycle), one `Update builds for esr153/Firefox`.
- `tasks.html`: runs labelled `esr153` should be rare -- release-sized thresholds on a channel
  ~3% of release's volume (4k reports a week on 2026-09-07). `filed_bug` rows with
  `channel = 'esr153'`.
- ESR has no measured population rates (`sigage._POPULATION_RATES`), like release: prompts drop
  the hardware comparison. `sigtrend` refuses it, like release (rate path off).

## Turning Fenix on (plan #16)

Fenix (Firefox for Android) nightly is a second PRODUCT on the channel label `nightly`, not a
channel: `config.products` and `ingest_products` are `["Firefox", "Fenix"]`, `product_channels`
pins Fenix to `["nightly"]`, `agent.products` triages both, and filing is ARMED for Fenix at
`skip` / cap 2 (`agent.autofile.products.Fenix`; step 3). Its builds come from the TaskCluster index
(`crashclouseau/buildsource.py` -> `tcindex`, the `mobile.fenix-nightly` leaf; Buildhub-as-firefox
missed 1 of 15 recent Fenix builds and only cross-checks), its Java/Kotlin stacks are read
(`java_stack_trace`, frames in `java.packages`) with the R8-remapped line numbers IGNORED behind
a pref (step 5), its native stacks as before. Unlike beta and ESR there is NO ingest-only canary
step: the product levers default ON in the config, so the deploy that carries the code is the
deploy that starts it -- the order below is what makes that safe.

1. **Deploy first, and check the enum.** The release phase runs
   `ALTER TYPE "PRODUCT_TYPE" ADD VALUE IF NOT EXISTS 'Fenix'` (`models._ENUM_ADDITIONS`);
   without it the first Fenix write (`Build.put_data`, or `ChannelDaily.upsert` from
   `sigtrend.backfill`, which runs first in `put_crashes`) raises `invalid input value for
   enum`. Look for `enum PRODUCT_TYPE: added value 'Fenix'` in the release log, or run
   `SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname =
   'PRODUCT_TYPE';`. **The label is irreversible** (Postgres never drops an enum value), and
   **`config.products` is NOT a kill switch**: that list DEFINES the enum, and editing it is a
   deploy that leaves the label and every Fenix row in place. The switches are the two env vars
   in step 2, effective on the next tick with nothing deployed.

2. **The two product levers.** Both default to the config's two-entry list; the channel
   variables fail closed. The difference is deliberate: `INGEST_CHANNELS` bought its fail-closed
   rule with the 2026-07-06 release incident above, where "absent" meant "every channel including
   one nobody could read". Here the list is `Firefox Fenix` and the danger runs the other way: a
   deploy that introduces a lever must not stop DESKTOP ingestion or triage for a tick because
   nobody set a new variable first. Set-but-empty still means NOTHING, with a warning, so each is
   a real kill switch:

   ```sh
   heroku config:set AGENT_PRODUCTS=Firefox       # stop the Fenix SPEND: next tick, no deploy
   heroku config:set INGEST_PRODUCTS=Firefox      # stop Fenix INGESTION too (builds, uuids, selection)
   heroku config:unset AGENT_PRODUCTS INGEST_PRODUCTS   # back to the config's "Firefox Fenix"
   ```

   As with the channel variables, `run_evidence_agent` re-checks the product before a non-forced
   run and `update.update` refuses a (channel, product) pair the deployment does not ingest, so
   dropping Fenix from a variable turns its already-queued jobs into no-ops.

3. **Filing: ARMED at `skip`, cap 2** (`agent.autofile.products.Fenix`, a per-product overlay on
   nightly's policy; `AUTOFILE_BUGS` stays the global switch). It shipped HELD
   (`enabled: false`) on 2026-09-15 afternoon -- plans/16 §11.4: ~40% of Fenix changeset authors
   cannot be needinfo'd and Clouseau would out-file the organic `Firefox for Android` rate ~15x
   -- and Calixte armed it the same evening, when the first tick's first culprit
   (35e32be2, `nsTSubstring<T>::Truncate | gfxPlatform::ReportTelemetry`, 85, second opinion
   corroborated) declined with `autofile held for product 'Fenix' (triage-only)`. To hold it
   again: `enabled: false` in that entry and a deploy. A product hold binds the ordinary filer,
   the spike filer (the sweep SKIPS a held product -- an escalation exists to file) AND `POST
   /api/tasks/trigger` with `file_bug: true`, and its declines are recorded on the dossier with
   the product, so they can be counted. KNOWN VENUE GAP, desktop's too: `resolve_product_component`
   adopts the regressor bug's own component first, so that first culprit's preview reads
   `Data Platform and Tools :: Monitoring & Alerting` (bug 1879888 is a telemetry-metric bug)
   where `Core :: Graphics` is meant; the `moz.build` `BUG_COMPONENT` rung is the fix (memory
   `filer-component-unresolved-out-of-retention-candidate`).

4. **What to watch on the first Fenix days.**
   - `tasks.html`: a Fenix run shows `N` with `fenix` under it in the Ch. column and the tooltip
     leads with the product; Firefox stays the bare letter. Projected ~11-12 native pairs a day
     plus a few Java ones (`thresholds.protos` 5 for Fenix; the proto-cluster dedup is per
     product, so a signature both products crash on costs one run per product).
   - `selection.html?product=Fenix` (the page has a product select) and `/api/spikes?product=Fenix`.
     Two outcomes are new for every product: `no_stack` (an `EMPTY: *` signature, declined before
     the spike test) and `no_protos` (a kept pair that yielded no proto/uuid).
   - The worker log: `Update builds for nightly/Fenix`, the `firefox-ci-tc.services.mozilla.com`
     index requests (one namespace POST per day, a GET per leaf) and, for a build whose task gave
     no revision, hg-edge `json-pushes` in a two-second window. A cold `builds` table pays the
     30-day lookback once (~30 POST + ~150 GET); after that, from the newest Fenix build minus a
     day. A 404 leaf is "not a CI APK" and no row, not an error.
   - The "N% worth investigating" badge is BLANK on a Fenix verdict
     (`agent.calibration.products.Fenix: {}`): the table was fit on desktop nightly and says
     nothing about Android. A number there is a bug.
   - `uv run python bin/audit_products.py`, once: CHECK 2 lists Fenix as `ours
     (config.products)` and only Focus/ReferenceBrowser (MozillaVPN at `--days 180`) as unmapped.
   - Expected, not a stall: `lastdate` is keyed by channel only, so Firefox nightly and Fenix
     nightly share one ingestion clock row; `cpu_info` is `unknown` on 40% of Fenix reports, so
     the hardware-noise prong says nothing there; `sigage` has no Fenix population rates.

5. **Java stacks and the line-number pref.** Fenix's JVM crashes carry a `java_stack_trace`
   whose line numbers went through R8's obfuscation map: `Keystore.kt:269` of the motivating
   crash is a KDoc comment line. `java.trust_line_numbers` in `config/global.json` (a top-level
   `java` block, because `java.py` runs on the INGESTION path) is `false`: frames carry
   `line_trusted: false`, scoring falls back to file/method matches (`Changeset._fuzzy_score`:
   new file 10, method 8, file 5), the stack hash uses file + function instead of the line, the
   prompts say the lines are R8-remapped, and `crashstack.html` shows the line as `~269` with the
   reason on hover and links the FILE (no `#l` anchor, no `&line=` on the codeview link). `true`
   restores line-proximity scoring, line citations and the anchors everywhere -- a config edit
   and a deploy; the flag is derived at read time, so history is relabelled consistently. Our
   packages are `java.packages` (`org.mozilla.`, `mozilla.components.`, `mozilla.appservices.`,
   `mozilla.telemetry.`); a crash whose frames are all the framework's is `no usable stack` to
   the trigger, exactly like a native crash without a `json_dump`.

## Actionable filings (a crash worth filing, no regressor claimed; 2026-09-17)

The principal has a third positive verdict, **`actionable`**: what fails and where is ESTABLISHED
(a cited source line and the condition that fires it) in code that is ours, the code's owner is
known, and NO changeset is claimed as the cause. Its `candidate` is the failing code's ORIGIN by
blame -- how the owner and the product::component are found -- and is accused of nothing. Born
from crash `0015b3bf` (release 155.0.1, `CheckLogMessage::~CheckLogMessage`, 75-day-old
signature, 110 installations): two runs on the same evidence said culprit 85 then abstain 25,
because neither box fit. Schema `agent/schema.py::Decision.actionable`; DB enum value added at
startup by `_ensure_enum_values` (`_ENUM_ADDITIONS`).

**What it files** (`report_bug.build_actionable_comment`): `Crash in [@ sig]` with no `[new in
release]` prefix and no tracking nomination (both say "new regression"), keyword `crash` only, no
`regressed_by`; the crash link, reason, frames, volume, "This signature has been reported since
build X (date), N days before the build above.", then **"This bug looks actionable because:"** +
the verdict's mechanism and consistency statements (the prompt asks for them as affirmative
facts) + "The failing code comes from <changeset> (bug N) by :nick.", the code references,
":nick, can you have a look please?" (the origin's author, verified account) and the provenance
footer. Nothing about what the crash is NOT: no skeptic block, no "Starting point", no "% worth
investigating".

**Gates of its own** in `bugzilla_apply.autofile_bug`, besides the ordinary ones (`min_confidence`
70 = `probable`, `verdicts`, `already_filed_for_signature` as a full stop, fixed-after-build,
known-on-train, security venue):
- a real POPULATION: distinct installations on the analysed build and later >=
  `spike.real_installs` (nightly 3, beta 6, release/ESR 50; Fenix nightly 3) -- the spike path's own
  bar for filing on volume alone, read off the same Socorro aggregation the bug's volume sentence
  quotes. Measured on the last 21 days of cited-mechanism `pre_existing` abstains: nightly median
  ONE installation (6 of 62 clear 3), release all >=50 -- the floor bites on nightly and is free
  on release;
- NO open same-application, non-meta bug on the signature, whatever `comment_on_existing` says:
  an open bug means someone can already act. Decline reads `open bug N exists; an actionable
  crash is filed only where no bug is`.
Upstream, the age gate flips sign for it: an origin that landed AFTER the signature was first
seen is not the origin -> `pre_existing` abstain (`actionable_origin_postdates_signature`). The
blind second opinion is not bought for it (`skipped_actionable`); no calibrated probability is
published (the badge shows the rung).

**To hold it**: take `"actionable"` out of `agent.autofile.verdicts` (a channel or product overlay
may narrow the list on its own); the verdict still shows on crashstack.html as ACTIONABLE with the
decline reason. **Expected stream**: ~2/week on nightly, <=1-2/day on release before the open-bug
rule (83 runs / 48 signatures in 21 days). Read the first week with `verdicts.verdict =
'actionable'`, `filed_bug`, and `filing_declined.skipped` LIKE `'%actionable floor%'` /
`'%filed only where no bug is%'`. `Feedback.classify` scores these `unknown` / `crash_invalid`
only (no regressor claim to confirm); a resolution-based "useful" bucket is a follow-up.

## Bucket filings on a signature a [meta] tracker holds (2026-09-18)

A signature an open `[meta]` bug carries in `cf_crash_signature` is a CATCH-ALL: for a shutdown
hang it is the main thread's wait, and every cause under it -- a different pool thread's work in
each report -- shares it. The people who own those (bug 1866944 for `nsThreadPool::
ShutdownWithTimeout`, 1633342 for necko) bucket the reports by the awaited thread's stack and file
one bug per bucket that BLOCKS the tracker, WITHOUT the signature (:jstutte, bugs 2073349 c1 and
2069191 c5). The filer used to see the tracker, call it "not a venue", and file a new
signature-titled bug beside it. Now, on such a signature:

- the analysis is handed the AWAITED WORK: the thread the spin-loop stack names, with its frames
  (`hang.awaited_summary`, prompt fact `AWAITED WORK`, dossier key `hang_awaited_work`), and an
  `actionable` verdict whose cited mechanism is only the wait code becomes a `pre_existing`
  abstain (`hang_wait_not_actionable`);
- a NEW bug is a BUCKET bug or nothing: titled for its cause (`verdict.title`, else `<work> blocks
  <pool> shutdown inside <call>` from the awaited thread, else the mechanism's first sentence),
  no `cf_crash_signature`, `blocks` the tracker(s) and `clouseau`, opening with "Bucket of bug N,
  filed without the signature"; with nothing to name the bucket the filer declines
  (`skipped: signature is held by [meta] bug N; the verdict names no bucket`);
- one bug per BUCKET, not per signature: a prior filing of ours stops a new one only when its
  recorded `bucket` (the awaited thread's key: `<work> | <call>`, the same on every platform) is
  the same or unknown. ONLY THE KEY IS AN IDENTITY: a bucket with no key (a non-hang) is one bug
  per signature, because its title is the model's sentence and two runs write two of them; the
  title only decides whether our own bucket bug is the venue for a same-titled spike;
- the spike filer does the same with a grounded analysis, and posts a spike it cannot name as a
  comment on the tracker (mode `spike_comment`, `venue_kind: meta`, no needinfo): the volume is
  signature-level information and the signature lives there.

Check after a deploy: the first bucket filing's `filed_bug` carries `bucket_title`, `meta_bugs`
and (on a hang) `bucket`; the created bug has an empty crash-signature field and the tracker in
its blocks list.

BEFORE THAT, THE BACKFILL. The three filings made before bucket bugs existed have no identity,
and an unknown identity matches every bucket -- so 2073349 (pool-shutdown family), the spike
filing behind 2071528 and 2069191 (necko's family) stop every new bucket on the two families
that matter most, the unfiled Linux CUPS bucket included. `bin/backfill_bucket.py` re-reads the
one report that CREATED each bug and writes its key and title to every ledger row for that bug;
comments never define another identity (2071528 later received an incorrectly routed
`nsSegmentedBuffer` spike comment). 2069191's socket thread was idle, so it gets the bug's own
summary as title and no key:

```
heroku run -a crash-clouseau-augmented -- python bin/backfill_bucket.py --bug 2073349 --bug 2071528 \
  --bug 2069191 --title "2069191=Socket thread priority event queue (TRR events) could starve regular even processing during shutdown"
# read the report, then the same line with --apply
```

Cleanup owed on BMO from before: 2069191 still carries the signature and the `topcrash` keyword
BugBot added for it (Jens moves both to the tracker, as on 2071528).

## Spike escalation (a real spike files a bug, culprit or not; plan #22)

Since 2026-09-07 a REAL spike — not `0 → 1`: the channel's crash floor, several distinct
installations, 3x the loudest of the preceding build-days AND of the signature's own builds over
the 21 days before (`spike.history_days`, read from Socorro per judgement; unreadable = not a
spike — added 2026-09-08 after bug 2070317 was filed on a one-build zero baseline), and a Poisson
excess at the crash-spikes dashboard's `major` alert rate (`crashclouseau/spikes.py`) — that the
ordinary triage did not
file gets one **Claude Opus 5 run at effort xhigh** (`agent.spike_escalation`) and a bug on
**every triaged channel**, the per-channel culprit-filing hold notwithstanding. The bug leads
with the volume; the investigator's analysis follows only where it grounded its claims in tool
reads. An open bug on the signature gets it as a comment; so does a bug we filed ourselves that a
human restricted or that was resolved FIXED after the spiking build (the spike is on builds
without the fix), and anybody's bug fixed after the build; otherwise a new bug. An APPEARANCE (`...0, 0, 0 -> 50`, no earlier report on the channel) carries the channel's
title mark — `[new in release]` on release, `[new in esr]` on ESR; a rise of an old signature does not. Rows land in `spike_escalations` (created by `_ensure_tables` on the release phase);
`GET /api/spikes` lists them, and `tasks.html` has a "Spike escalations" section above the
triage runs (status, the spike in numbers, the investigator's assessment, the bug and how it was
filed: `new` / `cmt` / `triage` = the ordinary triage had already filed it). An ordinary run on a
crash or signature the spike path filed shows that bug in its Bug column marked `spike`; the
"bugs filed" tile stays the ordinary filer's count.

| lever | what it does |
|---|---|
| `SPIKE_ESCALATION_ENABLED=0` | stops the SPEND (no investigations enqueued), no deploy |
| `AUTOFILE_BUGS=0` | stops the WRITES, as for every filer |
| `AGENT_CHANNELS` | which channels are swept, as for triage |
| `agent.spike_escalation.max_runs_per_day` / `daily_cap` (4 / 3 per channel) | bound a bad predicate at a nuisance, not an incident |

The investigator ran on Claude Fable 5.1 until 2026-09-08 and on Claude Opus 5 since
(`agent.spike_escalation.model`; the prompt is `crashclouseau/agent/prompts/spike.md`, the
generic crash-analysis prompt adapted to the MCP tools, which are what carry the tokens and the
allowlisted UA); an unavailable model falls back to `fallback_model` (opus 4.8). A run is $5–40
(`max_cost_usd`, a backstop whose CLI enforcement is unverified). The investigator and the second
opinion set `ClaudeAgentOptions.tools=[]` and the triage principal `tools=["Agent", "Task"]`
(all since 2026-09-07): the CLI's built-in `Bash`/`Read`/`Grep`/`Glob`/`Write`/`WebFetch` are no
longer registered for any agent, the MCP tools stay — live-probed on this SDK that day (a toy
MCP tool ran, a subagent still spawned, and the models reported no Bash; with `tools` unset the
same prompt ran `Bash`). Before that, 6 triage runs in a 2.4-hour window had made 21 `Grep`, 9
`Read` and 4 `Bash` calls on a dyno with no checkout. `payload->'usage'->'tools_used'` on a done
spike row is still the cheap day-one glance; for triage, watch that runs still spawn their
subagents (`▶ spawn` lines in the worker log) and that the abstain rate does not move.

## Before you deploy: check for live triage runs

A release restarts every dyno (SIGTERM, then SIGKILL ~30s later). A triage run takes
~20 minutes, so it cannot drain — the job is killed mid-analysis, the orphan reaper
re-enqueues it, and the whole run starts over at roughly $3 a time. Three deploys inside
one hour on 2026-07-28 produced 11 re-enqueues.

```sh
uv run python bin/predeploy.py && git push heroku augmented:main
uv run python bin/predeploy.py --wait   # ...or block until the queue drains (40min max)
```

Exit 0 means nothing alive would be lost; exit 1 lists the runs and prices the loss.
`--force` reports but never blocks. Runs already past `job_timeout` don't count — RQ has
killed those already.

## Verify after deploy
- `heroku run 'searchfox-cli --version'` → prints a version (build hook worked).
- Slug size in the build output — the `claude-agent-sdk` wheel bundles a large (~239 MB)
  `claude` CLI; confirm the slug is under Heroku's 500 MB limit.
- Outbound access to `searchfox.org` and `lando.moz.tools` (the latter is used by
  post-migration scoring to map git frame hashes → hg nodes).
- Tail the worker: `agent: <uuid> done (verdict=…)` lines (not repeated failures).
- `reports.html?channel=nightly` lists scored crashes; a `culprit`/`lead` tag appears on
  a UUID once its triage finishes.

## What you'll see
- **`reports.html`** fills with scored nightly crashes (ingestion — same as before the
  agent) and now **tags the UUIDs the agent found a culprit/lead for**, so the
  interesting ones are spottable from the index.
- **Full evidence** (mechanism, call path, area-experts, needinfo draft) is on
  `crashstack.html?uuid=…` per crash, or `GET /api/evidence?uuid=…` for the JSON.
- Agent evidence accumulates **slowly** (~20 min/run on the single agentworker), so
  early on most scored crashes won't be tagged yet.

## Analysing one crash by hand

`POST /api/tasks/trigger` analyses a crash the pipeline never selected (or re-runs one it did),
with the two decisions the pipeline otherwise makes: whether the run may write to Bugzilla and
whether it is listed on tasks.html. Both default to the cautious side (`file_bug: false`,
`show_in_tasks: true`) and are recorded on the dossier, so the reaper's re-run and a later
retrigger click honour them. A uuid we never ingested is fetched from Socorro and scored first;
its build must be inside the ingested window (`builds` table), or the reply says so. Up to 20
uuids per call, each a ~$1-3 run. Needs the write token in a header:

```
curl -s -X POST https://<app>.herokuapp.com/api/tasks/trigger \
  -H "X-Clouseau-Token: $API_WRITE_TOKEN" -H "Content-Type: application/json" \
  -d '{"uuids": ["2767868e-0d8d-4674-a1e6-c07c20260908"], "file_bug": false, "show_in_tasks": false}'
```

The result is on `crashstack.html?uuid=<uuid>` (and `/api/evidence?uuid=`) whatever
`show_in_tasks` says; a run with `file_bug: false` shows "Not filed: filing disabled for this
run" in the Bug column when it is listed. Each result names the crash's `product` and `channel`.
`file_bug: true` lets the run through the ordinary filing gates; it does NOT arm a product whose
filing is held (`agent.autofile.products.<product>.enabled: false`): that run is analysed and
declines with `autofile held for product '<product>' (triage-only)`. A Java-only crash
whose frames are all outside `java.packages` is refused as `no usable stack`, like a native crash
Socorro has no `json_dump` for.
