# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Detect signatures whose share of crash-reporting installations rises on a new release major.

Compare the current major's first days with the previous major's release period.
"""

import math
from datetime import datetime, timedelta, timezone

from . import config, sigage, spikes, utils
from .logger import logger

# Infer release days from each major's share of channel reports.
_RELEASE_SHARE = 0.05
_RELEASE_LOOKBACK_DAYS = 70
# Look back from release for the Nightly/Beta comparison.
_CYCLE_DAYS = 84
# Complete facets are needed to distinguish zero from an omitted build.
_BUILD_FACETS = 1000

# Cache successful queries for completed date windows.
_INSTALLS_CACHE: dict = {}
_CONCURRENT_Z = 2.0


def _query_params(product, channel, start, end, major=None):
    """Scope a Socorro query to a product/channel and ``[start, end)``."""
    params = {"product": product, "release_channel": utils.get_search_channel(channel),
              "date": [">=" + start.isoformat(), "<" + end.isoformat()], "_results_number": 0}
    if major is not None:
        params["major_version"] = str(major)
    return params


def release_days(product, channel, today):
    """Return ``{major: release date}`` within the lookback, or ``None`` on a failed read."""
    since = today - timedelta(days=_RELEASE_LOOKBACK_DAYS)
    params = dict(_query_params(product, channel, since, today + timedelta(days=1)),
                  **{"_histogram.date": "major_version", "_histogram_interval.date": "1d"})
    try:
        found = spikes._search(params)
    except Exception:
        logger.warning("versionstep: cannot read %s %s release days", product, channel,
                       exc_info=True)
        return None
    buckets = ((found if isinstance(found, dict) else {}).get("facets") or {}).get(
        "histogram_date")
    if not isinstance(buckets, list):
        return None
    out = {}
    for bucket in sorted(buckets, key=lambda b: b["term"]):
        day = datetime.strptime(str(bucket.get("term") or "")[:10], "%Y-%m-%d").date()
        total = int(bucket.get("count") or 0)
        for t in (bucket.get("facets") or {}).get("major_version") or []:
            try:
                major, count = int(t.get("term")), int(t.get("count") or 0)
            except (TypeError, ValueError):
                continue
            if major not in out and total and count >= _RELEASE_SHARE * total:
                out[major] = day
    # A major already current at the lookback's start has no known release day.
    return {m: d for m, d in out.items() if d > since + timedelta(days=1)}


def daily_installs(product, channel, major, start, end, signatures=None):
    """Return ``{date: distinct installations}`` for ``major`` on ``[start, end)``, or ``None``
    on a failed read. A day absent from the result had no report."""
    params = dict(_query_params(product, channel, start, end, major),
                  **{"_histogram.date": "_cardinality.install_time",
                     "_histogram_interval.date": "1d"})
    if signatures:
        params["signature"] = ["=" + s for s in signatures]
    try:
        found = spikes._search(params)
        buckets = found["facets"]["histogram_date"]
        return {datetime.strptime(str(b["term"])[:10], "%Y-%m-%d").date():
                int(b["facets"]["cardinality_install_time"]["value"]) for b in buckets}
    except Exception:
        logger.warning("versionstep: cannot read %s %s %s daily installations", product,
                       channel, major, exc_info=True)
        return None


def onset(sig_daily, all_daily, prev, cur, ratio):
    """Classify onset from daily installations in each major's comparison window.

    Return ``boundary``, ``later`` (with a date), or ``unresolved`` via ``sigage.step_timing``.
    Use installations so repeated reports do not shift the onset.
    """
    n = sum(sig_daily[prev].get(d, 0) for d in all_daily[prev])
    a = sum(all_daily[prev].values())
    if not n or not a:
        return {"kind": "unresolved"}
    row = {"daily": [[d.isoformat(), sig_daily[cur].get(d, 0), total]
                     for d, total in sorted(all_daily[cur].items())]}
    kind, day = sigage.step_timing(row, 1000.0 * n / a, ratio)
    if kind == "boundary":
        return {"kind": "boundary"}
    return {"kind": "later", "day": day} if day else {"kind": "unresolved"}


def installs(product, channel, major, start, end, facets_size):
    """Return ``{"installs", "signatures": {sig: [reports, installs]}}`` for ``major`` on
    ``[start, end)``, or ``None`` on a failed read."""
    key = (product, channel, major, start, end, facets_size)
    if key in _INSTALLS_CACHE:
        return _INSTALLS_CACHE[key]
    total = period_installs(product, channel, major, start, end)
    if total is None:
        return None
    base = _query_params(product, channel, start, end, major)
    try:
        sigs = spikes._search(dict(base, _facets="signature", _facets_size=facets_size,
                                   **{"_aggs.signature": "_cardinality.install_time"}))
    except Exception:
        logger.warning("versionstep: cannot read %s %s %s installs", product, channel, major,
                       exc_info=True)
        return None
    try:
        out = {"installs": total,
               "signatures": {
                   f["term"]: [int(f["count"]),
                               int(f["facets"]["cardinality_install_time"]["value"])]
                   for f in sigs["facets"]["signature"]}}
    except (KeyError, TypeError, ValueError):
        return None
    if end <= datetime.now(timezone.utc).date():
        _INSTALLS_CACHE[key] = out
    return out


def _excess(a, a_total, b, b_total):
    """``(z, ratio)`` of ``b`` of ``b_total`` installations against ``a`` of ``a_total``."""
    p = b_total / (a_total + b_total)
    n = a + b
    z = (b - n * p) / math.sqrt(n * p * (1 - p))
    return z, (b / b_total) / (a / a_total)


def carrier_version(sig_versions, all_versions, a, a_total, z_min, ratio):
    """Return the earliest version whose installation share clears the scan's bars against the
    previous major's ``a`` of ``a_total``, else the one with the highest excess, else ``None``."""
    best = None
    for version in sorted(all_versions, key=sigage._version_key):
        b, total = sig_versions.get(version, 0), all_versions[version]
        if not b or not total:
            continue
        z, r = _excess(a, a_total, b, total)
        if z >= z_min and r >= ratio:
            return version
        if best is None or z > best[0]:
            best = (z, version)
    return best[1] if best else None


