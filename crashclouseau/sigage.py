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
  * `_facets_size` is CAPPED at 10000, not rejected at it: 10000 is accepted on every query
    shape tried and 10001 is the first HTTP 400. This line used to say "10000 is rejected
    outright", which is false and was cited by three separate measurements as a reason a facet
    could not be widened -- while `config.facets_limit` has been 10000 and in production on
    every 20-minute tick the whole time (`datacollector.py`, `get_new_signatures` /
    `get_proto_small`). The note also outlived its subject: `signature_history` below passes no
    `_facets_size` at all now, so it describes the `build_id`-facet shape it replaced.
  * A date range of exactly 365 days is rejected too ("Date range is bigger than 365 days"),
    because the implicit upper bound is *now*. Clamp to 364.
  * `date` filters the crash REPORT date, not the build date, so even a short window surfaces
    very old buildids — which is exactly what makes this cheap.

Window truncation can only make first-seen look NEWER than it really is, so an age computed here
is a LOWER bound and the resulting downweight is conservative.
"""
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from libmozdata import socorro
from libmozdata.connection import Query

from crashclouseau import config, net, utils
from crashclouseau.logger import logger

# Socorro hard-rejects more than 365 days; the implicit "to now" upper bound pushes an exact
# 365 over the line. Public because the second-opinion agent's crash-stats tool states the
# window in its agent-facing text, and the figure must not drift between the two files.
MAX_WINDOW_DAYS = 364


def _buildid_to_dt(buildid):
    """``YYYYMMDDHHMMSS`` (UTC) -> datetime, or None when it is not a buildid."""
    try:
        return datetime.strptime(str(buildid)[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def buildid_day(buildid):
    """``YYYY-MM-DD`` from a buildid, or ``""``. Shared so the crash brief and the filed bug
    render the same first-seen date; a buildid alone is unreadable to a human."""
    dt_ = _buildid_to_dt(buildid)
    return dt_.strftime("%Y-%m-%d") if dt_ else ""


def _channel_total(result, channel):
    """How many reports *channel* has, off the response's ``release_channel`` facet.

    The query is no longer channel-filtered (see ``signature_history``), so the count the
    bit-flip gate needs has to be recovered from the facet rather than read off ``total``.

    ``beta`` is summed over BOTH terms ``utils.get_search_channel`` maps it to: Socorro still
    stores part of beta under ``aurora``, and dropping it loses about a third of the channel."""
    if not channel:
        total = result.get("total")
        return int(total) if isinstance(total, int) else None
    wanted = utils.get_search_channel(channel)
    wanted = {wanted} if isinstance(wanted, str) else set(wanted)
    rows = (result.get("facets") or {}).get("release_channel") or []
    if not rows:
        # An empty RESULT SET is a real zero; a missing facet on a non-empty one is not.
        return 0 if result.get("total") == 0 else None
    return sum(int(r.get("count") or 0) for r in rows if r.get("term") in wanted)


def _oldest_build(result):
    """The oldest buildid in an ascending-sorted, one-row result. ``None`` when there is none."""
    for hit in (result or {}).get("hits") or []:
        if hit.get("build_id"):
            return str(hit["build_id"])
    return None


def signature_history(signature, product="Firefox", channel="nightly", days=MAX_WINDOW_DAYS,
                      other_channel_floor=None):
    """How old a signature is and how much it crashes:
    ``{"first_seen", "first_seen_channel", "first_seen_any", "total", "total_other_channels"}``.

    ``first_seen`` is the ANSWER — the buildid the stale-signature gate and the agent should
    both reason from. The other three are the parts it is built out of, returned so the choice
    is inspectable rather than hidden.

    WHY THE ANSWER IS NOT SIMPLY THE CHANNEL'S OWN FIRST-SEEN. "Could this candidate be the
    crash's ORIGIN?" is not a per-channel question. Scoped to nightly it read exactly backwards
    for ``nsStyleContent::NonAltContentItems``: esr back to build 20251009121631 and release to
    20251106194447, 32 reports across the two — but its first NIGHTLY report is build
    20260811085340, so the gate saw a nine-day-old signature, stayed silent, and bug 2062934 was
    filed at 97% naming a four-week-old changeset into a bug where a human had already written
    "there's no recent regressor/regression-range to cast blame on here".

    WHY IT IS NOT SIMPLY THE ALL-CHANNEL FIRST-SEEN EITHER. Signature reuse is real, and an old
    report on another channel can be a different defect wearing the same name. Replayed over the
    35 filings the canary had made by 2026-08-20, the unfloored all-channel rule fired on 12 that
    the nightly one spared — and those 12 split roughly evenly into filings a human refuted and
    filings a human FIXED. It is not usable as-is.

    SO: the other channels' history is admitted only once there is enough of it to be evidence —
    ``other_channel_floor`` reports outside ``channel``, else the channel's own first-seen. On
    those same 12 the separation is clean: every filing a human refuted had 21 or more
    off-channel reports (2062934: 26, 2064137: 21, 2062335: 26, 2063003: 43, 2063902: 80 — the
    last two refuted in almost these words, "RESOLVED DUPLICATE" and "crash stats shows this is
    an existing crash signature"), and all three a human acted on had 9 or fewer (2063892: 9,
    2062286: 3, 2063864: 1). The default sits at the conservative end of that gap. Release is
    10%-sampled, so 20 reports there is nearer 200 real crashes — an established crash, not a
    stray. CALIBRATED ON TEN POINTS, though: re-measure it before trusting it far.

    The fallback is what makes this purely ADDITIVE. Below the floor the answer is byte-for-byte
    the value this returned before, so the change can only ever add a firing, never remove one —
    all six leads the gate already caught still trip it.

    ``total`` stays on ``channel``, because its consumer needs the opposite scope. It answers
    "has this signature ever been anything but a one-off?" for the bit-flip gate, whose
    population rates (``POPULATION_BIT_FLIP_RATE``) are nightly-measured precisely because
    release accumulates hardware noise that says nothing about a nightly crash — the all-channel
    flip rate is three times higher. Widening it would silently move that gate's denominator.
    ``report_bug.fetch_signature_stats`` computes yet another quantity (from this buildid on)
    for the bug comment; all three are deliberately different questions.

    TWO requests, issued together so they cost one round-trip: the oldest build on ``channel``
    cannot be read off the all-channel response, because the only ordering Socorro will give is
    over the whole result set and a count-ordered facet drops the oldest row.

    ``None`` for any field means we could not find out — never zero. A failed lookup must not
    read as "brand new" to the stale-signature gate nor as "a singleton" to the bit-flip gate."""
    empty = {"first_seen": None, "first_seen_channel": None, "first_seen_any": None,
             "total": None, "total_other_channels": None}
    if not signature:
        return empty
    if other_channel_floor is None:
        other_channel_floor = config.get_agent_signature_age()["other_channel_floor"]
    days = max(1, min(int(days or MAX_WINDOW_DAYS), MAX_WINDOW_DAYS))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    base = {
        "signature": "=" + signature,
        "product": product or "Firefox",
        "date": ">=" + since,
        # Sort ascending and take ONE row: the build_id facet is count-ordered, so paging it
        # can drop the oldest build, and an untruncated facet size is a 400.
        "_columns": ["build_id"],
        "_sort": "build_id",
        "_results_number": 1,
    }
    # Unfiltered, plus the facet that recovers the per-channel count the filter used to give.
    any_params = {**base, "_facets": "release_channel"}
    got = {}

    def make_handler(key):
        def handler(json_, data):
            data[key] = json_
        return handler

    queries = [Query(socorro.SuperSearch.URL, params=any_params,
                     handler=make_handler("any"), handlerdata=got)]
    if channel:
        queries.append(Query(socorro.SuperSearch.URL,
                             params={**base, "release_channel": utils.get_search_channel(channel)},
                             handler=make_handler("channel"), handlerdata=got))
    try:
        socorro.SuperSearch(queries=queries).wait()
    except Exception as exc:  # pragma: no cover - network; never break a seed
        logger.warning("sigage: signature history lookup failed for %r: %s", signature, exc)
        return empty

    any_result = got.get("any") or {}
    first_seen_any = _oldest_build(any_result)
    total = _channel_total(any_result, channel)
    overall = any_result.get("total")
    overall = int(overall) if isinstance(overall, int) else None
    other = None if (overall is None or total is None) else max(0, overall - total)

    if not channel:
        first_seen_channel = first_seen_any
    else:
        first_seen_channel = _oldest_build(got.get("channel") or {})

    # The floor. An unknown off-channel count is not a cleared floor: an unresolved lookup must
    # not be what widens the gate.
    if other is not None and other >= other_channel_floor and first_seen_any:
        first_seen = first_seen_any
    else:
        first_seen = first_seen_channel
    return {"first_seen": first_seen, "first_seen_channel": first_seen_channel,
            # The UNFLOORED all-channel value, computed above and previously discarded. Not a
            # third opinion on how old the signature is -- `first_seen` remains the answer -- but
            # the only one of the three that can catch a re-signaturing, because it is the only
            # one derived purely from what Elasticsearch still holds, with no rule applied on top.
            # Both `first_seen`'s alternatives lose the evidence: the channel-scoped value never
            # sees an off-channel report, and the floored value ignores fewer than
            # `other_channel_floor` of them -- which is exactly the shape a rename leaves behind.
            # Measured on the renamed `rlbox::detail::dynamic_check | ...`: 12 off-channel reports
            # back to build 20251106194447, below the floor of 20, so `first_seen` is None and the
            # inversion is invisible unless this value is used.
            "first_seen_any": first_seen_any,
            "total": total, "total_other_channels": other}


def first_seen_buildid(signature, product="Firefox", channel="nightly",
                       days=MAX_WINDOW_DAYS):
    """The oldest buildid this signature appears in, within ``days`` — across EVERY channel
    once the other channels carry enough reports to be evidence, else within ``channel``. See
    ``signature_history``, whose choice this returns unchanged so the agent and the
    deterministic gate can never disagree about how old a signature is. ``None`` when the
    lookup finds nothing or fails. Raises nothing."""
    return signature_history(signature, product, channel, days)["first_seen"]


# Socorro's own answer to "when did this signature first appear, ever". Not a SuperSearch: it is
# backed by a maintained table, so it is not bounded by the Elasticsearch retention window that
# caps everything else in this module.
SIGNATURE_FIRST_DATE_URL = "https://crash-stats.mozilla.org/api/SignatureFirstDate/"

# Batching is by URL BYTES, not by a count of signatures, because the two are not proportional and
# the count version is silently wrong. The endpoint takes a repeated `signatures` parameter, and
# crash-stats rejects a long query string outright. Signatures run from ~20 characters to Socorro's
# own `SigTruncate` ceiling of 255, and pipe-joined ones URL-encode to roughly three bytes per
# character, so a fixed count that is comfortable for short names overruns on long ones: measured
# against the 255-character signatures in our own `signatures` table, 10 per request is 3217 bytes
# and fine, 20 is 6559 and a **400**, 30 is 9925 and a **414**. A count of 50 was fine in testing
# only because the sample happened to be short.
#
# The failure that makes this worth doing carefully is not the HTTP error, it is what the error
# looks like downstream: the handler below logs and moves on, so a rejected batch leaves its
# signatures merely ABSENT from the result — indistinguishable from "Socorro has no row for this
# signature". A caller asking "is this signature brand new?" would read a dropped batch as a yes.
#
# 3800 leaves headroom under the ~4094-byte limit for the scheme, host and path.
_FIRST_DATE_URL_BUDGET = 3800


def _first_date_batches(signatures):
    """Split *signatures* into request-sized groups, measured in URL-encoded bytes.

    A signature too long to fit the budget on its own still goes out alone rather than being
    dropped: the server may well accept it, and a silent skip is the one outcome this function
    exists to avoid. See ``_FIRST_DATE_URL_BUDGET``."""
    batch, size = [], 0
    for sig in signatures:
        cost = len(urlencode({"signatures": sig})) + 1
        if batch and size + cost > _FIRST_DATE_URL_BUDGET:
            yield batch
            batch, size = [], 0
        batch.append(sig)
        size += cost
    if batch:
        yield batch


def first_seen_ever(signatures):
    """``{signature -> first buildid, ever}`` from Socorro's ``SignatureFirstDate`` API.

    THE MEASUREMENT THAT MOTIVATES THIS. Every other first-seen in this module comes from a
    SuperSearch bounded by ``MAX_WINDOW_DAYS``, and Socorro's Elasticsearch retention is shorter
    still — probed at roughly 178 days (the earliest Firefox crash a 2026-08 query can reach is
    2026-02-23). A signature older than the wall therefore reports its oldest *surviving* build,
    which makes an ancient signature look recent. Measured on the ten Firefox-nightly bugs the
    canary filed into `Core :: JavaScript*`, every one of which a module owner rejected:

    ==========  =======================  ==================
    bug         what the seed recorded   what is true
    ==========  =======================  ==================
    2062173     first seen 2025-12-27    **2017-10-28**
    2063364     candidate landed +68d    first seen 2019-01-24
    2062168     3 days old               first seen 2023-01-04
    2062114     first seen 2026-02-13    2025-03-10
    ==========  =======================  ==================

    The error has a direction: truncation can only move first-seen FORWARD, so a signature reads
    as newer than it is, never older.

    THIS IS NOT A DROP-IN REPLACEMENT FOR ``signature_history``'s ``first_seen``, and swapping it
    in would be a regression rather than a fix. ``_apply_signature_age_gate`` compares the
    candidate's push date against first-seen and downweights the lead when the candidate landed
    later; with a true, unbounded first-seen that comparison fires on any signature old enough,
    and a signature being old does not stop a new patch from causing the crash. Measured over the
    16 FIXED/DUPLICATE filings the canary has produced, true ages run 0, 0, 0, 0, 2, 39, 74, 158,
    536, 640, 1424, 3317, 3829, 4110, 4581 and 5767 days — so eight of them sit on signatures the
    unbounded clock calls years old, including ``FindSafeLength`` (first seen 2010-10-26, FIXED)
    and ``nsJARProtocolHandler::MimeService`` (2014-01-23, FIXED). The truncated instrument keeps
    those gaps small and is, by accident, the only reason the gate spares them. Changing the
    gate's clock is a separate decision with its own back-test; this function deliberately does
    not make it.

    WHAT IT IS FOR: the NOVELTY question — "is this signature brand new?" — where the unbounded
    value is strictly better and strictly conservative. It can only ever move a signature from
    "looks new" to "is old", because it sees a superset of the history SuperSearch sees. On the
    same two populations the separation is wide: the ten rejected JS filings are 283, 425, 514,
    1304, 1311, 1346, 1364, 1430, 2754 and 3205 days old, while five of the sixteen good filings
    sit at two days or less.

    Batched, and never raises: an unreachable API yields ``{}`` and a caller that cannot tell how
    old anything is, which must read as "we could not find out" and never as "brand new".
    Signatures the API has no row for are absent from the result for the same reason."""
    out = {}
    wanted = [s for s in (signatures or []) if s]
    if not wanted:
        return out
    for batch in _first_date_batches(wanted):
        try:
            r = net.get(SIGNATURE_FIRST_DATE_URL, params={"signatures": batch})
            r.raise_for_status()
            hits = (r.json() or {}).get("hits") or []
        except Exception as exc:  # pragma: no cover - network; never break a seed
            logger.warning("sigage: SignatureFirstDate lookup failed for %d signature(s): %s",
                           len(batch), exc)
            continue
        for hit in hits:
            sig, build = hit.get("signature"), hit.get("first_build")
            # A row with no build is no answer. `first_date` is not a substitute: the whole
            # module compares BUILD ids, and a crash date can post-date the build by months.
            if sig and build:
                out[sig] = str(build)
    return out


# The CPU models whose OWN defects make a crash report untrustworthy. One entry, and it is not a
# guess: Intel Raptor Lake desktop parts (13th/14th gen) have a documented instability that
# corrupts computation on perfectly healthy software, and Mozilla tracks the fallout in meta bug
# 1975808, "[meta] Raptor Lake (family 6 model 183 stepping 1) bugs" -- 48 dependent bugs when
# this was written, several of them layout/display-list crashes indistinguishable from a real one.
#
# Matched EXACTLY, against the same string mozilla/bugbot matches in `bugbot/crash/analyzer.py`,
# so the two filers cannot disagree about which hardware to distrust. Socorro renders `cpu_info`
# as a stable "family F model M stepping S", so an exact comparison is the right one.
BROKEN_CPUS = ("family 6 model 183 stepping 1",)

# Socorro renders `cpu_info` in TWO formats, and comparing against only one of them made every
# check above blind on 32-bit reports. Measured 2026-08-24 over Firefox nightly: all 12 of the top
# `cpu_arch=x86` terms carry a VENDOR prefix -- `GenuineIntel family 6 model 183 stepping 1`,
# `AuthenticAMD family 25 model 116 stepping 1` -- and none of the top 12 `cpu_arch=amd64` terms
# does. emilio caught it on bug 2065969, where the comment had told him the signature carried 0%
# Raptor Lake reports; the two crashes were Raptor Lake, on x86.
#
# THE BLIND ARCH WAS THE CONCENTRATED ONE: Raptor Lake is 25 of 98 x86 nightly reports (25.5%)
# against 546 of 6,274 amd64 (8.7%), ~3x. mozilla/bugbot compares the same string the same way
# (`bugbot/crash/analyzer.py`) and has the same blind spot.
#
# Normalised rather than adding the prefixed strings to `BROKEN_CPUS`: the literal patch is wrong
# for the next vendor and the next rendering, and what every site here wants is CPU IDENTITY, which
# the vendor id is redundant with. The lookahead means this only ever strips a token that PRECEDES
# a `family` -- ARM's `cpu_info` ("ARMv7 ARM part(0x4100c070) features: ...") has no `family` and is
# left exactly as it is, and arm64 carries no `cpu_info` facet at all.
_CPU_VENDOR_PREFIX = re.compile(r"^\S+\s+(?=family\s)")


def cpu_model(cpu_info):
    """``cpu_info`` reduced to its vendor-independent "family F model M stepping S" identity, so
    the same silicon compares equal whether the report came from a 32- or a 64-bit build."""
    return _CPU_VENDOR_PREFIX.sub("", str(cpu_info or "").strip())


# The models to distrust, keyed the way `cpu_model` returns them. Compare against THIS, never
# against `BROKEN_CPUS` directly -- that tuple is the amd64 rendering only.
BROKEN_CPU_MODELS = frozenset(cpu_model(c) for c in BROKEN_CPUS)

# What the same two measurements read across the NIGHTLY population, so a signature's share can
# be judged rather than merely quoted. Measured 2026-08-19 over 696,901 Firefox nightly reports
# in a 364-day window: 2.5% carry a bit-flip annotation, 4.1% come from a `BROKEN_CPUS` machine.
# Nightly-specific on purpose, to match the denominator `hardware_noise` uses -- the all-channel
# flip rate is 7.6%, three times higher, because release accumulates hardware noise that says
# nothing about a nightly crash. Constants rather than a second query: they move slowly, and the
# alternative is a 700k-document aggregation per run to refine a number used only to say "this is
# high".
POPULATION_BIT_FLIP_RATE = 0.025
POPULATION_BROKEN_CPU_RATE = 0.041

# ...AND THE SAME TWO NUMBERS PER CHANNEL, because they are a property of the POPULATION and beta
# is not nightly's. Re-measured 2026-08-25 with the shape of `hardware_noise` (Firefox,
# `get_search_channel(channel)`, 364 days): nightly 2.55% / 4.15% (n=692,770) -- which reproduces
# the two constants above, so the instrument agrees with the shipped values -- and beta 6.75% /
# 5.82% (n=269,501), i.e. 2.6x and 1.4x.
#
# WHY IT MATTERS THAT THIS IS PER CHANNEL: the number is printed to the model as "crash
# population: 2.5%" immediately before "the higher these are, the likelier it is that this
# signature is a failing-hardware artefact ... any mechanism you can construct for it will be
# fiction that fits". Telling a beta run that its 6% flip rate is 2.6x the population, when 6.75%
# IS the beta population, is an instruction to disbelieve an ordinary beta signature. Beta is
# 41.9% 32-bit x86 against nightly's 1.6%, which is the kind of difference that moves a
# hardware-annotation rate.
#
# `None` MEANS UNMEASURED AND MUST NOT FALL BACK TO NIGHTLY'S: every consumer drops the
# comparison instead, printing the signature's own share with no population claim beside it.
# Release is absent on purpose -- nobody measured it, and it is 10%-sampled.
_POPULATION_RATES = {
    "nightly": {"bit_flip": 0.025, "broken_cpu": 0.041, "top_cpu_share": 0.32},
    "beta": {"bit_flip": 0.0675, "broken_cpu": 0.0582, "top_cpu_share": None},
    "aurora": {"bit_flip": 0.0675, "broken_cpu": 0.0582, "top_cpu_share": None},
}

# The prose name of each population, so a sentence can say WHOSE population it is quoting.
POPULATION_LABEL = {
    "nightly": "Firefox-nightly",
    "beta": "Firefox-beta",
    "aurora": "Firefox-beta",
}


def _population(channel):
    """The rates for *channel*. An ABSENT channel falls back to nightly's; a channel that is
    NAMED but unmeasured (release) does not.

    The two cases are different questions and must not share an answer. "I was not told which
    channel" is a caller that predates this split -- every one of them is a nightly path, since
    that is the only channel the pipeline has run on -- so nightly is the behaviour-preserving
    answer, the same degradation `compiled_out.build_channel` takes. "This is release" is a
    channel we genuinely have no measurement for, and there the honest answer is to say nothing
    rather than publish nightly's denominator against it."""
    if not channel:
        return _POPULATION_RATES["nightly"]
    return _POPULATION_RATES.get(channel.lower(), {})


