# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from bisect import bisect_left
from collections import defaultdict
from datetime import datetime
import hashlib
import re
from libmozdata import socorro
import pytz
import six
import cpp_demangle
from . import config


_DEMANGLE_CACHE = {}


def demangle(name):
    """Best-effort C++ symbol demangle (cpp-demangle, in-process, cached).

    Returns the demangled name, or the input unchanged when it is not a well-formed
    mangled symbol (an already-demangled name, a plain identifier, or an off-by-one
    corrupted symbol id — cpp_demangle raises ValueError, we fall back)."""
    if not name:
        return name
    if name not in _DEMANGLE_CACHE:
        try:
            _DEMANGLE_CACHE[name] = cpp_demangle.demangle(name)
        except Exception:
            _DEMANGLE_CACHE[name] = name
    return _DEMANGLE_CACHE[name]


def get_search_channel(channel):
    """Get the search channel(s) for Socorro queries"""
    return ["beta", "aurora"] if channel == "beta" else channel


# ONE LAMBDA, TWO SIGNATURES. Socorro normalises a lambda's frame from what each toolchain
# demangles it to: MSVC's ``<lambda_1>`` collapses to ``<T>`` like any template argument, clang's
# ``$_95`` collapses to ``$``. So ``QuotaManager::Shutdown::<T>::operator()`` (Windows) and
# ``QuotaManager::Shutdown::$::operator()`` (Linux/macOS) are the SAME frame of the SAME
# defect, and every per-signature instrument -- the spike test, the daily rate, the filer's
# dedup, Bugzilla's ``cf_crash_signature`` search -- sees each half alone.
#
# Measured on the 2026-08-14 nightly build-day of exactly that signature: merged, 27 crashes
# against a prior-3-day maximum of 9 fires the 3x spike rule at the threshold; split, the ``<T>``
# half is 19 against 7 (needs 21) and the ``$`` half is 8 against 3 (needs 9). Neither was ever
# selected. The rise was real, a regressor was in the window, and the split alone lost it.
#
# Only a STANDALONE component is a lambda: ``::$::``, ``::<T>::`` or either at the end of a
# frame. ``SpinEventLoopUntil<T>`` is a template on a name and is left alone in both directions.
_LAMBDA_COMPONENT = re.compile(
    r"(?<=::)(?:\$(?:_\d+)?|<lambda(?:_|#)\d+>|\{lambda#\d+\})(?=::|$| \|)"
)
_LAMBDA_T_COMPONENT = re.compile(r"(?<=::)<T>(?=::|$| \|)")


def lambda_family(signature):
    """The one spelling every demangling of *signature*'s lambdas shares (the ``<T>`` form).

    Two signatures with the same family are the same frames; group by it before any
    per-signature decision. A signature with no lambda component is its own family."""
    return _LAMBDA_COMPONENT.sub("<T>", signature or "")


def lambda_siblings(signature):
    """Every spelling Socorro may give *signature*'s lambdas, itself included -- what a lookup
    keyed on the signature (our filings, Bugzilla's crash-signature field) has to ask for so
    the other half of the same defect is not invisible to it."""
    if not signature:
        return set()
    family = lambda_family(signature)
    return {signature, family, _LAMBDA_T_COMPONENT.sub("$", family)}


def lambda_families(signatures):
    """``{signature: [every member of its family, sorted]}`` for the signatures that share a
    family with at least one other in *signatures*. A lone signature is not listed: the caller
    treats it exactly as before."""
    by_family = defaultdict(list)
    for sgn in signatures:
        by_family[lambda_family(sgn)].append(sgn)
    return {
        sgn: sorted(members)
        for members in by_family.values()
        if len(members) > 1
        for sgn in members
    }


def merge_day_series(series):
    """Sum per-build-day ``{day: {"count", "bids": {bid: n}, "installs": {bid: k}}}`` series.

    Installs are summed too. That is exact here and only here: the halves of a lambda family are
    partitioned by TOOLCHAIN, i.e. by platform, so no installation can appear in two of them."""
    merged = {}
    for numbers in series:
        for day, entry in numbers.items():
            out = merged.setdefault(day, {"count": 0, "bids": {}, "installs": {}})
            out["count"] += entry.get("count", 0)
            for bid, n in entry.get("bids", {}).items():
                out["bids"][bid] = out["bids"].get(bid, 0) + n
            for bid, k in entry.get("installs", {}).items():
                out["installs"][bid] = out["installs"].get(bid, 0) + k
    return merged


