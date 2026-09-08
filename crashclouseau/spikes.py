# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""A REAL crash spike -- the fact that files a bug by itself.

THE SELECTOR'S TRIGGER IS NOT THIS. ``utils.is_spike`` decides what the pipeline SPENDS ON:
its from-zero branch fires on one crash after three quiet build-days, and replayed over 146
nightly run-days 88.1% of its selections came from that branch and 67.0% were a single crash.
That is the right bar for a ~$1-3 triage run that may abstain quietly; it is not a fact anyone
should be told about. ``0 -> 1`` is a selection, not a spike.

This module answers the OTHER question -- would a human looking at crash-stats say "this
signature spiked"? -- and it answers it the way the crash-spikes dashboard
(``../crash-spikes/spikes/dashboard``, the detector release management reads) does, minus the
seasonal model we do not have:

* **an absolute floor** in crashes (the channel's ``spike.floor``: 3 / 10 / 50) -- the
  dashboard's ``volume_share`` floor, scaled per channel the same way;
* **installations are first class** (``spike.real_installs``): "one machine crashing a thousand
  times is one machine", and an imaged fleet mints several install_times a minute, so nightly
  asks for three distinct installations where the selector asks for one;
* **the selector's own ratio** over the loudest of the preceding build-days (``spike.ratio``,
  3x), kept so a real spike is always also a selection;
* **a Poisson excess test**: the Anscombe residual ``2 (sqrt(n + 3/8) - sqrt(e + 3/8))`` of the
  day's count against that baseline, the statistic the dashboard scores every series with,
  held to the Gaussian quantile of the dashboard's ``major`` false-alarm rate (0.015% per
  signature-day, z ~ 3.6). The dashboard LEARNS its threshold per channel from heavy real
  tails and notes the Gaussian value is a floor those tails never go under; we take the floor
  of the strictest level and say so. What it buys: from a zero baseline six crashes are needed,
  not one; over a baseline of 1 nine; over 3 thirteen; above a baseline of ~10 the 3x ratio
  binds instead. All four conditions are AND-ed.

**The baseline is the signature's own history, not the selector's window.** The selection
log's ``baseline`` is the ``ndays`` build-days before the spike day -- three on nightly, one or
two on beta, where ``Build.get_last_versions(n=3)`` is the whole series. That is the right
window for deciding what to spend on and the wrong one for telling people a signature spiked:
one quiet build, or one build on which the compiler inlined a frame differently so the same
crash wore a sibling signature, is a zero baseline, and against a zero baseline six crashes are
"an appearance". Bug 2070317 (2026-09-08) was that: 10 reports of a 522-day-old OOM signature on
156.0b4 filed as an appearance from zero, when the preceding beta builds carried 90-221 each and
156.0b3 alone had symbolized the crash under its ``AllocateTenured`` sibling. So
``judge_selection`` reads the signature's per-build counts on the channel over the preceding
``spike.history_days`` (21) from Socorro (``build_history``) and the loudest of THOSE builds joins
the baseline the ratio and the excess test are held to. A history that cannot be read is not a
quiet one: no history, no spike. The cost is deliberate -- a signature fixed and regressed inside
the horizon is a rise the ordinary triage and the rate path still see, not a "spike" bug -- and
the comparison is conservative on a young build, whose count is held to the full-life counts of
older builds; a real regression is well past 3x anyway.

The rate path (``utils.RISING_RATE``) has no build-day count to test, so ``judge_rate`` applies
the same excess test to what it does have: the signature's 7-day distinct installations against
the installs its own 56-day rate predicts (``sigtrend.trend_facts``), on top of ``is_rising``.

