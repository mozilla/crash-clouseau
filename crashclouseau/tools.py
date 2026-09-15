# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from . import buildsource, datacollector, models


def get_changeset(buildid, channel, product):
    """The hg revision a build was made from: the ``builds`` table, then the product's build
    source (``buildhub`` for desktop, ``tcindex`` for Fenix -- the identical Buildhub function
    for Firefox, see ``buildsource``), then Socorro's topmost-filename vote.

    The vote is a Buildhub-only rung: it filters frames on ``hg:hg.mozilla.org/`` and a Fenix
    report's frames are ``git:github.com/...`` URIs, so for a TaskCluster product it costs a
    Socorro query and returns None (and ``tcindex.get_rev_from`` already ends on the push at
    the buildid second, which is total for a CI build)."""
    chgset = models.Build.get_changeset(buildid, channel, product)
    if not chgset:
        source = buildsource.for_product(product)
        chgset = source.get_rev_from(buildid, channel, product)
        if not chgset and source is buildsource.buildhub:
            chgset = datacollector.get_changeset(buildid, channel, product)
    return chgset
