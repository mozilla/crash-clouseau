# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from libmozdata import socorro
from libmozdata.lando import LandoCommitMapAPI, LandoMissingCommit
import re
from . import config, java, tools, utils
from .logger import logger


# Mercurial URI
HG_PAT = re.compile("hg:hg.mozilla.org[^:]*:([^:]*):([a-z0-9]+)")
# Git URI: Firefox source moved from hg.mozilla.org to
# github.com/mozilla-firefox/firefox, so crash frames now look like
# git:github.com/mozilla-firefox/firefox:<path>:<git-hash>
GIT_PAT = re.compile("git:github.com/[^:]*:([^:]*):([0-9a-f]+)")
# Lando exposes a git<->hg mapping; we convert the git hashes found in crash
# frames back to mercurial revs so the rest of the (hg-based) pipeline keeps
# working unchanged. Use libmozdata's maintained Lando client — it sets our
# User-Agent (from config) and raises LandoMissingCommit on a 404. Built lazily
# (its constructor requires the [User-Agent] config) so importing this module
# never depends on that config being present.
_LANDO = None
# Cache git->hg lookups for the lifetime of the worker. We cache misses too:
# many frames point at vendored, non-Firefox sources (the Rust std lib lives in
# rust-lang/rust, etc.) whose hashes have no Firefox hg counterpart, and a crash
# repeats the same hash across all its frames -- without caching we'd hammer
# lando (and spam the log) once per frame. Transient errors are NOT cached so
# they can be retried later.
_GIT2HG_CACHE = {}


def git2hg(git_hash):
    """Convert a git hash to its mercurial counterpart via lando (libmozdata).

    Returns "" when the hash has no Firefox hg counterpart (e.g. frames from
    vendored sources such as the Rust standard library)."""
    global _LANDO
    if git_hash in _GIT2HG_CACHE:
        return _GIT2HG_CACHE[git_hash]
    if _LANDO is None:
        _LANDO = LandoCommitMapAPI()
    try:
        hg_hash = _LANDO.git2hg(git_hash).hg_hash
    except LandoMissingCommit:
        # not a Firefox commit (vendored/3rd-party source): cache the miss
        _GIT2HG_CACHE[git_hash] = ""
        return ""
    except Exception as e:
        # network / transient / API error: don't cache, let it be retried
        logger.warning("Cannot convert git hash {} to hg via lando: {}".format(git_hash, e))
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
            interesting = amend(frames, files, interesting_chgsets)
            # Store the stack when a candidate scored onto a frame (on-stack) OR — with the
            # P1 off-stack path enabled — even when NONE did (off-stack): the frames MUST be
            # persisted so build_seed can seed the first-bad-build pushlog window and the
            # agent runs. Without this an off-stack crash is dropped right here (no
            # crashstack, useless=True, never enqueued), leaving build_seed's off-stack
            # branch unreachable. Gated by OFFSTACK_ENABLED so default ingestion is
            # unchanged; ``offstack`` flags the no-scored-changeset case for downstream logs.
            if interesting or config.get_agent_offstack()["enabled"]:
                res["nonjava"] = {
                    "frames": frames,
                    "hash": get_simplified_hash(frames),
                    "offstack": not interesting,
                }

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


# The shutdown-hang watchdog's top frame. `nsTerminator.cpp`'s `RunWatchdog` calls MOZ_CRASH on
# purpose once shutdown overruns its deadline, so on a hang report `crash_info.crashing_thread`
# names the thread that crashed DELIBERATELY, not the one that is stuck. Matched on the FUNCTION
# and not on `thread_name`: Linux caps a pthread name at 15 bytes, so "Shutdown Hang Terminator"
# arrives as "Shutdow~minator" there (measured), while the symbolized frame is stable.
_HANG_WATCHDOG_FRAME = "RunWatchdog"