def period_installs(product, channel, major, start, end, signatures=None):
    """Count distinct install times on ``[start, end)``, optionally filtered by signatures.

    Return ``None`` if the count cannot be read."""
    params = dict(_query_params(product, channel, start, end, major),
                  _facets="_cardinality.install_time")
    if signatures:
        params["signature"] = ["=" + s for s in signatures]
    try:
        return int(spikes._search(params)["facets"]["cardinality_install_time"]["value"])
    except Exception:
        logger.warning("versionstep: cannot read %s %s %s installations of %s", product,
                       channel, major, (signatures or ["all"])[:1], exc_info=True)
        return None


def concurrent(a, a_total, b, b_total, ratio):
    """Compare the previous major's signature share before and during the new release.

    ``a / a_total`` is the baseline; ``b / b_total`` is the concurrent share.
    ``rose`` requires both the ratio and z thresholds. Unreadable counts return ``None``."""
    if b is None or not a or not a_total or not b_total:
        return None
    z, r = _excess(a, a_total, b, b_total)
    return {"installs": b, "all_installs": b_total, "ratio": round(r, 2), "z": round(z, 2),
            "rose": z >= _CONCURRENT_Z and r >= ratio}


def score(before, now, z_min, ratio, min_installs):
    """Return the signatures whose installation share rose from ``before`` to ``now``.

    Scale the installation floor by the periods' population ratio. Skip signatures absent
    from the baseline. Both the binomial excess and share ratio must clear their thresholds.
    """
    a_total, b_total = before.get("installs") or 0, now.get("installs") or 0
    if not a_total or not b_total:
        return []
    floor = min_installs * b_total / a_total
    out = []
    for signature, (reports, b) in (now.get("signatures") or {}).items():
        prior = (before.get("signatures") or {}).get(signature)
        if not prior or not prior[1] or b < floor:
            continue
        a = prior[1]
        z, r = _excess(a, a_total, b, b_total)
        if z >= z_min and r >= ratio:
            out.append({"signature": signature, "installs": b, "reports": reports,
                        "from_installs": a, "from_reports": prior[0],
                        "ratio": round(r, 2), "z": round(z, 2)})
    return sorted(out, key=lambda f: -f["z"])


