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
bug (``report_bug.build_awaited_work_block``) and the filer (a bucket's key and title). ``census``
ranks other threads classified as busy for the prompt (``triage._census_lines``) and
the spike investigator's ``report`` tool.

MEASURED on 120 release reports of bug 1866944's three signatures, 40 per platform, 2026-09-18:
the pool named by the spin stack has exactly one busy thread in most dumps, and its top
non-wait frames bucket the population the way Jens's hand census did -- macOS 33/40 in
Suggest/viaduct, Linux 22/40 in ``nsPrinterCUPS`` under CUPS's ``sleep()`` retry (three pool
threads per report, two of them on the printer mutex), Windows 8/40 in the audio-session ALPC
connect, 7/40 in the taskbar pin, 5/40 in Suggest/viaduct too. Thread names are what Linux
leaves of them: 15 bytes, middle elided (``BgIOThr~ool #15`` for ``BgIOThreadPool #15``).
"""
import json
import os
import re

MAX_FRAMES = 16
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
    r"(?:Nt|Zw)(?:Read|Write)File|ReadFile|WriteFile|FlushFileBuffers|(?:Nt|Zw)FlushBuffersFile|"
    # OS and runtime wait symbols; callers determine whether the thread is idle.
    r"(?:Nt|Zw)RemoveIoCompletion(?:Ex)?|GetQueuedCompletionStatus(?:Ex)?|"
    r"(?:Nt|Zw)WaitForWorkViaWorkerFactory|WaitCoalesced|"
    r"(?:Nt|Zw)UserGetMessage|GetMessage[AW]?|(?:Nt|Zw)UserMsgWaitForMultipleObjectsEx|"
    r"MsgWaitForMultipleObjects(?:Ex)?|RealMsgWaitForMultipleObjectsEx|(?:Nt|Zw)UserWaitMessage|"
    r"WaitMessage|mozilla::widget::WinUtils::WaitForMessage|"
    r"Microsoft::CoreUI::Dispatch::Wait\w+::Callback_WaitAny|"
    r"_PR_MD_WAIT_CV|_PR_MD_WAIT|PR_WaitCondVar|PR_Wait|pt_TimedWait|"
    r"g_cond_wait(?:_until)?|g_async_queue_pop\w*|cnd_wait|cnd_timedwait|"
    r"semaphore_wait_trap|semaphore_timedwait_trap|__workq_kernreturn|"
    r"__libc_read|__libc_recvmsg|base::WaitableEvent::(?:Timed)?Wait|"
    r"rayon_core::sleep::Sleep::sleep|rayon_core::registry::WorkerThread::wait_until_cold|"
    r"crossbeam_channel::\S*recv\S*|crossbeam_channel::context::Context::wait_until\S*|"
    r"mio::poll::Poll::poll|mio::sys::\S*select\S*|std::sync::\S*[Cc]ondvar\S*"
    r")$")

# THREAD MACHINERY: the runnable/pool/thread-start frames every pool worker's stack ends in.
# Skipped when a stack is named; the outermost frame ABOVE them is the work the thread runs.
_PLUMBING_RE = re.compile(
    r"^(?:"
    r"nsThreadPool::Run|NS_ProcessNextEvent|nsThread::ProcessNextEvent|"
    r"mozilla::ipc::MessagePumpForNonMainThreads::Run|MessageLoop::Run\w*|nsThread::ThreadFunc|"
    r"_pt_root|_PR_NativeRunThread|pr_root|_pthread_start|thread_start(?:<T>)?|"
    r"BaseThreadInitThunk|patched_BaseThreadInitThunk|_{0,2}RtlUserThreadStart|start_thread|"
    r"NS_New\w*Runnable\w*(?:<T>)?(?:::[\w$]+)*|"
    r"__clone3?|clone3?|set_alt_signal_stack_and_start|mozilla::ThreadFuncPoolThread|"
    r"(?:\(anonymous namespace\)::)?ThreadFunc|"
    r"mozilla::detail::RunnableFunction<T>::Run|mozilla::detail::RunnableMethodImpl<T>::Run|"
    r"mozilla::RunnableTask::Run|mozilla::TaskController::\w+|mozilla::runnable_args_\w+.*|"
    r"nsRunnableMethod\w*::Run|mozilla::detail::ProxyRunnable<T>::Run|"
    r"mozilla::MozPromise<T>::ThenValue<T>::\w+|mozilla::MozPromise<T>::ThenValueBase::\w+|"
    r"std::__1::__thread_proxy<T>|std::__1::__thread_execute<T>|"
    r"js::detail::ThreadTrampoline<T>::Start|"
    r"mozilla::net::nsSocketTransportService::Run|mozilla::net::nsSocketTransportService::Poll|"
    r"base::Thread::ThreadMain|base::MessagePumpDefault::Run|base::MessagePumpKqueue::Run|"
    r"base::MessagePumpForIO::\w+|base::MessagePumpLibevent::Run|WatchdogMain|"
    # OS and runtime thread-pool and loop machinery.
    r"Tpp\w+|CRpcThreadCache::RpcWorkerThreadEntry|CRpcThread::WorkerLoop|"
    r"CDllHost::STAWorkerLoop|CDllHost::WorkerThread|DLLHostThreadEntry|"
    r"base::MessagePumpWin::Run|base::MessagePumpForUI::\w+|"
    r"g_thread_proxy|g_main_context_iterate(?:_unlocked)?|g_main_context_iteration|g_main_loop_run|"
    r"impl_thrd_routine|start_wqthread|_pthread_wqthread|event_base_loop|"
    r"__CFRunLoopRun|CFRunLoopRunSpecific|CFRunLoopRun|"
    r"rayon_core::registry::ThreadBuilder::run|rayon_core::registry::main_loop"
    r")$")

# Ignore these syscall stubs and unsymbolised OS frames when extracting work labels.
# An unknown OS frame alone does not establish that the thread is idle.
_STUB_RE = re.compile(r"^(?:KiFastSystemCallRet|KiFastSystemCall|KiIntSystemCall)$")
_OS_MODULE_RE = re.compile(
    r"^(?:ntdll\.dll|kernelbase\.dll|kernel32\.dll|win32u\.dll|user32\.dll|libc\.so\.\d+|"
    r"libpthread\.so\.\d+|ld-linux[\w.-]*|libsystem_\w+\.dylib|libdispatch\.dylib|libdyld\.dylib)$",
    re.I)

# Treat these thread-entry symbols as the stack boundary; discard trailing frames.
_ROOT_RE = re.compile(
    r"^(?:_{0,2}RtlUserThreadStart|_pthread_start|start_thread|__clone3?|clone3?|thread_start)$")

# COM modal-loop symbols make a top wait eligible for main-thread work extraction.
_COM_MODAL_RE = re.compile(
    r"^(?:CCliModalLoop::\w+|ModalLoop|CoWaitForMultipleHandles|CoWaitForMultipleObjects)$")

# The frames that decide a POOL WORKER is one: what an idle worker waits in.
_POOL_RUN_RE = re.compile(r"^(?:nsThreadPool::Run|mozilla::ThreadFuncPoolThread)$")

# In bug 2073276's NSPR revision, ``pruthr.c:435`` is the call to ``_PR_NotifyJoinWaiters`` after
# ``startFunc`` returned. Restrict the inference to that source location; `_pt_root` does not
# have the same behavior.
_RUN_LOOP_RE = re.compile(
    r"^(?:nsThread::ThreadFunc|nsThreadPool::Run|mozilla::ThreadFuncPoolThread|"
    r"NS_ProcessNextEvent|nsThread::ProcessNextEvent|MessageLoop::Run\w*|"
    r"mozilla::ipc::MessagePumpForNonMainThreads::Run|base::MessagePump\w*::Run|"
    r"base::Thread::ThreadMain)$")
_WINDOWS_THREAD_EXIT_RE = re.compile(r"^_PR_NativeRunThread$")
_WINDOWS_JOIN_WAIT_PATH = "nsprpub/pr/src/threads/combined/pruthr.c"
_WINDOWS_JOIN_WAIT_LINE = 435

# Recognized shutdown control-flow frames. On a running main thread, the frames above the first
# such frame are the sampled work to investigate. Bug 2074041 demonstrated why the actor walk
# itself is not enough: destroying managed actors necessarily runs their destructors.
_SHUTDOWN_MACHINERY_RE = re.compile(
    r"^(?:"
    r"nsThread::Shutdown\w*|nsThreadPool::Shutdown\w*|nsThreadManager::Shutdown\w*|"
    r"nsThreadManager::SpinEventLoopUntil\w*|mozilla::SpinEventLoopUntil\w*|"
    r"mozilla::AppShutdown::\w+|mozilla::ShutdownXPCOM|NS_ShutdownXPCOM|"
    r"mozilla::KillClearOnShutdown|nsObserverService::NotifyObservers|"
    r"nsAppStartup::(?:Quit|Observe|ExitLastWindowClosingSurvivalArea)|"
    r"mozilla::ipc::IProtocol::(?:ActorDisconnected|DestroySubtree|DoomSubtree|ActorDestroy)|"
    r"mozilla::ipc::MessageChannel::(?:Close|NotifyChannelClosed|NotifyMaybeChannelError|"
    r"OnNotifyMaybeChannelError|Clear|OnChannelErrorFromLink)|"
    r"mozilla::dom::ContentParent::(?:ShutDownProcess|ActorDestroy|MarkAsDead|"
    r"ShutDownMessageManager|RemoveFromList)|"
    r"mozilla::dom::ContentProcessManager::\w+"
    r")$")
# Prefer the frame that names what is shutting down; use the channel close as a fallback.
_SHUTDOWN_SUBJECT_TIERS = (
    re.compile(r"^(?:mozilla::dom::ContentParent::ShutDownProcess|nsThread::Shutdown|"
               r"nsThreadPool::Shutdown\w*|nsThreadManager::Shutdown\w*|mozilla::ShutdownXPCOM)$"),
    re.compile(r"^mozilla::ipc::MessageChannel::Close$"),
)

# Language/FFI and generated IPDL glue skipped when choosing the outer title/routing frame.
# Generated IPDL dispatch glue is also excluded from a main-thread work prefix.
_IPDL_GLUE_RE = re.compile(
    r"(?:^|::)P\w+(?:Parent|Child)::(?:DeallocManagee|RemoveManagee|OnMessageReceived|"
    r"OnCallReceived)\b")
_GLUE_RE = re.compile(
    r"^(?:mozilla::uniffi::|XPTC_|XPCWrappedNative::|XPC_WN_|js::|JS::|"
    r"mozilla::dom::\w+Binding::|mozilla::dom::binding_detail::|nsXPCWrappedJS|"
    r"mozilla::RunMicroTask|mozilla::CycleCollectedJSContext::)|(?:^|::)uniffi_|"
    r"(?:^|::)P\w+(?:Parent|Child)::(?:DeallocManagee|RemoveManagee|OnMessageReceived|"
    r"OnCallReceived)\b")

# `BgIOThreadPool #510` -> `BgIOThreadPool`; `BgIOThr~ool #15` -> `BgIOThr~ool`.
_INSTANCE_RE = re.compile(r"\s*#?\d+$")

# This signature identifies a watchdog sample rather than a main-thread fault.
_SHUTDOWNHANG_PREFIX = "shutdownhang |"


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


def _matches(regex, frame):
    """The frame's function matches *regex* as symbolised OR as cleaned: ``<std::sys::sync::
    condvar::futex::Condvar>::wait`` is a wait under both spellings."""
    fn = _function(frame)
    return bool(fn) and (bool(regex.match(fn)) or bool(regex.match(clean_symbol(fn))))


def is_wait(frame):
    return _matches(_WAIT_RE, frame)


def is_plumbing(frame):
    return _matches(_PLUMBING_RE, frame)


def is_transparent(frame):
    """Whether work extraction skips this syscall stub or unsymbolised OS frame."""
    fn = _function(frame)
    if fn:
        return bool(_STUB_RE.match(fn))
    return bool(_OS_MODULE_RE.match(str((frame or {}).get("module") or "").strip()))


def to_root(frames):
    """Keep dict frames through the first recognized thread-entry symbol, inclusive."""
    frames = [f for f in (frames or []) if isinstance(f, dict)]
    for i, f in enumerate(frames):
        if _ROOT_RE.match(clean_symbol(_function(f))):
            return frames[:i + 1]
    return frames


def is_idle(frames):
    """Whether ``work_frames`` finds no work labels. This is a stack heuristic,
    not proof that the thread is uninvolved in the hang."""
    return not work_frames(frames)


def has_exited(frames):
    """Recognize the source-mapped Windows NSPR join-wait seen in bug 2073276."""
    frames = frames or []
    if not frames or not is_idle(frames):
        return False
    labels = [clean_symbol(_function(f)) for f in frames]
    if not any(
            _WINDOWS_THREAD_EXIT_RE.match(clean_symbol(_function(f)))
            and _frame_path(f.get("file")) == _WINDOWS_JOIN_WAIT_PATH
            and f.get("line") == _WINDOWS_JOIN_WAIT_LINE
            for f in frames if isinstance(f, dict)):
        return False
    return not any(_RUN_LOOP_RE.match(x) for x in labels if x)


def is_machinery(frame):
    return _matches(_SHUTDOWN_MACHINERY_RE, frame)


def main_work(frames):
    """Extract work frames above recognized shutdown control flow.

    Return ``None`` without work or shutdown frames, or for an unsymbolised top frame.
    A top wait is rejected unless a COM modal-loop symbol occurs in the first eight frames.
    ``machinery`` prefers a shutdown subject over the first control-flow frame."""
    frames = [f for f in to_root(frames) if not _STUB_RE.match(_function(f))]
    if not frames or not _function(frames[0]):
        return None
    if is_wait(frames[0]) and not any(_matches(_COM_MODAL_RE, f) for f in frames[:8]):
        return None
    work, machinery = [], None
    subjects = [None] * len(_SHUTDOWN_SUBJECT_TIERS)
    for f in frames[:60]:
        if not isinstance(f, dict):
            continue
        if is_machinery(f):
            if machinery is None:
                machinery = f
            for tier, regex in enumerate(_SHUTDOWN_SUBJECT_TIERS):
                if subjects[tier] is None and _matches(regex, f):
                    subjects[tier] = f
            continue
        if (machinery is None and _label(f) and not is_transparent(f) and not is_wait(f)
                and not is_plumbing(f) and not _IPDL_GLUE_RE.search(_label(f))):
            work.append(f)
    if machinery is None or not work:
        return None
    subject = next((s for s in subjects if s is not None), None)
    return {"frames": work, "machinery": subject or machinery}


def main_summary(raw):
    """The main thread caught RUNNING at shutdown, for the summary: ``{"index", "name",
    "frames" (top ``MAX_FRAMES``, normalised), "work", "call", "machinery", "bucket", "files",
    "title"}``, or ``None`` when it is parked or ``main_work`` finds no shape. The title reports
    the sampled work and shutdown context without claiming that one sample proves causation.

    Only under a ``shutdownhang |`` signature: there the watchdog thread crashed and the main
    thread's frames are a live sample of what it was doing. On an ``AsyncShutdownTimeout`` the
    MAIN thread aborted on purpose, and its top frames are the abort, not work."""
    from crashclouseau import inspector

    if not str((raw or {}).get("signature") or "").startswith(_SHUTDOWNHANG_PREFIX):
        return None
    threads = ((raw or {}).get("json_dump") or {}).get("threads") or []
    idx = inspector.thread_for_analysis(raw)
    if not isinstance(idx, int) or not 0 <= idx < len(threads):
        return None
    if not isinstance(threads[idx], dict):
        return None
    frames = threads[idx].get("frames") or []
    found = main_work(frames)
    if not found:
        return None
    work = found["frames"]
    outer = [f for f in work if not _GLUE_RE.search(_label(f))] or work
    what, call = clean_symbol(_label(outer[-1])), clean_symbol(_label(work[0]))
    machinery = clean_symbol(_label(found["machinery"]))
    machinery_frames = [f for f in frames[:60] if isinstance(f, dict) and is_machinery(f)]
    title = "{} during {}".format(what, machinery)
    if call and call != what:
        title += " inside {}".format(call)
    return {"index": idx, "name": str(threads[idx].get("thread_name") or "").strip(),
            "frames": normalize_frames(frames, limit=MAX_FRAMES),
            "work": what, "call": call, "machinery": machinery,
            "work_frames": len(work),
            # A shared work/control-flow file cannot establish which code a citation supports.
            "machinery_files": sorted({_frame_path(f.get("file")) for f in machinery_frames
                                       if _frame_path(f.get("file"))}),
            # Preserve frame lines so the gate can distinguish the two regions.
            "work_lines": _frame_lines(work),
            "machinery_lines": _frame_lines(machinery_frames),
            "bucket": bucket_key(work),
            "files": sorted({_frame_path(f.get("file")) for f in work if _frame_path(f.get("file"))}),
            "title": title[:_MAX_TITLE]}


def _frame_lines(frames):
    """``[[path, line], ...]`` for the frames of *frames* that carry both, in stack order."""
    out = []
    for f in frames or []:
        path, line = _frame_path((f or {}).get("file")), (f or {}).get("line")
        if path and isinstance(line, int) and line > 0:
            out.append([path, line])
    return out


def _frame_path(uri):
    """The source path of a frame's ``file`` URI, or ``""``. Hg and git URIs both carry the path
    as their second colon-separated field; no hash conversion here, because that costs a lando
    round trip and this runs at prompt-building time."""
    text = str(uri or "")
    m = re.match(r"^(?:hg|git):[^:]*:([^:]*):", text)
    if m:
        return m.group(1)
    # Generated source URI, e.g. an IPDL implementation.
    m = re.match(r"^s3:gecko-generated-sources:[0-9a-f]+/([^:]+):?", text)
    if m:
        return m.group(1)
    return "" if ":" in text and text.startswith(("hg:", "git:", "s3:")) else text


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
                    "idle": is_idle(frames), "exited": has_exited(frames)})
    # Busy first, then the Windows exit shape, then idle.
    out.sort(key=lambda d: (d["idle"], not d["exited"], -len(d["frames"]), d["index"]))
    return out


def _rust_impl_path(text):
    """``<a::B<x>>::m`` / ``<a::B as t::T>::m`` -> ``a::B<x>::m``: a Rust method symbolised
    through its impl block, as Linux builds spell it, rewritten to the path macOS and Windows
    spell. Depth-aware, because the type's own generics nest inside the block. Anything else
    comes back unchanged."""
    if not text.startswith("<"):
        return text
    depth = 0
    for i, ch in enumerate(text):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
            if depth == 0:
                head, rest = text[1:i], text[i + 1:]
                if not rest.startswith("::"):
                    return text
                # `<Type as Trait>`: the type is what names the code; split at depth 0 only.
                d, cut = 0, None
                for j in range(len(head) - 3):
                    c = head[j]
                    if c == "<":
                        d += 1
                    elif c == ">":
                        d -= 1
                    elif d == 0 and head[j:j + 4] == " as ":
                        cut = j
                        break
                type_path = head[:cut] if cut is not None else head
                return type_path.strip() + rest
    return text


def clean_symbol(function):
    """A function as a bug title wants it: template arguments collapsed to ``<T>`` and the
    argument list dropped, the way Socorro's signature generator normalises.

    A Rust method on Linux arrives as ``<viaduct::client::Client>::send_sync`` or ``<A as
    Trait>::method`` where macOS and Windows symbolise ``viaduct::client::Client::send_sync``;
    the leading impl block is the type's path, so it is kept as one (``_rust_impl_path``) --
    otherwise the same cohort reads ``<T>::send_sync`` on one platform and
    ``viaduct::client::Client::send_sync`` on the others (6b31256d vs 37d5021a, 2026-09-18).

    Preserve ``(anonymous namespace)`` while removing argument lists."""
    text = _rust_impl_path(str(function or "").strip()).replace(_ANON, "\0")
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
    return "".join(out).strip().replace("\0", _ANON)


_ANON = "(anonymous namespace)"


def work_frames(frames):
    """Return frames with work labels, in stack order, through the recognized thread entry.
    Skip wait, machinery and transparent frames; retain other module-only frames."""
    return [f for f in to_root(frames)
            if _label(f) and not (is_transparent(f) or is_wait(f) or is_plumbing(f))]


def work_and_call(frames):
    """Return cleaned outermost and innermost work labels, or ``("", "")``.
    Prefer an outer label outside language glue. The inner label is not necessarily
    the blocking operation; inspect the full stack to establish that."""
    work = work_frames(frames)
    if not work:
        return "", ""
    outer = [f for f in work if not _GLUE_RE.search(_label(f))] or work
    return clean_symbol(_label(outer[-1])), clean_symbol(_label(work[0]))


def identity(symbol):
    """A symbol as a bucket KEY wants it: cleaned, then with every generic marker dropped.
    Windows symbolises a generic method's instantiation (``ingest<T>``) where macOS shows
    ``ingest`` (ca4f5fe5 vs 37d5021a), and an identity must not depend on which."""
    return clean_symbol(symbol).replace("<T>", "").strip()


def bucket_key(frames):
    """``<work> | <call>``: the cohort a report belongs to, as the dedup identity of a bucket.

    THE TWO ENDS AND NOT THE TOP THREE FRAMES, because the key has to agree across platforms
    and builds of ONE cohort, and the middle of a stack does not: on the Suggest/viaduct cohort
    macOS 155.0.1 (37d5021a) has ``viaduct::Request::send`` where Linux 154.0 (e794dacd) has it
    inlined away and shows ``fetch_changeset`` instead, so a first-three-frames key read three
    different cohorts. The runnable's entry and the blocking call are what survive inlining,
    and they are what the title says too."""
    what, call = (identity(x) for x in work_and_call(frames))
    if not what:
        return ""
    return (what if call == what else "{} | {}".format(what, call))[:400]


def bucket_title(frames, target):
    """``<work> blocks <pool> shutdown inside <call>`` -- the shape :jstutte gave the two bucket
    bugs he wrote from our reports (2071528 retitled 2026-09-14, 2073426 filed 2026-09-18) -- or
    ``""`` when the awaited thread has no work frame to name (see ``work_and_call``)."""
    what, call = work_and_call(frames)
    if not what or not target:
        return ""
    name = target.get("name") or ("thread pool" if target.get("kind") == "pool" else "thread")
    title = "{} blocks {} shutdown".format(what, name)
    if call and call != what:
        title += " inside {}".format(call)
    return title[:_MAX_TITLE]


def _with_main(out, main):
    """Promote sampled main-thread work to the summary's top-level routing fields."""
    out["main"] = main
    out["bucket"] = main["bucket"]
    out["files"] = main["files"]
    out["title"] = main["title"]
    return out


def awaited_summary(raw):
    """Summarize the observable subject of a shutdown hang.

    The subject is a busy awaited thread, a Windows thread in the proven post-``ThreadFunc``
    join-wait shape, or sampled main-thread work when no awaited target is identifiable. An
    all-idle awaited set yields counts only because the dump does not expose its work."""
    target = spin_target(raw)
    main = main_summary(raw)
    if target is None:
        if main is None:
            return None
        return _with_main({"target": None, "kind": None, "name": None, "threads": 0,
                           "busy": 0, "idle": 0, "exited": 0}, main)
    threads = awaited_threads(raw)
    busy = [t for t in threads if not t["idle"]]
    exited = [t for t in threads if t["idle"] and t.get("exited")]
    out = {"target": target["entry"], "kind": target["kind"], "name": target["name"],
           "threads": len(threads), "busy": len(busy), "idle": len(threads) - len(busy),
           "exited": len(exited)}
    if busy:
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
    if exited:
        head = exited[0]
        out["exited_thread"] = {"index": head["index"], "name": head["name"],
                                "frames": head["frames"][:MAX_FRAMES]}
        base = _INSTANCE_RE.sub("", head["name"]).strip() or target["name"] or "the awaited thread"
        # Include the main-thread sample because it supplies the title, files and candidate.
        out["bucket"] = "unjoined | {}".format(base)
        if main is not None and main.get("bucket"):
            out["bucket"] += " | {}".format(main["bucket"])
        out["bucket"] = out["bucket"][:400]
        if main is not None:
            out["main"] = main
            out["files"] = main["files"]
            title = ("{} finished its run loop; join pending while the main thread is busy in {}"
                     .format(base, main["work"]))
        else:
            out["files"] = []
            title = "{} finished its run loop; nsThread::Shutdown has not completed its join".format(
                base)
        out["title"] = title[:_MAX_TITLE]
    return out


# Vendored crates from crates.io: their blame is a vendor bump and their owner is nobody here.
# `third_party/application-services/` is Mozilla's own code and is kept (its bump's bug names
# the team: bug 1952588 "Vendor application-services to 138 for Suggest geo expansion", adw).
_NOT_OURS = ("third_party/rust/", "third_party/libwebrtc/", "gfx/wr/", "gfx/skia/")
_ORIGIN_ATTEMPTS = 3
_BUG_RE = re.compile(r"\bbug[ \t]*([0-9]+)", re.I)
_EMAIL_RE = re.compile(r"<([^<>@\s]+@[^<>\s]+)>")


def work_frame_candidates(frames):
    """Work frames to try for deterministic blame routing, best first.

    Prefer the outermost non-glue frame, then move inward. Skip frames without a source line and
    vendored code whose blame identifies an import rather than the underlying implementation."""
    work = work_frames(frames)
    outer = [f for f in work if not _GLUE_RE.search(_label(f))] or work
    out = []
    for f in reversed(outer):
        path = _frame_path(f.get("file"))
        if not path or not f.get("line") or path.startswith(_NOT_OURS):
            continue
        out.append(f)
        if len(out) >= _ORIGIN_ATTEMPTS:
            break
    return out


def _annotate_line(path, channel, node, line):
    """The hg annotate row for ``path:line`` at *node* on *channel*'s repo, or ``None``. One
    ``json-annotate`` request through libmozdata (our User-Agent, its retry); never raises."""
    try:
        from libmozdata.hgmozilla import Annotate

        data = Annotate.get(path, channel, node)
    except Exception:  # noqa: BLE001 - a lookup that fails is an origin we do not have
        return None
    rows = ((data or {}).get(path) or {}).get("annotate") or []
    for row in rows:
        try:
            if int(row.get("lineno", -1)) == int(line):
                return row
        except (TypeError, ValueError):
            continue
    return None


def origin_from_row(row, frame, stackpos=None):
    """A blame row as the ORIGIN record the seed carries: the changeset that last touched the
    awaited work's line, its bug, its author (hg's ``Name <email>``) and the frame it came from."""
    author = str((row or {}).get("author") or "").strip()
    m = _EMAIL_RE.search(author)
    desc = str((row or {}).get("desc") or "")
    bug = _BUG_RE.search(desc)
    return {
        "node": str((row or {}).get("node") or "")[:12],
        "bug": int(bug.group(1)) if bug else None,
        "author": author,
        "author_email": m.group(1) if m else "",
        "desc": desc.strip().splitlines()[0][:160] if desc.strip() else "",
        "path": _frame_path((frame or {}).get("file")),
        "line": (frame or {}).get("line"),
        "function": clean_symbol(_function(frame)),
        "stackpos": stackpos,
    }


def awaited_origin(raw, channel):
    """Blame an extracted awaited- or main-thread work frame for deterministic routing.

    Returns an ``origin_from_row`` record with ``source`` set to ``awaited`` or ``main``, or
    ``None`` when no candidate line can be blamed."""
    threads = awaited_threads(raw)
    busy = [t for t in threads if not t["idle"]]
    if busy:
        index = busy[0]["index"]
        all_frames = (((raw or {}).get("json_dump") or {}).get("threads") or [])[index].get(
            "frames") or []
        origin = _origin_of(work_frame_candidates(all_frames), all_frames, channel)
        return dict(origin, source="awaited") if origin else None
    # With no busy awaited thread, route by the extracted main-thread work when available.
    summary = awaited_summary(raw)
    main = (summary or {}).get("main")
    if not main or not (summary or {}).get("title"):
        return None
    all_frames = (((raw or {}).get("json_dump") or {}).get("threads") or [])[main["index"]].get(
        "frames") or []
    found = main_work(all_frames)
    if not found:
        return None
    origin = _origin_of(work_frame_candidates(found["frames"]), all_frames, channel)
    return dict(origin, source="main") if origin else None


def _origin_of(candidates, all_frames, channel):
    """The first of *candidates* whose line hg can blame, as ``origin_from_row`` shapes it."""
    from crashclouseau import inspector

    for frame in candidates:
        try:
            path, node = inspector.get_path_node(frame.get("file"))
        except Exception:  # noqa: BLE001 - lando may be down; the next frame may not need it
            continue
        if not path or not node:
            continue
        row = _annotate_line(path, channel or "nightly", node, frame.get("line"))
        if row and row.get("node"):
            return origin_from_row(row, frame, stackpos=all_frames.index(frame))
    return None


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


# Thread census: classify and rank threads from their stack symbols.

# Infer a wait kind from symbols, checking each pattern within its depth limit.
# The first matching kind wins; these labels do not identify the awaited resource.
_KINDS = tuple((kind, depth, re.compile(r"^(?:" + pattern + r")$")) for kind, depth, pattern in (
    ("com-out-mta", 20, r"MTAThreadWaitForCall|MTAThreadDispatchCrossApartmentCall"),
    ("com-out-sta", 20, r"CCliModalLoop::\w+|ModalLoop|CoWaitForMultipleHandles|"
                        r"CoWaitForMultipleObjects"),
    ("activation", 24, r"CRpcResolver::\w+"),
    ("rpc-out", 12, r"(?:Nt|Zw)AlpcSendWaitReceivePort|(?:Nt|Zw)AlpcConnectPort\w*|LRPC_\w+::\w+|"
                    r"I_RpcSendReceive|Ndr\w*ClientCall\w*"),
    ("sync-ipc", 12, r"mozilla::ipc::MessageChannel::Wait\w*Notify"),
    ("sendmessage", 10, r"(?:Nt|Zw)UserMessageCall|SendMessage\w*|(?:Nt|Zw)UserSendMessage\w*"),
    ("lock", 10, r"\w*EnterCriticalSection\w*|RtlpWaitOnCriticalSection|\w*AcquireSRWLock\w*|"
                 r"_{0,3}pthread_mutex_lock\w*|_{0,3}lll_lock_wait\w*|__GI___lll_lock_wait\w*|"
                 r"mozilla::detail::MutexImpl::lock|mozilla::RecursiveMutex::LockInternal|"
                 r"os_unfair_lock\w*|__psynch_mutexwait|mozilla::StaticMutex::Lock|"
                 r"mozilla::OffTheBooksMutex::Lock|std::sys::sync::mutex::\S+"),
    ("process", 8, r"(?:Nt|Zw)CreateUserProcess|CreateProcess\w*|waitpid|__waitpid|wait4"),
    ("file-io", 8, r"(?:Nt|Zw)(?:Create|Open|Read|Write|FlushBuffers|SetInformation|"
                   r"QueryInformation|QueryAttributes|QueryFullAttributes|QueryDirectory|"
                   r"DeviceIoControl|FsControl|CancelSynchronousIo|LockFile|UnlockFile|"
                   r"QueryVolumeInformation)File\w*|(?:Nt|Zw)Close|CreateFile\w*|ReadFile|"
                   r"WriteFile|FlushFileBuffers|(?:__GI_|__libc_|_)?f(?:data)?sync|"
                   r"(?:__GI_|__libc_)?p(?:read|write)\w*|(?:__GI_|__libc_)?open(?:at)?\w*|"
                   r"unlink|rename|fcntl|__fcntl\w*"),
    ("net", 8, r"recv\w*|__recv\w*|send|sendto|__send\w*|connect|__connect|getaddrinfo|"
               r"__GI_getaddrinfo|WSA\w+|SockWaitForSingleObject|WSPSelect"),
    ("sleep", 6, r"(?:Nt|Zw)DelayExecution|SleepEx|Sleep|__clock_nanosleep|__GI___nanosleep|"
                 r"__nanosleep|nanosleep|usleep|__sleep|sleep"),
    ("message-wait", 8, r"(?:Nt|Zw)UserGetMessage|GetMessage[AW]?|"
                        r"(?:Nt|Zw)UserMsgWaitForMultipleObjectsEx|MsgWaitForMultipleObjects\w*|"
                        r"RealMsgWaitForMultipleObjectsEx|(?:Nt|Zw)UserWaitMessage|WaitMessage|"
                        r"(?:Nt|Zw)UserPeekMessage|PeekMessage[AW]?|_PeekMessage|"
                        r"__CFRunLoopServiceMachPort|mach_msg\w*"),
))
# Waits on another thread or process first, then locks and I/O, then the rest.
_KIND_RANK = {"com-out-mta": 0, "com-out-sta": 0, "activation": 0, "rpc-out": 0, "sync-ipc": 0,
              "sendmessage": 0, "process": 0, "lock": 1, "file-io": 1, "net": 1, "sleep": 2,
              "running": 2, "wait": 3, "message-wait": 3}
# Symbols used to recognize incoming COM dispatch.
_SERVING_COM_RE = re.compile(
    r"^(?:ComInvokeWithLockAndIPID|ServerCall::ContextInvoke|CStdStubBuffer_Invoke|"
    r"DefaultStubInvoke|AppInvoke|ReentrantSTAInvokeInApartment|STAInvoke)$")
# Symbols used to recognize crash-reporter threads.
_REPORTER_RE = re.compile(r"^(?:google_breakpad::\S+|CrashGenerationServer::\S+)$")
_STATE_DEPTH = 6
_STATES_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "config", "thread_states.json")
_COMMON_STATES = None