Every number here comes from ``config/global.json`` and none was fitted on a case: the floors
are the selector's, the install floors are the channel install thresholds rounded up to "more
than one machine", and the z comes from a published alert rate. See
``tests/test_spike_escalation.py`` for what each condition does and does not admit.
"""
import math
from datetime import timedelta
from statistics import NormalDist

from libmozdata import socorro

from . import config, sigtrend, utils
from .logger import logger

# The Anscombe transform's stabilising constant.
_ANSCOMBE = 0.375


def excess_z(count, expected):
    """The Anscombe residual of ``count`` against ``expected``: ~N(0, 1) for a Poisson count, so
    it is comparable across a baseline of 0 and a baseline of 50 where a ratio is not."""
    count = max(float(count or 0), 0.0)
    expected = max(float(expected or 0), 0.0)
    return 2.0 * (math.sqrt(count + _ANSCOMBE) - math.sqrt(expected + _ANSCOMBE))


def z_threshold(alert_rate):
    """The one-sided Gaussian quantile for a false-alarm ``alert_rate`` per series-day. The
    dashboard's ``major`` rate of 0.00015 gives 3.62; its ``spike`` rate of 0.0015 gives 2.97.
    Clamped to a sane rate so a config typo cannot make the test admit everything or nothing."""
    rate = min(max(float(alert_rate or 0.00015), 1e-9), 0.5)
    return NormalDist().inv_cdf(1.0 - rate)


def day_installs(bids):
    """Distinct installations on the spike day: the sum over that day's builds of each build's
    ``cardinality_install_time``. An installation is on one build per day, so the sum is the
    day's count up to the odd machine that updated twice -- the same estimate the dashboard
    makes when it adds yesterday's installs to today's. ``bids`` is the selection log's
    ``{buildid: {"count": n, "installs": k}}``."""
    total = 0
    for info in (bids or {}).values():
        try:
            total += int((info or {}).get("installs") or 0)
        except (TypeError, ValueError):
            continue
    return total


# The `build_id` facet is count-ordered, so a truncated facet drops the QUIETEST builds -- which
# cannot move a max. 100 covers 21 nightly days with their respins several times over.
_HISTORY_FACETS = 100


def _history_params(signatures, product, channel, buildid, days):
    """The SuperSearch query for the signature's (or its siblings') per-build counts on the
    channel over the ``days`` before ``buildid``, that build excluded. The build-id range is
    what scopes the history, the date bound only keeps Socorro from scanning a year; both are
    derived from the buildid's own UTC fields, so no local clock can move them."""
    start = utils.get_build_date(str(buildid)) - timedelta(days=days)
    return {
        "signature": ["=" + s for s in signatures],
        "product": product,
        "release_channel": utils.get_search_channel(channel),
        "build_id": [">=" + start.strftime("%Y%m%d%H%M%S"), "<" + str(buildid)],
        "date": ">=" + start.strftime("%Y-%m-%d"),
        "_results_number": 0,
        "_facets": "build_id",
        "_facets_size": _HISTORY_FACETS,
    }


def _search(params):
    got = {}

    def handler(json_, data):
        if json_.get("errors"):
            raise Exception("SuperSearch errors: {}".format(json_["errors"]))
        data["result"] = json_

    socorro.SuperSearch(params=params, handler=handler, handlerdata=got).wait()
    return got.get("result")


def build_history(signatures, product, channel, buildid, days):
    """The signature's per-build report counts on the channel over the ``days`` before
    ``buildid``: ``[{"buildid", "count"}, ...]`` oldest first, ``[]`` when no earlier build in the
    horizon has a report of it, ``None`` when Socorro could not be read. Several signatures (a
    lambda's demanglings) are one history, summed per build by Socorro. Never raises."""
    sigs = [s for s in (signatures or []) if s]
    if not sigs or not buildid:
        return None
    try:
        result = _search(_history_params(sigs, product, channel, buildid, days))
    except Exception:
        logger.warning("spike: cannot read the build history of %s on %s-%s", sigs[0], product,
                       channel, exc_info=True)
        return None
    if not isinstance(result, dict) or "facets" not in result:
        return None
    rows = (result.get("facets") or {}).get("build_id") or []
    out = [{"buildid": str(r.get("term")), "count": int(r.get("count") or 0)}
           for r in rows if r.get("term") is not None]
    return sorted(out, key=lambda r: r["buildid"])


def is_real_spike(count, before, installs, *, floor, ratio, min_installs, z_min):
    """The four conditions, AND-ed (see the module docstring). ``before`` is the baseline
    window of preceding build-day counts; empty or all-zero is a from-zero appearance, which is
    admitted only when the count alone clears the floor AND the excess test -- never at one."""
    count = int(count or 0)
    installs = int(installs or 0)
    baseline = max(before) if before else 0
    if count < floor or installs < min_installs:
        return False
    if count < ratio * baseline:
        return False
    return excess_z(count, baseline) >= z_min


def judge_build_day(count, before, installs, product, channel, history=None):
    """``judge`` for a build-day selection: the spike facts as a dict when it is real, else
    ``None``. The dict carries every number the decision used, so the bug can quote them and a
    reader can check them against crash-stats.

    ``before`` is the selector's baseline (its last ``ndays`` build-days); ``history`` is the
    signature's own per-build counts over ``spike.history_days`` (``build_history``), or ``None``
    when the caller did not consult it. The loudest build of EITHER is the baseline the ratio
    and the excess test are held to. Pure arithmetic: the fetch is ``judge_selection``'s."""
    floor = config.get_spike("floor", product, channel)
    ratio = config.get_spike("ratio", product, channel)
    min_installs = config.get_spike("real_installs", product, channel)
    z_min = z_threshold(config.get_spike("real_alert_rate", product, channel))
    before = [int(x or 0) for x in (before or [])]
    counts = [int((h or {}).get("count") or 0) for h in (history or [])]
    series = before + counts
    if not is_real_spike(count, series, installs, floor=floor, ratio=ratio,
                         min_installs=min_installs, z_min=z_min):
        return None
    baseline = max(series) if series else 0
    facts = {
        "kind": "build_day",
        "count": int(count or 0),
        "installs": int(installs or 0),
        "baseline": before,
        "baseline_max": baseline,
        "ratio": round(count / float(baseline), 1) if baseline else None,
        "z": round(excess_z(count, baseline), 2),
        "floor": floor,
        "min_installs": min_installs,
        "z_min": round(z_min, 2),
    }
    if history is not None:
        facts["history"] = [dict(h) for h in history]
        facts["history_days"] = config.get_spike("history_days", product, channel)
        facts["history_max"] = max(counts) if counts else 0
    return facts


