# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Bug 2073349: a shutdown hang's AWAITED WORK, and the BUCKET bug filed for it.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
        uv run python -m unittest tests.test_hang_bucket

The report's main thread waited in `nsThreadPool::ShutdownWithTimeout` for `BgIOThreadPool`;
thread 25 of the same minidump, `BgIOThreadPool #510`, was in `SuggestStore::ingest ->
RemoteSettingsClient::sync -> viaduct::Client::send_sync`. The analysis explained the wait, the
bug carried the signature that bug 1866944 (the [meta]) already holds, and :jstutte asked us to
stop filing catch-alls (2073349 c1) and to learn it (2069191 c5). Four things are pinned here:

  1. `hang.awaited_summary` -- the awaited thread, its bucket and its title, from the dump;
  2. `triage._awaited_work_lines` -- the fact in front of both models;
  3. `orchestrator._apply_hang_wait_gate` -- an `actionable` mechanism that is the wait abstains;
  4. the BUCKET bug: `report_bug.build_bug_preview`, `bugzilla_apply.autofile_bug` and the
     spike filer file it named for its cause, without the signature, blocking the tracker.
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau import bugzilla_apply, hang, report_bug, spike_report  # noqa: E402
from crashclouseau.agent import orchestrator as orch, spike_escalation as se, triage  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    AbstainKind, Candidate, Claim, Confidence, Decision, Dossier, RefCitation, Verdict,
)
from crashclouseau.agent.spike_agent import SpikeFindings  # noqa: E402
from tests.test_autofile import _PREVIEW, _Base, _bug  # noqa: E402
from tests.test_spike_escalation import _FilerBase, _esc  # noqa: E402

_REV = "hg:hg.mozilla.org/releases/mozilla-release:{}:36f485dbc605"


def _f(function, module="XUL", path=None, line=None):
    frame = {"function": function, "module": module}
    if path:
        frame["file"] = _REV.format(path)
        frame["line"] = line or 1
    return frame