# A crash where NOTHING FAULTED at the crashing frame: a watchdog killed the process because
# work elsewhere exceeded a time budget. The three spellings Socorro gives them, plus the
# report type it sets for a hang, plus the reason strings the watchdogs write.
_WATCHDOG_SIGNATURE_PREFIXES = ("shutdownhang |", "AsyncShutdownTimeout |", "hang |")
_WATCHDOG_REASON = re.compile(r"timed out|timeout|hang|watchdog", re.IGNORECASE)


def is_watchdog_crash(signature=None, report_type=None, moz_crash_reason=None):
    """Is this a hang / timeout crash rather than a fault?

    THE QUESTION THESE CRASHES ASK IS DIFFERENT, and two of the pipeline's tools get it wrong
    when they do not know it. A watchdog signature fires whenever the awaited work exceeds the
    budget, so it is typically YEARS old, and a change that makes the work slower -- more I/O,
    more items, an extra fsync, a rescan, a lock held longer -- regresses it without
    "introducing" it. On 2026-08-15 the principal named exactly such a change for a
    ``shutdownhang | ... QuotaManager::Observer::Observe`` crash, and the blind second opinion
    refuted it with "first seen 273 days before the change, so the change cannot have
    introduced it" and "touches no shutdown code and cannot itself hang". The module owner
    confirmed the same change as the regressor the next day (bug 2063892). Both arguments are
    correct for a fault and inverted for a watchdog, and this predicate is how the gates and the
    prompts tell the two apart."""
    sig = (signature or "").strip()
    if any(sig.startswith(p) for p in _WATCHDOG_SIGNATURE_PREFIXES):
        return True
    if (report_type or "") == "hang":
        return True
    return bool(moz_crash_reason and _WATCHDOG_REASON.search(str(moz_crash_reason)))


def get_extension(filename):
    """Get file extension"""
    i = filename.rfind(".")
    if i != -1:
        return filename[i + 1:]
    return ""


def get_major(v):
    """Get major version from version"""
    return int(v.split(".")[0])


def get_colors():
    """Get gradient of colors for score"""
    N = config.get_max_score()
    h = (236 - 48) / N
    r = [int(48 + n * h) for n in range(0, N + 1)]
    colors = [""] * (N + 1)
    for n in range(0, N + 1):
        colors[n] = "#" + hex(r[-n - 1])[2:] + hex(r[n])[2:] + "30"
    return colors


def short_rev(rev):
    """Shorten a revision to 12 characters if needed"""
    if len(rev) > 12:
        return rev[:12]
    return rev


def score(x, a):
    """Compute the score for a line and the closest touched line in the patch"""
    # a <= x - 5 ==> 0.9
    # x - 5(n + 1) < a <= x - 5n ==> 0.9 - 0.1n
    # x - a - 5 < 5n <= x - a ==> n = floor((x - a) / 5)
    n = (x - a) // config.get_num_lines()
    N = config.get_max_score() - 1
    return 0 if n >= N else N - n


def get_line_score(line, lines):
    """Get the score for a line in a set of lines"""
    if not lines:
        return 0
    i = bisect_left(lines, line)
    if i == 0:
        return config.get_max_score() if line == lines[0] else 0

    if i == len(lines):
        return score(line, lines[i - 1])

    if line == lines[i]:
        return config.get_max_score()

    return score(line, lines[i - 1])


def get_file_url(repo_url, filename, node, line, original):
    """Get url for a file appearing in a stack trace"""
    if filename and node:
        s = "{}/annotate/{}/{}#l{}"
        return s.format(repo_url, node, filename, line), filename
    elif original:
        start = "s3:gecko-generated-sources:"
        if original.startswith(start):
            s = "https://crash-stats.mozilla.org/sources/highlight/?url="
            s += "https://gecko-generated-sources.s3.amazonaws.com/"
            s += original[len(start):-1]
            s += "#L-" + str(line)
            filename = original[original.index("/") + 1:-1]
            return s, filename
        elif original.startswith("git:github.com/"):
            sp = original.split(":")
            filename = sp[2]
            s = "https://{}/blob/{}/{}#L{}"
            return s.format(sp[1], sp[-1], filename, line), filename
    return "", filename