def wait_kind(frames):
    """Infer a kind from the top frames; ``running`` means no wait pattern matched."""
    frames = [f for f in to_root(frames) if not is_transparent(f)]
    names = [clean_symbol(_function(f)) for f in frames[:24]]
    for kind, depth, regex in _KINDS:
        if any(n and regex.match(n) for n in names[:depth]):
            return kind
    return "wait" if any(is_wait(f) for f in frames[:8]) else "running"


def serving_com(frames):
    return any(_matches(_SERVING_COM_RE, f) for f in to_root(frames)[:60])


def is_reporter(thread):
    thread = thread or {}
    if str(thread.get("thread_name") or "").startswith("Breakpad"):
        return True
    return any(_matches(_REPORTER_RE, f) for f in (thread.get("frames") or [])[:8])


def state_key(frames):
    """Join up to six cleaned labels, skipping transparent frames and stopping at the root."""
    frames = [f for f in to_root(frames) if not is_transparent(f)]
    return " < ".join(clean_symbol(_label(f)) for f in frames[:_STATE_DEPTH])


def common_states():
    """Load common-state keys; empty if the table is unreadable or invalid JSON.
    The builder counts distinct signature strings, which may describe related crashes."""
    global _COMMON_STATES
    if _COMMON_STATES is None:
        try:
            with open(_STATES_PATH) as handle:
                _COMMON_STATES = frozenset((json.load(handle) or {}).get("states") or {})
        except (OSError, ValueError):
            _COMMON_STATES = frozenset()
    return _COMMON_STATES


