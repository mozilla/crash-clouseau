# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from libmozdata import socorro
import re
import requests
from . import java, tools, utils
from .logger import logger


# Mercurial URI
HG_PAT = re.compile("hg:hg.mozilla.org[^:]*:([^:]*):([a-z0-9]+)")
# Git URI: Firefox source moved from hg.mozilla.org to
# github.com/mozilla-firefox/firefox, so crash frames now look like
# git:github.com/mozilla-firefox/firefox:<path>:<git-hash>
GIT_PAT = re.compile("git:github.com/[^:]*:([^:]*):([0-9a-f]+)")
# Lando exposes a git<->hg mapping; we convert the git hashes found in crash
# frames back to mercurial revs so the rest of the (hg-based) pipeline keeps
# working unchanged.
LANDO_GIT2HG = "https://lando.moz.tools/api/git2hg/firefox/{}"
# Cache git->hg lookups for the lifetime of the worker. We cache misses too:
# many frames point at vendored, non-Firefox sources (the Rust std lib lives in
# rust-lang/rust, etc.) whose hashes have no Firefox hg counterpart, and a crash
# repeats the same hash across all its frames -- without caching we'd hammer
# lando (and spam the log) once per frame. Transient errors are NOT cached so
# they can be retried later.
_GIT2HG_CACHE = {}


def git2hg(git_hash):
    """Convert a git hash to its mercurial counterpart using lando.

    Returns "" when the hash has no Firefox hg counterpart (e.g. frames from
    vendored sources such as the Rust standard library)."""
    if git_hash in _GIT2HG_CACHE:
        return _GIT2HG_CACHE[git_hash]
    try:
        r = requests.get(LANDO_GIT2HG.format(git_hash), timeout=30)
    except Exception as e:
        # network/transient error: don't cache, let it be retried
        logger.warning("Cannot reach lando for git hash {}: {}".format(git_hash, e))
        return ""
    if r.status_code == 404:
        # not a Firefox commit (vendored/3rd-party source): cache the miss
        _GIT2HG_CACHE[git_hash] = ""
        return ""
    try:
        r.raise_for_status()
        hg_hash = r.json().get("hg_hash", "")
    except Exception as e:
        logger.warning("Cannot convert git hash {} to hg: {}".format(git_hash, e))
        return ""
    _GIT2HG_CACHE[git_hash] = hg_hash
    return hg_hash


def get_crash_data(uuid):
    """Get the crash data from Socorro"""
    data = socorro.ProcessedCrash.get_processed(uuid)
    return data[uuid]


def get_crash(uuid, buildid, channel, mindate, chgset, filelog, interesting_chgsets):
    """Get the a crash with its uuid"""
    logger.info("Get {} for analyzis".format(uuid))
    data = get_crash_data(uuid)
    return get_crash_info(
        data, uuid, buildid, channel, mindate, chgset, filelog, interesting_chgsets
    )


def get_crash_by_uuid(uuid, mindate, filelog):
    """Get the a crash with its uuid"""
    logger.info("Get {} for analyzis".format(uuid))
    data = get_crash_data(uuid)
    buildid = data["build"]
    bid = utils.get_build_date(buildid)
    channel = data["release_channel"]
    interesting_chgsets = set()
    chgset = tools.get_changeset(bid, channel, data["product"])
    res = get_crash_info(
        data, uuid, bid, channel, mindate, chgset, filelog, interesting_chgsets
    )
    return res, channel, interesting_chgsets


def get_crash_info(
    data, uuid, buildid, channel, mindate, chgset, filelog, interesting_chgsets
):
    """Inspect the crash stack (Java's one too if present)"""
    res = {}
    java_st = data.get("java_stack_trace")
    jframes, files = java.inspect_java_stacktrace(java_st, chgset)

    if jframes:
        files = filelog(files, mindate, buildid, channel)
        if amend(jframes, files, interesting_chgsets):
            res["java"] = {"frames": jframes, "hash": get_simplified_hash(jframes)}
    else:
        if "json_dump" not in data:
            return None
        frames, files = inspect_stacktrace(data, chgset)
        if frames:
            files = filelog(files, mindate, buildid, channel)
            if amend(frames, files, interesting_chgsets):
                res["nonjava"] = {"frames": frames, "hash": get_simplified_hash(frames)}

    return res


def get_simplified_hash(frames):
    """Get a hash from the frames we have in the crash stack"""
    res = ""
    for frame in frames:
        if frame["line"] != -1:
            res += str(frame["stackpos"]) + "\n"
            res += frame["filename"] + "\n"
            res += str(frame["line"]) + "\n"
    if res != "":
        return utils.hash(res)
    return ""


def get_path_node(uri):
    """Get the file path and the hg node"""
    name = node = ""
    if uri:
        m = HG_PAT.match(uri)
        if m:
            name = m.group(1)
            node = utils.short_rev(m.group(2))
        else:
            m = GIT_PAT.match(uri)
            if m:
                name = m.group(1)
                # convert the git hash back to a mercurial rev so it can be
                # compared with the (hg) build node and changesets
                node = utils.short_rev(git2hg(m.group(2)))
    return name, node


def inspect_stacktrace(data, build_node):
    """Inspect the stack from the data and the check that the hg node
    from the build is the same that the one we have in stack data
    (the nodes could be different when the crash was occuring during an update)"""
    res = []
    files = set()
    dump = data["json_dump"]
    max_frames = 50
    if "threads" in dump:
        N = dump["crash_info"].get("crashing_thread")
        if N is not None:
            frames = dump["threads"][N]["frames"]
            frames = frames[0:max_frames]
            for n, frame in enumerate(frames):
                uri = frame.get("file")
                filename, node = get_path_node(uri)
                if node:
                    if node != build_node:
                        return [], set()
                    files.add(filename)
                fun = frame.get("function", "")
                line = frame.get("line", -1)
                module = frame.get("module", "")
                res.append(
                    {
                        "original": uri,
                        "filename": filename,
                        "changesets": [],
                        "module": module,
                        "function": fun,
                        "line": line,
                        "node": node,
                        "internal": node != "",
                        "stackpos": n,
                    }
                )
    return res, files


def amend(frames, files, interesting_chgsets):
    """Amend frame info"""
    interesting = False
    if files:
        for frame in frames:
            filename = frame["filename"]
            if filename in files:
                chgsets = files[filename]
                interesting_chgsets |= set(chgsets)
                frame["changesets"] = chgsets
                interesting = True
    return interesting
