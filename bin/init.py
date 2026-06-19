# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from crashclouseau import models, update

# Create the schema on a fresh database. models.create() is idempotent: it only
# creates the tables when they don't exist yet and is a no-op otherwise, so it
# won't wipe existing data on container restarts (unlike models.clear()).
print("create schema")
models.create()
models.HGAuthor.get_default_id()

print("update")
update.update_all()
