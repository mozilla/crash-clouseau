# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import requests
import time
from . import utils


URL = 'https://buildhub.prod.mozaws.net/v1/buckets/build-hub/collections/releases/search'


def get_prod(p):
    if p == 'fennec':
        return 'FennecAndroid'
    elif p == 'thunderbird':
        return 'Thunderbird'
    return 'Firefox'


def get_info(data):
    res = {}
    aggs = data['aggregations']
    for product in aggs['products']['buckets']:
        prod = get_prod(product['key'])
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


def get(min_date):
    prods = ['firefox', 'fennec', 'thunderbird']
    chans = ['nightly']
    buildid = utils.get_buildid(min_date)
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
                    {'range': {'build.id': {'gte': buildid}}}
                ]
            }
        },
        'size': 0}

    data = json.dumps(data)

    while True:
        r = requests.post(URL, data=data)
        if 'Backoff' in r.headers:
            time.sleep(5)
        else:
            res = get_info(r.json())
            break

    return res


def get_from(buildid, channel, product):
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

    data = json.dumps(data)

    while True:
        r = requests.post(URL, data=data)
        if 'Backoff' in r.headers:
            time.sleep(0.1)
        else:
            try:
                data = r.json()
                node = data['aggregations']['revisions']['buckets'][0]['key'][:12]
            except Exception:
                return ''
            break

    return node
