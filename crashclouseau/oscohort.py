# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Measure OS-version concentration and growth for a crash signature.

Compare the signature's shares with channel-wide shares within each OS family.
"""
from datetime import datetime, timedelta, timezone

from libmozdata import socorro

from crashclouseau import config, utils
from crashclouseau.logger import logger

FIELD = "platform_pretty_version"
DAYS = 56
MIN_REPORTS = 20
MIN_SHARE = 0.5
MIN_RATIO = 3.0
MIN_INSTALLS = 10
# Compare smoothed count ratios between window halves; require stronger growth in the cohort.
RISE_RATIO = 2.0
RISE_OVER_REST = 1.5
REST_MAX_RISE = 2.0

NO_OS_COHORT = {"cohort": None}

# Cache successful baselines by product, channel and window start.
_BASELINES = {}

# Android API level -> marketing version.
_ANDROID = {"21": "5.0", "22": "5.1", "23": "6", "24": "7.0", "25": "7.1", "26": "8.0",
            "27": "8.1", "28": "9", "29": "10", "30": "11", "31": "12", "32": "12L", "33": "13",
            "34": "14", "35": "15", "36": "16", "37": "17", "38": "18"}


def family(term):
    """The OS family of a ``platform_pretty_version`` term."""
    term = str(term or "")
    if term in ("", "Unknown"):
        return None
    for prefix, name in (("Windows", "Windows"), ("macOS", "macOS"), ("OS X", "macOS"),
                         ("Mac OS", "macOS"), ("Android", "Android")):
        if term.startswith(prefix):
            return name
    return "Linux"


def label(term):
    """``Android 37`` -> ``Android API 37 (Android 17)``; other terms unchanged."""
    term = str(term or "")
    if term.startswith("Android "):
        api = term.split(" ", 1)[1]
        if api in _ANDROID:
            return "Android API {} (Android {})".format(api, _ANDROID[api])
    return term


def _counts(rows):
    return {str(r.get("term")): int(r.get("count") or 0)
            for r in rows or [] if isinstance(r, dict) and r.get("term") is not None}


def summarize(result, baseline, since, days=DAYS, product=None, channel=None):
    """Compare signature and baseline facets; return {"cohort": None} when unavailable."""
    facets = (result or {}).get("facets") or {}
    own = _counts(facets.get(FIELD))
    if not own or not baseline:
        return dict(NO_OS_COHORT)
    # Evaluate each family so Windows traffic cannot hide a macOS concentration.
    leaders = {}
    for t, n in own.items():
        fam = family(t)
        if fam and (fam not in leaders or (n, t) > (own[leaders[fam]], leaders[fam])):
            leaders[fam] = t
    candidates = []
    for term in leaders.values():
        candidate = _candidate(term, own, baseline, facets, since, days)
        if candidate:
            candidates.append(candidate)
    if not candidates:
        return dict(NO_OS_COHORT)
    best = max(candidates, key=lambda c: (c["skewed"], c["reports"], c["cohort"]))
    return {"label": label(best["cohort"]), "product": product, "channel": channel,
            "since": since, "days": days, "total": int(result.get("total") or 0), **best}


def _candidate(top, own, baseline, facets, since, days):
    """Compare one version with its family; return None without baseline counts."""
    fam = family(top)
    sig_family = sum(n for t, n in own.items() if family(t) == fam)
    base_family = sum(n for t, n in baseline.items() if family(t) == fam)
    if not sig_family or not base_family or not baseline.get(top):
        return None
    share = own[top] / sig_family
    base_share = baseline[top] / base_family
    ratio = share / base_share
    installs = None
    for row in facets.get(FIELD) or []:
        if str(row.get("term")) == top:
            card = (row.get("facets") or {}).get("cardinality_install_time") or {}
            installs = card.get("value")
            break
    halves, weekly = _history(facets.get("histogram_date") or [], top, since, days)
    early, late, rest_early, rest_late = halves
    cohort_rise = (late + 1) / (early + 1)
    rest_rise = (rest_late + 1) / (rest_early + 1)
    skewed = all((
        own[top] >= MIN_REPORTS,
        share >= MIN_SHARE,
        ratio >= MIN_RATIO,
        installs is None or installs >= MIN_INSTALLS,
    ))
    rising = all((
        cohort_rise >= RISE_RATIO,
        cohort_rise >= RISE_OVER_REST * rest_rise,
        rest_rise <= REST_MAX_RISE,
    ))
    return {
        "cohort": top, "family": fam, "reports": own[top], "family_reports": sig_family,
        "installs": installs, "share": round(share, 3), "baseline_share": round(base_share, 3),
        "ratio": round(ratio, 1), "skewed": skewed,
        "halves": {"cohort": [early, late], "rest": [rest_early, rest_late]},
        "cohort_rise": round(cohort_rise, 1), "rest_rise": round(rest_rise, 1),
        "rising": skewed and rising,
        "weekly": weekly,
    }


def _history(histogram, top, since, days):
    """Count cohort/rest reports by window half and full weeks ending on the last report day."""
    buckets = sorted(histogram, key=lambda b: b["term"])
    if not buckets:
        return (0, 0, 0, 0), []
    first = datetime.fromisoformat(str(buckets[0]["term"])[:10])
    last = datetime.fromisoformat(str(buckets[-1]["term"])[:10])
    mid = datetime.fromisoformat(since) + timedelta(days=days // 2)
    early = late = rest_early = rest_late = 0
    weeks = {}
    for bucket in buckets:
        day = datetime.fromisoformat(str(bucket["term"])[:10])
        counts = _counts((bucket.get("facets") or {}).get(FIELD))
        mine = counts.get(top, 0)
        rest = int(bucket.get("count") or 0) - mine
        if day < mid:
            early, rest_early = early + mine, rest_early + rest
        else:
            late, rest_late = late + mine, rest_late + rest

        back = (last - day).days
        week = last - timedelta(days=back // 7 * 7 + 6)
        row = weeks.setdefault(week, [week.strftime("%Y-%m-%d"), 0, 0])
        row[1] += mine
        row[2] += rest
    return (early, late, rest_early, rest_late), [weeks[w] for w in sorted(weeks) if w >= first]


def _search(params):
    result = {}
    socorro.SuperSearch(params=params, handler=lambda j, d: d.update(j),
                        handlerdata=result).wait()
    return result


def _scope(product, channel, since):
    """Scope queries to the product, channel, date and ESR major, if specified."""
    params = {"product": product, "release_channel": utils.get_search_channel(channel),
              "date": ">=" + since}
    major = config.esr_major(channel)
    if major is not None:
        params["major_version"] = major
    return params


def _baseline(product, channel, since):
    key = (product, channel, since)
    if key in _BASELINES:
        return _BASELINES[key]
    params = dict(_scope(product, channel, since), _results_number=0, _facets=FIELD,
                  _facets_size=500)
    counts = _counts((_search(params).get("facets") or {}).get(FIELD))
    if counts:
        for stale in [k for k in _BASELINES if k[2] != since]:
            del _BASELINES[stale]
        _BASELINES[key] = counts
    return counts


def os_cohort(signature, product="Firefox", channel="nightly", days=DAYS, now=None):
    """Fetch cohort facts; return NO_OS_COHORT on missing input or lookup failure."""
    if not signature or not channel:
        return dict(NO_OS_COHORT)
    product = product or "Firefox"
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    params = {
        "signature": "=" + signature, **_scope(product, channel, since),
        "_results_number": 0, "_facets": FIELD, "_facets_size": 200,
        "_aggs." + FIELD: "_cardinality.install_time",
        "_histogram.date": FIELD, "_histogram_interval.date": "1d",
    }
    try:
        result = _search(params)
        baseline = _baseline(product, channel, since)
    except Exception as exc:  # pragma: no cover - network failure
        logger.warning("oscohort: lookup failed for %r: %s", signature, exc)
        return dict(NO_OS_COHORT)
    return summarize(result, baseline, since, days, product, channel)
