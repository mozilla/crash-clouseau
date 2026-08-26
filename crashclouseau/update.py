# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import os
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
        if start_date is None:
            # Fresh database or no interesting changesets yet: use the last
            # completed scan when available, otherwise backfill the configured
            # window.
            _, start_date = models.LastDate.get(channel)
            if start_date is None:
                start_date = end_date - relativedelta(days=config.get_ndays_of_data())
            else:
                start_date += relativedelta(seconds=1)
        else:
            start_date += relativedelta(seconds=1)

    logger.info(
        "Get pushlog data for {} ({} to {}): started".format(
            channel, start_date, end_date
        )
    )
    data = pushlog(start_date, end_date, channel=channel)
    logger.info("Get pushlog data: retrieved")
    min_date, _ = models.Changeset.add(data, end_date, channel)
    logger.info("Get pushlog data: finished.")
    return end_date


def put_report(uuid, buildid, channel, product, chgset):
    """Put a report in the database"""
    if channel == "nightly":
        mindate = buildid - relativedelta(days=config.get_ndays())
    else:
        # +1 second, and on beta that second is load-bearing: the previous build row is the
        # merge-day build, whose revision is a MEMBER of the central->beta merge push, so
        # every one of that push's ~5,100 changesets carries exactly this pushdate and
        # `mindate` lands one second above all of them. The window is then the cycle's
        # uplifts (measured 45-122 changesets) instead of the whole merged cycle. See
        # `tests/test_beta_windows.py`, which pins it, and plan #18 item 9 for why the
        # merge-day `builds` row must never be removed.
        mindate = models.Build.get_pushdate_before(buildid, channel, product)
        if mindate is None:
            # No earlier build row: the builds table was pruned (`Node.clean` +
            # ON DELETE CASCADE) or this is the first build ever ingested on the channel.
            # Fall back to the nightly rule rather than lose the report — a wider window is
            # a worse window, not a broken one, and the scorer still ranks by line
            # proximity. Loud, because a persistent one means the builds table is not
            # keeping up with the uuid backlog.
            logger.warning(
                "no build before %s on %s/%s: falling back to a %d-day candidate window",
                utils.get_buildid(buildid), product, channel, config.get_ndays(),
            )
            mindate = buildid - relativedelta(days=config.get_ndays())
        else:
            mindate += relativedelta(seconds=1)

    interesting_chgsets = set()
    res = inspector.get_crash(
        uuid,
        buildid,
        channel,
        mindate,
        chgset,
        models.Changeset.find,
        interesting_chgsets,
    )
    if res is None:
        # 'json_dump' is not in crash data
        return

    useless = True
    chgsets = models.Changeset.to_analyze(chgsets=interesting_chgsets, channel=channel)
    for nodeid, node in chgsets:
        try:
            data = patch.parse(node, channel=channel)
        except Exception as e:
            # ONE candidate's diff, not the report. `patch.parse` now raises on a non-200
            # instead of handing back `{}` (hg answers 406 to a throttled bulk reader), and
            # letting that propagate would `UUID.set_error` a report whose only problem is
            # somebody else's rate limiter — and an errored report is never retried. The
            # changeset stays `analyzed=False`, so `analyze_patches` picks it up later; this
            # crash is simply scored without it.
            logger.warning("cannot parse %s on %s (%s): scoring without it", node, channel, e)
            continue
        models.Changeset.add_analyzis(data, nodeid, channel)

    frames = res.get("nonjava")
    sh = jsh = ""
    if frames:
        sh = frames["hash"]
        if not models.UUID.is_stackhash_existing(sh, buildid, channel, product, False):
            models.CrashStack.put_frames(uuid, frames, False, commit=True, channel=channel)
            useless = False

    jframes = res.get("java")
    if jframes:
        jsh = jframes["hash"]
        if not models.UUID.is_stackhash_existing(jsh, buildid, channel, product, True):
            models.CrashStack.put_frames(uuid, jframes, True, commit=True, channel=channel)
            useless = False

    models.UUID.add_stack_hash(uuid, sh, jsh)
    models.UUID.set_analyzed(uuid, useless)

    if not useless:
        # Fire the evidence agent for a scored report. Lazy import keeps
        # claude-agent-sdk out of the ingestion/web import path; wrapped so an
        # enqueue failure can never break ingestion.
        try:
            from .agent.orchestrator import enqueue_agent

            enqueue_agent(uuid, channel)
        except Exception as e:
            logger.warning("could not enqueue evidence agent for %s: %s", uuid, e)


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