def census(raw):
    """Classify threads and rank those with work labels; ``None`` without a thread list:
    ``{"threads", "rows": [{"index", "name", "work", "call", "kind", "serving_com", "common"}],
    "idle": [(index, name)], "no_stack": [index], "skipped": {index: why}}``.

    Skip analysed, crashing and recognized crash-reporter threads. Rank work rows by
    common-state membership (members last), wait kind, then thread index. Display caps
    are applied by callers."""
    from crashclouseau import inspector

    dump = (raw or {}).get("json_dump") or {}
    threads = dump.get("threads") or []
    if not threads:
        return None
    skipped = {}
    analysed = inspector.thread_for_analysis(raw)
    if isinstance(analysed, int):
        skipped[analysed] = "analysed thread"
    crashing = (dump.get("crash_info") or {}).get("crashing_thread")
    if isinstance(crashing, int) and crashing not in skipped:
        skipped[crashing] = "crashing thread"
    common = common_states()
    rows, idle, no_stack = [], [], []
    for i, t in enumerate(threads):
        if not isinstance(t, dict) or i in skipped:
            continue
        if is_reporter(t):
            skipped[i] = "crash reporter"
            continue
        frames = t.get("frames") or []
        name = str(t.get("thread_name") or "").strip()
        if not frames:
            no_stack.append(i)
        elif is_idle(frames):
            idle.append((i, name))
        else:
            work, call = work_and_call(frames)
            rows.append({"index": i, "name": name, "work": work, "call": call,
                         "kind": wait_kind(frames), "serving_com": serving_com(frames),
                         "common": state_key(frames) in common})
    rows.sort(key=lambda r: (r["common"], _KIND_RANK.get(r["kind"], 2), r["index"]))
    return {"threads": len(threads), "rows": rows, "idle": idle, "no_stack": no_stack,
            "skipped": skipped}


def census_row(row, width=90):
    """``<index> <name>: <work> | <call> [<kind>, ...]`` for one census row."""
    work, call = (row.get("work") or "?")[:width], (row.get("call") or "")[:width]
    what = work if call in ("", work) else "{} | {}".format(work, call)
    tags = [row.get("kind") or "?"]
    if row.get("serving_com"):
        tags.append("serving a COM call")
    if row.get("common"):
        tags.append("common state")
    return "{} {}: {} [{}]".format(row.get("index"), row.get("name") or "(unnamed)", what,
                                   ", ".join(tags))