def scan(product, channel, today=None, cfg=None):
    """Return the current major's step against the previous major, or ``None`` on a failed read.

    ``fires`` is empty outside ``[first_day, last_day]`` of the current major's release."""
    cfg = cfg or config.get_version_step()
    today = today or datetime.now(timezone.utc).date()
    days = release_days(product, channel, today)
    if days is None:
        return None
    out = {"product": product, "channel": channel, "fires": []}
    if len(days) < 2:
        return dict(out, skipped="fewer than two releases in the lookback")
    cur = max(days)
    prev = cur - 1
    if prev not in days:
        return dict(out, skipped="no release day for {}".format(prev))
    age = (today - days[cur]).days
    out.update(major=cur, from_major=prev, release_day=days[cur], from_release_day=days[prev],
               age=age)
    if age < cfg["first_day"] or age > cfg["last_day"]:
        return dict(out, skipped="day {} of {}".format(age, cur))
    before = installs(product, channel, prev, days[prev], days[cur], cfg["facets_size"])
    now = installs(product, channel, cur, days[cur], today, cfg["facets_size"])
    if before is None or now is None:
        return None
    fires = score(before, now, cfg["z_min"], cfg["ratio"], cfg["min_installs"])
    out.update(installs=now["installs"], from_installs=before["installs"], fires=fires)
    if not fires:
        return out
    spans = {prev: (days[prev], days[cur]), cur: (days[cur], today)}
    all_daily = {m: daily_installs(product, channel, m, *span)
                 for m, span in spans.items()}
    by_version = _query_params(product, channel, days[cur], today, cur)
    all_versions = _versions(by_version)
    # Query each signature directly: capped facets can omit nonzero counts.
    during = period_installs(product, channel, prev, days[cur], today)
    for fire in fires:
        same = (period_installs(product, channel, prev, days[cur], today, [fire["signature"]])
                if during else None)
        fire["concurrent"] = concurrent(fire["from_installs"], before["installs"], same, during,
                                        cfg["ratio"])
        sig_versions = (_versions(dict(by_version, signature=["=" + fire["signature"]]))
                        if all_versions is not None else None)
        fire["version"] = (carrier_version(sig_versions, all_versions, fire["from_installs"],
                                           before["installs"], cfg["z_min"], cfg["ratio"])
                           if sig_versions is not None else None)
        sig_daily = {m: daily_installs(product, channel, m, *span, [fire["signature"]])
                     for m, span in spans.items()}
        readable = all(v is not None for series in (all_daily, sig_daily) for v in series.values())
        fire["onset"] = (onset(sig_daily, all_daily, prev, cur, cfg["ratio"])
                         if readable else {"kind": "unresolved"})
    return out


def spike_facts(result, fire, cfg):
    """The escalation's ``spike`` payload for one firing of ``scan``."""
    return {"kind": "version_step", "product": result["product"], "channel": result["channel"],
            "major": result["major"], "from_major": result["from_major"],
            "release_day": result["release_day"].isoformat(),
            "from_release_day": result["from_release_day"].isoformat(), "age": result["age"],
            "installs": fire["installs"], "reports": fire["reports"],
            "from_installs": fire["from_installs"], "from_reports": fire["from_reports"],
            "all_installs": result["installs"], "from_all_installs": result["from_installs"],
            "ratio": fire["ratio"], "z": fire["z"], "ratio_min": cfg["ratio"],
            "onset": fire.get("onset") or {"kind": "unresolved"}, "version": fire.get("version"),
            "concurrent": fire.get("concurrent"),
            "z_min": cfg["z_min"], "min_installs": cfg["min_installs"],
            "window_start": result["release_day"].isoformat(),
            "window_end": (result["release_day"] + timedelta(days=result["age"] - 1)).isoformat()}


