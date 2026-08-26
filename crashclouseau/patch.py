# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from libmozdata.hgmozilla import RawRevision
from parsepatch.patch import Patch
from .logger import logger
from . import net, utils


class EmptyPatch(Exception):
    """``raw-rev`` answered, and the parse found no interesting file in it.

    Distinct from a fetch failure (which raises out of ``net.get``): this one is a
    LEGITIMATE answer for a changeset that touches nothing we score on. It exists so
    ``Changeset.add_analyzis`` can tell the two apart — see ``models.Changeset.add_analyzis``,
    which now refuses to mark a changeset analysed on an empty parse."""


def parse(chgset, channel="nightly", chunk_size=1000000):
    """Fetch and parse one changeset's diff into ``{filename: {added, deleted, touched, new}}``.

    FETCHED THROUGH ``net.get``, NOT through ``parsepatch``. ``Patch.parse_changeset`` does a
    bare ``requests.get(url, stream=True)`` -- no User-Agent, no timeout, no
    ``raise_for_status`` -- and it was the only HTTP client in the tree off the allowlisted
    ``crash-clouseau`` UA. hg.mozilla.org rate-limits an unidentified bulk reader with **406**,
    and a 406 body parses to ``{}``, which the caller then wrote to the database as
    "analysed, no lines touched": that candidate scores 0 forever, unrecoverably without
    ``Changeset.reset``, and nothing logged an error.

    Measured on ``releases/mozilla-beta/raw-rev/0535872fe489`` (2026-08-25), three clients
    against the same URL: bare ``requests`` **406 / 0 bytes**; an explicit
    ``python-requests/2.32`` UA **406 / 0 bytes**; ``net.get`` **200 / 8,881 bytes**. The
    discrimination is on the UA and it is deterministic. Serial parses got away with it (12
    of 12 succeeded); a 189-fetch burst tripped the throttle and then 100% of bare fetches
    returned ``{}`` while ``net.get`` stayed at 200 -- and 330 patches parsed cleanly through
    ``net.get`` with 0 failures. Live on nightly today; beta only multiplies the trigger
    (~+12% in-cycle volume, and a merge push that slips ``pushlog.collect``'s merge rule
    would dump 1,932-2,678 fetches at once).

    ``chunk_size`` is kept for call-signature compatibility and is unused: a raw-rev body is
    a handful of KB to a few MB and ``net.get``'s read timeout is a gap-between-bytes limit,
    so there is nothing to stream around.

    Raises on a non-200 (the caller logs and leaves the changeset un-analysed for a retry)
    and returns ``{}`` when the diff genuinely touches no interesting file."""
    url = "{}/{}".format(RawRevision.get_url(channel), chgset)
    logger.info("Get patch for revision {}".format(chgset))
    try:
        r = net.get(url)
        r.raise_for_status()
        return Patch.parse_patch(
            r.text, file_filter=utils.is_interesting_file, skip_comments=True
        )
    except Exception as e:
        msg = "Error in parsing patch with revision {}"
        logger.error(msg.format(chgset))
        raise e
