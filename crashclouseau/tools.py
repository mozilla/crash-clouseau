# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from . import buildhub, datacollector, models


def get_changeset(buildid, channel, product):
    chgset = models.Build.get_changeset(buildid, channel, product)
    if not chgset:
        chgset = buildhub.get_rev_from(buildid, channel, product)
        if not chgset:
            chgset = datacollector.get_changeset(buildid, channel, product)
    return chgset