def population_bit_flip_rate(channel=None):
    """Share of *channel* reports carrying a bit-flip annotation, or ``None`` if unmeasured."""
    return _population(channel).get("bit_flip")


def population_broken_cpu_rate(channel=None):
    """Share of *channel* reports from a known-defective CPU, or ``None`` if unmeasured."""
    return _population(channel).get("broken_cpu")


def population_top_cpu_share_median(channel=None):
    """Median top-``cpu_info`` share across *channel*'s signatures, or ``None`` if unmeasured.

    ``None`` on beta, and that is the honest answer: 0.32 was measured over 200 Firefox-NIGHTLY
    signatures and nobody has run the same sample on beta. A consumer with ``None`` must not
    quote "the median Firefox-nightly signature" to a beta run -- it drops the comparison and
    states the share alone."""
    return _population(channel).get("top_cpu_share")


def population_label(channel=None):
    """"Firefox-beta" / "Firefox-nightly", for a sentence that quotes one of the rates above.

    Same fallback as ``_population``: an absent channel is a nightly caller, so the label must
    not say "Firefox" while the number beside it is nightly's."""
    if not channel:
        return POPULATION_LABEL["nightly"]
    return POPULATION_LABEL.get(channel.lower(), "Firefox")

# Where the TOP `cpu_info` share sits across the population, so "58 of 58 reports are on one
# processor model" can be read as remarkable or as ordinary instead of merely quoted. Measured
# 2026-08-21 over 200 Firefox-nightly signatures drawn the way the spike selector draws them
# (>=5 reports in a 14-day window; 318 qualified, 200 sampled, seed 20260821): min 0.05, p25
# 0.14, MEDIAN 0.32, p75 0.78, max 1.00, and 26 of the 200 (13%) sit on exactly ONE model.
# `_POPULATION_DEFAULTS["concentrated_share"]` 0.5 is NOT this number and must not be borrowed
# for it: that one was fit on `install_time`, whose median is 0.18 and p75 0.47, whereas 0.5 on
# `cpu_info` fires on 35% of the population. Reporting only -- see `hardware_noise` for the
# sweep that killed every attempt to suppress on this.