def _versions(params):
    """``{version: installations}`` for ``params``, or ``None`` on a failed read."""
    try:
        return _facet_installs(params, "version")
    except Exception:
        logger.warning("versionstep: cannot read the versions of %s", params.get("signature"),
                       exc_info=True)
        return None


def _facet_installs(params, field):
    params = dict(params, _results_number=0, _facets=field, _facets_size=_BUILD_FACETS,
                  **{"_aggs." + field: "_cardinality.install_time"})
    found = spikes._search(params)
    facets = found.get("facets") if isinstance(found, dict) else None
    terms = facets.get(field) if isinstance(facets, dict) else None
    if not isinstance(terms, list):
        raise ValueError("no {} facet in the response".format(field))
    if len(terms) >= _BUILD_FACETS:
        raise ValueError("{} facet truncated at {} terms".format(field, _BUILD_FACETS))
    return {str(f["term"]): int(f["facets"]["cardinality_install_time"]["value"])
            for f in terms}


def cycle_series(product, signatures, major, release_day):
    """Return the signature's installation shares on nightly and beta for ``major`` and the
    major before, and its nightly per-build series for ``major``. ``None`` on a failed read."""
    since = (release_day - timedelta(days=_CYCLE_DAYS)).isoformat()
    sig = ["=" + s for s in signatures]
    nightly = {"product": product, "release_channel": "nightly", "date": ">=" + since,
               "version": ["{}.0a1".format(major - 1), "{}.0a1".format(major)]}
    beta = {"product": product, "release_channel": utils.get_search_channel("beta"),
            "date": ">=" + since, "major_version": [str(major - 1), str(major)]}
    try:
        n_sig = _facet_installs(dict(nightly, signature=sig), "version")
        n_all = _facet_installs(nightly, "version")
        b_sig = _facet_installs(dict(beta, signature=sig), "major_version")
        b_all = _facet_installs(beta, "major_version")
        builds_sig = _facet_installs(dict(nightly, signature=sig,
                                          version="{}.0a1".format(major)), "build_id")
        builds_all = _facet_installs(dict(nightly, version="{}.0a1".format(major)), "build_id")
    except Exception:
        logger.warning("versionstep: cannot read the %s cycle of %s", major, signatures[:1],
                       exc_info=True)
        return None

    def share(sig_counts, all_counts, key):
        a = all_counts.get(key) or 0
        return {"installs": sig_counts.get(key, 0), "all_installs": a,
                "per_10k": round(1e4 * sig_counts.get(key, 0) / a, 2) if a else None}

    return {
        "nightly": {str(m): share(n_sig, n_all, "{}.0a1".format(m)) for m in (major - 1, major)},
        "beta": {str(m): share(b_sig, b_all, str(m)) for m in (major - 1, major)},
        "builds": [dict(share(builds_sig, builds_all, b), buildid=b)
                   for b in sorted(set(builds_all) | set(builds_sig))],
    }


def sample_uuid(product, channel, signatures, major, release_day, version=None):
    """Return a recent ``(uuid, buildid)``, preferring ``version``, then any version of ``major``.

    Return ``None`` if no report is found or a query fails.
    """
    params = {"product": product, "release_channel": utils.get_search_channel(channel),
              "signature": ["=" + s for s in signatures], "major_version": str(major),
              "date": ">=" + release_day.isoformat(), "_columns": ["uuid", "build_id"],
              "_sort": "-date", "_results_number": 1}
    queries = [dict(params, version=version), params] if version else [params]
    for query in queries:
        try:
            hits = (spikes._search(query) or {}).get("hits") or []
        except Exception:
            logger.warning("versionstep: cannot sample a report of %s", signatures[:1],
                           exc_info=True)
            return None
        if hits and hits[0].get("uuid") and hits[0].get("build_id"):
            return str(hits[0]["uuid"]), str(hits[0]["build_id"])
    return None
