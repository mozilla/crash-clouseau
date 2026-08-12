#!/usr/bin/env python
# Heroku release-phase entry point: create the DB schema idempotently on every deploy.
#
# Nothing else on the Heroku path creates tables (the web/worker/clock entrypoints
# never call models.create()), so on a fresh Postgres every ingestion job would die on
# a missing relation and reports.html would 404. This runs in the release phase (after
# build, before the new release goes live).
#
# models.create() only builds tables when they are missing (gated on the "lastdate"
# table) and runs _ensure_enum_values() to add any new enum values (e.g. "lead") to a
# long-lived DB, so it is a safe no-op once the schema exists. HGAuthor.get_default_id()
# seeds the default (empty) author row that Node.hgauthor references. It deliberately
# does NOT run ingestion (update_all) -- the clock dyno owns that -- so the release
# phase stays fast and never blocks a deploy on the network.
from crashclouseau import archetypes, models

print("release: ensuring DB schema", flush=True)
fresh = models.create()
models.HGAuthor.get_default_id()
print("release: schema {}".format("created" if fresh else "already present"), flush=True)

# Built-in crash archetypes (crashclouseau/archetypes.py). This was the ONLY thing seeding them
# and it lived exclusively in bin/init.py -- the docker-compose entrypoint -- so no deploy had
# ever written them: prod's `archetypes` table was empty from the day the feature landed, and
# every dossier recorded `"archetypes": []`. Jens Stutte's rule from bug 2062119 has therefore
# never been in front of a production run.
#
# AFTER models.create(), not before: `archetypes` is a post-deploy table, so it only exists once
# _ensure_tables() has made it (see models._ADDED_TABLES).
#
# Idempotent and NON-DESTRUCTIVE -- seed() skips a slug that already has a row, because these are
# DB-editable on purpose: a deploy must not revert a tuned `guidance`, nor re-enable a row someone
# switched off after it misfired. `seed_quietly` swallows its own failures so a table it cannot
# write can never fail a deploy; the pipeline just runs with no hints, as it has all along.
print("release: seed archetypes:", archetypes.seed_quietly(), flush=True)
