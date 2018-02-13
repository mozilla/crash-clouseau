# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from datetime import datetime
from dateutil.relativedelta import relativedelta
from libmozdata import socorro
from libmozdata.bugzilla import Bugzilla
import pytz
from . import utils
from .logger import logger


BZ_FIELDS = ['id', 'summary', 'status', 'dupe_of', 'cf_crash_signature']


def get_bz_search(signature, start_date):
    params = {'include_fields': BZ_FIELDS,
              'f1': 'cf_crash_signature',
              'o1': 'substring',
              'v1': signature,
              'f2': 'creation_ts',
              'o2': 'greaterthan',
              'v2': start_date}

    return params


def get_bugs(signature, wait=True):
    # return a dict: bugid => buginfo
    # if buginfo is None => security bug

    if not signature:
        return {}

    logger.info('Get bugs for signature {}: started.'.format(signature))

    def bug_handler(bug, data):
        if 'cf_crash_signature' in bug:
            if signature in utils.get_signatures([bug['cf_crash_signature']]):
                data[bug['id']] = bug
            del bug['cf_crash_signature']

    start_date = pytz.utc.localize(datetime.utcnow())
    start_date -= relativedelta(hours=2)
    data = {}
    bz = Bugzilla(get_bz_search(signature, start_date),
                  bughandler=bug_handler,
                  bugdata=data).get_data()

    bugs = socorro.Bugs.get_bugs([signature])[signature]
    bz.wait()
    bz_bugs = set(data.keys())

    old_bugs = []
    for bug in bugs:
        if bug not in bz_bugs:
            old_bugs.append(bug)
            # the bug is in Socorro and not in search query
            data[bug] = None

    bz = Bugzilla(bugids=old_bugs, include_fields=BZ_FIELDS,
                  bughandler=bug_handler, bugdata=data)
    if wait:
        bz.wait()
        logger.info('Get bugs: finished.')
        return data

    logger.info('Get bugs: finished.')

    return bz, data
