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
from . import config


def get_products():
    return config.get_products()


def get_channels():
    return config.get_channels()


def get_extension(filename):
    i = filename.rfind('.')
    if i != -1:
        return filename[i + 1:]
    return ''


def get_major(v):
    return int(v.split('.')[0])


def get_colors():
    N = config.get_max_score()
    h = (236 - 48) / N
    r = [int(48 + n * h) for n in range(0, N + 1)]
    colors = [''] * (N + 1)
    for n in range(0, N + 1):
        colors[n] = '#' + hex(r[-n - 1])[2:] + hex(r[n])[2:] + '30'
    return colors


def short_rev(rev):
    if len(rev) > 12:
        return rev[:12]
    return rev


def get_correct_score(score):
    try:
        score = int(score)
        if score < 0:
            score = -1
        elif score > config.get_max_score():
            score = config.get_max_score()
    except Exception:
        score = -1
    return score


def score(x, a):
    # a <= x - 5 ==> 0.9
    # x - 5(n + 1) < a <= x - 5n ==> 0.9 - 0.1n
    # x - a - 5 < 5n <= x - a ==> n = floor((x - a) / 5)
    n = (x - a) // config.get_num_lines()
    N = config.get_max_score() - 1
    return 0 if n >= N else N - n


def get_line_score(line, lines):
    if not lines:
        return 0
    i = bisect_left(lines, line)
    if i == 0:
        if line == lines[0]:
            return config.get_max_score()
        return 0
    if i == len(lines):
        return score(line, lines[i - 1])

    if line == lines[i]:
        return config.get_max_score()

    return score(line, lines[i - 1])


def get_file_url(repo_url, filename, node, line, original):
    if filename and node:
        s = '{}/annotate/{}/{}#l{}'
        return s.format(repo_url, node, filename, line), filename
    elif original:
        start = 's3:gecko-generated-sources:'
        if original.startswith(start):
            s = 'https://crash-stats.mozilla.com/sources/highlight/?url='
            s += 'https://gecko-generated-sources.s3.amazonaws.com/'
            s += original[len(start):-1]
            s += '#L-' + str(line)
            filename = original[original.index('/') + 1:-1]
            return s, filename
        elif original.startswith('git:github.com/'):
            sp = original.split(':')
            filename = sp[2]
            s = 'https://{}/blob/{}/{}#L{}'
            return s.format(sp[1], sp[-1], filename, line), filename
    return '', filename


def is_interesting_file(filename):
    return get_extension(filename) in config.get_extensions()


def get_build_date(bid):
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


def is_buildids_same_day(a, b):
    return a // 1000000 == b // 1000000


def get_buildid(date):
    if isinstance(date, datetime):
        date = date.astimezone(pytz.utc)
        return date.strftime('%Y%m%d%H%M%S')

    return date


def hash(s):
    return hashlib.sha224(s.encode('utf-8')).hexdigest()


def compare_numbers(n, before):
    return n and all(x == 0 for x in before)


def get_spike_indices(numbers, ndays):
    # we've something like [0, 0, 0, 2, 0, 0, 0, 3, 1, 0, 0, 9] and ndays=3
    # and we want to get [3, 7]
    for i in range(ndays, len(numbers)):
        if compare_numbers(numbers[i], numbers[(i - ndays):i]):
            yield i


def get_new_crashing_bids(numbers, ndays):
    data = [(k, v['count']) for k, v in numbers.items()]
    data = sorted(data)
    nums = [n for _, n in data]
    res = {}
    for i in get_spike_indices(nums, ndays):
        bids = numbers[data[i][0]]['bids']
        for bid, n in sorted(bids.items()):
            if n:
                res[bid] = n
                break
    return res


def get_sgns_by_bids(signatures):
    sgn_by_bid = defaultdict(lambda: list())
    for sgn, info in signatures.items():
        for bid in info['bids'].keys():
            sgn_by_bid[bid].append(sgn)
    return sgn_by_bid


def get_params_for_link(**query):
    params = {'_facets': ['url',
                          'user_comments',
                          'install_time',
                          'version',
                          'address',
                          'moz_crash_reason',
                          'reason',
                          'build_id',
                          'platform_pretty_version',
                          'signature',
                          'useragent_locale']}
    params.update(query)
    return params


def make_url_for_signature(sgn, date, buildid, channel, product):
    params = get_params_for_link(signature='=' + sgn,
                                 release_channel=channel,
                                 product=product,
                                 build_id=buildid,
                                 date='>=' + str(date))
    url = socorro.SuperSearch.get_link(params)
    url += '#crash-reports'
    return url
