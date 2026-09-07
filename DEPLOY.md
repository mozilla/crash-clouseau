# Deploying the agent-based Clouseau (Heroku)

Checklist for standing up a fresh Heroku app running the evidence agent.

**"Nightly-only, observe-only" is out of date and has been for weeks.** The deployment this
document describes runs **nightly, beta AND release** triage (`AGENT_CHANNELS="nightly beta
release"`) and **files bugs unattended on all three** (`AUTOFILE_BUGS=1`): nightly at
`daily_cap` 10 with `comment_on_existing: "comment"` (measured at 2.68 filings/day over 30
days); beta at cap 3 with `skip`, held from 2026-08-26 and **armed 2026-09-07** after a
fortnight of 40 held runs, 0 filed and 2 at the rung (both the QuotaManager spike a human had
already filed as bug 2069097); release at cap 2 with `skip`, the `[new in release]` title and a
tracking nomination, held 08-31 and armed 09-07 (`plans/20-release-channel-support.md`). A
channel can still be held with `agent.autofile.channels.<ch>.enabled: false`, which beats the
global arm. Read "Cost controls" below as what bounds the spend, not as evidence that there is
none.

Several things are automated by the repo now; the rest are one-time app setup.

## Automated by the repo (no action needed)
- **DB schema** — the `release:` phase runs `bin/release.py` on every deploy
  (`models.create()` is idempotent + adds the `lead` enum value; no ingestion is run).
- **`searchfox-cli`** — `bin/post_compile` fetches the pinned static-musl binary at
  build time and exports `$SEARCHFOX_CLI` via `.profile.d` (the agent needs it for
  call-graph grounding; it queries searchfox.org over the network).
- **Agent isolation** — the Procfile runs a dedicated `agentworker` (queue `agent`) so
  ~20-min triage runs never block the ingestion `worker` (queues `high default low`).
- **Cost controls** — the analysed channels (`agent.channels`, overridable per deploy-free
  env var `AGENT_CHANNELS`), one run per proto-signature cluster **per channel** (dedup),
  a per-channel `autofile.daily_cap`, and a **sonnet** principal tier are all in
  `config/global.json`.

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
   - Do **NOT** set a Bugzilla token (observe-only): with none, the apply route
     hard-fails safe and Clouseau is strictly read-only. Enabling Bugzilla writes wants
     product-owner sign-off (the app is unauthenticated + CORS-open).
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

## Spike escalation (a real spike files a bug, culprit or not; plan #22)

Since 2026-09-07 a REAL spike — not `0 → 1`: the channel's crash floor, several distinct
installations, 3x the loudest preceding build-day and a Poisson excess at the crash-spikes
dashboard's `major` alert rate (`crashclouseau/spikes.py`) — that the ordinary triage did not
file gets one **Claude Fable 5.1 run at effort xhigh** (`agent.spike_escalation`) and a bug on
**every triaged channel**, the per-channel culprit-filing hold notwithstanding. The bug leads
with the volume; the investigator's analysis follows only where it grounded its claims in tool
reads. An APPEARANCE (`...0, 0, 0 -> 50`, no earlier report on the channel) carries the channel's
title mark — `[new in release]` on release; a rise of an old signature does not. Rows land in `spike_escalations` (created by `_ensure_tables` on the release phase);
`GET /api/spikes` lists them.

| lever | what it does |
|---|---|
| `SPIKE_ESCALATION_ENABLED=0` | stops the SPEND (no investigations enqueued), no deploy |
| `AUTOFILE_BUGS=0` | stops the WRITES, as for every filer |
| `AGENT_CHANNELS` | which channels are swept, as for triage |
| `agent.spike_escalation.max_runs_per_day` / `daily_cap` (4 / 3 per channel) | bound a bad predicate at a nuisance, not an incident |

Fable 5.1 needs the org's 30-day data retention setting (it is not served under zero data
retention); an unavailable model falls back to `fallback_model` (opus). A run is $5–40
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
