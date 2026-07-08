# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from bisect import bisect_left
from collections import defaultdict
from datetime import datetime
import hashlib
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
    """Check if the file extension is one of the extensions we have in the configuration file (global.json)"""
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


def get_new_crashing_bids(numbers, ndays, threshold, floor, ratio):
    """Get the crashing buildids for the spike days (see ``get_spike_indices``); within a
    spike day keep the first buildid whose install count reaches ``threshold``."""
    data = [(k, v["count"]) for k, v in numbers.items()]
    data = sorted(data)
    nums = [n for _, n in data]
    res = {}
    big = False
    for i in get_spike_indices(nums, ndays, floor, ratio):
        day, count = data[i]
        if count >= 500:
            big = True
        bids = numbers[day]["bids"]
        for bid, n in sorted(bids.items()):
            if n and numbers[day]["installs"][bid] >= threshold:
                res[bid] = n
                break
    return res, big


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
    """Build a Socorro's url for a given signature"""
    params = get_params_for_link(
        signature="=" + sgn,
        release_channel=channel,
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