def _chain_is_running(queue, func):
    """Is the self-re-enqueuing analysis chain for *func* already on *queue*?

    ``len(queue) <= 1`` USED TO STAND IN FOR THIS AND IT MEASURED THE WRONG THING. The clock
    enqueues one ``update`` job per (product, channel) onto this same queue every 20 minutes, so
    the depth it was reading was mostly the CLOCK's, not the chain's. With nightly alone an
    executing ``update`` saw an empty queue and the chain was seeded; with nightly + beta it can
    see 2, and ``analyze_reports`` becomes a silent no-op for that tick -- at three channels or
    two products, for every tick. It fails by doing NOTHING, with no log line: the shape of the
    four silent no-ops this codebase has already been bitten by.

    Counting only the chain's own function name is the whole fix. Best-effort: if the queue
    cannot be read we say NO and enqueue, because a duplicate chain job costs one redundant
    ``to_analyze`` (which returns the same row and is idempotent) while a missing one stalls
    ingestion until the next tick."""
    name = "{}.{}".format(func.__module__, func.__name__)
    try:
        return any(getattr(job, "func_name", None) == name for job in queue.jobs)
    except Exception:                                   # pragma: no cover - redis hiccup
        logger.warning("could not read the queue to check for %s; enqueuing anyway", name)
        return False


def analyze_reports():
    """Seed the report-scoring chain unless it is already running."""
    queue = worker.get_queue()
    if not _chain_is_running(queue, analyze_one_report):
        queue.enqueue_call(func=analyze_one_report, result_ttl=0)


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
    """Seed the patch-parsing chain unless it is already running. See ``_chain_is_running``."""
    queue = worker.get_queue()
    if not _chain_is_running(queue, analyze_one_patch):
        queue.enqueue_call(func=analyze_one_patch, result_ttl=0)


def update_builds(date, channel, product):
    """Fill the ``builds`` table from Buildhub, back to ``buildhub_lookback_ndays``.

    NOT ``get_ndays()`` (3). ``update()`` calls ``put_filelog`` first, which sets
    ``LastDate.maxdate`` to now, so ``date`` here is essentially "now" and the subtraction is
    the whole lookback. Three days is shorter than beta's build interval often enough that
    **71% of switch-on moments left the selection window under-filled** — see
    ``config.get_buildhub_lookback_ndays`` for the measurement. Idempotent (``put_data`` is
    ``on_conflict_do_nothing``), so the wider window costs one larger POST, not duplicate
    rows."""
    logger.info("Update builds for {}/{}: started.".format(channel, product))
    if not date:
        _, date = models.LastDate.get(channel)
        if date is None:
            date = pytz.utc.localize(datetime.utcnow())
        date -= relativedelta(days=config.get_buildhub_lookback_ndays())
    data = buildhub.get(date, channel, prods=product)
    if data:
        models.Build.put_data(data)
    logger.info("Update builds: finished.")


def put_crashes(date, channel, product):
    """Get and put crashes data in the database"""
    if not date:
        date = pytz.utc.localize(datetime.utcnow())
    data, selection = dc.get_new_signatures(product, channel, date)

    # Record what the selector DECLINED before anything else can fail: `stats` below only
    # ever holds the pairs we kept, so without this a signature we passed over leaves no
    # trace at all. Failure here is swallowed inside record_many/prune.
    models.Selection.record_many(selection, product, channel, run_date=date)
    models.Selection.prune()

    errors = set()
    for sgn, i in data.items():
        sgnid = None
        for bid, protos in i["protos"].items():
            bidid = models.Build.get_id(bid, channel, product)
            if bidid is None:
                errors.add(bid)
                continue
            if sgnid is None:
                sgnid = models.Signature.get_id(sgn)
            models.Stats.add(sgnid, bidid, i["bids"][bid], i["installs"][bid])
            for proto in protos:
                uuid = proto["uuid"]
                proto_sgn = proto["proto"]
                models.UUID.add(uuid, sgnid, proto_sgn, bidid, commit=False)
        models.commit()

    for bid in errors:
        logger.info("No buildid in db for {}/{}/{}".format(bid, product, channel))


def update(date, channel, product, analyze=True):
    """Update all the data for a given date/channel/product"""
    logger.info("Update data: started.")
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

    logger.info("Update data: finished.")


def update_in_queue(channel, product, date=None):
    """Update in the queue"""
    queue = worker.get_queue()
    queue.enqueue_call(func=update, args=(date, channel, product), result_ttl=0)


def update_all(products=None, channels=None, date=None):
    """Update all. Channels default to $INGEST_CHANNELS (space-separated) when set,
    else all configured channels — lets a canary ingest nightly-only
    (`heroku config:set INGEST_CHANNELS=nightly`) without touching the shared config
    (which also defines the CHANNEL_TYPE enum, so it must keep every channel)."""
    if products is None:
        products = config.get_products()
    if channels is None:
        channels = os.getenv("INGEST_CHANNELS", "").split() or config.get_channels()
    for product in products:
        for channel in channels:
            update_in_queue(channel, product)