def is_interesting_file(filename):
    """Is this file's extension in ``config/interesting_extensions.json``?
    (c/h/H, cpp/cc/cxx/hh/hpp/hxx, java, rs, mm/m.)

    WHAT THIS GATES: ``Changeset``/``File`` ROW CREATION, and nothing else. 63.3% of
    non-merge mozilla-central changesets in a 28-day window (3,698 of 5,839,
    2026-07-23..08-19) touch no listed file and so get no rows at all.

    WHAT IT DOES NOT GATE, despite looking like it must: the on-stack candidate set.
    ``Changeset.find`` joins on STACK-FRAME filenames, and a Firefox crash frame's source
    file is always C/C++/Rust/ObjC — 11,717 of 11,717 in-tree frame files over an
    840-report nightly sample, and 1,085 of 1,085 over the 52 auto-filed bugs, ALREADY
    have a listed extension. Replaying every filing's 3-day window with this filter
    REMOVED gives the identical candidate set: 184 -> 184 over the 52 filings,
    1,275 -> 1,275 over 616 control reports, 0 reports gain a candidate, and the top-20 is
    unchanged on all 15 human-``regressed_by`` filings. Admitting a family costs rows and
    buys nothing: pref +2.2%, build +5.9%, idl +5.3%, js +102.8%, kt +69.4% — all with +0
    candidates.

    COUNTER-EXAMPLE, the one case this list really does hide: bug 2057317 (our filing
    2062806, RESOLVED FIXED) landed only ``AdsClient.sys.mjs`` / ``AdsFeed.sys.mjs`` plus
    xpcshell tests. Admitting ``.mjs`` would STILL not have scored it — the crash frames
    are ``GeneratedScaffolding.cpp`` and ``ads-client/src/ffi.rs``, which that changeset
    never touches. The agent found it; the scorer structurally could not.
    And the pref-flip archetype is NOT the counter-example it looks like: bug 2045970
    (``585a77d8786a``, the confirmed regressor of three of our filings) touches
    ``StaticPrefList.yaml`` AND five C++ files and was scored — 45.2% of pref-touching
    window changesets are visible for that reason, and a pref-ONLY flip is 2.0% of them.

    The one excluded family that CAN reach a frame is Kotlin, via
    ``java.inspect_java_stacktrace`` (``(Foo.kt:56)``): 20 of 72 ``org.mozilla.*`` frames
    in a Fenix nightly java-stack sample. Adding ``.kt`` here is inert on its own — the
    blocker is ``java.get_java_files`` (scrapes the ARCHIVED mozilla/gecko-dev, filters
    ``.java`` only, and runs from ``create.py`` rather than the schedule). See plans/16.

    If you came here looking for on-stack RECALL, it is not in this list:
    ``inspector.get_path_node`` drops 17.6% of frame file URIs, and ~9% of those are real
    in-tree paths from Linux distro rebuilds and ``s3:gecko-generated-sources``."""
    return get_extension(filename) in config.get_extensions()


def get_build_date(bid):
    """Get a date (UTC) from a buildid"""
    if isinstance(bid, six.string_types):
        Y = int(bid[0:4])
        m = int(bid[4:6])
        d = int(bid[6:8])
        H = int(bid[8:10])
        M = int(bid[10:12])
        S = int(bid[12:])
    else:
        # 20160407164938 == 2016 04 07 16 49 38
        N = 5
        r = [0] * N
        for i in range(N):
            r[i] = bid % 100
            bid //= 100
        Y = bid
        S, M, H, d, m = r
    d = datetime(Y, m, d, H, M, S)
    dutc = pytz.utc.localize(d)

    return dutc


def get_buildid(date):
    """Get a buildid from a date"""
    if isinstance(date, datetime):
        date = date.astimezone(pytz.utc)
        return date.strftime("%Y%m%d%H%M%S")

    return date


def hash(s):
    """Compute a hash for a string"""
    return hashlib.sha224(s.encode("utf-8")).hexdigest()


def is_spike(n, before, floor, ratio):
    """True if the day count ``n`` is worth flagging given the preceding ``before`` window.

    Union of two conditions -- a strict EXTENSION of the original step rule, so everything
    it caught still fires, plus two cases it never could:

    * appears from a HARD zero -- ``max(before) == 0`` and ``n >= 1``. This IS the old rule
      (``n and all(x == 0 for x in before)``), kept verbatim so no current detection is
      lost: e.g. ``0,0,0 -> N`` for any ``N >= 1``.
    * spikes over a nonzero baseline -- ``n >= floor`` AND ``n >= ratio * max(before)``: it
      clears an absolute floor and stands out against the LOUDEST recent day (``max``, not
      ``mean``, so one busy prior day can't be averaged into a phantom spike). Adds the two
      cases the step rule could never reach: a spike after a stray blip (``0,0,1,0 -> 3``
      with floor=ratio=3) and a sudden worsening of a live signature (``10,20,10 -> 150``).

    ``floor``/``ratio`` (config ``spike``) gate ONLY the second condition -- the from-zero
    sensitivity is unchanged."""
    baseline = max(before) if before else 0
    if baseline == 0:
        return n >= 1
    return n >= floor and n >= ratio * baseline


