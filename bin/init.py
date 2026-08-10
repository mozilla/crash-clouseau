# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from crashclouseau import archetypes, models, update

# Create the schema on a fresh database. models.create() is idempotent: it only
# creates the tables when they don't exist yet and is a no-op otherwise, so it
# won't wipe existing data on container restarts (unlike models.clear()).
print("create schema")
models.create()
models.HGAuthor.get_default_id()

# Built-in crash archetypes. Idempotent and non-destructive: an existing row is left alone,
# because these are DB-editable on purpose and a deploy must not revert a tuned one.
print("seed archetypes:", archetypes.seed_quietly())

print("update")
update.update_all()
