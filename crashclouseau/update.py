# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from datetime import datetime
from dateutil.relativedelta import relativedelta
from libmozdata import utils as lmdutils
import pytz
from .logger import logger
from .models import LastDate, UUID, CrashStack, Changeset
from .pushlog import pushlog
from . import datacollector as dc
from . import buildhub, config, inspector, models, utils, worker, patch


def put_filelog(channel='nightly', start_date=None, end_date=None):
    if not end_date:
        end_date = pytz.utc.localize(datetime.utcnow())
    if not start_date:
        _, start_date = LastDate.get(channel)

    logger.info('Get pushlog data: started')
    data = pushlog(start_date, end_date, channel=channel)
    logger.info('Get pushlog data: retrieved')
    min_date, _ = Changeset.add(data, end_date, channel)
    logger.info('Get pushlog data: added in db')
    return end_date


def put_report(uuid, buildid, channel, chgset):
    ndays = config.get_ndays()
    interesting_chgsets = set()
    res = inspector.get_crash(uuid, buildid,
                              channel, ndays,
                              chgset, Changeset.find,
                              interesting_chgsets)

    useless = True
    chgsets = Changeset.to_analyze(chgsets=interesting_chgsets, channel=channel)
    for nodeid, node in chgsets:
        data = patch.parse(node, channel=channel)
        Changeset.add_analyzis(data, nodeid, channel)

    frames = res.get('nonjava')
    sh = jsh = ''
    if frames:
        sh = frames['hash']
        if not UUID.is_stackhash_existing(sh, False):
            CrashStack.put_frames(uuid, frames, False, commit=True)
            useless = False

    jframes = res.get('java')
    if jframes:
        jsh = jframes['hash']
        if not UUID.is_stackhash_existing(jsh, True):
            CrashStack.put_frames(uuid, jframes, True, commit=True)
            useless = False

    UUID.add_stack_hash(uuid, sh, jsh)
    UUID.set_analyzed(uuid, useless)


def analyze_one_report():
    a = UUID.to_analyze()
    if a:
        try:
            put_report(*a)
        except Exception as e:
            logger.error(e, exc_info=True)
            UUID.set_error(a[0])
        analyze_reports()
    else:
        analyze_patches()


def analyze_reports():
    queue = worker.get_queue()
    if len(queue) <= 1:
        queue.enqueue_call(func=analyze_one_report,
                           result_ttl=0)


def analyze_one_patch():
    nodeid, node, channel = models.Changeset.to_analyze()
    if node:
        try:
            data = patch.parse(node, channel=channel)
            Changeset.add_analyzis(data, nodeid, channel)
        except Exception as e:
            logger.error(e, exc_info=True)
        analyze_patches()


def analyze_patches():
    queue = worker.get_queue()
    if len(queue) <= 1:
        queue.enqueue_call(func=analyze_one_patch,
                           result_ttl=0)


def update_builds(date, channel='nightly'):
    logger.info('Update builds: started.')
    if not date:
        _, date = LastDate.get(channel)
        date -= relativedelta(days=1)
    data = buildhub.get(date)
    models.Build.put_data(data)
    logger.info('Update builds: finished.')


def put_crashes(date=None, channel='nightly'):
    if not date:
        date = pytz.utc.localize(datetime.utcnow())
    products = utils.get_products()
    data = dc.get_new_signatures(products,
                                 date=date,
                                 channel=channel)

    errors = set()
    for prod, i in data.items():
        for sgn, j in i.items():
            sgnid = None
            for bid, protos in j['protos'].items():
                bidid = models.Build.get_id(bid, channel, prod)
                if bidid is None:
                    errors.add((bid, channel, prod))
                    continue
                if sgnid is None:
                    sgnid = models.Signature.get_id(sgn)
                models.Stats.add(sgnid, bidid, j['bids'][bid])
                for proto in protos:
                    uuid = proto['uuid']
                    proto_sgn = proto['proto']
                    UUID.add(uuid, sgnid, proto_sgn,
                             bidid, commit=False)
            models.commit()

    for bid, channel, prod in errors:
        logger.info('No buildid in db for {}/{}/{}'.format(bid, prod, channel))


def update(date):
    logger.info('Update data: started.')
    put_filelog()
    if date:
        date = lmdutils.get_date_ymd(date)
    update_builds(date)

    try:
        put_crashes(date=date)
    except Exception as e:
        logger.error(e, exc_info=True)

    analyze_reports()
    logger.info('Update data: finished.')


def update_in_queue(date=None):
    queue = worker.get_queue()
    queue.enqueue_call(func=update,
                       args=(date, ),
                       result_ttl=0)