POPULATION_TOP_CPU_SHARE_MEDIAN = 0.32

# The floor under the share BEFORE it may be stated, and it is the definition of the sample above
# rather than a tuned number: every signature in that 200 had >=5 reports, so a share computed
# from fewer has no population to be compared with. It is also forced. A signature with ONE
# report has one `cpu_info` string, hence one term and a top share of exactly 1.00 -- arithmetic,
# not an observation -- and single-report crashes are this pipeline's normal case: 18 of the 52
# bugs the canary has filed have exactly one report, 17 of them read 1.00, and 19 of the 27
# filings that clear `report_bug._HARDWARE_NOTE_LIFT` sit under 5 reports (measured 2026-08-21).
# Reporting only: `_apply_bit_flip_gate` records the share for every verdict whatever the sample,
# because the whole point of recording it is to find out later whether it predicts anything.
POPULATION_TOP_CPU_SHARE_MIN_REPORTS = 5

# The full shape of a `hardware_noise` answer, every value unknown. Callers that must produce
# the shape without asking (the gate disabled, the lookup raised) copy this rather than
# re-listing the keys: a hand-copied list is exactly how a newly added key ends up present on
# the answered path and absent on the disabled one.
NO_HARDWARE_NOISE = {
    "reports": None, "bit_flip_reports": None, "broken_cpu_reports": None,
    "bit_flip_rate": None, "broken_cpu_rate": None,
    "cpu_reports": None, "cpu_terms": None, "top_cpu_term": None, "top_cpu_share": None,
}


