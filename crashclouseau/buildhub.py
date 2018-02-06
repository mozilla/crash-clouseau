# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import requests
import time
from . import utils
from .logger import logger


URL = 'https://buildhub.prod.mozaws.net/v1/buckets/build-hub/collections/releases/search'
PRODS = {'Firefox': 'firefox',
         'FennecAndroid': 'fennec',
         'Thunderbird': 'thunderbird'}
RPRODS = {v: k for k, v in PRODS.items()}


def make_request(params, sleep, retry, callback):
    params = json.dumps(params)

    for _ in range(retry):
        r = requests.post(URL, data=params)
        if 'Backoff' in r.headers:
            time.sleep(sleep)
        else:
            try:
                return callback(r.json())
            except Exception as e:
                logger.error(e, exc_info=True)
                return None

    logger.error('Too many attempts in buildhub.make_request (retry={})'.format(retry))

    return None


def get(min_buildid, max_buildid=None, chans=['nightly'], prods=['firefox', 'fennec', 'thunderbird']):
    prods = [PRODS.get(x, x) for x in prods]
    r = {}
    if min_buildid:
        r['gte'] = utils.get_buildid(min_buildid)
    if max_buildid:
        r['lte'] = utils.get_buildid(max_buildid)

    data = {
        'aggs': {
            'products': {
                'terms': {
                    'field': 'source.product',
                    'size': len(prods)
                },
                'aggs': {
                    'channels': {
                        'terms': {
                            'field': 'target.channel',
                            'size': len(chans)
                        },
                        'aggs': {
                            'buildids': {
                                'terms': {
                                    'field': 'build.id',
                                    'size': 1000
                                },
                                'aggs': {
                                    'revisions': {
                                        'terms': {
                                            'field': 'source.revision',
                                            'size': 1
                                        }
                                    },
                                    'versions': {
                                        'terms': {
                                            'field': 'target.version',
                                            'size': 1
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
        'query': {
            'bool': {
                'filter': [
                    {'terms': {'target.channel': chans}},
                    {'terms': {'source.product': prods}},
                    {'range': {'build.id': r}}
                ]
            }
        },
        'size': 0}

    def get_info(data):
        res = {}
        aggs = data['aggregations']
        for product in aggs['products']['buckets']:
            prod = product['key']
            prod = RPRODS.get(prod, prod)
            if prod in res:
                res_p = res[prod]
            else:
                res[prod] = res_p = {}
            for channel in product['channels']['buckets']:
                chan = channel['key']
                if chan in res_p:
                    res_pc = res_p[chan]
                else:
                    res_p[chan] = res_pc = {}

                for buildid in channel['buildids']['buckets']:
                    bid = utils.get_build_date(buildid['key'])
                    rev = buildid['revisions']['buckets'][0]['key']
                    version = buildid['versions']['buckets'][0]['key']
                    res_pc[bid] = {'revision': utils.short_rev(rev),
                                   'version': utils.get_major(version)}
        return res

    return make_request(data, 1, 100, get_info)


def get_rev_from(buildid, channel, product):
    buildid = utils.get_buildid(buildid)
    product = PRODS.get(product, product)
    data = {
        'aggs': {
            'revisions': {
                'terms': {
                    'field': 'source.revision',
                    'size': 1
                }
            }
        },
        'query': {
            'bool': {
                'filter': [
                    {'term': {'target.channel': channel}},
                    {'term': {'source.product': product}},
                    {'term': {'build.id': buildid}}
                ]
            }
        },
        'size': 0}

    def cb(data):
        return utils.short_rev(data['aggregations']['revisions']['buckets'][0]['key'])

    return make_request(data, 0.1, 100, cb)


def get_two_last(buildid, channel, product):
    buildid = utils.get_buildid(buildid)
    product = PRODS.get(product, product)
    data = {
        'aggs': {
            'buildids': {
                'terms': {
                    'field': 'build.id',
                    'size': 2,
                    'order': {
                        '_term': 'desc'
                    }
                },
                'aggs': {
                    'revisions': {
                        'terms': {
                            'field': 'source.revision',
                            'size': 1
                        }
                    },
                    'versions': {
                        'terms': {
                            'field': 'target.version',
                            'size': 1
                        }
                    }
                }
            }
        },
        'query': {
            'bool': {
                'filter': [
                    {'term': {'target.channel': channel}},
                    {'term': {'source.product': product}},
                    {'range': {'build.id': {'lte': buildid}}}
                ]
            }
        },
        'size': 0}

    def get_info(data):
        bids = []
        for i in data['aggregations']['buildids']['buckets']:
            buildid = i['key']
            revision = utils.short_rev(i['revisions']['buckets'][0]['key'])
            version = utils.get_major(i['versions']['buckets'][0]['key'])
            bids.append({'buildid': buildid,
                         'revision': revision,
                         'version': version})

        x = bids[0]
        bids[0] = bids[1]
        bids[1] = x

        return bids

    return make_request(data, 0.1, 100, get_info)
