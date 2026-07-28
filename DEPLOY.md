# Deploying the agent-based Clouseau (Heroku)

Checklist for standing up a fresh Heroku app running the evidence agent as a
**nightly-only, observe-only** canary. Several things are automated by the repo now;
the rest are one-time app setup.

## Automated by the repo (no action needed)
- **DB schema** — the `release:` phase runs `bin/release.py` on every deploy
  (`models.create()` is idempotent + adds the `lead` enum value; no ingestion is run).
- **`searchfox-cli`** — `bin/post_compile` fetches the pinned static-musl binary at
  build time and exports `$SEARCHFOX_CLI` via `.profile.d` (the agent needs it for
  call-graph grounding; it queries searchfox.org over the network).
- **Agent isolation** — the Procfile runs a dedicated `agentworker` (queue `agent`) so
  ~20-min triage runs never block the ingestion `worker` (queues `high default low`).
- **Cost controls** — nightly-only (`agent.channels`), one run per proto-signature
  cluster (dedup), and a **sonnet** principal tier are already in `config/global.json`.

## One-time app setup (required)
1. **Add-ons:** Heroku Postgres + Heroku Redis. (`DATABASE_URL`/`REDIS_URL` are set
   automatically; the worker handles the `rediss://` SSL params.)
2. **Config vars:**
   - `heroku config:set ANTHROPIC_API_KEY=…` — **required**; the SDK reads it from the
     env and nothing in code sets it. Without it every agent run errors (silently, since
     runs are failure-isolated) and crashstack panels stay empty.
   - `heroku config:set SOCORRO_TOKEN=…` — **required for reports.html to populate**; the
     scoring/ingestion path needs a crash-stats token with protected-data scope. Copy it
     from the existing app.
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