# Crash 37d5021a-d0d8-4a25-ad5d-992110260918 (release 155.0.1, macOS), in shape.
_MAIN = {"thread_name": "MainThread", "frames": [
    _f("__psynch_cvwait", "libsystem_kernel.dylib"),
    _f("_pthread_cond_wait", "libsystem_pthread.dylib"),
    _f("mozilla::detail::ConditionVariableImpl::wait(mozilla::detail::MutexImpl&)",
       "libmozglue.dylib", "mozglue/misc/ConditionVariable_posix.cpp", 92),
    _f("mozilla::TaskController::GetRunnableForMTTask(bool)", "XUL",
       "xpcom/threads/TaskController.cpp", 772),
    _f("NS_ProcessNextEvent(nsIThread*, bool)", "XUL", "xpcom/threads/nsThreadUtils.cpp", 471),
    _f("nsThreadPool::ShutdownWithTimeout(int)", "XUL", "xpcom/threads/nsThreadPool.cpp", 615),
    _f("nsThreadManager::ShutdownNonMainThreads()", "XUL", "xpcom/threads/nsThreadManager.cpp",
       390),
    _f("mozilla::ShutdownXPCOM(nsIServiceManager*)", "XUL", "xpcom/build/XPCOMInit.cpp", 592),
    _f("XREMain::XRE_main(int, char**, mozilla::BootstrapConfig const&)", "XUL",
       "toolkit/xre/nsAppRunner.cpp", 6639),
]}
_AS = "third_party/application-services/components/"
_SUGGEST = {"thread_name": "BgIOThreadPool #510", "frames": [
    _f("__psynch_cvwait", "libsystem_kernel.dylib"),
    _f("_pthread_cond_wait", "libsystem_pthread.dylib"),
    _f("pollster::Signal::wait", "XUL", "third_party/rust/pollster/src/lib.rs", 69),
    _f("viaduct::client::Client::send_sync", "XUL", _AS + "viaduct/src/client.rs", 70),
    _f("viaduct::Request::send", "XUL", _AS + "viaduct/src/lib.rs", 102),
    _f("remote_settings::client::ViaductApiClient::make_request", "XUL",
       _AS + "remote_settings/src/client.rs", 654),
    _f("remote_settings::client::RemoteSettingsClient<C>::perform_sync_operation", "XUL",
       _AS + "remote_settings/src/client.rs", 367),
    _f("remote_settings::client::RemoteSettingsClient<C>::sync", "XUL",
       _AS + "remote_settings/src/client.rs", 383),
    _f("remote_settings::RemoteSettingsClient::sync", "XUL", _AS + "remote_settings/src/lib.rs",
       190),
    _f("suggest::store::SuggestStoreInner<S>::ingest", "XUL", _AS + "suggest/src/store.rs", 636),
    _f("uniffi_suggest_fn_method_suggeststore_ingest", "XUL", _AS + "suggest/src/store.rs", 176),
    _f("mozilla::uniffi::ScaffoldingCallHandlerUniffiSuggestFnMethodSuggeststoreIngest::"
       "MakeRustCall(mozilla::uniffi::RustCallStatus*)", "XUL",
       "toolkit/components/uniffi-js/GeneratedScaffolding.cpp", 12502),
    _f("mozilla::detail::RunnableFunction<mozilla::uniffi::UniffiSyncCallHandler::"
       "CallAsyncWrapper(std::__1::unique_ptr<mozilla::uniffi::UniffiSyncCallHandler>, "
       "mozilla::dom::GlobalObject const&)::$_0>::Run()", "XUL", "xpcom/threads/nsThreadUtils.h",
       535),
    _f("nsThreadPool::Run()", "XUL", "xpcom/threads/nsThreadPool.cpp", 442),
    _f("NS_ProcessNextEvent(nsIThread*, bool)", "XUL", "xpcom/threads/nsThreadUtils.cpp", 471),
    _f("mozilla::ipc::MessagePumpForNonMainThreads::Run(base::MessagePump::Delegate*)", "XUL",
       "ipc/glue/MessagePump.cpp", 297),
    _f("MessageLoop::Run()", "XUL", "ipc/chromium/src/base/message_loop.cc", 363),
    _f("nsThread::ThreadFunc(void*)", "XUL", "xpcom/threads/nsThread.cpp", 375),
    _f("_pt_root", "libnss3.dylib", "nsprpub/pr/src/pthreads/ptthread.c", 191),
    _f("_pthread_start", "libsystem_pthread.dylib"),
    _f("thread_start", "libsystem_pthread.dylib"),
]}
_IDLE_POOL = {"thread_name": "BgIOThreadPool #3", "frames": [
    _f("__psynch_cvwait", "libsystem_kernel.dylib"),
    _f("_pthread_cond_wait", "libsystem_pthread.dylib"),
    _f("mozilla::detail::ConditionVariableImpl::wait_for(mozilla::detail::MutexImpl&, "
       "mozilla::BaseTimeDuration<mozilla::TimeDurationValueCalculator> const&)",
       "libmozglue.dylib", "mozglue/misc/ConditionVariable_posix.cpp", 120),
    _f("nsThreadPool::Run()", "XUL", "xpcom/threads/nsThreadPool.cpp", 442),
    _f("NS_ProcessNextEvent(nsIThread*, bool)", "XUL", "xpcom/threads/nsThreadUtils.cpp", 471),
    _f("mozilla::ipc::MessagePumpForNonMainThreads::Run(base::MessagePump::Delegate*)", "XUL",
       "ipc/glue/MessagePump.cpp", 297),
    _f("MessageLoop::Run()", "XUL", "ipc/chromium/src/base/message_loop.cc", 363),
    _f("nsThread::ThreadFunc(void*)", "XUL", "xpcom/threads/nsThread.cpp", 375),
    _f("_pt_root", "libnss3.dylib"), _f("_pthread_start", "libsystem_pthread.dylib"),
    _f("thread_start", "libsystem_pthread.dylib"),
]}
# A Linux worker blocked on the printer mutex (3edb8139, the CUPS bucket): the name is what
# Linux leaves of `BgIOThreadPool #4`, and a mutex wait is not idle.
_CUPS_MUTEX = {"thread_name": "BgIOThr~Pool #4", "frames": [
    _f("__GI___lll_lock_wait", "libc.so.6"),
    _f("___pthread_mutex_lock", "libc.so.6"),
    _f("mozilla::RecursiveMutex::LockInternal()", "libxul.so", "xpcom/threads/RecursiveMutex.cpp",
       50),
    _f("nsPrinterCUPS::IsCUPSVersionAtLeast(unsigned long, unsigned long, unsigned long) const",
       "libxul.so", "widget/nsPrinterCUPS.cpp", 120),
    _f("nsPrinterCUPS::SupportsColor() const", "libxul.so", "widget/nsPrinterCUPS.cpp", 200),
    _f("mozilla::detail::RunnableFunction<mozilla::SpawnPrintBackgroundTask<nsPrinterBase, "
       "bool, >(nsPrinterBase&, mozilla::dom::Promise&)::$_0>::Run()", "libxul.so",
       "xpcom/threads/nsThreadUtils.h", 535),
    _f("nsThreadPool::Run()", "libxul.so", "xpcom/threads/nsThreadPool.cpp", 442),
    _f("NS_ProcessNextEvent(nsIThread*, bool)", "libxul.so", "xpcom/threads/nsThreadUtils.cpp",
       471),
    _f("nsThread::ThreadFunc(void*)", "libxul.so", "xpcom/threads/nsThread.cpp", 375),
    _f("_pt_root", "libnspr4.so"), _f("start_thread", "libc.so.6"), _f("__clone3", "libc.so.6"),
]}
_SOCKET_IDLE = {"thread_name": "Socket Thread", "frames": [
    _f("NtWaitForSingleObject", "ntdll.dll"), _f("SockWaitForSingleObject", "mswsock.dll"),
    _f("WSPSelect", "mswsock.dll"), _f("select", "ws2_32.dll"),
    _f("_PR_MD_PR_POLL(PRPollDesc*, int, unsigned int)", "nss3.dll",
       "nsprpub/pr/src/md/windows/w32poll.c", 222),
    _f("mozilla::net::nsSocketTransportService::Run()", "xul.dll",
       "netwerk/base/nsSocketTransportService2.cpp", 1224),
    _f("NS_ProcessNextEvent(nsIThread*, bool)", "xul.dll", "xpcom/threads/nsThreadUtils.cpp", 471),
    _f("nsThread::ThreadFunc(void*)", "xul.dll", "xpcom/threads/nsThread.cpp", 375),
    _f("thread_start<unsigned int (__cdecl*)(void *),1>", "ucrtbase.dll"),
    _f("BaseThreadInitThunk", "kernel32.dll"), _f("RtlUserThreadStart", "ntdll.dll"),
]}
_WATCHDOG = {"thread_name": "Shutdown Hang Terminator", "frames": [
    _f("mozilla::(anonymous namespace)::RunWatchdog(void*)", "XUL",
       "toolkit/components/terminator/nsTerminator.cpp", 300),
    _f("_pt_root", "libnss3.dylib"), _f("_pthread_start", "libsystem_pthread.dylib"),
    _f("thread_start", "libsystem_pthread.dylib"),
]}

_SIG = "shutdownhang | mozilla::SpinEventLoopUntil<T> | nsThreadPool::ShutdownWithTimeout"
_SUGGEST_TITLE = ("suggest::store::SuggestStoreInner<T>::ingest blocks BgIOThreadPool shutdown "
                  "inside viaduct::client::Client::send_sync")