def hardware_noise(signature, product="Firefox", channel="nightly", days=MAX_WINDOW_DAYS):
    """How much of this SIGNATURE is hardware error rather than a bug anyone can fix?

    ``{"reports", "bit_flip_reports", "broken_cpu_reports", "bit_flip_rate", "broken_cpu_rate",
    "cpu_reports", "cpu_terms", "top_cpu_term", "top_cpu_share"}``. ``NO_HARDWARE_NOISE`` is the
    same shape with every value ``None``, which is what "we could not find out" returns.

    WRITTEN FOR BUG 2064600. Clouseau filed a display-list crash at 97% worth-investigating and
    Timothy Nikkel replied within twenty minutes: "About 50% of the crashes with this signature
    have non-zero bit flip probability. That might be something you want to include in your llm
    prompt to consider. And there is also several of the known buggy family 6 model 183 stepping 1
    without a bit flip annotation. ... I always look for these two things in crash reports." Both
    numbers check out -- 3 of the signature's 6 nightly reports carry a flip annotation, and 139
    of its 142 Raptor Lake reports carry none -- and Clouseau could see neither, because it read
    the flip field only for the ONE report it was triaging and never read ``cpu_info`` at all.

    THE TWO SIGNALS ARE NEARLY DISJOINT, which is why both are measured rather than one. Across
    all channels that signature has 107 reports with a flip annotation and 142 on a Raptor Lake,
    and just 3 that are BOTH: the stackwalker's heuristic wants a faulting address one bit away
    from something plausible, and a Raptor Lake miscomputation rarely looks like that. Each covers
    a different third of the signature. A per-report flip check -- all Clouseau had -- sees
    neither.

    THE DENOMINATOR IS THE WHOLE RULE, and getting it wrong is not a detail. Re-measured
    2026-08-21 over all 52 bugs the canary has filed (19 FIXED/DUPLICATE/ASSIGNED, 8
    INVALID/WORKSFORME, 25 still open): computed across all products and channels over 180 days
    the same thresholds fire on 8 of the 52, and one of them is bug 2062219 (``nsAtom::IsStatic``),
    RESOLVED FIXED -- a real defect, killed, because that signature runs 49% bit flips over a
    release population and 12% in nightly. Restricted to the crash's OWN product and channel it
    fires on 6 and kills ZERO of the 19 FIXED/DUPLICATE/ASSIGNED filings, while still catching
    two INVALID ones (2062173 and 2063364) and bug 2064600 itself. Release accumulates years of
    failing consumer hardware on a hot signature; that says nothing about whether a nightly crash
    is real, and averaging the two populations together lets the larger one decide.

    THE FULL 364-DAY WINDOW, unlike bugbot's few weeks, for the same reason in reverse: a nightly
    slice is SMALL. Bug 2064600's signature has 6 nightly reports in a year and 1 in the last 28
    days, so bugbot's window would see no sample at all here. bugbot can afford a short one
    because it is scanning for busy signatures worth filing; we are handed one crash and have to
    judge it. Window truncation can only shrink the sample, never inflate a rate.

    Reports, not machines -- and checked. A single failing machine filing hundreds of reports
    would fake any share computed this way, so the confound was measured on the signature that
    prompted this: its flip-annotated reports span more than 100 distinct ``install_time`` values,
    the largest contributing 3. Read ``distinct_signatures`` in ``machine.py`` for the
    complementary rule that catches the one-broken-machine case directly.

    THE FOUR CPU-SPREAD KEYS ARE FREE, and bug 2065373 is why they are kept. The `cpu_info`
    facet is already fetched to count Raptor Lakes and every row but one was being thrown away.
    :jstutte reviewed that filing and asked "could clouseau do some OS / install distribution
    checks on the socorro data?" -- and the run already held the answer: 58 reports, ONE
    `cpu_info` row, `[["family 25 model 117 stepping 2", 58]]`. What the two models and the filed
    bug were shown instead was `broken_cpu_rate` 0.0, a hardware clean bill computed from the
    same rows that say the whole population is a single processor model. `top_cpu_share` is
    denominated on `cpu_reports`, the reports that HAVE a cpu_info string, not on `total`:
    Socorro carries one for 2,552 of 15,329 Firefox-nightly macOS reports (16.6%) against 99.8%
    on Windows and 98.1% on Linux, so dividing by `total` would report a mac-heavy signature as
    unconcentrated when it is simply unmeasured.

    `top_cpu_share` IS NOT A SUPPRESSOR AND MUST NOT BECOME ONE -- measured, not assumed. On the
    52 filings at the gate's own `min_signature_reports` floor of 5, every threshold from 0.40 to
    0.95 suppresses at least one of the 19 controls while catching at most the single INVALID the
    shipped `broken_cpu_rate` rule already catches. 0.50 eats FIVE: 2062052 (FIXED,
    `ScreenOrientation::Create`, 6 reports, 6/6 on one CPU), 2063678 (FIXED,
    `libc.so.6 | cuEGLApiInit`, 1111 reports at 0.97, a Mageia/NVIDIA bug), 2063809 (FIXED,
    `ff_vk_exec_add_dep_frame`, 0.54, AMD Vulkan), 2061180 (DUPLICATE, `libvulkan_radeon.so`,
    0.77) and 2063864 (DUPLICATE, `setsockopt_syscall`, 0.83); 0.80 still eats those last three,
    0.90 and 0.95 eat 2062052 and 2063678, and even 1.00 eats 2062052 -- no value of this
    statistic is free. The shipped rule eats 0 of the 19. Against the 8 bad filings
    the statistic reads AUC 0.333 on 3 bad versus 13 controls at that floor -- it separates them
    in the wrong direction (median top share 0.148 bad, 0.407 control). The one variant that eats
    no control, `cpu_terms == 1 and reports >= 20`, fires on 20 of 174 eligible background
    signatures and 8 of those 20 carry a real Firefox bug, among them
    `sync15::bso::content::content_with_id_to_json` -> bug 2056116, this repo's own off-stack
    pref-flip archetype. Concentration is a SCOPE hint -- a driver, a distribution, an
    instruction set -- so it is reported (`triage._cpu_spread_line`,
    `report_bug.build_hardware_note`) and never gated.

    AN EMPTY `cpu_info` FACET IS UNKNOWN, NOT ZERO, and the two facets differ on exactly this.
    Socorro sets `possible_bit_flips_max_confidence` only when the stackwalker found a candidate,
    so no rows there means no report has one: a real zero. `cpu_info` is simply missing on some
    reports, and on macOS it is missing on 83% of them, so no rows there means we do not know
    what hardware this signature runs on. This used to return `broken_cpu_rate` 0.0 for a
    population Socorro says nothing about -- a fabricated clean bill on 3 of the 52 filings
    (2062806 FIXED, 2063002 DUPLICATE, 2062335) and on 3 of the 200 background signatures, 39 of
    which are missing more than 10% of their CPU strings. It changes no suppression, because
    `orchestrator._signature_is_mostly_hardware`'s rate tests are positive requirements and both
    ``None`` and 0.0 answer False; what it stops is the crash brief and the filed bug stating a
    clean bill nobody measured.

    ONE SuperSearch, ~300ms, on a run that already takes ~20 minutes. ``None`` rather than 0 on
    every failure path, because this feeds a suppression and "we could not find out" must never
    be able to satisfy a threshold."""
    empty = dict(NO_HARDWARE_NOISE)
    if not signature:
        return empty
    days = max(1, min(int(days or MAX_WINDOW_DAYS), MAX_WINDOW_DAYS))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    params = {
        "signature": "=" + signature,
        "product": product or "Firefox",
        "date": ">=" + since,
        "_facets": ["possible_bit_flips_max_confidence", "cpu_info"],
        # `possible_bit_flips_max_confidence` takes 17 distinct values across the entire corpus,
        # so summing its facet is exact. `cpu_info` has a long tail, but truncation is SAFE here
        # for a reason worth stating: facets come back COUNT-ORDERED, so a model holding a
        # material share of a signature is always near the top, and anything the cut hides is by
        # construction too small to clear a threshold. That is the exact inverse of the `build_id`
        # trap documented at the top of this module, where the row we needed was the rarest.
        #   That argument is about the two RATES and does not extend to `cpu_reports`, which
        #   sums this facet and denominates `top_cpu_share`: a truncated facet shrinks the
        #   denominator and INFLATES the share. Left unguarded deliberately -- across the 252
        #   signatures measured on 2026-08-21 the widest `cpu_info` facet held 190 terms and
        #   none was truncated -- and the share is reported, never gated, so the cost of being
        #   wrong is a sentence, not a suppression.
        "_facets_size": 200,
        "_results_number": 0,
    }
    if channel:
        # Through `utils.get_search_channel`, never raw. Socorro files a third of beta under
        # `aurora`: a raw "beta" returns 154,768 of the 264,278 Firefox beta+aurora reports in a
        # 364-day window, dropping 41% of the channel (measured 2026-08-21). A no-op on nightly,
        # which is all that runs today, and not a cosmetic one the day beta or Fenix is enabled
        # -- on `js::jit::CompilerFrameInfo::sync` the raw channel reads cpu 0.077 against 0.045
        # corrected, so a shrunken denominator can push a signature OVER the suppression line,
        # and `mozilla::ActiveScrolledRoot::GetNearestScrollASR` reads n=5 raw against n=10,
        # straddling `min_signature_reports`.
        params["release_channel"] = utils.get_search_channel(channel)
    got = {}

    def handler(json_, data):
        data["result"] = json_

    try:
        socorro.SuperSearch(params=params, handler=handler, handlerdata=got).wait()
    except Exception as exc:  # pragma: no cover - network; never break a seed
        logger.warning("sigage: hardware-noise lookup failed for %r: %s", signature, exc)
        return empty
    result = got.get("result") or {}
    total = result.get("total")
    if not isinstance(total, int) or total <= 0:
        # Zero rows is NOT "a clean signature": an empty response is equally what a malformed or
        # throttled query returns, and this feeds a suppression. Say we do not know.
        return empty
    facets = result.get("facets") or {}

    def _sum(field, keep=None, if_empty=0):
        # `if_empty` is the None-vs-0.0 distinction and it differs per facet, which is why it is
        # a parameter and not a constant: an empty flip facet is a measured ZERO (Socorro only
        # sets the field when the stackwalker found a candidate), an empty `cpu_info` facet is
        # an UNKNOWN (the field is simply absent on those reports).
        rows = facets.get(field)
        if not isinstance(rows, list):
            return None
        if not rows:
            return if_empty
        return sum(r.get("count") or 0 for r in rows
                   if isinstance(r, dict) and (keep is None or keep(r.get("term"))))

    flips = _sum("possible_bit_flips_max_confidence")
    broken = _sum("cpu_info", keep=lambda t: cpu_model(t) in BROKEN_CPU_MODELS, if_empty=None)
    # The rows the Raptor Lake sum throws away. Already paid for, and they answer the question
    # `broken_cpu_rate` cannot: WHICH processor, and how many different ones.
    #
    # Grouped by `cpu_model` for the same reason the sum above is: otherwise one signature with
    # reports from both build architectures would have `broken_cpu_rate` treating them as ONE
    # processor while `cpu_terms` counted TWO and `top_cpu_share` split its own denominator.
    grouped = {}
    for row in (facets.get("cpu_info") or []):
        if isinstance(row, dict):
            model = cpu_model(row.get("term"))
            grouped[model] = grouped.get(model, 0) + (row.get("count") or 0)
    cpu_reports = sum(grouped.values())
    top = max(grouped.items(), key=lambda kv: kv[1], default=None)
    return {
        "reports": total,
        "bit_flip_reports": flips,
        "broken_cpu_reports": broken,
        "bit_flip_rate": None if flips is None else flips / total,
        "broken_cpu_rate": None if broken is None else broken / total,
        "cpu_reports": cpu_reports or None,
        "cpu_terms": len(grouped) or None,
        "top_cpu_term": None if top is None else top[0],
        "top_cpu_share": ((top[1] / cpu_reports)
                          if (top is not None and cpu_reports) else None),
    }


