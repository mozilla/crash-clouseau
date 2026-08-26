# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Has this signature's crash RATE changed? — the question the spike selector cannot ask.

WHY THIS EXISTS. ``datacollector.get_new_signatures`` decides per BUILD-DAY: a count clears a floor
and jumps over the loudest of the preceding three build-days. Replayed over 146 nightly run-days,
**88.1% of its selections come from the from-zero branch and 67.0% are a single crash** — one crash
on a build-day whose three predecessors were quiet is a selection, and there are ~187 signatures
crashing exactly once on a typical nightly day. It is a fine trigger and it carries almost no
information about whether the thing is worth a human's time: a selected (signature, run-day) is
followed by a human-filed crash bug within 30 days 1.30% of the time against 0.46% for a declined
one (relative risk 2.80, 95% CI 2.56-3.05), and WITHIN a signature the timing of its selections is
no better than chance at predicting when a human will file (permutation lift 0.99).

Bug 2063336 is the case that paid for this module. `AsyncShutdownTimeout | profile-before-change |
CookiePersistentStorage: cookies.sqlite closing`: 24 nightly reports in six months, never more than
2 on one build-day, spread over 18 buildids. The selector picked it on **20 run-days** and the
pipeline produced nothing, while :aryx filed it off a statistic we never computed — "single digit
crashes per version to 14 reports from 14 installs of Firefox 155.0a1". This module computes that
statistic.

WHAT IT IS. The distinct installations hitting a signature over a trailing window, against its own
rate over a longer baseline, with the CHANNEL's distinct installations as the exposure denominator.

* **Installations, never reports.** One machine has produced 81,843 of 86,196 reports in a past
  measurement here. Over 12 matched thresholds on 294,422 replayed rows the install-based test beat
  the report-based one every time with non-overlapping CIs (relative risk 16.9 vs 11.0, firing half
  as often). Reports are carried alongside so a human surface can quote both, and are never the
  test.
* **The denominator is not optional.** Nightly's distinct installs per day fell from a median 860
  (2026-06) to 462 (2026-08). Over that ramp a signature at a constant per-install rate lost half
  its raw count. Comparing counts across it measures the user base.
* **A 7-day window absorbs the working week by construction.** Nightly install exposure runs
  Wed 805 / Sun 624, a peak-to-trough of 1.29x. A 3-day window has to fight that; a 7-day one
  cannot see it.
* **56 days of baseline, and the length is load-bearing.** Replayed at the honest standard — credit
  only for an alarm landing 3 to 30 days before the human filed — a 14-day baseline reaches 6 of
  the 57 test-split human cases, 28 days reaches 10, and 56 days reaches 16, at 1.8 / 2.8 / 4.3
  alarms per run-day. Recall per unit cost is flat across the three (3.3 / 3.6 / 3.7); absolute
  reach is 2.7x better at 56 days, for 2.5 extra alarms a day against a pipeline already running
  ~68. Cost is not the binding constraint, so take the long baseline.

WHAT THE SCORE IS NOT. ``score`` is the upper tail of a Gamma-Poisson (negative binomial)
predictive, and it is **not a probability** — measured against a temporally shuffled null it is
anti-conservative by three to five orders of magnitude (at a nominal 1e-6 the null fires 7.5-22.2
times a day, not 0.002). It is a monotone ordering statistic and nothing else. Do not print it as a
p-value, do not derive a false-discovery rate from it, and do not let it into a threshold that
claims a confidence level. Benjamini-Hochberg on it is measurably pointless: the number of
signatures tested per run-day barely varies (1,838-2,163), so a BH cutoff is a constant alpha in
disguise and ties one at equal cost.