def _hang(threads, spin="default: nsThreadPool::ShutdownWithTimeout BgIOThreadPool", **over):
    """A processed shutdown hang: the main thread first, the watchdog last."""
    threads = [_MAIN, *threads, _WATCHDOG]
    data = {
        "report_type": "hang", "crashing_thread": 0, "signature": _SIG,
        "process_type": "parent", "release_channel": "release",
        "shutdown_progress": "xpcom-shutdown-threads",
        "moz_crash_reason": "Shutdown hanging at step XPCOMShutdownThreads. Something is "
                            "blocking the main-thread.",
        "xpcom_spin_event_loop_stack": spin,
        "json_dump": {"crash_info": {"crashing_thread": len(threads) - 1,
                                     "type": "EXC_BAD_ACCESS / KERN_INVALID_ADDRESS"},
                      "threads": threads},
    }
    data.update(over)
    return data


def _seed(raw, signature=_SIG):
    return {"uuid": "37d5021a", "signature": signature, "channel": "release",
            "product": "Firefox", "raw_crash": raw}


class TestSpinTarget(unittest.TestCase):
    def test_the_pool_the_spin_loop_names(self):
        t = hang.spin_target(_hang([]))
        self.assertEqual((t["kind"], t["name"]), ("pool", "BgIOThreadPool"))
        # Nested entries: the INNERMOST one is what the main thread is waiting for right now.
        nested = "default: nsThreadManager::Shutdown|nsThreadPool::ShutdownWithTimeout " \
                 "BackgroundThreadPool"
        t = hang.spin_target(_hang([], spin=nested))
        self.assertEqual((t["kind"], t["name"]), ("pool", "BackgroundThreadPool"))
        # A build older than bug 1976556 names no pool.
        t = hang.spin_target(_hang([], spin="default: nsThreadPool::ShutdownWithTimeout"))
        self.assertEqual((t["kind"], t["name"]), ("pool", ""))

    def test_a_single_named_thread(self):
        spin = ("default: nsThread::Shutdown: sqldb:tabnotes.sqlite #9|nsThread::Shutdown: "
                "sqldb:domain_to_categories.sqlite #7")
        t = hang.spin_target(_hang([], spin=spin))
        self.assertEqual((t["kind"], t["name"]), ("thread", "sqldb:domain_to_categories.sqlite #7"))

    def test_the_spin_loops_whose_thread_has_a_fixed_name(self):
        for entry, name in (("nsHttpConnectionMgr::Shutdown", "Socket Thread"),
                            ("ParentImpl::ShutdownBackgroundThread", "IPDL Background"),
                            ("QuotaManager::Observer::Observe profile-before-change-qm",
                             "QuotaManager IO"),
                            ("CacheFileIOManager::ShutdownEvent", "Cache2 I/O")):
            with self.subTest(entry=entry):
                t = hang.spin_target(_hang([], spin="default: " + entry))
                self.assertEqual((t["kind"], t["name"]), ("thread", name))

    def test_what_names_no_thread_is_none(self):
        # An AsyncShutdown spinner waits on JS blockers, which `async_shutdown_timeout` names.
        self.assertIsNone(hang.spin_target(
            _hang([], spin="default: AsyncShutdown Spinner for profile-before-change")))
        self.assertIsNone(hang.spin_target(_hang([], spin="")))
        self.assertIsNone(hang.spin_target({}))


class TestThreadNames(unittest.TestCase):
    def test_instances_and_the_linux_elision_match(self):
        for name, wanted in (("BgIOThreadPool #510", "BgIOThreadPool"),
                             ("BgIOThr~ool #15", "BgIOThreadPool"),
                             ("BgIOThr~Pool #2", "BgIOThreadPool"),
                             ("Backgro~Pool #2", "BackgroundThreadPool"),
                             ("StreamT~ns #247", "StreamTrans"),
                             ("sqldb:domain_to_categories.sqlite #8",
                              "sqldb:domain_to_categories.sqlite #7"),
                             ("Socket Thread", "Socket Thread")):
            with self.subTest(name=name):
                self.assertTrue(hang.thread_matches(name, wanted))

    def test_a_bare_prefix_does_not(self):
        # `Backgro` is also how Linux starts `BackgroundFileSaver`'s name.
        self.assertFalse(hang.thread_matches("Backgro~aver #1", "BackgroundThreadPool"))
        self.assertFalse(hang.thread_matches("Background", "BackgroundThreadPool"))
        self.assertFalse(hang.thread_matches("BgIOThreadPool #1", "BackgroundThreadPool"))
        self.assertFalse(hang.thread_matches("", "BgIOThreadPool"))


