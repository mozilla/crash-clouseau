# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Detect rises across builds on two channels of the same product."""
import json
from datetime import datetime, timedelta, timezone

from libmozdata import socorro

from . import config, report_bug, utils
from .logger import logger

_FACETS_SIZE = 10000
# Share results across RQ job processes.
_CACHE_KEY = "clouseau:onset:{}:{}"
_CACHE_TTL_S = 3 * 3600
_LOCK_TTL_S = 15 * 60


def _hour(asof):
    if not isinstance(asof, datetime):
        asof = datetime.now(timezone.utc)
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=timezone.utc)
    return asof.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _signature_counts(product, channel, start, end, installs):
    """``{signature: (reports, installs)}`` for one channel and window."""
    got = {}
    params = {
        "product": product,
        "release_channel": utils.get_search_channel(channel),
        "date": [">=" + report_bug._socorro_time(start), "<" + report_bug._socorro_time(end)],
        "_results_number": 0,
        "_facets": "signature",
        "_facets_size": _FACETS_SIZE,
    }
    if installs:
        params["_aggs.signature"] = "_cardinality.install_time"

    def handler(json, data):
        data.update(json)

    socorro.SuperSearch(params=params, handler=handler, handlerdata=got).wait()
    rows = (got.get("facets") or {}).get("signature")
    if rows is None or got.get("errors"):
        raise LookupError("no complete signature facet for {} {}: {}".format(
            product, channel, got.get("errors")))
    out = {}
    for row in rows:
        agg = (row.get("facets") or {}).get("cardinality_install_time") or {}
        out[row["term"]] = (int(row.get("count") or 0), int(agg.get("value") or 0))
    return out


def _day_buckets(start, end):
    """Number of UTC calendar days intersecting ``[start, end)``."""
    return ((end - timedelta(microseconds=1)).date() - start.date()).days + 1


def rising_on_two(onsets, rule, channels):
    """Return ``(first, second)``: the loudest channel meeting ``rule["first"]`` and another
    meeting ``rule["second"]``, or ``None``."""
    rows = {ch: row for ch, row in (onsets or {}).items() if ch in channels}

    def loudest(bar):
        return sorted(report_bug.channels_with_onset(rows, bar),
                      key=lambda ch: (-rows[ch]["reports"], ch))

    seconds = loudest(rule["second"])
    for first in loudest(rule["first"]):
        for second in seconds:
            if first != second:
                return first, second
    return None


def _find(product, rule, channels, now):
    start = now - timedelta(hours=int(rule["hours"]))
    days = int(rule["baseline_days"])
    first, second = rule["first"], rule["second"]
    recent = {ch: _signature_counts(product, ch, start, now, True) for ch in channels}
    # The mean per UTC-day bucket is a lower bound on the busiest day's count.
    base_start = start - timedelta(days=days)
    buckets = _day_buckets(base_start, start)
    totals = {ch: _signature_counts(product, ch, base_start, start, False) for ch in channels}

    def may_pass(ch, sig, bar):
        reports, installs = recent[ch].get(sig, (0, 0))
        # A missing baseline total must not rule out a candidate.
        mean = totals[ch][sig][0] / buckets if sig in totals[ch] else 0
        return report_bug.meets_onset(reports, installs, mean, bar)

    shortlist = set()
    for ch in channels:
        for sig in recent[ch]:
            if may_pass(ch, sig, first) and any(
                    may_pass(other, sig, second) for other in channels if other != ch):
                shortlist.add(sig)
    found = []
    for sig in sorted(shortlist):
        if config.is_ignored_signature(sig) or utils.signature_class(sig) == "empty":
            continue
        onsets = report_bug.fetch_channel_onsets(
            {"signature": sig, "product": product}, rule["hours"], days, asof=now)
        pair = rising_on_two(onsets, rule, channels)
        if pair:
            found.append({"signature": sig, "first": pair[0], "second": pair[1],
                          "onsets": {ch: onsets[ch] for ch in pair}})
    logger.info("onset: %s has %d two-channel rise(s) in %d shortlisted at %s", product,
                len(found), len(shortlist), now.isoformat())
    return found


def _redis():
    from . import worker

    return worker.conn


def find(product, asof=None):
    """Return two-channel rises, cached in Redis by product and UTC hour.

    Return [] when disabled, a scan fails, or another scan holds the lock.
    Redis errors fall back to an uncached scan.
    """
    import redis

    rule = config.get_onset_selection()
    channels = tuple((rule or {}).get("channels", {}).get(product) or ())
    if not rule or len(channels) < 2:
        return []
    now = _hour(asof)
    key = _CACHE_KEY.format(product, now.strftime("%Y%m%d%H"))
    conn = lock = None
    try:
        conn = _redis()
        cached = conn.get(key)
        if cached is not None:
            return json.loads(cached)
        lock = conn.lock(key + ":lock", timeout=_LOCK_TTL_S, blocking=False)
        if not lock.acquire():
            logger.info("onset: another scan of %s is running for %s", product, now.isoformat())
            return []
    except (redis.exceptions.RedisError, ValueError) as exc:
        logger.warning("onset: no shared cache for %s (%s); scanning", product, exc)
        conn = lock = None
    try:
        found = _find(product, rule, channels, now)
    except Exception:
        logger.error("onset: cannot scan %s", product, exc_info=True)
        found = None
    try:
        try:
            if found is not None and conn is not None:
                conn.set(key, json.dumps(found), ex=_CACHE_TTL_S)
        finally:
            if lock is not None:
                lock.release()
    except redis.exceptions.RedisError as exc:
        logger.warning("onset: could not share the scan of %s (%s)", product, exc)
    return found or []
