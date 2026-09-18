# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""The work a shutdown hang is waiting for: which thread, what it is doing, how to name it.

A ``shutdownhang | ...`` signature is generated from the MAIN thread, and on a shutdown hang the
main thread is parked in a wait -- ``nsThreadPool::ShutdownWithTimeout``, ``SpinEventLoopUntil``,
``nsHttpConnectionMgr::Shutdown`` -- for work that is happening on ANOTHER thread. Every cause
under that wait shares the signature, so the signature is a catch-all and its causes are
tracked as separate bugs blocking a ``[meta]`` (bug 1866944 for the pool shutdown, 1633342 for
necko's). The people who triage these bucket the reports by what the AWAITED thread is doing
and file one actionable bug per bucket, without the signature (:jstutte, bug 2073349 comment 1,
bug 2069191 comment 5, 2026-09-18).

Bug 2073349 is what this module exists to stop. The report's main thread was in
``BackgroundEventTarget::Shutdown()`` waiting for ``BgIOThreadPool``; thread 25 of the same
minidump, ``BgIOThreadPool #510``, was in ``SuggestStore::ingest -> RemoteSettingsClient::sync
-> viaduct::Client::send_sync`` waiting for a network request that shutdown could no longer time
out. The analysis explained the wait (``ShutdownWithTimeout(-1)`` arms no timer), took the owner
from the wait code's blame, and never mentioned thread 25. Jens filed the bucket himself the
same morning (bug 2073426), with our report as example 1. The awaited thread was in the payload
all along -- as ``xpcom_spin_event_loop_stack`` naming the pool, and as the pool's one busy
thread in ``json_dump.threads``.

Everything here is deterministic and reads the processed crash only; it reaches the prompt
(``triage._awaited_work_lines``), the dossier (``orchestrator._record_hang_awaited_work``), the
bug (``report_bug.build_awaited_work_block``) and the filer (a bucket's key and title).

MEASURED on 120 release reports of bug 1866944's three signatures, 40 per platform, 2026-09-18:
the pool named by the spin stack has exactly one busy thread in most dumps, and its top
non-wait frames bucket the population the way Jens's hand census did -- macOS 33/40 in
Suggest/viaduct, Linux 22/40 in ``nsPrinterCUPS`` under CUPS's ``sleep()`` retry (three pool
threads per report, two of them on the printer mutex), Windows 8/40 in the audio-session ALPC
connect, 7/40 in the taskbar pin, 5/40 in Suggest/viaduct too. Thread names are what Linux
leaves of them: 15 bytes, middle elided (``BgIOThr~ool #15`` for ``BgIOThreadPool #15``).
"""
import re

MAX_FRAMES = 16
_MAX_BUCKET_FRAMES = 3
_MAX_TITLE = 200

# The innermost entry of `xpcom_spin_event_loop_stack` names what the main thread waits for.
# `nsThreadPool::ShutdownWithTimeout <pool>` carries the pool's name since bug 1976556 (2025-07);
# `nsThread::Shutdown: <name>` a single named thread. The map covers the spin loops whose awaited
# thread has a fixed name: they do not say it, so it is said here.
_POOL_ENTRY = re.compile(r"^nsThreadPool::ShutdownWithTimeout(?:\s+(\S+))?$")
_THREAD_ENTRY = re.compile(r"^nsThread::Shutdown:?\s+(.+)$")
_ENTRY_THREADS = {
    "nsHttpConnectionMgr::Shutdown": "Socket Thread",
    "ParentImpl::ShutdownBackgroundThread": "IPDL Background",
    "QuotaManager::Observer::Observe": "QuotaManager IO",
    "CacheFileIOManager::ShutdownEvent": "Cache2 I/O",
}

# WAITING, as a frame: condition variables, futexes, mutex acquisition, sleeps, event-loop polls
# and the kernel entries under them. These say the thread is parked, not what it is doing, so
# they are skipped when a stack is named and they are what an IDLE pool worker consists of.
_WAIT_RE = re.compile(
    r"^(?:"
    r"__psynch_cvwait|_{1,3}pthread_cond_(?:timed)?wait\w*|__GI___pthread_cond_\w+|"
    r"pthread_cond_(?:timed)?wait\w*|__futex_abstimed_wait\w*|__GI___futex_abstimed_wait\w*|"
    r"__internal_syscall_cancel|__syscall_cancel\w*|syscall|futex_wait\w*|__futex_wait\w*|"
    r"__GI___lll_lock_wait\w*|__lll_lock_wait\w*|_{1,3}pthread_mutex_lock\w*|"
    r"pthread_mutex_lock|mozilla::RecursiveMutex::LockInternal|mozilla::detail::MutexImpl::\w+|"
    r"RtlEnterCriticalSection|RtlpEnterCriticalSection\w*|RtlpWaitOnCriticalSection|"
    r"SleepConditionVariableSRW|RtlSleepConditionVariableSRW|(?:Nt|Zw)WaitForAlertByThreadId|"
    r"RtlpWaitOnAddress\w*|RtlWaitOnAddress|WaitOnAddress|WaitForSingleObject\w*|"
    r"WaitForMultipleObjects\w*|(?:Nt|Zw)WaitForSingleObject\w*|"
    r"(?:Nt|Zw)WaitForMultipleObjects\w*|(?:Nt|Zw)DelayExecution|SleepEx|Sleep|"
    r"__clock_nanosleep|__GI___nanosleep|__nanosleep|nanosleep|__sleep|sleep|usleep|"
    r"(?:Nt|Zw)AlpcSendWaitReceivePort|(?:Nt|Zw)AlpcConnectPort\w*|"
    r"mozilla::detail::ConditionVariableImpl::wait\w*|mozilla::OffTheBooksCondVar::Wait\w*|"
    r"mozilla::CondVar::Wait\w*|mozilla::Monitor::Wait\w*|mozilla::ReentrantMonitor::Wait\w*|"
    r"<?std::sys::sync::condvar::\S+|<?std::sys::\S+|<?std::thread::\S+|"
    r"<?core::ops::function::\S+|pollster::Signal::wait|<pollster::Signal>::wait|"
    r"poll|__poll|__GI___poll|__libc_poll|ppoll|__GI_ppoll|PR_Poll|_PR_MD_PR_POLL|select|"
    r"__select|WSPSelect|SockWaitForSingleObject|kevent\d*|epoll_wait|epoll_pwait\w*|WSAPoll|"
    r"WSAWaitForMultipleEvents|mach_msg\w*|__CFRunLoopServiceMachPort|"
    r"read|__read|__GI___read|write|__write|__GI___write|pread\w*|pwrite\w*|recv\w*|__recv\w*|"
    r"send|sendto|__send\w*|connect|__connect|fsync|fdatasync|__fsync|"
    r"(?:Nt|Zw)(?:Read|Write)File|ReadFile|WriteFile|FlushFileBuffers|(?:Nt|Zw)FlushBuffersFile"
    r")$")

# THREAD MACHINERY: the runnable/pool/thread-start frames every pool worker's stack ends in.
# Skipped when a stack is named; the outermost frame ABOVE them is the work the thread runs.
_PLUMBING_RE = re.compile(
    r"^(?:"
    r"nsThreadPool::Run|NS_ProcessNextEvent|nsThread::ProcessNextEvent|"
    r"mozilla::ipc::MessagePumpForNonMainThreads::Run|MessageLoop::Run\w*|nsThread::ThreadFunc|"
    r"_pt_root|_PR_NativeRunThread|pr_root|_pthread_start|thread_start(?:<T>)?|"
    r"BaseThreadInitThunk|patched_BaseThreadInitThunk|RtlUserThreadStart|start_thread|"
    r"__clone3?|clone3?|set_alt_signal_stack_and_start|mozilla::ThreadFuncPoolThread|ThreadFunc|"
    r"mozilla::detail::RunnableFunction<T>::Run|mozilla::detail::RunnableMethodImpl<T>::Run|"
    r"mozilla::RunnableTask::Run|mozilla::TaskController::\w+|mozilla::runnable_args_\w+.*|"
    r"nsRunnableMethod\w*::Run|mozilla::detail::ProxyRunnable<T>::Run|"
    r"mozilla::MozPromise<T>::ThenValue<T>::\w+|mozilla::MozPromise<T>::ThenValueBase::\w+|"
    r"std::__1::__thread_proxy<T>|std::__1::__thread_execute<T>|"
    r"js::detail::ThreadTrampoline<T>::Start|"
    r"mozilla::net::nsSocketTransportService::Run|mozilla::net::nsSocketTransportService::Poll|"
    r"base::Thread::ThreadMain|base::MessagePumpDefault::Run|base::MessagePumpKqueue::Run|"
    r"base::MessagePumpForIO::\w+|base::MessagePumpLibevent::Run|WatchdogMain"
    r")$")

# The frames that decide a POOL WORKER is one: what an idle worker waits in.
_POOL_RUN_RE = re.compile(r"^(?:nsThreadPool::Run|mozilla::ThreadFuncPoolThread)$")

# Language/FFI glue between the work and the frame that names it: uniffi scaffolding, XPConnect,
# the JS engine. Skipped only when choosing the OUTERMOST frame for a title -- the work a
# ``uniffi_suggest_fn_method_suggeststore_ingest`` frame runs is the ``SuggestStore::ingest``
# above it.
_GLUE_RE = re.compile(
    r"^(?:mozilla::uniffi::|XPTC_|XPCWrappedNative::|XPC_WN_|js::|JS::|"
    r"mozilla::dom::\w+Binding::|mozilla::dom::binding_detail::|nsXPCWrappedJS|"
    r"mozilla::RunMicroTask|mozilla::CycleCollectedJSContext::)|(?:^|::)uniffi_")

# `BgIOThreadPool #510` -> `BgIOThreadPool`; `BgIOThr~ool #15` -> `BgIOThr~ool`.
_INSTANCE_RE = re.compile(r"\s*#?\d+$")


def spin_entries(value):
    """The entries of an ``xpcom_spin_event_loop_stack`` value, outermost first, bare."""
    text = str(value or "").strip()
    if not text:
        return []
    text = re.sub(r"^default:\s*", "", text)
    return [e.strip() for e in text.split("|") if e.strip()]


def spin_target(raw):
    """What the main thread is waiting for, from the INNERMOST spin-loop entry: ``{"entry",
    "kind": "pool" | "thread", "name"}``, or ``None`` when the entry names nothing this module
    knows how to find in the thread list (an ``AsyncShutdown Spinner`` waits on JS blockers,
    which the ``async_shutdown_timeout`` annotation already names)."""
    entries = spin_entries((raw or {}).get("xpcom_spin_event_loop_stack"))
    if not entries:
        return None
    inner = entries[-1]
    m = _POOL_ENTRY.match(inner)
    if m:
        return {"entry": inner, "kind": "pool", "name": (m.group(1) or "").strip()}
    m = _THREAD_ENTRY.match(inner)
    if m:
        return {"entry": inner, "kind": "thread", "name": m.group(1).strip()}
    head = inner.split()[0]
    if head in _ENTRY_THREADS:
        return {"entry": inner, "kind": "thread", "name": _ENTRY_THREADS[head]}
    return None


def thread_matches(name, wanted):
    """Is *name*, as the minidump has it, an instance of *wanted*?

    Instance numbers are dropped on both sides (``BgIOThreadPool #510`` is a ``BgIOThreadPool``
    thread). Linux caps a pthread name at 15 bytes and Gecko elides the MIDDLE to fit, keeping
    a head and a tail around ``~`` (``BgIOThr~ool #15``, ``Backgro~Pool #2``, ``StreamT~ns
    #247``): the elided form matches when the wanted name starts with the head and ends with the
    tail. Nothing else matches -- a bare prefix rule would take ``Backgro`` for both
    ``BackgroundThreadPool`` and ``BackgroundFileSaver``."""
    name = str(name or "").strip()
    wanted = str(wanted or "").strip()
    if not name or not wanted:
        return False
    if name == wanted:
        return True
    base = _INSTANCE_RE.sub("", name).strip()
    wbase = _INSTANCE_RE.sub("", wanted).strip()
    if base == wbase:
        return True
    if "~" in base:
        head, tail = base.split("~", 1)
        return bool(head) and wbase.startswith(head) and wbase.endswith(tail)
    return False


def _function(frame):
    return str((frame or {}).get("function") or "").strip()


def _label(frame):
    """What a frame is called in a key or a title: its function, else its module."""
    return _function(frame) or str((frame or {}).get("module") or "").strip()


def is_wait(frame):
    return bool(_WAIT_RE.match(clean_symbol(_function(frame))))


def is_plumbing(frame):
    return bool(_PLUMBING_RE.match(clean_symbol(_function(frame))))


def is_idle(frames):
    """Is this thread doing NOTHING that names a subsystem -- every frame a wait or thread
    machinery? True of a pool worker parked in ``nsThreadPool::Run``'s own wait for an event,
    of the socket thread in its poll, of a thread that has already left its run loop; false as
    soon as one frame says what the thread is doing, a mutex it is blocked on included (two of
    the three CUPS threads in the census sit on the printer's ``RecursiveMutex``, and they are
    part of that bucket, not idle)."""
    return not work_frames(frames)


def _frame_path(uri):
    """The source path of a frame's ``file`` URI, or ``""``. Hg and git URIs both carry the path
    as their second colon-separated field; no hash conversion here, because that costs a lando
    round trip and this runs at prompt-building time."""
    text = str(uri or "")
    m = re.match(r"^(?:hg|git):[^:]*:([^:]*):", text)
    if m:
        return m.group(1)
    return "" if ":" in text and text.startswith(("hg:", "git:")) else text


def normalize_frames(frames, limit=MAX_FRAMES):
    """Socorro's frame dicts as the compact rows the prompt, the bug and the dossier share."""
    out = []
    for i, f in enumerate((frames or [])[:limit]):
        if not isinstance(f, dict):
            continue
        row = {"stackpos": i, "function": _function(f),
               "module": str(f.get("module") or "").strip(),
               "filename": _frame_path(f.get("file"))}
        line = f.get("line")
        if isinstance(line, int) and line > 0:
            row["line"] = line
        out.append(row)
    return out


def awaited_threads(raw):
    """The threads the main thread waits for, busy first then deepest first: ``[{"index",
    "name", "frames", "idle"}]``. ``[]`` when the spin stack names nothing findable.

    For a pool the candidates are the threads named for it; a pool with no name (a build older
    than bug 1976556) yields every pool worker in the dump. A dump whose pool threads are ALL
    idle is a real and common shape: the work finished after the watchdog fired, or the pool
    thread had already exited; the caller says so rather than naming a subsystem."""
    target = spin_target(raw)
    if target is None:
        return []
    threads = ((raw or {}).get("json_dump") or {}).get("threads") or []
    out = []
    for i, t in enumerate(threads):
        if not isinstance(t, dict):
            continue
        name = str(t.get("thread_name") or "").strip()
        frames = t.get("frames") or []
        if target["kind"] == "pool" and not target["name"]:
            if not any(_POOL_RUN_RE.match(clean_symbol(_function(f))) for f in frames[:60]):
                continue
        elif not thread_matches(name, target["name"]):
            continue
        out.append({"index": i, "name": name, "frames": normalize_frames(frames, limit=60),
                    "idle": is_idle(frames)})
    out.sort(key=lambda d: (d["idle"], -len(d["frames"]), d["index"]))
    return out


def clean_symbol(function):
    """A function as a bug title or a bucket key wants it: template arguments collapsed to
    ``<T>`` and the argument list dropped, the way Socorro's signature generator normalises."""
    text = str(function or "").strip()
    out, depth = [], 0
    for ch in text:
        if ch == "<":
            if depth == 0:
                out.append("<T>")
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            if ch == "(":
                break
            out.append(ch)
    return "".join(out).strip()


def work_frames(frames):
    """The frames that say what a thread is DOING: neither a wait nor thread machinery,
    innermost first. Module-only frames (no symbol) are kept under their module name."""
    return [f for f in frames or [] if _label(f) and not is_wait(f) and not is_plumbing(f)]


def bucket_key(frames):
    """The first ``_MAX_BUCKET_FRAMES`` work frames, cleaned and ``|``-joined: the cohort a report
    belongs to, the way Socorro's proto-signature buckets a crashing thread."""
    labels = [clean_symbol(_label(f)) for f in work_frames(frames)[:_MAX_BUCKET_FRAMES]]
    return " | ".join(x for x in labels if x)[:400] if labels else ""


def bucket_title(frames, target):
    """``<work> blocks <pool> shutdown inside <call>`` -- the shape :jstutte gave the two bucket
    bugs he wrote from our reports (2071528 retitled 2026-09-14, 2073426 filed 2026-09-18) -- or
    ``""`` when the awaited thread has no work frame to name.

    The WORK is the outermost work frame that is not language glue (the runnable's entry, e.g.
    ``SuggestStore::ingest`` rather than the uniffi scaffolding under it); the CALL is the
    innermost work frame (``viaduct::Client::send_sync``, ``_cupsCreateDest``). One frame gives
    only the first half."""
    work = work_frames(frames)
    if not work or not target:
        return ""
    outer = [f for f in work if not _GLUE_RE.search(_label(f))] or work
    what = clean_symbol(_label(outer[-1]))
    call = clean_symbol(_label(work[0]))
    if not what:
        return ""
    name = target.get("name") or ("thread pool" if target.get("kind") == "pool" else "thread")
    title = "{} blocks {} shutdown".format(what, name)
    if call and call != what:
        title += " inside {}".format(call)
    return title[:_MAX_TITLE]


def awaited_summary(raw):
    """Everything downstream wants to know about the awaited work, or ``None`` when the spin
    stack names nothing this module can find: ``target`` (the innermost spin entry), ``kind``,
    ``name``, ``threads``/``busy``/``idle`` counts, and -- when a thread is busy -- ``thread``
    (``index``, ``name``, the top ``MAX_FRAMES`` frames), ``bucket``, ``files``, ``title`` and
    ``other_busy`` (the other busy threads' indexes, names and buckets)."""
    target = spin_target(raw)
    if target is None:
        return None
    threads = awaited_threads(raw)
    busy = [t for t in threads if not t["idle"]]
    out = {"target": target["entry"], "kind": target["kind"], "name": target["name"],
           "threads": len(threads), "busy": len(busy), "idle": len(threads) - len(busy)}
    if not busy:
        return out
    head = busy[0]
    frames = head["frames"]
    out["thread"] = {"index": head["index"], "name": head["name"],
                     "frames": frames[:MAX_FRAMES]}
    out["bucket"] = bucket_key(frames)
    # The WORK frames' files only: the thread-start and event-loop files under them are on
    # every thread in the process and say nothing about this one.
    out["files"] = sorted({f["filename"] for f in work_frames(frames) if f.get("filename")})
    out["title"] = bucket_title(frames, target)
    out["other_busy"] = [{"index": t["index"], "name": t["name"],
                          "bucket": bucket_key(t["frames"])} for t in busy[1:4]]
    return out


def thread_files(raw, index):
    """The source paths on thread *index*'s stack, for the gate that asks whether a mechanism
    cites the WAITING thread's code or the awaited thread's."""
    threads = ((raw or {}).get("json_dump") or {}).get("threads") or []
    if not isinstance(index, int) or not 0 <= index < len(threads):
        return set()
    frames = (threads[index] or {}).get("frames") or []
    return {p for p in (_frame_path((f or {}).get("file")) for f in frames[:60]) if p}


# A function longer than this is a template instantiation spelled out (a `RunnableFunction`
# over a lambda's full signature ran to 380 characters on 37d5021a); the head names it.
_MAX_FUNCTION_CHARS = 120


def frames_text(frames, limit=MAX_FRAMES):
    """``<stackpos>  <module>  <function>  <file>:<line>`` per frame, the format Socorro
    pre-fills into a crash bug and ``report_bug.build_frames_block`` prints."""
    lines = []
    for f in (frames or [])[:limit]:
        fn = f.get("function") or ""
        if len(fn) > _MAX_FUNCTION_CHARS:
            fn = fn[:_MAX_FUNCTION_CHARS] + "..."
        loc = f.get("filename") or ""
        if loc and f.get("line"):
            loc = "{}:{}".format(loc, f["line"])
        desc = "  ".join(x for x in (f.get("module") or "", fn, loc) if x)
        lines.append("{}  {}".format(f.get("stackpos"), desc).rstrip())
    return "\n".join(lines)