def thread_for_analysis(data):
    """Index of the thread whose stack describes the failure.

    Normally ``json_dump.crash_info.crashing_thread`` — the thread that faulted. A HANG is the
    exception, and bug 2064436 is why: on crash ec1ff67a that field pointed at thread 45, the
    "Shutdown Hang Terminator", whose seven frames are ``RunWatchdog -> _PR_NativeRunThread ->
    pr_root -> thread_start -> BaseThreadInitThunk -> RtlUserThreadStart`` and carry no
    information about the hang whatsoever. The only files it scores are thread-plumbing boilerplate
    — ``nsTerminator.cpp``, nspr's ``pruthr.c``/``w95thred.c``, ``WindowsDllBlocklist.cpp`` — which
    no window changeset touches, so nothing was interesting, the crash went OFF-STACK, and the
    agent was handed the whole pushlog window with no stack signal at all. From that it produced a
    fluent mechanism about a "MediaTrackGrph" thread this process never had, and Andreas Pehrson
    closed it INVALID: "I see no proof in the profile that this parent process is using or has
    used a MediaTrackGraph. No MediaTrackGrph thread, no GraphRunner thread."

    The hung main thread's stack was in the same payload all along, as thread 0, and it names the
    real blocker: ``ConditionVariableImpl::wait -> NS_ProcessNextEvent ->
    nsThreadPool::ShutdownWithTimeout -> ShutdownXPCOM``. Measured on that crash, this function
    turns 4 boilerplate files into 6 real ones (``nsThreadPool.cpp``, ``XPCOMInit.cpp``,
    ``nsThreadUtils.cpp``, ``nsAppRunner.cpp``, ...), which is the difference between an off-stack
    guess and a scored candidate set.

    Socorro already knows this: its own TOP-LEVEL ``crashing_thread`` reads 0 on that crash, and
    the ``shutdownhang | ...`` signature we triage is generated from the main thread. So the field
    to follow is the top-level one, and the invariant restored here is that OUR stack describes
    the same thread as the SIGNATURE we are triaging — on 2064436 the bug's ``cf_crash_signature``
    was thread 0's while the comment's "Top 9 frames" were thread 45's.

    Narrow on purpose — three conditions, all required, so an ordinary crash is untouched:
    ``report_type == "hang"``, the two fields disagree, and the ``crash_info`` thread's top frame
    is the watchdog. Measured over 40 sampled nightly hang reports: 9 diverge, every one of them a
    ``shutdownhang`` with ``RunWatchdog`` on top and the top-level field pointing at the main
    thread; the other 31 agree and are left alone. Falls back to ``crash_info`` on anything
    unexpected — a bad index must not lose the stack."""
    dump = data.get("json_dump") or {}
    threads = dump.get("threads") or []
    n = (dump.get("crash_info") or {}).get("crashing_thread")
    if not isinstance(n, int) or not 0 <= n < len(threads):
        return n
    if (data.get("report_type") or "") != "hang":
        return n
    alt = data.get("crashing_thread")
    if not isinstance(alt, int) or alt == n or not 0 <= alt < len(threads):
        return n
    top = ((threads[n].get("frames") or [{}])[0] or {}).get("function") or ""
    if _HANG_WATCHDOG_FRAME not in top:
        return n
    logger.info(
        "hang: analysing thread %s (%s) instead of the watchdog thread %s (%s)",
        alt, threads[alt].get("thread_name"), n, threads[n].get("thread_name"))
    return alt


def inspect_stacktrace(data, build_node):
    """Inspect the stack from the data and the check that the hg node
    from the build is the same that the one we have in stack data
    (the nodes could be different when the crash was occuring during an update)"""
    res = []
    files = set()
    dump = data["json_dump"]
    max_frames = 50
    if "threads" in dump:
        # NOT `crash_info.crashing_thread` directly: on a hang that is the watchdog thread that
        # crashed on purpose, and its stack says nothing. See `thread_for_analysis`.
        N = thread_for_analysis(data)
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