class TestAwaitedWork(unittest.TestCase):
    def test_the_busy_pool_thread_its_bucket_and_its_title(self):
        s = hang.awaited_summary(_hang([_IDLE_POOL, _SUGGEST]))
        self.assertEqual((s["kind"], s["name"], s["threads"], s["busy"], s["idle"]),
                         ("pool", "BgIOThreadPool", 2, 1, 1))
        self.assertEqual((s["thread"]["index"], s["thread"]["name"]), (2, "BgIOThreadPool #510"))
        self.assertEqual(s["bucket"], "viaduct::client::Client::send_sync | viaduct::Request::send"
                                      " | remote_settings::client::ViaductApiClient::make_request")
        # The shape Jens gave 2071528 and 2073426: the work, the pool, the blocking call. The
        # uniffi scaffolding under `ingest` is glue and does not name the work.
        self.assertEqual(s["title"], _SUGGEST_TITLE)
        # The WORK frames' files only -- no message loop, no thread start.
        self.assertIn(_AS + "viaduct/src/client.rs", s["files"])
        self.assertIn(_AS + "suggest/src/store.rs", s["files"])
        self.assertNotIn("ipc/chromium/src/base/message_loop.cc", s["files"])
        self.assertNotIn("xpcom/threads/nsThreadPool.cpp", s["files"])
        self.assertEqual(s["other_busy"], [])
        self.assertLessEqual(len(s["thread"]["frames"]), hang.MAX_FRAMES)

    def test_idle_is_no_work_frame_so_a_mutex_wait_is_busy(self):
        self.assertTrue(hang.is_idle(_IDLE_POOL["frames"]))
        self.assertTrue(hang.is_idle(_SOCKET_IDLE["frames"]))
        self.assertFalse(hang.is_idle(_SUGGEST["frames"]))
        self.assertFalse(hang.is_idle(_CUPS_MUTEX["frames"]))
        self.assertEqual(hang.bucket_key(_CUPS_MUTEX["frames"]),
                         "nsPrinterCUPS::IsCUPSVersionAtLeast | nsPrinterCUPS::SupportsColor")

    def test_busy_before_idle_and_the_other_busy_threads_are_listed(self):
        s = hang.awaited_summary(_hang([_IDLE_POOL, _CUPS_MUTEX, _SUGGEST]))
        self.assertEqual(s["thread"]["name"], "BgIOThreadPool #510")
        self.assertEqual([o["name"] for o in s["other_busy"]], ["BgIOThr~Pool #4"])
        self.assertEqual((s["busy"], s["idle"]), (2, 1))

    def test_all_idle_says_so_and_names_nothing(self):
        s = hang.awaited_summary(_hang([_IDLE_POOL]))
        self.assertEqual((s["threads"], s["busy"], s["idle"]), (1, 0, 1))
        self.assertNotIn("thread", s)
        self.assertNotIn("title", s)
        s = hang.awaited_summary(_hang([_SOCKET_IDLE], spin="default: nsHttpConnectionMgr::Shutdown"))
        self.assertEqual((s["kind"], s["name"], s["busy"]), ("thread", "Socket Thread", 0))

    def test_an_unnamed_pool_takes_every_pool_worker(self):
        s = hang.awaited_summary(_hang([_IDLE_POOL, _SUGGEST, _SOCKET_IDLE],
                                       spin="default: nsThreadPool::ShutdownWithTimeout"))
        self.assertEqual(s["threads"], 2, "the socket thread is not a pool worker")
        self.assertEqual(s["thread"]["name"], "BgIOThreadPool #510")

    def test_no_thread_of_that_name(self):
        s = hang.awaited_summary(_hang([_SUGGEST], spin="default: nsThreadPool::"
                                                        "ShutdownWithTimeout StreamTrans"))
        self.assertEqual((s["threads"], s["busy"]), (0, 0))
        self.assertIsNone(hang.awaited_summary(_hang([_SUGGEST], spin="")))

    def test_symbols_are_cleaned_the_way_socorro_does(self):
        self.assertEqual(hang.clean_symbol("suggest::store::SuggestStoreInner<suggest::rs::"
                                           "Client>::ingest<suggest::rs::Client>(suggest::Q)"),
                         "suggest::store::SuggestStoreInner<T>::ingest<T>")
        self.assertEqual(hang.clean_symbol("thread_start<unsigned int (__cdecl*)(void *),1>"),
                         "thread_start<T>")
        self.assertEqual(hang.clean_symbol(""), "")

    def test_frames_text_is_socorros_layout_with_long_functions_cut(self):
        text = hang.frames_text(hang.normalize_frames(_SUGGEST["frames"]), 3)
        lines = text.split("\n")
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "0  libsystem_kernel.dylib  __psynch_cvwait")
        self.assertIn("third_party/rust/pollster/src/lib.rs:69", lines[2])
        long = hang.frames_text(hang.normalize_frames(_SUGGEST["frames"]), 14).split("\n")[12]
        self.assertIn("...", long)
        self.assertLess(len(long), 260)


class TestThePromptFact(unittest.TestCase):
    def test_the_awaited_thread_reaches_the_crash_facts(self):
        facts = "\n".join(triage._crash_facts({"raw_crash": _hang([_IDLE_POOL, _SUGGEST])}))
        self.assertIn("AWAITED WORK", facts)
        self.assertIn("Thread 2 `BgIOThreadPool #510`, 1 idle:", facts)
        self.assertIn("viaduct::client::Client::send_sync", facts)
        self.assertIn("suggest::store::SuggestStoreInner<S>::ingest", facts)
        self.assertIn("Bucket (its first non-wait frames): viaduct::client::Client::send_sync",
                      facts)
        # The direction, stated once: the wait is the symptom, the awaited work the finding.
        self.assertIn("the wait in the analysed stack is the symptom", facts)
        self.assertIn("`<work> blocks BgIOThreadPool shutdown inside <call>`", facts)

    def test_all_idle_is_said_in_the_kinds_own_words(self):
        lines = "\n".join(triage._awaited_work_lines(_hang([_IDLE_POOL])))
        self.assertIn("1 thread in this dump, all idle in the pool's own wait", lines)
        self.assertIn("do NOT name a subsystem for it", lines)
        lines = "\n".join(triage._awaited_work_lines(
            _hang([_SOCKET_IDLE], spin="default: nsHttpConnectionMgr::Shutdown")))
        self.assertIn("waiting for Socket Thread (1 thread in this dump, idle in its own event "
                      "loop", lines)
        lines = "\n".join(triage._awaited_work_lines(
            _hang([_SUGGEST], spin="default: nsThreadPool::ShutdownWithTimeout StreamTrans")))
        self.assertIn("no thread of that name is in this dump", lines)

    def test_nothing_on_a_fault_or_an_unnamed_wait(self):
        self.assertEqual(triage._awaited_work_lines({}), [])
        self.assertEqual(triage._awaited_work_lines(
            _hang([_SUGGEST], spin="default: AsyncShutdown Spinner for quit-application")), [])
        facts = "\n".join(triage._crash_facts({"raw_crash": {"reason": "SIGSEGV"}}))
        self.assertNotIn("AWAITED WORK", facts)

    def test_an_ordinary_fault_inside_a_spin_loop_is_not_called_a_shutdown_hang(self):
        # The annotation describes nested event loops on ordinary crashes too. A recognised
        # shutdown-shaped entry is not enough to redirect analysis away from the faulting stack.
        raw = _hang([_SUGGEST], report_type="crash", signature="mozilla::Foo::Bar",
                    moz_crash_reason="MOZ_CRASH(oops)")
        facts = "\n".join(triage._crash_facts(
            {"signature": "mozilla::Foo::Bar", "raw_crash": raw}))
        self.assertNotIn("WATCHDOG / TIMEOUT CRASH", facts)
        self.assertNotIn("AWAITED WORK", facts)


