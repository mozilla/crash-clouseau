# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import json


__GLOBAL = None
__EXTS = None
__LOCAL = None


def _get_global():
    global __GLOBAL
    if not __GLOBAL:
        with open('./config/global.json', 'r') as In:
            __GLOBAL = json.load(In)
    return __GLOBAL


def _get_exts():
    global __EXTS
    if not __EXTS:
        with open('./config/interesting_extensions.json', 'r') as In:
            data = json.load(In)
            __EXTS = set(x for v in data.values() for x in v)
    return __EXTS


def _get_local():
    global __LOCAL
    if not __LOCAL:
        try:
            with open('./config/local.json', 'r') as In:
                __LOCAL = json.load(In)
        except Exception:
            __LOCAL = {}
    return __LOCAL


def get_channels():
    return _get_global()['channels']


def get_products():
    return _get_global()['products']


def get_limit_facets():
    return _get_global()['facets_limit']


def get_ndays():
    return _get_global()['backward_lookup_ndays']


def get_ndays_of_data():
    return _get_global()['max_ndays']


def get_extensions():
    return _get_exts()


def get_max_score():
    return _get_global()['score']['max']


def get_num_lines():
    return _get_global()['score']['number_of_lines']


def get_database():
    return _get_local().get('database', '')


def get_redis():
    return _get_local().get('redis', '')
