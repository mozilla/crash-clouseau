# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import sys


def get_files(f, files):
    """Get a number for a given file f and keep the assocation in files"""
    res = []
    for f in filter(None, f.split(' ')):
        if f in files:
            res.append(files[f])
        else:
            files[f] = n = len(files)
            res.append(n)
    return res


def get_log(hgpath, out_path='', last_rev=0, rev='tip', merge=True, files=False):
    """Get pushlog from a local mercurial repo"""
    import hglib

    client = hglib.open(hgpath)
    client.pull(update=True)

    revrange = '{}:{}'.format(rev, last_rev)
    entries = ['node', 'author', 'pushdate|isodate',
               'date|isodate', 'desc', 'p1node',
               'p2node', 'pushid']
    if files:
        entries += ['file_adds', 'file_mods', 'file_dels']

    entries = map(lambda x: '{' + x + '}', entries)
    if sys.version_info >= (3, 0):
        entries = map(lambda x: bytes(x, 'utf-8'), entries)
    else:
        entries = map(lambda x: bytes(x), entries)

    entries = list(entries)
    template = b'\\0'.join(entries) + b'\\0'
    N = len(entries)
    args = hglib.util.cmdbuilder(b'log',
                                 r=revrange,
                                 template=template,
                                 cwd=hgpath,
                                 M=merge)
    out = client.rawcommand(args)
    client.close()
    out = out.decode('utf-8')
    out = out.split('\0')
    nullid = '0' * 40
    res = []

    if files:
        files = {}
        for i in range(0, len(out) - 1, N):
            node, author, pushdate, date, desc, p1node, p2node, pushid, adds, mods, dels = out[i:(i + N)]
            parents = [p1node, p2node]
            parents = list(filter(lambda p: p != nullid, parents))
            res.append({'rev': node,
                        'author': author,
                        'pushdate': pushdate,
                        'date': date,
                        'desc': desc,
                        'parents': parents,
                        'pushid': pushid,
                        'added': get_files(adds, files),
                        'modified': get_files(mods, files),
                        'deleted': get_files(dels, files)})
        res = {'files': files,
               'log': res}
    else:
        for i in range(0, len(out) - 1, N):
            node, author, pushdate, date, desc, p1node, p2node, pushid = out[i:(i + N)]
            parents = [p1node, p2node]
            parents = list(filter(lambda p: p != nullid, parents))
            res.append({'rev': node,
                        'author': author,
                        'pushdate': pushdate,
                        'date': date,
                        'desc': desc,
                        'parents': parents,
                        'pushid': pushid})

    if out_path:
        with open(out_path, 'w') as Out:
            json.dump(res, Out)

    return res