_JSON_REV_CACHE: dict = {}


def json_rev(node, channel="nightly"):
    """hg's ``json-rev`` for a changeset: ``{node, pushdate, git_commit, ...}``.

    ONE request serves both things we want about a changeset — when it landed
    (``pushdate_for_node``) and its git counterpart (``git_commit_for_node``) — so they share
    this cache rather than each paying for the same lookup. The endpoint is SLOW (measured
    8-13s on mozilla-central, whichever of hg.mozilla.org / hg-edge you ask), which is why it
    belongs in the worker and never on a page render.

    Takes a SHORT rev, unlike lando's ``hg2git`` which needs the full 40 chars. ``{}`` when
    unresolvable; raises nothing. Cached per ``(node, channel)`` — both fields are immutable."""
    if not node:
        return {}
    key = (node, channel or "")
    if key in _JSON_REV_CACHE:
        return _JSON_REV_CACHE[key]
    from libmozdata.hgmozilla import Mercurial

    from crashclouseau import net

    out = {}
    repo_url = Mercurial.get_repo_url(channel) if channel else ""
    if repo_url:
        try:
            r = net.get("{}/json-rev/{}".format(repo_url, node), allow_redirects=True)
            r.raise_for_status()
            out = r.json() or {}
        except Exception as exc:  # pragma: no cover - network; never break a gate
            logger.warning("sigage: json-rev lookup failed for %s: %s", node, exc)
    _JSON_REV_CACHE[key] = out
    return out