def get_spike_indices(numbers, ndays, floor, ratio):
    """Yield indices ``i >= ndays`` where ``numbers[i]`` is a spike vs the ``ndays`` before
    it (see ``is_spike``).

    e.g. numbers=[0, 0, 0, 2, 0, 0, 0, 8, 1, 0, 0, 30], ndays=3, floor=3, ratio=3
    -> [3, 7, 11]: 3 and 7 appear from a zero window (n >= 1); 11 is a spike over a stray
    blip ([1, 0, 0] -> needs >= max(3, 3*1) = 3). The old step rule yielded only [3, 7] --
    the lone 1 in index 11's window blocked it; index 8's 1 is below the floor."""
    for i in range(ndays, len(numbers)):
        if is_spike(numbers[i], numbers[(i - ndays):i], floor, ratio):
            yield i


"""Why a build-day was, or was not, selected. Recorded for every near-miss so
``models.Selection`` can answer "we had a spike and you did nothing" — see
``evaluate_days``."""
SELECTED = "selected"
NOT_SPIKING = "not_spiking"
UNTESTABLE_PREFIX = "untestable_prefix"
BELOW_INSTALL_THRESHOLD = "below_install_threshold"
IMMATURE = "immature"
# A build-day removed from the series before it was evaluated, because the build carried
# essentially no users. See ``datacollector.get_no_user_build_floor``: the harm is not that we
# might select it, it is that a zero-crash build-day is a ZERO BASELINE for the day after it,
# and ``is_spike``'s from-zero branch is gated by neither ``floor`` nor ``ratio``.
DROPPED_NO_USERS = "dropped_no_users"
# Selected by the RATE path, not the spike test: an EXISTING signature whose exposure-normalised
# daily rate rose (``sigtrend.rising_candidates``) while no single build-day ever cleared the
# 3x-over-the-previous-three bar. That bar is blind to a rise spread over days by construction:
# ``QuotaManager::Shutdown::<T>::operator()`` tripled on nightly between 2026-07-16 and 07-21,
# its loudest build-day was 2.25x, and the 7-day rate read 3.7-4.7x for a week.
RISING_RATE = "rising_rate"


def pick_latest_build(numbers, threshold):
    """``(day, buildid, count)`` for the newest build-day with a build that carries at least
    ``threshold`` installations, or ``None``. What the rate path analyses: the rise is a
    property of the whole week, so the freshest build with users is the one to read."""
    for day in sorted(numbers, reverse=True):
        entry = numbers[day]
        installs = entry.get("installs", {})
        for bid, n in sorted(entry.get("bids", {}).items(), reverse=True):
            if n and installs.get(bid, 0) >= threshold:
                return day, bid, n
    return None


def _as_date(when):
    """A ``date`` from a date/datetime (tz-aware or not); ``None`` passes through."""
    if when is None:
        return None
    return when.date() if isinstance(when, datetime) else when