AND IT IS NOT A GATE. Every key here is recorded and rendered; none moves a rung, a score or a
verdict. The statistic's measured value is as an ORDERING over work the pipeline already pays for
(ranking the deployed rule's own ~68/run-day candidate list beats its arbitrary order by 4-5x at a
fixed spend, one-sided binomial p = 2.9e-06) and as a FACT for the agent and the bug comment. Its
recall at usable lead is *half* what today's full spend reaches, so wiring it as a filter would cost
cases. See ``spike/trend/REPORT.md`` §2, §12.
"""

import math
from datetime import date, timedelta

from libmozdata import socorro

from . import config, models, utils
from .logger import logger

# The trailing window under test, and the baseline it is compared against. See the module
# docstring for why 7 and 56 rather than anything shorter.
WINDOW_DAYS = 7
BASELINE_DAYS = 56

# Below this many distinct installations in the window there is nothing to say. Every good
# operating point measured carries a floor; the recommendation holds over 3-5 and the
# precision/recall curve is flat across it.
MIN_INSTALLS = 3

# How much of the baseline must actually have been collected before a comparison is allowed. A
# fresh database, or one that has been down for a week, must produce NO facts rather than a
# confident-looking ratio against three days of history — the same rule as `sigage`, which omits a
# key instead of zeroing it so that an unknown age can never read as a new signature.
MIN_BASELINE_COVERAGE = 21

# And how much of the WINDOW must have been collected. Separate from the baseline's bar because it
# guards a different lie: with 2 of 7 days collected the RATE is still right (both sides divide by
# the exposure actually seen) but the sentence "in the last 7 days" is not, and a two-day window
# compared against eight weeks is not the statistic this module claims to compute.
MIN_WINDOW_COVERAGE = 5

# THE WORDING BAR, and it is deliberately higher than the bar for computing the facts at all.
# The two thresholds do different jobs and separating them costs nothing:
#
#   * The FACTS (and therefore `signature_trend_score`, the ordering statistic) are computed from
#     MIN_INSTALLS = 3. That is what a ranking over the selector's candidate list would read, and
#     on bug 2063336 it puts the signature in play from 2026-07-26 — 18 days before :aryx filed.
#   * The SENTENCE waits until the claim is supportable. Measured on the real rollup for
#     2026-08-12, 1,052 active nightly signatures: ratio >= 2 with 3 installs speaks about 97 of
#     them (9.2%), and its weakest members are `UIItemsView::_OnBatchTimer` at 2.02x on 3 installs
#     against a baseline of ~1.5 — a sentence that says "the crash rate has risen" about noise.
#     ratio >= 3 with 5 installs speaks about 32 (3.0%), i.e. roughly 2 of the ~68 crashes the
#     pipeline analyses in a day.
#
# The cost is honest and it is the reason both numbers are here rather than one: on the anchor the
# SENTENCE starts on 2026-08-10 (6 installs, 17.1x) instead of 2026-07-26 (3 installs, 43.2x), so
# tightening it costs 15 days of the lead that the SCORE keeps. That is the right trade for prose —
# the crash is being analysed on those earlier days anyway, because the selector picked it, so the
# note buys nothing by speaking early and costs prompt dilution on 9% of every run.
#
# NOT FITTED ON THE ANCHOR: it clears every one of the twelve (ratio, floor) combinations measured
# (2/3/5/10 x 3/5/8), so no choice in that grid is what puts it in scope.
MIN_INTERESTING_RATIO = 3.0
WORDING_MIN_INSTALLS = 5

# How far back a backfill will go on a cold table. WINDOW + BASELINE plus a week of slack, so the
# statistic works on the first run after a deploy instead of in two months.
BACKFILL_DAYS = WINDOW_DAYS + BASELINE_DAYS + 7

# Socorro's terms facets are COUNT-ordered and truncate silently, so this has to sit above the
# distinct-signature count of the busiest channel-day. Nightly runs ~250-300 and beta ~290-450;
# release runs ~3,200, which is why `collect_day` refuses a channel it cannot cover.
FACETS_SIZE = 2000
MAX_SIGNATURES = 1500

# Channels this collector can actually cover, and the reason is arithmetic rather than policy.
# A day's per-signature facet has to fit in one COUNT-ordered page or its quietest signatures --
# the only ones this module is for -- vanish silently. Distinct signatures per day, measured over
# 206 days: nightly 250-300, beta 290-450, **release ~3,200**. Release is also 10% sampled in
# Socorro, so its install counts are not comparable with the other two anyway. An unsupported
# channel is skipped cheaply instead of failing the MAX_SIGNATURES check on ~70 days of backfill
# every single run.
SUPPORTED_CHANNELS = ("nightly", "beta")


def _lgamma(x):
    return math.lgamma(x)


def nb_upper_tail(k, r, p):
    """``P(X >= k)`` for the Gamma-Poisson predictive, ``r > 0`` real.

    ``P(X = x) = Gamma(x+r)/(Gamma(r) x!) p**r (1-p)**x``. Summed forward from 0 because ``k`` is
    small here (a signature with hundreds of installs a week is not a case this module is for) and
    the closed form needs an incomplete beta this codebase has no dependency for."""
    if k <= 0:
        return 1.0
    if p >= 1.0:
        return 0.0
    if p <= 0.0:
        return 1.0
    lp, lq, lgr = math.log(p), math.log1p(-p), _lgamma(r)
    total = 0.0
    for x in range(int(k)):
        total += math.exp(_lgamma(x + r) - lgr - _lgamma(x + 1) + r * lp + x * lq)
        if total >= 1.0:
            return 0.0
    return max(0.0, 1.0 - total)


def tail_score(k, base_count, base_exposure, test_exposure, prior=0.5):
    """The ordering statistic: how surprising ``k`` is, given the baseline and the exposures.

    A Gamma(``base_count`` + prior, ``base_exposure``) posterior on the per-exposure rate, whose
    predictive over ``test_exposure`` is NB(r = base_count + prior, p = b/(b+e1)).

    **The Jeffreys prior is the load-bearing part.** With a flat prior a zero baseline makes the
    rate zero and the first crash infinitely surprising — which is exactly the trap
    ``utils.is_spike``'s from-zero branch fell into from the other direction, and it is where 88%
    of the deployed selector's noise comes from. With ``prior=0.5`` a single crash against a silent
    baseline scores ~0.5 and orders below a real rise, which is the behaviour wanted.

    ``None`` when there is no exposure to compare against."""
    if base_exposure <= 0 or test_exposure <= 0:
        return None
    return nb_upper_tail(k, base_count + prior, base_exposure / float(base_exposure + test_exposure))


def collect_day(product, channel, day):
    """Fetch and store one day of per-signature installs for a channel. Never raises.

    ONE SuperSearch. ``date`` is ``processed_crash.date_processed``, so a day's row is what was
    visible that day and the series stays causal — see ``models.ChannelDaily``.

    Returns the number of signature rows written, or 0. A day whose channel row is written but
    whose signature list is empty is a real, quiet day; a day with no channel row at all was never
    collected, and ``trend_facts`` tells the two apart by counting channel rows."""
    got = {}

    def handler(json_, data):
        if json_["errors"]:
            raise Exception("SuperSearch errors: {}".format(json_["errors"]))
        facets = json_["facets"]
        data["total"] = json_["total"]
        data["installs"] = facets.get("cardinality_install_time", {}).get("value", 0)
        rows = {}
        for entry in facets.get("signature", []) or []:
            installs = entry["facets"]["cardinality_install_time"]["value"]
            # A signature Socorro can count but whose install cardinality comes back 0 still
            # happened on at least one machine; the same coercion as `datacollector`.
            rows[entry["term"]] = (entry["count"], installs or 1)
        data["rows"] = rows

    nxt = day + timedelta(days=1)
    params = {
        "product": product,
        # beta must be ["beta", "aurora"]: a beta build and its DevEdition twin share a buildid,
        # and dropping the twin loses ~36% of the channel's reports -- i.e. a third of the
        # denominator this table exists to be.
        "release_channel": utils.get_search_channel(channel),
        "date": [">=" + day.isoformat(), "<" + nxt.isoformat()],
        "_aggs.signature": ["_cardinality.install_time"],
        "_facets": ["_cardinality.install_time"],
        "_results_number": 0,
        "_facets_size": FACETS_SIZE,
    }
    try:
        socorro.SuperSearch(params=params, handler=handler, handlerdata=got).wait()
    except Exception:
        logger.error("Cannot collect %s-%s daily rates for %s", product, channel, day,
                     exc_info=True)
        return 0
    if not got:
        return 0
    rows = got.get("rows") or {}
    if len(rows) >= MAX_SIGNATURES:
        # Refused rather than truncated. Socorro orders terms facets by COUNT, so an over-full day
        # silently drops its QUIETEST signatures -- which are precisely the ones this module is
        # for. Release runs ~3,200 distinct signatures a day and cannot be covered this way.
        logger.warning(
            "%s-%s on %s returned %d signatures (>= %d): refusing to store a truncated day",
            product, channel, day, len(rows), MAX_SIGNATURES)
        return 0
    if not models.ChannelDaily.upsert(product, channel, day, got.get("total", 0),
                                      got.get("installs", 0)):
        return 0
    return models.SignatureDaily.record_day(product, channel, day, rows)


def backfill(product, channel, asof=None, days=BACKFILL_DAYS, budget=None):
    """Collect the days in the window that are missing, oldest first. Never raises.

    Idempotent and cheap: a steady-state run finds one day missing (yesterday's, and today's
    partial), a cold table finds ~70 and spends ~35 seconds once. ``budget`` caps the number of
    days fetched in a single call so a cold start cannot stretch one update run without bound;
    the next run picks up where this one stopped, because the gap is what drives the work.

    TODAY IS ALWAYS REFETCHED. Its row is partial by construction — a build holds 0.2-2.7% of its
    eventual crashes on its own ship day — so the collector overwrites it every run rather than
    treating it as done."""
    if channel not in SUPPORTED_CHANNELS:
        return 0
    asof = asof or date.today()
    start = asof - timedelta(days=days)
    known = models.ChannelDaily.known_days(product, channel, start, asof)
    todo = [start + timedelta(days=i) for i in range((asof - start).days + 1)]
    todo = [d for d in todo if d not in known or d >= asof]
    if budget is not None:
        todo = todo[:budget]
    written = 0
    for day in todo:
        written += collect_day(product, channel, day)
    if todo:
        logger.info("Collected %d day(s) of %s-%s daily rates (%d signature rows)",
                    len(todo), product, channel, written)
    return written


def _window_sums(series, exposure, start, end):
    """``(installs, reports, exposure, collected_days)`` over an inclusive day range.

    A day with no signature row counts as zero; a day with no EXPOSURE row is not counted at all,
    in either the numerator or the denominator. That is what keeps a gap in collection from
    reading as a quiet period -- the rate is a ratio over the days that were actually seen."""
    ins = rep = exp = 0
    seen = 0
    day = start
    while day <= end:
        got = exposure.get(day)
        if got is not None:
            seen += 1
            exp += got[1]
            r, i = series.get(day, (0, 0))
            ins += i
            rep += r
        day += timedelta(days=1)
    return ins, rep, exp, seen


def trend_facts(product, channel, signature, asof=None,
                window=WINDOW_DAYS, baseline=BASELINE_DAYS):
    """Exposure-normalised rate change for one signature. ``{}`` when it cannot be computed.

    Keys are written out LITERALLY rather than built from a loop. A computed key is invisible to
    ``tests/test_corroboration_registry.py``'s scanner, and four ``sigage`` keys were live in prod
    dossiers and undeclared for exactly that reason. A corroboration key has to be greppable.

    Returns ``{}`` -- never a zero or a False -- when the baseline is too thin to compare against.
    A rate change we could not measure must not read as an absence of one."""
    asof = asof or date.today()
    win_end = asof
    win_start = asof - timedelta(days=window - 1)
    base_end = win_start - timedelta(days=1)
    base_start = base_end - timedelta(days=baseline - 1)

    exposure = models.ChannelDaily.series(product, channel, base_start, win_end)
    if not exposure:
        return {}
    series = models.SignatureDaily.series(product, channel, signature, base_start, win_end)

    w_ins, w_rep, w_exp, w_days = _window_sums(series, exposure, win_start, win_end)
    b_ins, b_rep, b_exp, b_days = _window_sums(series, exposure, base_start, base_end)
    if b_days < MIN_BASELINE_COVERAGE or w_days < MIN_WINDOW_COVERAGE:
        return {}
    if not w_exp or not b_exp:
        return {}
    if w_ins < MIN_INSTALLS:
        return {}

    expected = b_ins * (w_exp / float(b_exp))
    score = tail_score(w_ins, b_ins, b_exp, w_exp)
    # One subscript assignment per key, not a dict literal. The registry scanner in
    # tests/test_corroboration_registry.py recognises writes by shape, and a literal that only
    # reaches `corroborations` through a `{**a, **b}` merge two modules away is not one of them --
    # so a dict literal here declares nothing and the "declared but never written" check fails.
    # `sigage.age_facts` was unrolled for the same reason.
    facts = {}
    # The days actually COLLECTED, not the nominal window. If the rollup missed a day the
    # sentence has to say "the last 6 days" or it is claiming coverage it does not have -- and
    # every surface reads this key to write that phrase.
    facts["signature_trend_window_days"] = w_days
    facts["signature_trend_installs"] = w_ins
    facts["signature_trend_reports"] = w_rep
    facts["signature_trend_baseline_days"] = b_days
    facts["signature_trend_baseline_installs"] = b_ins
    facts["signature_trend_expected_installs"] = round(expected, 2)
    if expected > 0:
        facts["signature_trend_ratio"] = round(w_ins / expected, 2)
    if score is not None:
        facts["signature_trend_score"] = score
    return facts


def is_rising(facts):
    """Does this look like a real rate change, for WORDING and ROUTING -- never for a verdict.

    Deliberately a ratio-and-floor test rather than a cut on ``signature_trend_score``: the score
    is an ordering statistic whose tail is anti-conservative by orders of magnitude (see the module
    docstring), so a threshold on it would import a confidence it does not have. The ratio is what
    a human would say out loud."""
    if not facts:
        return False
    ratio = facts.get("signature_trend_ratio")
    if ratio is None or ratio < MIN_INTERESTING_RATIO:
        return False
    return facts.get("signature_trend_installs", 0) >= WORDING_MIN_INSTALLS


def describe(facts):
    """One sentence, in the form :aryx used, or ``None``.

    "14 reports from 14 installs of Firefox 155.0a1" was enough for a module owner to fix bug
    2063336 in five days with no regressor ever named. The sentence IS the deliverable for this
    class -- 15 of the 32 fixes among the human-filed existing-signature bugs (47%) named no
    regressor at all, against 36 of 56 that did in the new-signature class."""
    if not facts:
        return None
    ins = facts.get("signature_trend_installs")
    exp = facts.get("signature_trend_expected_installs")
    ratio = facts.get("signature_trend_ratio")
    days = facts.get("signature_trend_window_days")
    base_days = facts.get("signature_trend_baseline_days")
    if ins is None or exp is None or ratio is None:
        return None
    return (
        "{} distinct installations in the last {} days ({} reports), against {} expected "
        "from this signature's own rate over the preceding {} days — {}x, normalised for "
        "the channel's daily installation count.".format(
            ins, days, facts.get("signature_trend_reports"), exp, base_days, ratio)
    )


def collect_all(products=None, channels=None, asof=None, budget=None):
    """Backfill every configured product/channel. Called from the daily update."""
    products = products if products is not None else config.get_products()
    channels = channels if channels is not None else config.get_channels()
    total = 0
    for product in products:
        for channel in channels:
            total += backfill(product, channel, asof=asof, budget=budget)
    models.SignatureDaily.prune()
    models.ChannelDaily.prune()
    return total
