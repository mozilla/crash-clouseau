# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from dateutil.relativedelta import relativedelta
from .models import Changeset
from . import config, inspector, pushlog


def filelog(filenames, buildid, channel, ndays):
    if filenames:
        res = Changeset.find(filenames, buildid, channel, ndays)
        if res is None:
            # the buildid modulo n days is not in the pushlog
            mindate = buildid - relativedelta(days=ndays)
            logs = pushlog.puhslog(mindate, buildid,
                                   channel=channel,
                                   file_filter=lambda f: f in filenames)
            res = {}
            for log in logs:
                node = log['node']
                for f in log['files']:
                    if f in res:
                        res[f].append(node)
                    else:
                        res[f] = [node]
        return res
    return None


def get(uuid, ndays=config.get_ndays()):
    res, channel, chgsets = inspector.get_crash_by_uuid(uuid, ndays, filelog)
