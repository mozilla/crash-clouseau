# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""`mcp__crash__threads`: the triage's census and selected-thread reader.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
        uv run python -m unittest tests.test_crash_threads
"""
import asyncio
import os
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau.agent import second_opinion, triage  # noqa: E402
from crashclouseau.agent.tools import crashstats, threads  # noqa: E402


def _f(function, module="xul.dll"):
    return {"function": function, "module": module}


_ROOT = [_f("BaseThreadInitThunk", "kernel32.dll"), _f("RtlUserThreadStart", "ntdll.dll")]
_RAW = {
    "signature": "shutdownhang | Foo", "report_type": "hang", "crashing_thread": 0,
    "json_dump": {"crash_info": {"crashing_thread": 3}, "threads": [
        {"thread_name": "MainThread", "frames": [_f("NtWaitForAlertByThreadId", "ntdll.dll"),
                                                 _f("mozilla::Waiter::Wait()")] + _ROOT},
        {"thread_name": "", "frames": [_f("NtAlpcSendWaitReceivePort", "ntdll.dll"),
                                       _f("LRPC_CCALL::SendReceive(_RPC_MESSAGE*)", "rpcrt4.dll"),
                                       _f("Service::Call()", "service.dll")] + _ROOT},
        {"thread_name": "", "frames": [_f("NtWaitForWorkViaWorkerFactory", "ntdll.dll"),
                                       _f("TppWorkerThread", "ntdll.dll")] + _ROOT},
        {"thread_name": "Shutdown Hang Terminator",
         "frames": [_f("mozilla::(anonymous namespace)::RunWatchdog(void*)")] + _ROOT},
    ]},
}
_CRASH = {"uuid": "u-1", "signature": "shutdownhang | Foo", "channel": "nightly",
          "product": "Firefox", "stack": "", "pin_rev": "", "raw_crash": _RAW}


def _run(**kwargs):
    return asyncio.run(threads.threads(threads.ThreadsCtx(raw=kwargs.pop("raw", _RAW)), **kwargs))


class TestTheTool(unittest.TestCase):
    def test_it_lists_the_threads_and_prints_the_analysed_stack_by_default(self):
        out = _run()
        self.assertIn("threads (4):", out)
        self.assertIn("    1 (unnamed): Service::Call | LRPC_CCALL::SendReceive [rpc-out]", out)
        self.assertIn("  idle (1): 2 (unnamed)", out)
        self.assertIn("thread 0 (MainThread) stack:", out)

    def test_any_thread_can_be_read_and_a_bad_index_falls_back(self):
        self.assertIn("thread 1 (unnamed) stack:", _run(thread=1))
        self.assertIn("Service::Call()", _run(thread=1))
        self.assertIn("thread 0 (MainThread) stack:", _run(thread=99))

    def test_no_thread_list(self):
        self.assertEqual(_run(raw={}), "(no thread list in the minidump)")

    def test_it_is_the_report_tools_thread_part(self):
        with mock.patch.object(crashstats.inspector, "get_crash_data", return_value=_RAW):
            report = asyncio.run(crashstats.report(crashstats.CrashStatsCtx(), "u-1"))
        self.assertIn(_run(), report)


class TestTheWiring(unittest.TestCase):
    def test_the_principal_and_three_roles_get_it_bound_to_the_seed(self):
        got = {}
        real = triage.build_sdk_server

        def spy(name, ctx, tools, **kwargs):
            got[name] = ctx
            return real(name, ctx, tools, **kwargs)

        with mock.patch.object(triage, "build_sdk_server", spy):
            o = triage.build_options(_CRASH, searchfox_client=object())
        self.assertIs(got["crash"].raw, _RAW)
        self.assertIn("mcp__crash__threads", o.allowed_tools)
        for role in ("crash-interpreter", "data-flow-tracer", "skeptic"):
            self.assertIn("mcp__crash__threads", o.agents[role].tools, role)
            self.assertIn("mcp__crash__threads", o.agents[role].prompt, role)
        for role in ("call-graph-explorer", "patch-scout"):
            self.assertNotIn("mcp__crash__threads", o.agents[role].tools, role)

    def test_the_second_opinion_gets_it(self):
        o = second_opinion.build_options(_CRASH, None, searchfox_client=object())
        self.assertIn("mcp__crash__threads", o.allowed_tools)
        self.assertIn("mcp__crash__threads", second_opinion._system_prompt())


class TestTheRules(unittest.TestCase):
    def test_a_missing_stack_is_checked_not_asserted(self):
        self.assertIn("Never write that a thread's stack is missing", triage._system_prompt())
        skeptic = triage.build_options(_CRASH, searchfox_client=object()).agents["skeptic"].prompt
        self.assertIn("any claim that a thread's stack is missing", skeptic)
        self.assertIn("`fail` what the dump contradicts", skeptic)

    def test_on_a_hang_an_unnamed_thread_can_hold_the_wait(self):
        text = "\n".join(triage._thread_inventory(_RAW))
        self.assertIn("an unnamed thread is not a hiding place for a Gecko subsystem", text)
        self.assertIn("On a hang, an unnamed thread can be waiting", text)
        fault = dict(_RAW, signature="mozilla::Foo", report_type="crash")
        self.assertNotIn("On a hang", "\n".join(triage._thread_inventory(fault)))


if __name__ == "__main__":
    unittest.main()