def _actionable(paths, title="", decision=Decision.actionable):
    cits = [RefCitation(filename=p, line=1) for p in paths]
    return Dossier(
        candidate=Candidate(node="5017c221a10c", bug=1747526, author="Nika Layzell"),
        verdict=Verdict(decision=decision, confidence=Confidence.probable, title=title,
                        mechanism=Claim(statement="the wait has no timer", citations=cits),
                        consistency=Claim(statement="flat rate", citations=cits),
                        needinfo_draft=None if decision == Decision.actionable else "please look"),
    )


class TestTheOrchestrator(unittest.TestCase):
    def test_the_awaited_work_is_recorded_on_a_hang_and_only_there(self):
        d = _actionable(["xpcom/threads/nsThreadPool.cpp"])
        orch._record_hang_awaited_work(d, _seed(_hang([_IDLE_POOL, _SUGGEST])))
        work = d.corroborations["hang_awaited_work"]
        self.assertEqual((work["thread"]["name"], work["title"]),
                         ("BgIOThreadPool #510", _SUGGEST_TITLE))
        d = _actionable(["xpcom/threads/nsThreadPool.cpp"])
        raw = dict(_hang([_SUGGEST]), report_type="crash", moz_crash_reason=None)
        orch._record_hang_awaited_work(d, _seed(raw, signature="mozilla::Foo::Bar"))
        self.assertNotIn("hang_awaited_work", d.corroborations)
        d = _actionable(["xpcom/threads/nsThreadPool.cpp"])
        orch._record_hang_awaited_work(d, _seed(_hang([_SUGGEST], spin="")))
        self.assertNotIn("hang_awaited_work", d.corroborations)

    def _gated(self, paths, threads=(_IDLE_POOL, _SUGGEST), decision=Decision.actionable):
        d = _actionable(paths, title="kept", decision=decision)
        seed = _seed(_hang(list(threads)))
        orch._record_hang_awaited_work(d, seed)
        orch._apply_hang_wait_gate(d, seed)
        return d

    def test_a_mechanism_that_is_the_wait_is_not_actionable(self):
        # Bug 2073349's citations: nsThreadManager.cpp:214 and nsThreadPool.cpp:533/588/615.
        d = self._gated(["xpcom/threads/nsThreadManager.cpp", "xpcom/threads/nsThreadPool.cpp"])
        v = d.verdict
        self.assertEqual((v.decision, v.abstain_kind), (Decision.abstain, AbstainKind.pre_existing))
        self.assertIn("explains the main thread's wait", v.abstain_reason)
        self.assertIn("thread 2 `BgIOThreadPool #510`", v.abstain_reason)
        self.assertEqual(d.corroborations["hang_wait_not_actionable"],
                         ["xpcom/threads/nsThreadManager.cpp", "xpcom/threads/nsThreadPool.cpp"])
        # The mechanism and the title stay on the page; only the filing is gone.
        self.assertEqual(v.mechanism.statement, "the wait has no timer")
        self.assertEqual(v.title, "kept")

    def test_the_wait_code_off_the_stack_counts_as_the_wait_too(self):
        # `SpinEventLoopUntil.h` is inlined into the main thread's frames and never a frame file.
        d = self._gated(["xpcom/threads/SpinEventLoopUntil.h"])
        self.assertEqual(d.verdict.decision, Decision.abstain)

    def test_a_mechanism_on_the_awaited_thread_stands(self):
        d = self._gated(["xpcom/threads/nsThreadManager.cpp", _AS + "viaduct/src/client.rs"])
        self.assertEqual(d.verdict.decision, Decision.actionable)
        self.assertNotIn("hang_wait_not_actionable", d.corroborations)

    def test_a_mechanism_through_code_off_both_stacks_stands(self):
        # Jens's 2073426: the viaduct necko backend's timer and the Suggest blocker, neither on
        # thread 25, plus the wait. Not a wait-code-only set, so untouched.
        d = self._gated(["xpcom/threads/nsThreadManager.cpp",
                         "services/application-services/components/viaduct-necko/backend.cpp"])
        self.assertEqual(d.verdict.decision, Decision.actionable)

    def test_it_fires_when_the_awaited_threads_are_all_idle(self):
        # The catch-all with nothing visible behind it is still the catch-all.
        d = self._gated(["xpcom/threads/nsThreadPool.cpp"], threads=(_IDLE_POOL,))
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertIn("not visible in this dump", d.verdict.abstain_reason)

    def test_only_actionable_and_only_with_a_cited_mechanism(self):
        d = self._gated(["xpcom/threads/nsThreadPool.cpp"], decision=Decision.lead)
        self.assertEqual(d.verdict.decision, Decision.lead)
        # A cited mechanism whose citations name no source path (a struct layout) leaves the
        # gate nothing to compare, so it stands.
        d = _actionable(["xpcom/threads/nsThreadPool.cpp"], title="t")
        d.verdict = d.verdict.model_copy(update={"mechanism": Claim(
            statement="the wait has no timer",
            citations=[{"kind": "struct_layout", "type_name": "nsThreadPool", "offset": 8}])})
        seed = _seed(_hang([_SUGGEST]))
        orch._record_hang_awaited_work(d, seed)
        orch._apply_hang_wait_gate(d, seed)
        self.assertEqual(d.verdict.decision, Decision.actionable)
        # No spin target recorded: nothing to compare against.
        d = _actionable(["xpcom/threads/nsThreadPool.cpp"])
        orch._apply_hang_wait_gate(d, _seed(_hang([_SUGGEST], spin="")))
        self.assertEqual(d.verdict.decision, Decision.actionable)

    def test_citation_paths_come_from_filenames_and_searchfox_links(self):
        claim = Claim(statement="s", citations=[
            RefCitation(filename="/xpcom/threads/nsThreadPool.cpp", line=615),
            {"kind": "searchfox", "permalink": "https://searchfox.org/firefox-release/rev/36f4/"
                                               "xpcom/threads/nsThreadManager.cpp#210-216",
             "symbol_id": "x", "repo": "mozilla-release"},
            {"kind": "searchfox", "permalink": "https://searchfox.org/firefox-release/source/"
                                               "storage/mozStorageConnection.cpp#973-990",
             "symbol_id": "y", "repo": "mozilla-release"},
        ])
        self.assertEqual(orch._citation_paths(claim),
                         {"xpcom/threads/nsThreadPool.cpp", "xpcom/threads/nsThreadManager.cpp",
                          "storage/mozStorageConnection.cpp"})
        self.assertEqual(orch._citation_paths(None), set())

    def test_the_gates_run_it(self):
        import inspect

        src = inspect.getsource(orch.apply_deterministic_gates)
        record = "_record_hang_awaited_work(result.dossier, seed)"
        gate = "_apply_hang_wait_gate(result.dossier, seed)"
        self.assertIn(record, src)
        self.assertIn(gate, src)
        self.assertLess(src.index(record), src.index(gate))


