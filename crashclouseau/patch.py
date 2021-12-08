# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from libmozdata.hgmozilla import RawRevision
from parsepatch.patch import Patch
from .logger import logger
from . import utils


def parse(chgset, channel="nightly", chunk_size=1000000):
    url = RawRevision.get_url(channel)
    logger.info("Get patch for revision {}".format(chgset))
    try:
        res = Patch.parse_changeset(
            url, chgset, file_filter=utils.is_interesting_file, skip_comments=True
        )
        return res
    except Exception as e:
        msg = "Error in parsing patch with revision {}"
        logger.error(msg.format(chgset))
        raise e
