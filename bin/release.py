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
from crashclouseau import models

print("release: ensuring DB schema", flush=True)
fresh = models.create()
models.HGAuthor.get_default_id()
print("release: schema {}".format("created" if fresh else "already present"), flush=True)