_META = {"id": 1866944, "keywords": ["meta", "crash"], "product": "Core",
         "creation_time": "2023-11-28T03:28:48Z", "regressed_by": []}
_UI = {"uuid": "37d5021a", "signature": _SIG, "channel": "release", "product": "Firefox",
       "version": "155.0.1", "buildid": "20260903215306"}


def _preview(dossier, meta_bugs=None, ui=_UI):
    with mock.patch.object(report_bug, "resolve_product_component",
                           return_value=("Core", "XPCOM")), \
            mock.patch.object(report_bug, "fetch_crash_reason",
                              return_value={"moz_crash_reason": "Shutdown hanging",
                                            "report_type": "hang"}), \
            mock.patch.object(report_bug, "fetch_signature_stats",
                              return_value=(False, {"count": 324, "installs": 119})), \
            mock.patch("crashclouseau.models.Node.authors_for", return_value={}), \
            mock.patch.object(report_bug, "_bugzilla_user",
                              return_value={"exists": True, "nick": "nika"}):
        return report_bug.build_bug_preview(ui, {"frames": [
            {"stackpos": 0, "function": "__psynch_cvwait", "filename": "", "line": 0,
             "module": "libsystem_kernel.dylib"}]}, dossier, meta_bugs=meta_bugs)


def _dossier(**over):
    work = hang.awaited_summary(_hang([_IDLE_POOL, _SUGGEST]))
    d = {"candidate": {"node": "5017c221a10c", "bug": 1747526, "author": "Nika Layzell"},
         "corroborations": {"hang_awaited_work": work, "candidate_in_pushlog_window": False},
         "verdict": {"decision": "actionable", "confidence": "probable",
                     "mechanism": {"statement": "`BackgroundEventTarget::Shutdown()` shuts the "
                                                "pool down with no timer, so the wait is "
                                                "unbounded. More words follow here.",
                                   "citations": []},
                     "consistency": {"statement": "The rate is flat.", "citations": []}}}
    d.update(over)
    return d


