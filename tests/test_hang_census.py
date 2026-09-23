# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""`hang.census`: which threads of a dump are not idle, what they wait on, and their ranking.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
        uv run python -m unittest tests.test_hang_census

`tests/hang/census_panel.json` contains 283 threads labelled idle, 71 labelled busy,
and 57 reports with selected awaited threads. Labels are fixture expectations, not
proof of runtime state. The bounds below are measured on this fixture.
"""
import asyncio
import json
import os
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau import hang  # noqa: E402
from crashclouseau.agent import triage  # noqa: E402
from crashclouseau.agent.tools import crashstats  # noqa: E402

_PANEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hang", "census_panel.json")


def _f(function, module="xul.dll"):
    return {"function": function, "module": module}


_WIN_ROOT = [_f("BaseThreadInitThunk", "kernel32.dll"), _f("RtlUserThreadStart", "ntdll.dll")]
_POOL_IDLE = [_f("ZwWaitForWorkViaWorkerFactory", "ntdll.dll"), _f("TppWorkerThread", "ntdll.dll")]
_HOST_STA_IDLE = [_f("NtUserGetMessage", "win32u.dll"), _f("GetMessageW", "user32.dll"),
                  _f("CDllHost::STAWorkerLoop()", "combase.dll"),
                  _f("CDllHost::WorkerThread()", "combase.dll"),
                  _f("CRpcThread::WorkerLoop()", "combase.dll"),
                  _f("CRpcThreadCache::RpcWorkerThreadEntry(void*)", "combase.dll")]
_IO_LOOP_IDLE = [_f("NtRemoveIoCompletion", "ntdll.dll"),
                 _f("GetQueuedCompletionStatus", "kernelbase.dll"),
                 _f("base::MessagePumpForIO::DoRunLoop()"),
                 _f("base::MessagePumpWin::Run(base::MessagePump::Delegate*)"),
                 _f("MessageLoop::RunHandler()"), _f("base::Thread::ThreadMain()"),
                 _f("(anonymous namespace)::ThreadFunc(void*)")]
_RAYON_IDLE = [{"module": "libc.so.6"}, _f("std::sys::sync::condvar::futex::Condvar::wait", "libxul.so"),
               _f("rayon_core::registry::WorkerThread::wait_until_cold", "libxul.so"),
               _f("rayon_core::registry::ThreadBuilder::run", "libxul.so"),
               _f("start_thread", "libc.so.6"), _f("__clone3", "libc.so.6")]
# A COM host STA serving an incoming call that is itself waiting.
_SERVING = [_f("ZwWaitForAlertByThreadId", "ntdll.dll"),
            _f("SleepConditionVariableSRW", "kernelbase.dll"),
            _f("Windows::Internal::Shell::TaskFlow::DataEngine::OffloadToWorkerThreadIfNeeded",
               "TaskFlowDataEngine.dll"),
            _f("CApplicationDestinations::RemoveAllDestinations(void)", "windows.storage.dll"),
            _f("CStdStubBuffer_Invoke", "combase.dll"),
            _f("ComInvokeWithLockAndIPID(ServerCall*, tagIPIDEntry*, bool*)", "combase.dll"),
            _f("ThreadWndProc", "combase.dll"), _f("DispatchMessageW", "user32.dll"),
            _f("CDllHost::STAWorkerLoop()", "combase.dll")] + _WIN_ROOT
_MTA_OUT = [_f("ZwWaitForMultipleObjects", "ntdll.dll"),
            _f("WaitForMultipleObjectsEx", "kernelbase.dll"),
            _f("MTAThreadWaitForCall(CSyncClientCall*, WaitForCallReason, unsigned long)",
               "combase.dll"),
            _f("CSyncClientCall::SendReceive(tagRPCOLEMESSAGE*, unsigned long*)", "combase.dll"),
            _f("ObjectStubless()", "combase.dll"), _f("JumpListWork()"),
            _f("mozilla::TaskQueue::Runner::Run()"), _f("nsThreadPool::Run()")] + _WIN_ROOT
_ACTIVATION = [_f("NtAlpcSendWaitReceivePort", "ntdll.dll"),
               _f("LRPC_BASE_CCALL::DoSendReceive(void)", "rpcrt4.dll"),
               _f("ObjectStubless()", "combase.dll"),
               _f("CRpcResolver::DelegateActivationToSCM(bool)", "combase.dll"),
               _f("CoCreateInstance", "combase.dll"),
               _f("Helpers::TryGetService(void)", "Shell.dll")] + _WIN_ROOT
_LOCK = [_f("RtlpWaitOnCriticalSection", "ntdll.dll"), _f("RtlEnterCriticalSection", "ntdll.dll"),
         _f("mozilla::storage::SQLiteMutex::lock()"), _f("Work()"), _f("nsThreadPool::Run()")]


def _raw(threads, crashing=None, signature="shutdownhang | Foo"):
    """A hang report: thread 0 is the analysed main thread, *crashing* the watchdog."""
    crashing = len(threads) - 1 if crashing is None else crashing
    return {"signature": signature, "report_type": "hang", "crashing_thread": 0,
            "json_dump": {"crash_info": {"crashing_thread": crashing}, "threads": threads}}


def _watchdog():
    return {"thread_name": "Shutdown Hang Terminator",
            "frames": [_f("mozilla::(anonymous namespace)::RunWatchdog(void*)")] + _WIN_ROOT}


class TestTheVocabulary(unittest.TestCase):
    def test_os_and_runtime_waits_for_work_are_idle(self):
        for frames in (_POOL_IDLE + _WIN_ROOT, _HOST_STA_IDLE + _WIN_ROOT, _IO_LOOP_IDLE + _WIN_ROOT,
                       _RAYON_IDLE):
            self.assertTrue(hang.is_idle(frames), frames[0])

    def test_the_x86_syscall_stub_is_skipped(self):
        frames = [_f("KiFastSystemCallRet", "ntdll.dll")] + _POOL_IDLE + _WIN_ROOT
        self.assertTrue(hang.is_idle(frames))
        self.assertEqual(hang.bucket_key([_f("KiFastSystemCallRet", "ntdll.dll")] + _LOCK),
                         hang.bucket_key(_LOCK))

    def test_an_unsymbolised_os_frame_says_nothing_but_a_foreign_one_is_work(self):
        self.assertTrue(hang.is_idle(_RAYON_IDLE))
        self.assertFalse(hang.is_idle([{"module": "injected.dll"}] + _POOL_IDLE + _WIN_ROOT))

    def test_frames_below_the_thread_root_are_not_work(self):
        mac = [_f("__psynch_cvwait", "libsystem_kernel.dylib"), _f("TimerThread::Run()", "XUL"),
               _f("_pthread_start", "libsystem_pthread.dylib"),
               _f("thread_start", "libsystem_pthread.dylib"), _f("pt_SetSocketOption", "libnss3.dylib")]
        self.assertEqual(hang.bucket_key(mac), "TimerThread::Run")

    def test_the_anonymous_namespace_is_a_scope(self):
        self.assertEqual(hang.clean_symbol("mozilla::(anonymous namespace)::RunWatchdog(void*)"),
                         "mozilla::(anonymous namespace)::RunWatchdog")
        self.assertEqual(hang.clean_symbol("(anonymous namespace)::ThreadFunc(void*)"),
                         "(anonymous namespace)::ThreadFunc")
        self.assertEqual(hang.clean_symbol("Str(JSContext*, (anonymous namespace)::X)"), "Str")

    def test_a_host_sta_serving_a_call_is_busy(self):
        self.assertFalse(hang.is_idle(_SERVING))
        self.assertTrue(hang.serving_com(_SERVING))
        self.assertFalse(hang.serving_com(_MTA_OUT))


class TestWaitKinds(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(hang.wait_kind(_MTA_OUT), "com-out-mta")
        self.assertEqual(hang.wait_kind(_ACTIVATION), "activation")
        self.assertEqual(hang.wait_kind(_LOCK), "lock")
        self.assertEqual(hang.wait_kind(_SERVING), "wait")
        self.assertEqual(hang.wait_kind([_f("NtFlushBuffersFile", "ntdll.dll"), _f("Work()")]),
                         "file-io")
        self.assertEqual(hang.wait_kind([_f("NtAlpcSendWaitReceivePort", "ntdll.dll"),
                                         _f("LRPC_CCALL::SendReceive(_RPC_MESSAGE*)", "rpcrt4.dll"),
                                         _f("Work()")]), "rpc-out")
        self.assertEqual(hang.wait_kind([_f("memcpy", "vcruntime140.dll"), _f("Work()")]), "running")


class TestTheMainThread(unittest.TestCase):
    _MACHINERY = [_f("nsThreadManager::SpinEventLoopUntilInternal(nsTSubstring<char> const&)")]

    def test_a_message_wait_in_the_event_loop_is_parked(self):
        frames = [_f("NtUserMsgWaitForMultipleObjectsEx", "win32u.dll"),
                  _f("MsgWaitForMultipleObjectsEx", "user32.dll"),
                  _f("nsAppShell::ProcessNextNativeEvent(bool)"),
                  _f("NS_ProcessNextEvent(nsIThread*, bool)")] + self._MACHINERY
        self.assertIsNone(hang.main_work(frames))

    def test_a_message_wait_in_the_com_modal_loop_is_the_callers_work(self):
        frames = [_f("ZwUserMsgWaitForMultipleObjectsEx", "win32u.dll"),
                  _f("RealMsgWaitForMultipleObjectsEx", "user32.dll"),
                  _f("CCliModalLoop::BlockFn(void**, unsigned long, unsigned long*)", "combase.dll"),
                  _f("ModalLoop(CSyncClientCall*)", "combase.dll"),
                  _f("ObjectStubless()", "combase.dll"),
                  _f("mozilla::widget::ToastNotification::CloseAlert(nsTSubstring<char16_t> const&)")
                  ] + self._MACHINERY
        found = hang.main_work(frames)
        self.assertEqual(hang.clean_symbol(found["frames"][-1]["function"]),
                         "mozilla::widget::ToastNotification::CloseAlert")

    def test_the_x86_stub_does_not_make_a_parked_main_thread_run(self):
        frames = [_f("KiFastSystemCallRet", "ntdll.dll"), _f("NtFlushBuffersFile", "ntdll.dll"),
                  _f("nsCycleCollector::FreeSnowWhite(bool)")] + self._MACHINERY
        self.assertIsNone(hang.main_work(frames))


class TestTheCensus(unittest.TestCase):
    def _census(self, common=frozenset()):
        threads = [
            {"thread_name": "MainThread", "frames": _LOCK},
            {"thread_name": "Timer", "frames": [_f("NtWaitForMultipleObjects", "ntdll.dll"),
                                                _f("TimerThread::Run()"),
                                                _f("nsThread::ThreadFunc(void*)")] + _WIN_ROOT},
            {"thread_name": "Breakpad ExceptionHandler",
             "frames": [_f("NtGetContextThread", "ntdll.dll"),
                        _f("google_breakpad::ExceptionHandler::ExceptionHandlerThreadMain(void*)")]},
            {"thread_name": "", "frames": _POOL_IDLE + _WIN_ROOT},
            {"thread_name": "Pool #1", "frames": _LOCK},
            {"thread_name": "", "frames": _ACTIVATION},
            {"thread_name": "Empty", "frames": []},
            _watchdog(),
        ]
        with mock.patch.object(hang, "_COMMON_STATES", common):
            return hang.census(_raw(threads))

    def test_rows_skip_the_subjects_and_rank_by_what_they_wait_on(self):
        c = self._census()
        self.assertEqual(c["skipped"], {0: "analysed thread", 7: "crashing thread",
                                        2: "crash reporter"})
        self.assertEqual([r["index"] for r in c["rows"]], [5, 4, 1])
        self.assertEqual([r["kind"] for r in c["rows"]], ["activation", "lock", "wait"])
        self.assertEqual(c["idle"], [(3, "")])
        self.assertEqual(c["no_stack"], [6])

    def test_a_common_state_is_ranked_last_not_dropped(self):
        activation = hang.state_key(_ACTIVATION)
        c = self._census(common=frozenset({activation}))
        self.assertEqual([r["index"] for r in c["rows"]], [4, 1, 5])
        self.assertTrue(c["rows"][-1]["common"])

    def test_a_row_reads_index_name_work_call_and_tags(self):
        row = {"index": 40, "name": "", "work": "ComInvokeWithLockAndIPID", "call": "Offload",
               "kind": "wait", "serving_com": True, "common": False}
        self.assertEqual(hang.census_row(row),
                         "40 (unnamed): ComInvokeWithLockAndIPID | Offload [wait, serving a COM call]")

    def test_no_thread_list_is_none(self):
        self.assertIsNone(hang.census({"json_dump": {}}))

    def test_the_shipped_table_loads(self):
        with open(hang._STATES_PATH) as handle:
            table = json.load(handle)
        self.assertGreater(len(table["states"]), 100)
        self.assertGreaterEqual(min(table["states"].values()), table["min_signatures"])
        with mock.patch.object(hang, "_COMMON_STATES", None):
            self.assertEqual(hang.common_states(), frozenset(table["states"]))
        with mock.patch.object(hang, "_COMMON_STATES", None), \
                mock.patch.object(hang, "_STATES_PATH", "/nonexistent/thread_states.json"):
            self.assertEqual(hang.common_states(), frozenset())


class TestTheWiring(unittest.TestCase):
    def _hang_raw(self):
        threads = [{"thread_name": "MainThread", "frames": _LOCK},
                   {"thread_name": "", "frames": _ACTIVATION}, _watchdog()]
        return _raw(threads, signature="shutdownhang | Foo")

    def test_a_hang_carries_the_census_and_a_fault_does_not(self):
        raw = self._hang_raw()
        text = "\n".join(triage._crash_facts({"signature": raw["signature"], "raw_crash": raw}))
        self.assertIn("THREADS NOT IDLE (1 of 3, common states last", text)
        self.assertIn("1 (unnamed): Helpers::TryGetService | LRPC_BASE_CCALL::DoSendReceive "
                      "[activation]", text)
        fault = dict(raw, signature="mozilla::Foo", report_type="crash")
        text = "\n".join(triage._crash_facts({"signature": "mozilla::Foo", "raw_crash": fault}))
        self.assertNotIn("THREADS NOT IDLE", text)

    def test_the_report_tool_lists_the_census(self):
        with mock.patch.object(crashstats.inspector, "get_crash_data", return_value=self._hang_raw()):
            out = asyncio.run(crashstats.report(crashstats.CrashStatsCtx(), "u-1"))
        self.assertIn("threads (3):", out)
        self.assertIn("  0 MainThread: analysed thread", out)
        self.assertIn("  not idle (1), ranked", out)
        self.assertIn("    1 (unnamed): Helpers::TryGetService", out)


class TestTheLabelledPanel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(_PANEL) as handle:
            cls.panel = json.load(handle)
        cls.strings = [s.split("\t", 1) for s in cls.panel["strings"]]

    def frames(self, refs):
        return [{"module": self.strings[i][0], "function": self.strings[i][1]} for i in refs]

    def test_busy_threads_stay_busy(self):
        busy = [t for t in self.panel["threads"] if t["label"] == "B"]
        self.assertEqual(len(busy), 71)
        missed = [t["cell"] for t in busy if hang.is_idle(self.frames(t["frames"]))]
        self.assertEqual(missed, [])

    def test_idle_threads_called_busy_do_not_grow(self):
        idle = [t for t in self.panel["threads"] if t["label"] == "I"]
        self.assertEqual(len(idle), 283)
        wrong = sum(1 for t in idle if not hang.is_idle(self.frames(t["frames"])))
        self.assertLessEqual(wrong, 94)

    def test_the_awaited_thread_ranks_near_the_top(self):
        ranks = []
        for report in self.panel["reports"]:
            threads = [{"thread_name": "", "frames": []} for _ in range(report["count"])]
            for i, t in report["threads"].items():
                threads[int(i)] = {"thread_name": t["name"], "frames": self.frames(t["frames"])}
            raw = {"signature": report["signature"], "report_type": "hang",
                   "crashing_thread": report["analysed"],
                   "json_dump": {"crash_info": {"crashing_thread": report["crashing"]},
                                 "threads": threads}}
            order = [r["index"] for r in hang.census(raw)["rows"]]
            ranks.append(min(order.index(i) for i in report["awaited"] if i in order) + 1)
        self.assertEqual(len(ranks), 57)
        self.assertGreaterEqual(sum(1 for r in ranks if r <= 5), 51)
        self.assertGreaterEqual(sum(1 for r in ranks if r <= 10), 55)


if __name__ == "__main__":
    unittest.main()