# The repo a changeset ORIGINALLY lands in. Everything on a release branch that did not arrive
# as an uplift came from here, and this is where its real landing date lives.
ORIGIN_CHANNEL = "nightly"


def landing_pair(node, channel="nightly"):
    """``(origin_pushdate, channel_pushdate)`` for a changeset, each ``[epoch, tz]`` or ``None``.

    Two clocks, because on a release branch they are DIFFERENT and only one of them is a
    landing date. ``pushlog.collect`` stamps every changeset with its PUSH date in the repo it
    was read from, and a central->beta merge is ONE push carrying a whole cycle -- so every
    changeset in it reports the merge date. Measured 2026-08-25: four sampled non-merge members
    of beta push 27990 (``f44045181a24``, ``7f4d7e8c27d6``, ``2c98bfc534ef``, ``02297ff55cd2``)
    all report beta ``2026-08-13T14:15:59`` against central ``2026-07-21T09:46:48`` -- a 23.2-day
    forward shift, identical across the push -- and six sampled members of push 27533 (6,917
    changesets, all at ``2026-07-20T17:14:53``) give central dates from 06-15 to 07-13, i.e. a
    drift of 6.9 to 34.8 days depending on where in the cycle the change landed.

    ``None`` for the origin is the UPLIFT case and not an error: a beta uplift is an hg graft
    with a NEW hash, so central has never heard of it (0 of 1,009 candidate-bearing in-cycle beta
    changesets exist on m-c under the same hash) and the beta push date IS its landing date.

    Costs one extra ``json-rev`` on a non-nightly channel, cached per (node, channel) like every
    other call here; on nightly the two are the same request and the pair is free."""
    own = json_rev(node, channel).get("pushdate") or None
    if not channel or channel == ORIGIN_CHANNEL:
        return own, own
    origin = json_rev(node, ORIGIN_CHANNEL).get("pushdate") or None
    return origin, own


