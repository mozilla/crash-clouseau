# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""How OLD was a crash signature when the build we are triaging was produced?

The triage pipeline's premise is "the regressor is somewhere in this build's pushlog window".
That premise only holds if the signature is actually NEW as of that build. When the signature
has existed for months, nothing in the window introduced it, and naming a window changeset
produces a confident-looking lead that cannot be right.

This is not hypothetical. Over the canary's first three prod days, 10 of 10 HIGH-confidence
blind-second-opinion refutations rested on exactly this argument, and all 10 verified
deterministically with a median gap of 178 days between the signature first appearing and the
named candidate landing (`spike/verify_so_timing_claims.py`). 24 of 32 reported leads came from
signatures already more than a week old.

One Socorro lookup answers it. Four gotchas, all learned the hard way and all encoded below:

  * The `build_id` FACET is ordered by COUNT, so truncating it silently drops the oldest build —
    the one value we need. Sort ASCENDING by `build_id` and take one row instead.
  * `_facets_size=10000` is rejected outright (HTTP 400).
  * A date range of exactly 365 days is rejected too ("Date range is bigger than 365 days"),
    because the implicit upper bound is *now*. Clamp to 364.
  * `date` filters the crash REPORT date, not the build date, so even a short window surfaces
    very old buildids — which is exactly what makes this cheap.

Window truncation can only make first-seen look NEWER than it really is, so an age computed here
is a LOWER bound and the resulting downweight is conservative.
"""
from datetime import datetime, timedelta, timezone

from libmozdata import socorro

from crashclouseau.logger import logger

# Socorro hard-rejects more than 365 days; the implicit "to now" upper bound pushes an exact
# 365 over the line.
_MAX_WINDOW_DAYS = 364


def _buildid_to_dt(buildid):
    """``YYYYMMDDHHMMSS`` (UTC) -> datetime, or None when it is not a buildid."""
    try:
        return datetime.strptime(str(buildid)[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def first_seen_buildid(signature, product="Firefox", channel="nightly",
                       days=_MAX_WINDOW_DAYS):
    """The OLDEST buildid this signature appears in, within ``days``. ``None`` when the lookup
    finds nothing or fails. Raises nothing."""
    if not signature:
        return None
    days = max(1, min(int(days or _MAX_WINDOW_DAYS), _MAX_WINDOW_DAYS))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    params = {
        "signature": "=" + signature,
        "product": product or "Firefox",
        "date": ">=" + since,
        # Sort ascending and take ONE row: the build_id facet is count-ordered, so paging it
        # can drop the oldest build, and an untruncated facet size is a 400.
        "_columns": ["build_id"],
        "_sort": "build_id",
        "_results_number": 1,
    }
    if channel:
        params["release_channel"] = channel
    got = {}

    def handler(json_, data):
        data["result"] = json_

    try:
        socorro.SuperSearch(params=params, handler=handler, handlerdata=got).wait()
    except Exception as exc:  # pragma: no cover - network; never break a seed
        logger.warning("sigage: first-seen lookup failed for %r: %s", signature, exc)
        return None
    for hit in (got.get("result") or {}).get("hits") or []:
        if hit.get("build_id"):
            return str(hit["build_id"])
    return None


def to_datetime(value):
    """Best-effort UTC datetime from any of the pushdate shapes the candidate builders produce:
    a tz-aware ``datetime`` (on-stack, straight from the DB column), hg's ``[epoch, tzoffset]``
    pair or a bare epoch number (off-stack, from ``json-pushes``), an ISO string, or a
    ``YYYYMMDDHHMMSS`` buildid. ``None`` when it is none of those."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (list, tuple)):
        return to_datetime(value[0]) if value else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.isdigit() and len(text) in (8, 14):
        return _buildid_to_dt(text)
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def days_landed_after_first_seen(first_seen, pushdate):
    """Days the candidate landed AFTER the signature was first seen. Positive means the crash
    already existed before the candidate landed — so that candidate cannot be its ORIGIN.
    ``None`` when either side is unknown/unparseable.

    This is the comparison that actually discriminates. Comparing first-seen against the crash's
    BUILD date instead does NOT: two thirds of all triaged signatures are old, so a build-based
    rule fires on ~83% of independently-CONFIRMED leads as well as on the wrong ones. Measured
    on 23 real prod leads with the blind second opinion as the yardstick, this comparison at a
    7-day threshold fires on 10/10 high-confidence refutations while sparing 5 of 6
    corroborated leads."""
    seen_dt = _buildid_to_dt(first_seen)
    push_dt = to_datetime(pushdate)
    if seen_dt is None or push_dt is None:
        return None
    return round((push_dt - seen_dt).total_seconds() / 86400.0, 1)
