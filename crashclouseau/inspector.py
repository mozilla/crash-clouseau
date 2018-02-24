# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from libmozdata import socorro
import re
from . import java, tools, utils
from .logger import logger


# Mercurial URI
HG_PAT = re.compile('hg:hg.mozilla.org[^:]*:([^:]*):([a-z0-9]+)')


def get_crash_data(uuid):
    """Get the crash data from Socorro"""
    data = socorro.ProcessedCrash.get_processed(uuid)
    return data[uuid]


def get_crash(uuid, buildid, channel, mindate,
              chgset, filelog, interesting_chgsets):
    """Get the a crash with its uuid"""
    logger.info('Get {} for analyzis'.format(uuid))
    data = get_crash_data(uuid)
    return get_crash_info(data, uuid, buildid, channel, mindate,
                          chgset, filelog, interesting_chgsets)


def get_crash_by_uuid(uuid, mindate, filelog):
    """Get the a crash with its uuid"""
    logger.info('Get {} for analyzis'.format(uuid))
    data = get_crash_data(uuid)
    buildid = data['build']
    bid = utils.get_build_date(buildid)
    channel = data['release_channel']
    interesting_chgsets = set()
    chgset = tools.get_changeset(bid, channel, data['product'])
    res = get_crash_info(data, uuid, bid, channel, mindate,
                         chgset, filelog, interesting_chgsets)
    return res, channel, interesting_chgsets


def get_crash_info(data, uuid, buildid, channel, mindate,
                   chgset, filelog, interesting_chgsets):
    """Inspect the crash stack (Java's one too if present)"""
    res = {}
    java_st = data.get('java_stack_trace')
    jframes, files = java.inspect_java_stacktrace(java_st, chgset)

    if jframes:
        files = filelog(files, mindate, buildid, channel)
        if amend(jframes, files, interesting_chgsets):
            res['java'] = {'frames': jframes,
                           'hash': get_simplified_hash(jframes)}
    else:
        frames, files = inspect_stacktrace(data, chgset)
        if frames:
            files = filelog(files, mindate, buildid, channel)
            if amend(frames, files, interesting_chgsets):
                res['nonjava'] = {'frames': frames,
                                  'hash': get_simplified_hash(frames)}
            else:
                logger.info('UUID {} - hash {} - Files 1 {} - Files 2 {}'.format(uuid,
                                                                                 get_simplified_hash(frames),
                                                                                 prev,
                                                                                 files))

    return res


def get_simplified_hash(frames):
    """Get a hash from the frames we have in the crash stack"""
    res = ''
    for frame in frames:
        if frame['line'] != -1:
            res += str(frame['stackpos']) + '\n' + frame['filename'] + '\n' + str(frame['line']) + '\n'
    if res != '':
        return utils.hash(res)
    return ''


def get_path_node(uri):
    """Get the file path and the hg node"""
    name = node = ''
    if uri:
        m = HG_PAT.match(uri)
        if m:
            name = m.group(1)
            node = utils.short_rev(m.group(2))
    return name, node


def inspect_stacktrace(data, build_node):
    """Inspect the stack from the data and the check that the hg node
       from the build is the same that the one we have in stack data
       (the nodes could be different when the crash was occuring during an update)"""
    res = []
    files = set()
    dump = data['json_dump']
    if 'threads' in dump:
        N = data.get('crashedThread')
        if N is not None:
            frames = dump['threads'][N]['frames']
            for n, frame in enumerate(frames):
                uri = frame.get('file')
                filename, node = get_path_node(uri)
                if node:
                    if node != build_node:
                        return [], set()
                    files.add(filename)
                fun = frame.get('function', '')
                line = frame.get('line', -1)
                module = frame.get('module', '')
                res.append({'original': uri,
                            'filename': filename,
                            'changesets': [],
                            'module': module,
                            'function': fun,
                            'line': line,
                            'node': node,
                            'internal': node != '',
                            'stackpos': n})
    return res, files


def amend(frames, files, interesting_chgsets):
    """Amend frame info"""
    interesting = False
    if files:
        for frame in frames:
            filename = frame['filename']
            if filename in files:
                chgsets = files[filename]
                interesting_chgsets |= set(chgsets)
                frame['changesets'] = chgsets
                interesting = True
    return interesting
