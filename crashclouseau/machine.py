# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""What else has the machine that produced this crash been crashing on?

A machine with failing memory does not crash in one place. It scatters: bug 2062168 and bug
2062173 were both filed on 2026-08-10 from ONE installation that produced 21 crashes across 20
distinct signatures in two days -- JS GC, jemalloc, heap free, Intel graphics, Windows display.
Jan de Mooij closed the first of them with the rule this module implements: "I think this one is
bad hardware rather than a regression. It's just one crash report and that installation has
multiple crashes with distinct signatures."

``install_time`` IS NOT A MACHINE ID, and the two ways it fails both matter here:

* It is a (machine, BUILD) id -- the updater resets it. 56,012 of 56,111 nightly install_times
  map to exactly one buildid and the median install is 1.1 days old at crash time. So a machine's
  visible history is short, the rule can never fire on an install's first crash, and a longer
  lookback buys almost nothing (measured: 14 days and 2 days return the same answers).
* It COLLIDES. 11% of install_times with 3+ signatures span more than one CPU model -- one
  install second shared by many machines (VM and distro images). Bug 2061961's looks like a
  scattergun (6 crashes, 5 signatures) and is nothing of the sort: 4 distinct CPUs, 3 distinct
  operating systems. That is a bad ID, not a bad machine.

The second failure is why ``distinct_cpus`` exists and why the gate requires it to be 1. It is
not a refinement -- it is the mechanism test. Across 141k nightly crashes the scatter effect is
strong where the id resolves to one CPU (-7.0pp on later reproduction, z=-4.4) and VANISHES where
it does not (+1.0pp, p=0.77). The signal appears exactly where "one bad machine" predicts it and
disappears where it predicts it should.
"""
from datetime import datetime, timedelta, timezone

from libmozdata import socorro

from crashclouseau.logger import logger

# Rows fetched in the single lookup. Higher than any real install's crash count (the worst
# observed was 21) and, crucially, truncation is SAFE: fewer rows can only UNDERCOUNT distinct
# signatures, and the gate fires on a count being high. It fails toward reporting.
_MAX_ROWS = 200


def _to_dt(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def install_history(install_time, product="Firefox", channel="nightly", before=None, days=14):
    """What this installation has crashed on: ``{distinct_signatures, distinct_cpus, crashes,
    span_seconds}``, every value ``None`` when it could not be established.

    ONE SuperSearch, returning rows rather than facets. Facets would need
    ``_facets=_cardinality.signature`` (a facet VALUE, not a ``_cardinality.x=true`` parameter --
    getting that wrong returns nothing, silently) and still could not give the time span; the
    rows give all four quantities at once and are computed here where they are testable.

    ``before`` bounds the window at the triaged crash's own timestamp so the answer is CAUSAL.
    Clouseau triages within ~20 minutes of a crash arriving, so in production this changes almost
    nothing -- but without it an offline replay would score the rule against crashes that had not
    happened yet, which is how a hindsight signal gets mistaken for a live one.

    ``None`` rather than 0 on every failure path: the gate SUPPRESSES a verdict on these numbers,
    so "we could not find out" must never be able to satisfy a threshold."""
    empty = {"distinct_signatures": None, "distinct_cpus": None,
             "crashes": None, "span_seconds": None}
    if not install_time:
        return empty
    end = _to_dt(before) or datetime.now(timezone.utc)
    params = {
        "install_time": str(install_time),
        "product": product or "Firefox",
        "date": [">=" + (end - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d"),
                 "<=" + end.strftime("%Y-%m-%dT%H:%M:%S")],
        "_columns": ["signature", "cpu_info", "date"],
        "_results_number": _MAX_ROWS,
        "_sort": "date",
    }
    if channel:
        params["release_channel"] = channel
    got = {}

    def handler(json_, data):
        data["result"] = json_

    try:
        socorro.SuperSearch(params=params, handler=handler, handlerdata=got).wait()
    except Exception as exc:  # pragma: no cover - network; never break a seed
        logger.warning("machine: install history lookup failed for %s: %s", install_time, exc)
        return empty
    hits = (got.get("result") or {}).get("hits")
    if not hits:
        # No rows is NOT "a quiet machine" — an empty response is equally what a failed or
        # malformed query returns, and this feeds a suppression. Say we do not know.
        return empty
    sigs = {h.get("signature") for h in hits if h.get("signature")}
    cpus = {h.get("cpu_info") for h in hits if h.get("cpu_info")}
    stamps = sorted(d for d in (_to_dt(h.get("date")) for h in hits) if d is not None)
    span = (stamps[-1] - stamps[0]).total_seconds() if len(stamps) > 1 else 0.0
    return {
        "distinct_signatures": len(sigs) or None,
        # An empty cpu_info column is unknown, not "one CPU" — the gate REQUIRES <= 1, so a
        # zero here would satisfy the de-alias guard on no evidence at all.
        "distinct_cpus": len(cpus) or None,
        "crashes": len(hits),
        "span_seconds": span,
    }