def judge_rate(facts, product, channel):
    """``judge`` for a rate-path selection: the signature's 7-day installations against the
    installs its own baseline rate predicts, held to the same install floor and excess test.
    ``facts`` is ``sigtrend.trend_facts``' dict; ``{}`` (a rate we could not measure) is never
    a spike."""
    if not sigtrend.is_rising(facts):
        return None
    installs = facts.get("signature_trend_installs") or 0
    expected = facts.get("signature_trend_expected_installs") or 0.0
    min_installs = config.get_spike("real_installs", product, channel)
    z_min = z_threshold(config.get_spike("real_alert_rate", product, channel))
    z = excess_z(installs, expected)
    if installs < min_installs or z < z_min:
        return None
    return {
        "kind": "rate",
        "installs": int(installs),
        "expected_installs": expected,
        "reports": facts.get("signature_trend_reports"),
        "ratio": facts.get("signature_trend_ratio"),
        "window_days": facts.get("signature_trend_window_days"),
        "baseline_days": facts.get("signature_trend_baseline_days"),
        "z": round(z, 2),
        "min_installs": min_installs,
        "z_min": round(z_min, 2),
    }


def judge_selection(row, product, channel, trend_facts=None):
    """Is this selection-log row (``models.Selection.to_dict()``) a real spike? The facts dict,
    or ``None``. Only a pair the pipeline SELECTED can be one: a declined pair has no crashes
    ingested to investigate, and a ``selected`` row already cleared the selector's bar, so this
    only ever tightens.

    A ``rising_rate`` row has no baseline of its own (``[]``); it is judged on the trend facts
    the caller measured (``sigtrend.trend_facts`` as of today), never on its count.

    A ``selected`` row is judged against the selector's baseline AND the signature's own
    per-build history over ``spike.history_days`` (``build_history``, one SuperSearch); a history
    Socorro would not give us is not a quiet one, so the row is not a spike until it can be
    read (the sweep asks again every tick)."""
    outcome = (row or {}).get("outcome")
    if outcome == utils.RISING_RATE:
        return judge_rate(trend_facts or {}, product, channel)
    if outcome != utils.SELECTED:
        return None
    signatures = list(row.get("signatures") or []) or [row.get("signature")]
    history = build_history(signatures, product, channel, row.get("picked"),
                            config.get_spike("history_days", product, channel))
    if history is None:
        logger.info("spike: no build history for %s on %s-%s; not judged a spike",
                    row.get("signature"), product, channel)
        return None
    return judge_build_day(
        row.get("number") or 0, row.get("baseline") or [], day_installs(row.get("bids")),
        product, channel, history=history,
    )


def describe(spike, channel=None, build_day=None, buildid=None):
    """One sentence with the numbers, for the prompt and the bug -- the same arithmetic both
    ways, so a model and a human are never shown two different spikes."""
    if not spike:
        return None
    where = "{} build".format(channel) if channel else "build"
    if spike.get("kind") == "rate":
        return (
            "{} distinct installations hit this signature in the last {} days ({} reports), "
            "against {} expected from its own rate over the preceding {} days -- {}x, "
            "normalised for the channel's daily installation count.".format(
                spike.get("installs"), spike.get("window_days"), spike.get("reports"),
                spike.get("expected_installs"), spike.get("baseline_days"), spike.get("ratio"))
        )
    before = spike.get("baseline") or []
    if before and max(before) > 0:
        against = "the preceding {} build-day{} had {} report{}".format(
            len(before), "" if len(before) == 1 else "s",
            ", ".join(str(b) for b in before), "" if len(before) == 1 else "s")
    else:
        against = "none of the preceding {} build-day{} had any".format(
            len(before) or "", "" if len(before) == 1 else "s").replace("  ", " ")
    history = spike.get("history")
    if history is not None:
        days = spike.get("history_days")
        loudest = int(spike.get("history_max") or 0)
        if loudest > 0:
            n = len(history)
            against += ", and the loudest of its {} earlier build{} in the {} days before had {}".format(
                n, "" if n == 1 else "s", days, loudest)
        else:
            against += ", and no build in the {} days before had any report of it".format(days)
    when = ""
    if build_day:
        when = " of {}".format(build_day)
    if buildid:
        when += " (buildid {})".format(buildid)
    return "{} reports from {} distinct installations on the {}{}; {}.".format(
        spike.get("count"), spike.get("installs"), where, when, against)
