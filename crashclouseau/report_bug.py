# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import asyncio
import functools
from jinja2 import Environment, FileSystemLoader
import libmozdata.config
from libmozdata.hgmozilla import Mercurial
import requests
from urllib.parse import parse_qs, urlencode, urlparse
from . import buginfo, models, utils


def get_bz_query(data):
    """Get the Bugzilla query inside the Socorro web page"""
    needle = 'href=\"https://bugzilla.mozilla.org/enter_bug.cgi?'
    i = data.index(needle)
    if i != -1:
        j = data.index('\"', i + len(needle))
        if j != -1:
            bz_url = data[i + len('href=\"'):j]
            query = parse_qs(urlparse(bz_url).query)
            return query
    return {}


def improve(query, bzdata, bugid):
    """Improve the Bugzilla query we found with other useful info"""
    if 'bugs' in bzdata and len(bzdata['bugs']) == 1:
        bzdata = bzdata['bugs'][0]
        query['product'] = bzdata['product']
        query['component'] = bzdata['component']
        query['keywords'] = '{},regression'.format(query['keywords'][0])
        query['blocked'] = 'clouseau,{}'.format(bugid)
        return bzdata['assigned_to']
    return ''


def get_stats(data, buildid):
    """Get crash stats from Socorro to put in the bug report"""
    res = {}
    for i in data['facets']['build_id']:
        count = i['count']
        facets = i['facets']
        it = len(facets['install_time'])
        if it == 100:
            it = facets['cardinality_install_time']['value']
        res[i['term']] = {'count': count,
                          'installs': it}

    if len(res) == 1:
        return True, res[buildid]
    else:
        count = 0
        installs = 0
        for v in res.values():
            count += v['count']
            installs += v['installs']
        return False, {'count': count,
                       'installs': installs}


def finalize_comment(bzquery, first, stats, info, changeset, bugid):
    """Finalize the comment to put in the bug report"""
    comment = bzquery['comment'][0]
    env = Environment(loader=FileSystemLoader('templates'))
    template = env.get_template('bug.txt')
    channel = info['channel']
    url = Mercurial.get_repo_url(channel)
    url = '{}/rev?node={}'.format(url, changeset)
    if channel == 'nightly':
        version = 'nightly {}'.format(utils.get_major(info['version']))
    else:
        version = info['version']

    comment = template.render(socorro_comment=comment,
                              count=stats['count'],
                              installs=stats['installs'],
                              version=version,
                              buildid=info['buildid'],
                              bugid=bugid,
                              changeset_url=url,
                              first=first)
    comment = comment.replace('\\n', '\n')
    bzquery['comment'] = comment
    bzurl = 'https://bugzilla.mozilla.org/enter_bug.cgi'
    return bzurl + '?' + urlencode(bzquery, True)


async def get_info_helper(uuid, changeset):
    info = models.UUID.get_info(uuid)
    bugid = models.Node.get_bugid(changeset, info['channel'])
    sgn = info['signature']
    bzw, bugsdata = buginfo.get_bugs(sgn, wait=False)

    cs = 'https://crash-stats.mozilla.com/report/index/' + uuid
    bz = 'https://bugzilla.mozilla.org/rest/bug'
    bzh = {'X-Bugzilla-API-Key': libmozdata.config.get('Bugzilla', 'token', '')}
    bzq = {'id': bugid,
           'include_fields': ['product',
                              'component',
                              'assigned_to']}
    cs_api = 'https://crash-stats.mozilla.com/api/SuperSearch/'
    cs_api_q = {'signature': '=' + info['signature'],
                'build_id': '>=' + info['buildid'],
                'product': info['product'],
                'release_channel': info['channel'],
                '_aggs.build_id': ['install_time',
                                   '_cardinality.install_time'],
                '_results_number': 0,
                '_facets': 'release_channel',
                '_facets_size': 100}

    loop = asyncio.get_event_loop()
    f1 = loop.run_in_executor(None, functools.partial(requests.get, cs))
    if bugid:
        f2 = loop.run_in_executor(None, functools.partial(requests.get, bz, headers=bzh, params=bzq))
    f3 = loop.run_in_executor(None, functools.partial(requests.get, cs_api, params=cs_api_q))
    r1 = await f1
    if bugid:
        r2 = await f2
    r3 = await f3
    bzquery = get_bz_query(r1.text)
    first, stats = get_stats(r3.json(), int(info['buildid']))
    bzdata = r2.json() if bugid else {}
    ni = improve(bzquery, bzdata, bugid)
    url = finalize_comment(bzquery, first, stats, info, changeset, bugid)

    bzw.wait()

    return url, ni, sgn, bugsdata


def get_info(uuid, changeset):
    """Get the info (comment and Bugzilla stuff) to put in the bug report"""
    return asyncio.get_event_loop().run_until_complete(get_info_helper(uuid, changeset))