def pushdate_for_node(node, channel="nightly"):
    """When a changeset landed (``[epoch, tzoffset]``), or ``None``.

    This is the FALLBACK for the stale-signature gate. The seed pre-computes a pushdate for
    every candidate in the build's pushlog window, but the agent can choose a candidate that
    was never in that window (it found it via blame), and such a candidate has no pre-computed
    landing date -- so the gate used to silently no-op on precisely the crashes it exists to
    catch. Seen in prod on ``0cf2a052-2eae-4228-824f-6284d0260728``: the candidate landed 126
    days after the signature first appeared, the gate skipped, and only the (paid) blind second
    opinion noticed.

    THE ORIGIN REPO WINS WHEN IT KNOWS THE NODE. Off nightly, the channel's own push date is the
    date the changeset ARRIVED on that branch, which for anything that came in with a cycle
    merge is the merge date -- up to ~35 days after the code was written (see ``landing_pair``).
    Five consumers read this number and every one of them wants "when did this code first
    exist": the stale-signature gate compares it against the signature's first-seen buildid, the
    prompt prints it as ``landed=``, and ``bugzilla_apply._bug_for_this_regression`` asks whether
    an open bug predates it -- and a clock that runs late there errs toward accepting an OLD bug
    as this crash's venue, which is the direction beta's file-only-new-bugs rule forbids.

    Falls back to the channel's own date, which is the genuine uplift answer. Note this is NOT
    the same decision as ``backedout_by_for_node`` / ``same_push_backout_target`` make: those
    ask what happened to the changeset ON THIS BRANCH, so they correctly stay on the channel
    repo."""
    origin, own = landing_pair(node, channel)
    return origin or own


def git_commit_for_node(node, channel="nightly"):
    """The git sha for an hg changeset, or ``""``. Firefox lives in both forges since the
    hg->git migration, so the filed bug links the changeset on each; resolved HERE, in the
    worker, and persisted on the candidate so no page render ever pays for it."""
    return json_rev(node, channel).get("git_commit") or ""


def backedout_by_for_node(node, channel="nightly"):
    """The sha that BACKED OUT this changeset: ``""`` when hg says it was not backed out, and
    ``None`` when we could not find out.

    TRI-STATE on purpose. A backed-out candidate is SUPPRESSED outright, not downweighted, so
    "we don't know" must never collapse into "it's clean" — a failed lookup has to leave the
    verdict alone. ``json_rev`` returns (and caches) ``{}`` for every no-answer case: an empty
    channel makes no request at all, and a 404/timeout is swallowed. Hence the ``node`` sentinel
    rather than testing ``backedoutby`` directly, which is simply ABSENT on a clean changeset
    and would be indistinguishable from a failure.

    Free in practice: this is the same cached ``json-rev`` request ``pushdate_for_node`` and
    ``git_commit_for_node`` already make for this same node on every online run.

    Note it says nothing about WHEN the backout landed, and it stays set forever — a change
    that was backed out and later RE-LANDED (as a new node) still reports the old backout."""
    rev = json_rev(node, channel)
    if not rev.get("node"):
        return None
    return rev.get("backedoutby") or ""


def desc_for_node(node, channel="nightly"):
    """A changeset's commit message, or ``""`` when we could not find out.

    Free: the same cached ``json-rev`` request already serves ``pushdate_for_node`` /
    ``git_commit_for_node`` / ``backedout_by_for_node`` for this node on every online run.
    Wanted for the MIRROR of ``backedout_by_for_node``'s predicate — hg tells us straight out
    whether a changeset WAS backed out, but whether it IS ITSELF a backout only shows up in
    its description (``pushlog.is_backed_out``)."""
    return json_rev(node, channel).get("desc") or ""


_PUSH_CACHE: dict = {}

# What a backout says it undoes, in the two shapes mozilla-central actually uses. The GIT one
# dominates since the hg->git migration: Lando writes `Revert "<title>"` + `This reverts commit
# <40-char GIT sha>`, and NOT ONE of 909 backout descriptions sampled across pushes 44620-45020
# names an hg short hash. The hg one is kept for `hg backout`-style descriptions (one line per
# backed-out changeset, so ``findall``).
_REVERTS_GIT_RE = re.compile(r"^This reverts commit ([0-9a-f]{40})", re.M)
_BACKED_OUT_RE = re.compile(r"[Bb]acked out changeset ([0-9a-f]{12,40})")


def push_for_node(node, channel="nightly"):
    """The ``json-pushes`` record of the push that landed ``node``, ``full=1`` so every member
    changeset carries its own ``node`` + ``desc``. ``{}`` when unresolvable; raises nothing.

    Cached per ``(node, channel)`` like ``json_rev`` — a push is immutable once landed. Its
    ONE caller (``same_push_backout_target``) only runs for a candidate that is itself a
    backout, which is ~0.5% of runs, so this never costs a normal triage anything."""
    if not node:
        return {}
    key = (node, channel or "")
    if key in _PUSH_CACHE:
        return _PUSH_CACHE[key]
    from libmozdata.hgmozilla import Mercurial

    from crashclouseau import net

    out = {}
    repo_url = Mercurial.get_repo_url(channel) if channel else ""
    if repo_url:
        try:
            r = net.get(
                "{}/json-pushes".format(repo_url),
                params={"changeset": node, "version": "2", "full": "1"},
                allow_redirects=True,
            )
            r.raise_for_status()
            for pushid, push in ((r.json() or {}).get("pushes") or {}).items():
                out = {**push, "pushid": pushid}
                break
        except Exception as exc:  # pragma: no cover - network; never break a gate
            logger.warning("sigage: json-pushes lookup failed for %s: %s", node, exc)
    _PUSH_CACHE[key] = out
    return out