def evaluate_days(
    numbers,
    ndays,
    threshold,
    floor,
    ratio,
    today=None,
    mature_after=None,
    mature_installs=1,
):
    """Decide, per build-day, whether it is a spike worth analysing — and say why not.

    Returns ``(picked, big, records)``. ``picked`` is the ``{buildid: count}`` the caller
    analyses; ``records`` is one dict per build-day describing the decision, so a declined
    day leaves a trace instead of vanishing (``datacollector`` used to drop it with
    ``data[sgn] = None``).

    Two rules beyond the plain spike test:

    * **The un-evaluable prefix.** ``get_spike_indices`` starts at ``ndays`` because the
      first days of the window have no full baseline. Those days are NOT tested, and a
      build sliding through that prefix as its crashes arrive is how
      ``mozilla::places::History::History`` was missed: on the 2026-08-07 run its
      build-day held 4 crashes and ``is_spike(4, [0])`` was already True, but it sat at
      index 1. The prefix stays (a partial baseline is not a baseline), but a day that
      would have spiked there is now RECORDED as ``untestable_prefix`` — at ``count >=
      floor``, so a lone crash at index 0 (empty baseline, so the from-zero rule always
      fires) does not flood the log.
    * **Maturity.** With a wider build window, most newly-visible build-days are old ones
      whose crashes have long since arrived. A day older than ``mature_after`` must clear
      ``floor`` outright — the from-zero rule alone is not enough — and its buildid must
      carry ``mature_installs`` distinct installations. That second half is what keeps one
      imaged fleet out: 24 installs created inside 24 minutes still counts as 24 here, but
      a single machine flooding one signature counts as 1. Passing ``today=None`` disables
      maturity entirely and restores the pre-window behaviour."""
    data = sorted((k, v["count"]) for k, v in numbers.items())
    nums = [n for _, n in data]
    today = _as_date(today)
    picked = {}
    big = False
    records = []

    for i, (day, count) in enumerate(data):
        before = nums[max(0, i - ndays):i]
        spiked = is_spike(count, before, floor, ratio)
        bids = numbers[day]["bids"]
        installs = numbers[day]["installs"]
        record = {
            "day": day,
            "count": count,
            "index": i,
            "baseline": before,
            "evaluable": i >= ndays,
            "spiked": spiked,
            "bids": dict(bids),
            "installs": dict(installs),
            "picked": None,
            "outcome": NOT_SPIKING,
        }
        records.append(record)

        if i < ndays:
            # Never tested — but say so when it WOULD have fired, which is the blind spot.
            if spiked and count >= floor:
                record["outcome"] = UNTESTABLE_PREFIX
            continue

        if not spiked:
            continue

        if count >= 500:
            big = True

        age = None if today is None else (today - _as_date(day)).days
        mature = mature_after is not None and age is not None and age > mature_after
        record["age"] = age
        if mature and count < floor:
            record["outcome"] = IMMATURE
            continue

        needed = max(threshold, mature_installs) if mature else threshold
        for bid, n in sorted(bids.items()):
            if n and installs.get(bid, 0) >= needed:
                picked[bid] = n
                record["picked"] = bid
                record["outcome"] = SELECTED
                break
        else:
            record["outcome"] = IMMATURE if mature else BELOW_INSTALL_THRESHOLD

    return picked, big, records


def get_new_crashing_bids(numbers, ndays, threshold, floor, ratio, **kwargs):
    """Get the crashing buildids for the spike days; within a spike day keep the first
    buildid whose install count reaches ``threshold``. Thin wrapper over
    ``evaluate_days`` — see there for the prefix and maturity rules."""
    picked, big, _ = evaluate_days(numbers, ndays, threshold, floor, ratio, **kwargs)
    return picked, big


def get_sgns_by_bids(signatures):
    """Get signatures by buildid from the data"""
    sgn_by_bid = defaultdict(lambda: list())
    for sgn, info in signatures.items():
        for bid in info["bids"].keys():
            sgn_by_bid[bid].append(sgn)
    return sgn_by_bid


def get_params_for_link(**query):
    """Get the params to use to generate Socorro's urls"""
    params = {
        "_facets": [
            "url",
            "user_comments",
            "install_time",
            "version",
            "address",
            "moz_crash_reason",
            "reason",
            "build_id",
            "platform_pretty_version",
            "signature",
            "useragent_locale",
        ]
    }
    params.update(query)
    return params


def make_url_for_signature(sgn, date, buildid, channel, product):
    """Build a Socorro's url for a given signature.

    ``get_search_channel``, like every other query keyed on our channel label. This link sits on
    crashstack.html and reports.html NEXT TO the population panel, which counts beta+aurora --
    so with the raw label the two disagreed on the same page: the panel's number included
    Developer Edition and the link the reader clicks to check it did not, and that is 25.5% of
    the reports overall and up to 72.4% on a single signature (build 20260819090452)."""
    params = get_params_for_link(
        signature="=" + sgn,
        release_channel=get_search_channel(channel),
        product=product,
        build_id=buildid,
        date=">=" + str(date),
    )
    url = socorro.SuperSearch.get_link(params)
    url += "#crash-reports"
    return url


def get_signatures(signatures):
    """Get the signatures available in the Bugzilla crash field"""
    res = set()
    for s in signatures:
        if "[@" in s:
            sgns = map(lambda x: x.strip(), s.split("[@"))
            sgns = filter(None, sgns)
            sgns = map(lambda x: x[:-1].strip(), sgns)
        else:
            sgns = map(lambda x: x.strip(), s.split("\n"))
            sgns = filter(None, sgns)
        res |= set(sgns)

    return res
