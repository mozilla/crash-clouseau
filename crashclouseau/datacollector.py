# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from collections import defaultdict
import copy
from datetime import datetime
from dateutil.relativedelta import relativedelta
import functools
from libmozdata import socorro, utils as lmdutils
from libmozdata.connection import Connection, Query
import re
from . import config, utils
from .logger import logger


def get_buildids(search_buildid, search_date, products, channel='nightly'):

    def handler(json, data):
        if json['errors'] or not json['facets']['build_id']:
            return
        for facets in json['facets']['build_id']:
            bid = facets['term']
            for prod in facets['facets']['product']:
                prod = prod['term']
                data[prod].append(bid)

    params = {'product': products,
              'release_channel': channel,
              'build_id': search_buildid,
              'date': search_date,
              '_aggs.build_id': 'product',
              '_facets': 'release_channel',
              '_results_number': 0,
              '_facets_size': 100}

    data = {p: list() for p in products}
    socorro.SuperSearch(params=params,
                        handler=handler,
                        handlerdata=data).wait()

    data = {p: sorted(b) for p, b in data.items()}

    return data


def get_new_signatures(products, date='today', channel='nightly'):
    limit = config.get_limit_facets()
    ndays = config.get_ndays()
    today = lmdutils.get_date_ymd(date)
    few_days_ago = today - relativedelta(days=ndays + 5)
    few_days_ago = datetime(few_days_ago.year,
                            few_days_ago.month,
                            few_days_ago.day)
    search_buildid = ['>=' + utils.get_buildid(few_days_ago),
                      '<=' + utils.get_buildid(today)]
    search_date = '>=' + lmdutils.get_date_str(few_days_ago)

    bids = get_buildids(search_buildid, search_date, products, channel)
    base = {}
    for p, v in bids.items():
        base[p] = base_p = {}
        for bid in v:
            bid = utils.get_build_date(bid)
            day = datetime(bid.year, bid.month, bid.day)
            if day not in base_p:
                base_p[day] = {'bids': {},
                               'count': 0}
            base_p[day]['bids'][bid] = 0

    logger.info('Get crash numbers for {}-{}: started.'.format(products,
                                                               channel))

    def handler(base, json, data):
        if json['errors'] or not json['facets']['signature']:
            raise Exception('Error in json data from SuperSearch')
        for facets in json['facets']['signature']:
            bids = facets['facets']['build_id']
            numbers = copy.deepcopy(base)
            for bid in bids:
                count = bid['count']
                bid = bid['term']
                bid = utils.get_build_date(bid)
                day = datetime(bid.year, bid.month, bid.day)
                numbers[day]['count'] += count
                numbers[day]['bids'][bid] = count
            bids = utils.get_new_crashing_bids(numbers, ndays)
            if bids:
                sgn = facets['term']
                data[sgn] = {'bids': bids,
                             'protos': {b: [] for b in bids},
                             'installs': {b: 0 for b in bids}}

    base_params = {'product': '',
                   'release_channel': channel,
                   'date': search_date,
                   'build_id': '',
                   '_aggs.signature': 'build_id',
                   '_results_number': 0,
                   '_facets': 'release_channel',
                   '_facets_size': limit}

    data = {}
    queries = []
    for prod in products:
        data[prod] = data_prod = {}
        for bid in bids[prod]:
            params = copy.deepcopy(base_params)
            params['product'] = prod
            params['build_id'] = bid
            hdler = functools.partial(handler, base[prod])
            queries.append(Query(socorro.SuperSearch.URL,
                                 params=params,
                                 handler=hdler,
                                 handlerdata=data_prod))

    socorro.SuperSearch(queries=queries).wait()

    logger.info('Get crash numbers for {}-{}: finished.'.format(products,
                                                                channel))
    get_proto(products, data, search_date, channel=channel)

    if 'FennecAndroid' in products:
        # Java crashes don't have any proto-signature...
        get_uuids(data['FennecAndroid'], search_date, channel=channel)

    return data