def revert_targets(desc):
    """Every changeset ``desc`` says it reverts, as 12-char hg hashes — or ``None`` when we
    cannot enumerate them exactly.

    ``None`` covers three cases that must NOT be told apart, because all three mean "we do not
    know what this undoes": the description names nothing, it names a git commit lando could
    not map, or lando was unreachable (``inspector.git2hg`` returns ``""`` for a genuine
    non-Firefox commit AND for a transient failure). Every one of them has to reach the caller
    as unknown, since the only thing a caller does with a complete answer is SUPPRESS.

    Deliberately reads the description rather than matching against the push's members: a
    sheriff routinely reverts and RELANDS in one push, and a reland carries the reverted
    patch's title verbatim, so any title-similarity match happily "proves" that a backout of a
    days-old changeset is same-push. Measured on live mozilla-central, that mistake makes 6.4%
    of matches point at a node the backout does not revert at all."""
    if not desc:
        return None
    targets = {h[:12] for h in _BACKED_OUT_RE.findall(desc)}
    git_shas = _REVERTS_GIT_RE.findall(desc)
    if not targets and not git_shas:
        return None
    from crashclouseau import inspector

    for git_sha in git_shas:
        hg_hash = inspector.git2hg(git_sha)
        if not hg_hash:
            return None
        targets.add(hg_hash[:12])
    return targets or None


def same_push_backout_target(node, channel="nightly"):
    """Does ``node`` back out changesets that ALL landed in ``node``'s own push?

    TRI-STATE like ``backedout_by_for_node``: the first such changeset, ``""`` when the answer
    is no, and ``None`` when we could not find out. The distinction matters because a hit
    SUPPRESSES the verdict outright, so an unresolvable lookup must never read as a hit — nor
    as a clean "no".

    WHY THIS IS THE PRECISE DISCRIMINATOR. A backout is only interesting as a "regressor" when
    it restores a crash that some build had stopped shipping. If everything it reverts landed
    in its own push, no build ever contained any of it: the tree's content is identical before
    the push and after it, so the changeset provably changed nothing. Seen in prod on
    ``00b44d2a-4343-4caa-9e12-907550260802``, where a fix and its same-day revert both reached
    mozilla-central in autoland merge push 44977 (``dom/onnx/InferenceSession.cpp`` is
    byte-identical at the push parent, at the revert and at the push head) and the pipeline
    still reported the revert as the culprit at 97%.

    ALL of them, not any: proving one of three reverted patches is same-push says nothing about
    the other two, and a target that landed in an EARLIER push is exactly the case where the
    tree did differ and the backout is a real regressor worth reporting."""
    targets = revert_targets(desc_for_node(node, channel))
    if targets is None:
        return None
    members = push_for_node(node, channel).get("changesets") or []
    if not members:
        return None
    by_short = {(m.get("node") or "")[:12]: (m.get("node") or "")
                for m in members if m.get("node")}
    if not targets <= set(by_short):
        return ""
    # Push order, so a multi-changeset backout names the first thing it undid rather than
    # whichever hash happens to sort first.
    for member in members:
        short = (member.get("node") or "")[:12]
        if short in targets:
            return member.get("node")
    return ""


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


def signature_age_days(first_seen, buildid):
    """How old the signature already was when the build being triaged was produced.

    A DIFFERENT question from ``days_landed_after_first_seen``, and the difference is why both
    exist. That one asks whether a specific CANDIDATE can be the origin, and it is the right
    question for a downweight: a signature merely being old says nothing, since two thirds of
    triaged signatures are old and a new patch can perfectly well break code that has crashed
    before. This one asks whether the signature is BRAND NEW, which is only useful in the
    opposite direction — as evidence FOR looking, never against it.

    ``None`` when either side is unknown, because a signature whose age we could not establish
    is not a new one. Pair it with ``first_seen_ever``: computed from the windowed ``first_seen``
    it will read a years-old signature as days old whenever the window truncates."""
    seen_dt = _buildid_to_dt(first_seen)
    build_dt = _buildid_to_dt(buildid)
    if seen_dt is None or build_dt is None:
        return None
    return round((build_dt - seen_dt).total_seconds() / 86400.0, 1)


# How far the two clocks must disagree, in days, before an inversion is called a re-signaturing
# rather than cron lag. `SignatureFirstDate` is refreshed by a cron walking a rolling window of
# `date_processed`, so it trails the search index by up to a day in the ordinary course of events.
# The gap is wide: measured over 450 non-novel control signatures the only one to invert at all did
# so by 1 day, and the real inversions in our own corpus sit at 40.5 and 45.6 days.
RENAME_DRIFT_DAYS = 30

# How far apart the two clocks must be before a human-facing surface bothers to explain the
# disagreement. Below this they tell the same story and a second date is only noise.
CLOCK_DISAGREEMENT_DAYS = 30

# Where "new" stops, for WORDING ONLY -- it selects which true thing is worth saying first in the
# crash brief and whether the filed bug says "new" or "not new", and moves no rung, score or
# decision. Deliberately not the stale-signature gate's threshold, so nobody later reads it as one.
NEW_SIGNATURE_DAYS = 7


def age_facts(buildid, windowed, ever, observed=None):
    """Both first-seen clocks, the age each implies, and whether they invert. ``{}`` if neither
    answered.

    ONE function because three places need the same arithmetic and must not be allowed to
    disagree about it: the recorder that writes these into ``corroborations``
    (``orchestrator._record_signature_age_facts``), the crash brief that now tells the agent how
    old the signature is (``triage._signature_age_lines``), and the filed bug that now tells the
    reader (``report_bug.build_signature_age_note``). Prose differs between the three; the numbers
    behind it cannot.

    ``windowed`` is the 364-day SuperSearch answer and ``ever`` Socorro's ``SignatureFirstDate``
    table; see ``first_seen_ever`` for why the second is the true one and why substituting it into
    ``_apply_signature_age_gate`` would be a regression. ``observed`` is the UNFLOORED all-channel
    first-seen used for the inversion only, defaulting to ``windowed``.

    Keys are omitted rather than zeroed whenever a lookup did not answer: a signature whose age we
    could not establish must never read as a new one."""
    # Unrolled, with LITERAL keys, rather than the `"signature_first_seen_" + key` loop this used
    # to be: a computed key is invisible to the registry scanner in
    # tests/test_corroboration_registry.py, and all four of these were live in prod dossiers and
    # undeclared because of it. A corroboration key has to be greppable.
    facts = {}
    if windowed:
        facts["signature_first_seen_windowed"] = windowed
        age = signature_age_days(windowed, buildid)
        if age is not None:
            facts["signature_age_days_windowed"] = age
    if ever:
        facts["signature_first_seen_ever"] = ever
        age = signature_age_days(ever, buildid)
        if age is not None:
            facts["signature_age_days_ever"] = age
    observed = observed or windowed
    if observed and ever:
        drift = signature_age_days(ever, observed)
        if drift is not None:
            facts["signature_clock_drift_days"] = drift
            if drift <= -RENAME_DRIFT_DAYS:
                facts["signature_rename_suspected"] = True
    return facts