class TestTheBucketBug(unittest.TestCase):
    def test_the_title_comes_from_the_model_then_the_awaited_thread_then_the_mechanism(self):
        d = _dossier()
        d["verdict"]["title"] = "  Suggest ingest blocks   BgIOThreadPool shutdown "
        self.assertEqual(report_bug.bucket_title(d),
                         "Suggest ingest blocks BgIOThreadPool shutdown")
        d["verdict"]["title"] = ""
        self.assertEqual(report_bug.bucket_title(d), _SUGGEST_TITLE)
        d["corroborations"] = {}
        self.assertEqual(report_bug.bucket_title(d),
                         "BackgroundEventTarget::Shutdown() shuts the pool down with no timer, "
                         "so the wait is unbounded")
        d["verdict"]["mechanism"]["statement"] = "short"
        d["verdict"]["consistency"]["statement"] = ""
        self.assertEqual(report_bug.bucket_title(d), "")
        self.assertEqual(report_bug.bucket_title({}), "")

    def test_the_first_sentence_is_a_title(self):
        self.assertEqual(report_bug._first_sentence(
            "The [timer](https://x) in `backend.cpp` never fires. Then more."),
            "The timer in backend.cpp never fires")
        long = "word " * 60
        cut = report_bug._first_sentence(long)
        self.assertTrue(cut.endswith("...") and len(cut) <= 154)

    def test_the_opener_names_the_tracker_and_the_signature(self):
        text = report_bug.build_bucket_opener([_META], _SIG)
        self.assertTrue(text.startswith("Bucket of bug 1866944, filed without the signature"))
        self.assertIn("`[@ {}]`".format(_SIG), text)
        self.assertIn("This bug blocks the tracker", text)
        self.assertEqual(report_bug.build_bucket_opener([], _SIG), "")

    def test_the_awaited_block_prints_the_thread_that_matters(self):
        block = report_bug.build_awaited_work_block(_dossier()["corroborations"])
        self.assertTrue(block.startswith("The thread the main thread is waiting for -- thread 2 "
                                         "`BgIOThreadPool #510` (1 idle):"))
        self.assertIn("suggest::store::SuggestStoreInner<S>::ingest", block)
        self.assertIsNone(report_bug.build_awaited_work_block({}))
        self.assertIsNone(report_bug.build_awaited_work_block(
            {"hang_awaited_work": {"threads": 1, "busy": 0}}))

    def test_a_bucket_bug_is_named_for_its_cause_without_the_signature_and_blocks_the_meta(self):
        p = _preview(_dossier(), meta_bugs=[_META])
        self.assertEqual(p["title"], _SUGGEST_TITLE)
        self.assertEqual(p["cf_crash_signature"], "")
        self.assertEqual(p["blocked"], ["clouseau", 1866944])
        self.assertEqual(p["bucket"], {"meta_bugs": [1866944],
                                       "key": "viaduct::client::Client::send_sync | viaduct::"
                                              "Request::send | remote_settings::client::"
                                              "ViaductApiClient::make_request"})
        self.assertTrue(p["comment"].startswith("Bucket of bug 1866944, filed without the "
                                                "signature"))
        self.assertIn("The thread the main thread is waiting for -- thread 2 `BgIOThreadPool "
                      "#510`", p["comment"])
        self.assertNotIn("please add it to the tracker", p["comment"])
        self.assertNotIn("Crash in [@", p["title"])

    def test_the_regressor_filing_shape_takes_the_same_bucket_form(self):
        d = _dossier(verdict={"decision": "lead", "confidence": "probable",
                              "mechanism": {"statement": "x", "citations": []},
                              "needinfo_draft": "could you look?"})
        d["corroborations"]["candidate_in_pushlog_window"] = True
        p = _preview(d, meta_bugs=[_META])
        # Release's `[new in release]` mark is a statement about the signature: off.
        self.assertEqual(p["title"], _SUGGEST_TITLE)
        self.assertEqual(p["cf_crash_signature"], "")
        self.assertIn("Bucket of bug 1866944", p["comment"])
        self.assertIn("The thread the main thread is waiting for", p["comment"])

    def test_without_a_tracker_nothing_changes(self):
        p = _preview(_dossier())
        self.assertEqual(p["title"], "Crash in [@ {}]".format(_SIG))
        self.assertEqual(p["cf_crash_signature"], "[@ {}]".format(_SIG))
        self.assertEqual(p["blocked"], ["clouseau"])
        self.assertIsNone(p["bucket"])
        self.assertNotIn("Bucket of", p["comment"])
        # The awaited thread is printed on every hang bug, tracker or not.
        self.assertIn("The thread the main thread is waiting for", p["comment"])

    def test_a_tracker_but_nothing_to_name_falls_back_to_the_plain_form(self):
        # The page preview's shape; the filer itself declines before building it.
        d = _dossier(corroborations={})
        d["verdict"]["mechanism"]["statement"] = "short"
        d["verdict"]["consistency"]["statement"] = "short"
        p = _preview(d, meta_bugs=[_META])
        self.assertEqual(p["title"], "Crash in [@ {}]".format(_SIG))
        self.assertIsNone(p["bucket"])

    def test_an_empty_signature_is_not_posted(self):
        payload = bugzilla_apply._create_payload(
            {**_PREVIEW, "cf_crash_signature": "", "blocked": ["clouseau", 1866944]}, "")
        self.assertNotIn("cf_crash_signature", payload)
        payload = bugzilla_apply._create_payload(_PREVIEW, "")
        self.assertEqual(payload["cf_crash_signature"], "[@ Foo::Bar]")


class TestTheFilerRecordsTheBucket(_Base):
    def test_the_filing_carries_the_bucket_and_its_title(self):
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(1866944, keywords=["meta"])]
        bucket = {**_PREVIEW, "title": _SUGGEST_TITLE, "cf_crash_signature": "",
                  "blocked": ["clouseau", 1866944],
                  "bucket": {"meta_bugs": [1866944], "key": "viaduct | request | make_request"}}
        with mock.patch("crashclouseau.report_bug.build_bug_preview", return_value=bucket):
            res = self._file(dossier={"candidate": {"node": "n"},
                                      "verdict": {"title": _SUGGEST_TITLE}})
        self.assertEqual((res["filed"], res["mode"]), (True, "new_bug"))
        self.assertEqual(res["bucket"], "viaduct | request | make_request")
        self.assertEqual(res["bucket_title"], _SUGGEST_TITLE)
        self.assertEqual(res["meta_bugs"], [1866944])
        self.assertEqual(self.created[0]["summary"], _SUGGEST_TITLE)
        self.assertNotIn("cf_crash_signature", self.created[0])
        # The tracker is linked in the blocks PUT with the `clouseau` alias.
        self.assertEqual(self.puts[0][1], {"blocks": {"add": ["clouseau", 1866944]}})

    def test_bucket_helpers(self):
        self.assertEqual(bugzilla_apply._bucket_of(
            {"corroborations": {"hang_awaited_work": {"bucket": "a | b"}}}), "a | b")
        self.assertEqual(bugzilla_apply._bucket_of({}), "")
        self.assertTrue(bugzilla_apply._different_bucket({"bucket": "a"}, "b"))
        self.assertFalse(bugzilla_apply._different_bucket({"bucket": "a"}, "a"))
        self.assertFalse(bugzilla_apply._different_bucket({"bucket": ""}, "b"))
        self.assertFalse(bugzilla_apply._different_bucket({}, ""))