def get_proto(products, signatures, search_date, channel='nightly'):
    limit = config.get_limit_facets()
    logger.info('Get proto-signatures for {}-{}: started.'.format(products,
                                                                  channel))

    def handler(json, data):
        if not json['facets']['proto_signature']:
            return
        bid = json['facets']['build_id'][0]['term']
        bid = utils.get_build_date(bid)
        for facets in json['facets']['proto_signature']:
            proto = facets['term']
            count = facets['count']
            facets = facets['facets']
            sgn = facets['signature'][0]['term']
            uuid = facets['uuid'][0]['term']
            data[sgn]['protos'][bid].append({'proto': proto,
                                             'count': count,
                                             'uuid': uuid})
        for facets in json['facets']['signature']:
            sgn = facets['term']
            count = facets['facets']['cardinality_install_time']['value']
            data[sgn]['installs'][bid] += count

    base_params = {'product': '',
                   'release_channel': channel,
                   'date': search_date,
                   'build_id': '',
                   'signature': '',
                   '_aggs.proto_signature': ['uuid', 'signature'],
                   '_aggs.signature': '_cardinality.install_time',
                   '_results_number': 0,
                   '_facets': 'build_id',
                   '_facets_size': limit}

    queries = []
    for prod in products:
        pparams = copy.deepcopy(base_params)
        pparams['product'] = prod
        sgns_prod = signatures[prod]
        sgns_by_bids = utils.get_sgns_by_bids(sgns_prod)

        for bid, all_signatures in sgns_by_bids.items():
            params = copy.deepcopy(pparams)
            params['build_id'] = utils.get_buildid(bid)

            for sgns in Connection.chunks(all_signatures, 10):
                params = copy.deepcopy(params)
                params['signature'] = ['=' + s for s in sgns]
                queries.append(Query(socorro.SuperSearch.URL,
                                     params=params,
                                     handler=handler,
                                     handlerdata=sgns_prod))

    socorro.SuperSearch(queries=queries).wait()

    logger.info('Get proto-signatures for {}-{}: finished.'.format(products,
                                                                   channel))


def get_uuids(signatures, search_date, channel='nightly'):
    limit = config.get_limit_facets()
    logger.info('Get uuids for FennecAndroid-{}: started.'.format(channel))

    def handler(json, data):
        if json['errors'] or not json['facets']['signature']:
            return
        bid = json['facets']['build_id'][0]['term']
        bid = utils.get_build_date(bid)
        for facets in json['facets']['signature']:
            sgn = facets['term']
            count = facets['count']
            facets = facets['facets']
            uuid = facets['uuid'][0]['term']
            protos = data[sgn]['protos'][bid]
            if not protos:
                protos.append({'proto': '',
                               'count': count,
                               'uuid': uuid})

    base_params = {'product': 'FennecAndroid',
                   'release_channel': channel,
                   'date': search_date,
                   'build_id': '',
                   'signature': '',
                   '_aggs.signature': 'uuid',
                   '_results_number': 0,
                   '_facets': 'build_id',
                   '_facets_size': limit}

    queries = []
    sgns_by_bids = utils.get_sgns_by_bids(signatures)

    for bid, all_signatures in sgns_by_bids.items():
        params = copy.deepcopy(base_params)
        params['build_id'] = utils.get_buildid(bid)

        for sgns in Connection.chunks(all_signatures, 10):
            params = copy.deepcopy(params)
            params['signature'] = ['=' + s for s in sgns]
            queries.append(Query(socorro.SuperSearch.URL,
                                 params=params,
                                 handler=handler,
                                 handlerdata=signatures))
    socorro.SuperSearch(queries=queries).wait()

    logger.info('Get uuids for FennecAndroid-{}: finished.'.format(channel))


def get_changeset(buildid, channel, product):
    search_date = '>=' + lmdutils.get_date_str(buildid)
    buildid = utils.get_buildid(buildid)
    logger.info('Get changeset for {}-{}-{}.'.format(buildid,
                                                     product,
                                                     channel))

    def handler(json, data):
        pat = re.compile(r'^.*:([0-9a-f]+)$')
        if not json['facets']['build_id']:
            return
        for facets in json['facets']['build_id']:
            for tf in facets['facets']['topmost_filenames']:
                m = pat.match(tf['term'])
                if m:
                    chgset = m.group(1)
                    count = tf['count']
                    data[chgset] += count

    params = {'product': product,
              'release_channel': channel,
              'build_id': buildid,
              'date': search_date,
              'topmost_filenames': '@\"hg:hg.mozilla.org/\".*:[0-9a-f]+',
              '_aggs.build_id': 'topmost_filenames',
              '_results_number': 0,
              '_facets': 'product',
              '_facets_size': 100}

    data = defaultdict(lambda: 0)
    socorro.SuperSearch(params=params,
                        handler=handler,
                        handlerdata=data).wait()
    chgset = None
    if data:
        chgset, _ = max(data.items(), key=lambda p: p[1])
        chgset = utils.short_rev(chgset)

    logger.info('Get changeset: finished.')

    return chgset
