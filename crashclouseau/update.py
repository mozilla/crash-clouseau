# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from datetime import datetime
from dateutil.relativedelta import relativedelta
from libmozdata import utils as lmdutils
import pytz
from .logger import logger
from .pushlog import pushlog
from . import datacollector as dc
from . import buildhub, config, inspector, models, utils, worker, patch


def put_build(buildid, product, channel, version, node=None):
    """Put a build in the database"""
    buildid = utils.get_build_date(buildid)
    if not node:
        node = dc.get_changeset(buildid, channel, product)
    nodeid = models.Node.get_id(node, channel)
    models.Build.put_build(buildid, nodeid, product, channel, version)


def put_filelog(channel, start_date=None, end_date=None):
    """Get and put the filelog in the database"""
    if not end_date:
        end_date = pytz.utc.localize(datetime.utcnow())
    if not start_date:
        start_date = models.Node.get_max_date(channel)
        start_date += relativedelta(seconds=1)

    logger.info('Get pushlog data for {} ({} to {}): started'.format(channel, start_date, end_date))
    data = pushlog(start_date, end_date, channel=channel)
    logger.info('Get pushlog data: retrieved')
    min_date, _ = models.Changeset.add(data, end_date, channel)
    logger.info('Get pushlog data: finished.')
    return end_date


def put_report(uuid, buildid, channel, product, chgset):
    """Put a report in the database"""
    if channel == 'nightly':
        mindate = buildid - relativedelta(days=config.get_ndays())
    else:
        mindate = models.Build.get_pushdate_before(buildid, channel, product)
        mindate += relativedelta(seconds=1)

    interesting_chgsets = set()
    res = inspector.get_crash(uuid, buildid,
                              channel, mindate,
                              chgset, models.Changeset.find,
                              interesting_chgsets)
    if res is None:
        # 'json_dump' is not in crash data
        return

    useless = True
    chgsets = models.Changeset.to_analyze(chgsets=interesting_chgsets, channel=channel)
    for nodeid, node in chgsets:
        data = patch.parse(node, channel=channel)
        models.Changeset.add_analyzis(data, nodeid, channel)

    frames = res.get('nonjava')
    sh = jsh = ''
    if frames:
        sh = frames['hash']
        if not models.UUID.is_stackhash_existing(sh, buildid, channel, product, False):
            models.CrashStack.put_frames(uuid, frames, False, commit=True)
            useless = False

    jframes = res.get('java')
    if jframes:
        jsh = jframes['hash']
        if not models.UUID.is_stackhash_existing(jsh, buildid, channel, product, True):
            models.CrashStack.put_frames(uuid, jframes, True, commit=True)
            useless = False

    models.UUID.add_stack_hash(uuid, sh, jsh)
    models.UUID.set_analyzed(uuid, useless)


def analyze_one_report(uuid=None):
    """Get a non-analyzed UUID in the database and analyze it"""
    a = models.UUID.to_analyze(uuid)
    if a:
        try:
            put_report(*a)
        except Exception as e:
            logger.error(e, exc_info=True)
            models.UUID.set_error(a[0])
        analyze_reports()
    else:
        analyze_patches()


def analyze_reports():
    """Analyze all the non-analyzed reports available in the database"""
    queue = worker.get_queue()
    if len(queue) <= 1:
        queue.enqueue_call(func=analyze_one_report,
                           result_ttl=0)


def analyze_one_patch():
    """Get a non-analyzed patch in the database and analyze it"""
    nodeid, node, channel = models.Changeset.to_analyze()
    if node:
        try:
            data = patch.parse(node, channel=channel)
            models.Changeset.add_analyzis(data, nodeid, channel)
        except Exception as e:
            logger.error(e, exc_info=True)
        analyze_patches()


def analyze_patches():
    """Analyze all the non-analyzed patches available in the database"""
    queue = worker.get_queue()
    if len(queue) <= 1:
        queue.enqueue_call(func=analyze_one_patch,
                           result_ttl=0)


def update_builds(date, channel, product):
    """Update the builds"""
    logger.info('Update builds for {}/{}: started.'.format(channel,
                                                           product))
    if not date:
        _, date = models.LastDate.get(channel)
        date -= relativedelta(days=config.get_ndays())
    data = buildhub.get(date, channel, prods=product)
    if data:
        models.Build.put_data(data)
    logger.info('Update builds: finished.')


def put_crashes(date, channel, product):
    """Get and put crashes data in the database"""
    if not date:
        date = pytz.utc.localize(datetime.utcnow())
    data = dc.get_new_signatures(product,
                                 channel,
                                 date)

    errors = set()
    for sgn, i in data.items():
        sgnid = None
        for bid, protos in i['protos'].items():
            bidid = models.Build.get_id(bid, channel, product)
            if bidid is None:
                errors.add(bid)
                continue
            if sgnid is None:
                sgnid = models.Signature.get_id(sgn)
            models.Stats.add(sgnid, bidid, i['bids'][bid], i['installs'][bid])
            for proto in protos:
                uuid = proto['uuid']
                proto_sgn = proto['proto']
                models.UUID.add(uuid, sgnid, proto_sgn,
                                bidid, commit=False)
        models.commit()

    for bid in errors:
        logger.info('No buildid in db for {}/{}/{}'.format(bid, product, channel))


def update(date, channel, product, analyze=True):
    """Update all the data for a given date/channel/product"""
    logger.info('Update data: started.')
    put_filelog(channel)
    if date:
        date = lmdutils.get_date_ymd(date)
    update_builds(date, channel, product)

    try:
        put_crashes(date, channel, product)
    except Exception as e:
        logger.error(e, exc_info=True)

    if analyze:
        analyze_reports()

    logger.info('Update data: finished.')


def update_in_queue(channel, product, date=None):
    """Update in the queue"""
    queue = worker.get_queue()
    queue.enqueue_call(func=update,
                       args=(date, channel, product),
                       result_ttl=0)


def update_all(products=config.get_products(),
               channels=config.get_channels(),
               date=None):
    """Update all"""
    for product in products:
        for channel in channels:
            update_in_queue(channel, product)