class TestTheSpikeFilerBucketMode(_FilerBase):
    def _held(self):
        return mock.patch.object(bugzilla_apply, "_open_bugs_for_signature", return_value=[_META])

    def test_a_grounded_spike_on_a_held_signature_files_a_bucket_bug(self):
        with self._held():
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual(res["mode"], "spike_new_bug")
        self.assertEqual(res["meta_bugs"], [1866944])
        self.assertEqual(res["bucket_title"],
                         "The buffer allocator rewrite made a fresh content process OOM")
        payload = self.created[0]
        self.assertEqual(payload["summary"], res["bucket_title"])
        self.assertNotIn("cf_crash_signature", payload)
        self.assertTrue(payload["description"].startswith("Bucket of bug 1866944, filed without "
                                                          "the signature"))
        self.assertNotIn("please add it to the tracker", payload["description"])
        self.assertEqual(res["blocks"], ["clouseau", 1866944])

    def test_a_hang_takes_its_title_from_the_awaited_thread(self):
        self.brief["raw_crash"] = _hang([_IDLE_POOL, _SUGGEST])
        self.brief["is_hang"] = True
        with self._held():
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual(res["mode"], "spike_new_bug")
        self.assertEqual(self.created[0]["summary"], _SUGGEST_TITLE)
        self.assertEqual(res["bucket"], "viaduct::client::Client::send_sync | viaduct::Request::"
                                        "send | remote_settings::client::ViaductApiClient::"
                                        "make_request")
        self.assertIn("The thread the main thread is waiting for -- thread 2 `BgIOThreadPool "
                      "#510`", self.created[0]["description"])

    def test_a_later_spike_in_the_same_bucket_uses_the_bucket_bug(self):
        self.brief["raw_crash"] = _hang([_IDLE_POOL, _SUGGEST])
        self.brief["is_hang"] = True
        se.models.SpikeEscalation.prior_bug_for.return_value = 2073426
        se._bug_state.return_value = {
            "id": 2073426, "status": "NEW", "resolution": "", "resolved": None,
            "assigned_to": ""}
        bugzilla_apply._bugs_by_id.return_value = [{"id": 2073426, "resolution": ""}]

        with self._held():
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)

        self.assertEqual((res["bug"], res["mode"], res["venue_kind"]),
                         (2073426, "spike_comment", "own_bucket"))
        self.assertEqual(self.created, [])
        self.assertEqual(self.comments[0][0], 2073426)
        se.models.SpikeEscalation.prior_bug_for.assert_called_with(
            ["mozilla::Foo::Bar"],
            bucket="viaduct::client::Client::send_sync | viaduct::Request::send | "
                   "remote_settings::client::ViaductApiClient::make_request",
            bucket_title=_SUGGEST_TITLE,
        )

    def test_a_spike_with_nothing_to_name_goes_to_the_tracker_as_a_comment(self):
        for findings, grounded in ((None, False), (self.findings, False),
                                   (SpikeFindings(summary="short"), True)):
            with self.subTest(findings=findings, grounded=grounded), self._held():
                self.created.clear()
                self.comments.clear()
                res = se.file_spike_bug(_esc(), self.brief, findings, grounded=grounded)
                self.assertEqual((res["filed"], res["bug"], res["mode"], res["venue_kind"]),
                                 (True, 1866944, "spike_comment", "meta"))
                self.assertIsNone(res["needinfo"], "the tracker's people are not needinfo'd")
                self.assertEqual(self.created, [])
                self.assertIn("crash volume spiked", self.comments[0][1])

    def test_an_unnamed_memory_safety_spike_is_not_posted_to_the_public_tracker(self):
        self.brief["raw_crash"] = {
            "json_dump": {"crash_info": {"address": "0xe5e5e5e5e5e5e5e5"}}}
        with self._held():
            res = se.file_spike_bug(_esc(), self.brief, None, grounded=False)
        self.assertFalse(res["filed"])
        self.assertIn("not posted publicly", res["skipped"])
        self.assertTrue(res["memory_unsafe_signals"])
        self.assertEqual(self.comments, [])
        self.assertEqual(self.created, [])

    def test_our_public_bug_that_dropped_the_signature_is_not_a_venue(self):
        # 2071528 after Jens made it the audio-session bucket: open, public, no signature. Our
        # `nsSegmentedBuffer` spike went onto it the next day through `own_restricted`.
        with mock.patch.object(se.models.Dossier, "already_filed_for_signature",
                               return_value={"uuid": "u-0", "bug": 2071528}), \
                mock.patch.object(se, "_bug_state", return_value={
                    "id": 2071528, "status": "NEW", "resolution": "", "resolved": None,
                    "assigned_to": ""}), \
                mock.patch.object(bugzilla_apply, "_bugs_by_id",
                                  return_value=[{"id": 2071528, "resolution": ""}]):
            below = se.resolve_venue_below_public(["mozilla::Foo::Bar"], "Firefox",
                                                  "20260903093145", "tok")
            self.assertIsNone(below)
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual(res["mode"], "spike_new_bug")
        self.assertEqual(self.comments, [])

    def test_venue_note_no_longer_explains_the_tracker_away(self):
        self.assertIsNone(spike_report.venue_note(meta_bugs=[_META]))
        self.assertEqual(spike_report.spike_bucket_title(
            {"raw_crash": _hang([_SUGGEST])}, None), _SUGGEST_TITLE)
        self.assertEqual(spike_report.spike_bucket_title({"raw_crash": {}}, None), "")

    def test_an_ordinary_spike_inside_a_spin_loop_has_no_awaited_work_bucket(self):
        raw = _hang([_SUGGEST], report_type="crash", signature="mozilla::Foo::Bar",
                    moz_crash_reason="MOZ_CRASH(oops)")
        brief = {"signature": "mozilla::Foo::Bar", "raw_crash": raw}
        self.assertEqual(spike_report.spike_bucket_title(brief, None), "")
        self.assertEqual(spike_report.spike_bucket_key(brief), "")


if __name__ == "__main__":
    unittest.main()
