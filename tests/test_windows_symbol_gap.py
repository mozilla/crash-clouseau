# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""Windows symbol-gap selection and job gates."""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import models, onset, sigfamily, spikes, utils  # noqa: E402
from crashclouseau.agent import spike_escalation as se  # noqa: E402
from tests.selection_helpers import (  # noqa: E402
    AgentHarness, FakeEscalation, SelectorHarness, SpikeSweepHarness, selection_row,
)

GAP = "shutdownhang | NtUserMsgWaitForMultipleObjectsEx | <unknown in combase.dll> | NdrpClientCall2"
GAP_BARE = "shutdownhang | ntdll.dll | kernelbase.dll | mozilla::MaybeLeakRefPtr<T>::~MaybeLeakRefPtr"
REAL = "shutdownhang | ZwUserMsgWaitForMultipleObjectsEx | CCliModalLoop::BlockFn"
KEPT = (
    "<unknown in mfx_media_merit_64.pdb>",
    "<unknown in mfx_mft_h264ve_64.dll> | CServerContextActivator::CreateInstance",
    "<unknown in amdxc64.dll> | NDXGI::CDevice::DestroyDriverInstance",
    "@0x0 | mozilla::wr::RendererOGL::MaybeCaptureScreenPixels",
    "libc.so | core::ptr::drop_in_place<T> | core::ptr::drop_in_place<T>",
    "libc.so.6 | PR_Lock | nssSession_EnterMonitor | find_objects",
    "__delayLoadHelper2 | _tailMerge_user32.dll | nsWindow::OnWindowPosChanged",
    # A system frame below a third-party module must not cause a decline.
    "mfx_media_merit_64.dll | mfx_mft_h264ve_64.dll | mfplat.dll | SystemFunction036",
    "<unknown in mfx_media_merit_64.pdb> | mfplat.dll | SystemFunction036",
)


class TestThePredicate(unittest.TestCase):
    def test_unresolved_windows_system_frames_are_a_gap(self):
        for sig in (GAP, GAP_BARE, "<unknown in ntdll.pdb>", "KERNELBASE.dll | foo",
                    "<unknown in api-ms-win-core-synch-l1-2-0.dll> | foo"):
            self.assertTrue(sigfamily.has_windows_symbol_gap(sig), sig)

    def test_other_unsymbolicated_tops_are_not(self):
        for sig in KEPT + (REAL, "", None):
            self.assertFalse(sigfamily.has_windows_symbol_gap(sig), sig)


class TestTheSelector(SelectorHarness, unittest.TestCase):
    def test_a_spiking_gap_signature_is_declined_as_symbol_gap(self):
        builds = [20260911212439, 20260912212439, 20260913212439, 20260914212439]
        population = {b: {REAL: (2, 2)} for b in builds[:3]}
        population[builds[3]] = {REAL: (40, 30), GAP: (40, 30)}
        data, selection, rising = self._collect(population, builds)

        self.assertNotIn(GAP, data)
        mine = [r for r in selection if r["signature"] == GAP]
        self.assertEqual({r["outcome"] for r in mine}, {utils.SYMBOL_GAP})
        self.assertTrue(all(r["picked"] is None and not r["evaluable"] for r in mine))
        self.assertIn(REAL, data)
        self.assertIn(GAP, rising.call_args.args[4])


class TestTheAgentGates(AgentHarness, unittest.TestCase):
    def test_enqueue_agent_returns_without_a_job_for_a_gap(self):
        gq, queue = self._enqueue(GAP)
        gq.assert_not_called()
        queue.enqueue_call.assert_not_called()

    def test_enqueue_agent_still_enqueues_a_symbolicated_signature(self):
        _, queue = self._enqueue(REAL)
        queue.enqueue_call.assert_called_once()

    def test_a_queued_run_on_a_gap_does_not_spend(self):
        self._run(GAP).assert_not_called()

    def test_a_queued_run_on_a_symbolicated_signature_builds_a_seed(self):
        self._run(REAL).assert_called_once_with("u-fenix")


class TestTheSpikeSweep(SpikeSweepHarness, unittest.TestCase):
    def test_a_gap_signature_is_never_escalated(self):
        self.assertEqual(self._sweep([selection_row(32, [1, 0, 2], 21, signature=GAP)]), 0)
        self.assertEqual(self.created, [])
        spikes.judge_selection.assert_not_called()

    def test_a_deferred_gap_row_is_closed_not_restarted(self):
        row = FakeEscalation(id=7, signature=GAP_BARE, payload={}, status="deferred",
                             attempts=0, build_day="2026-11-01")
        with mock.patch.object(models.SpikeEscalation, "deferred", return_value=[row]):
            self.assertEqual(se._restart("Firefox", "nightly", self.cfg, 2), 0)
        self.assertEqual(self.enqueued, [])
        self.assertEqual(row.status, "done")
        self.assertFalse(row.payload["filing"]["filed"])

    def test_a_filing_recorded_before_the_worker_died_is_kept(self):
        filing = {"filed": True, "bug": 2093074, "mode": "new"}
        row = FakeEscalation(id=10, signature=GAP, payload={"filing": dict(filing)},
                             status="error", attempts=1, build_day="2026-11-01")
        with mock.patch.object(models.SpikeEscalation, "retryable", return_value=[row]):
            self.assertEqual(se._restart("Firefox", "nightly", self.cfg, 2), 0)
        self.assertEqual(row.status, "done")
        self.assertEqual(row.payload["filing"], filing)

    def test_a_deferred_real_row_is_still_restarted(self):
        row = FakeEscalation(id=8, signature=REAL, payload={}, status="deferred",
                             attempts=0, build_day="2026-11-01")
        with mock.patch.object(models.SpikeEscalation, "deferred", return_value=[row]):
            self.assertEqual(se._restart("Firefox", "nightly", self.cfg, 2), 1)
        self.assertEqual(self.enqueued, [8])

    def test_a_queued_gap_job_does_not_spend(self):
        row = FakeEscalation(id=9, signature=GAP, payload={}, status="pending",
                             attempts=0, build_day="2026-11-01")
        with mock.patch.object(models.SpikeEscalation, "get", return_value=row), \
                mock.patch.object(se, "build_spike_brief",
                                  side_effect=AssertionError("the job built a brief")):
            se.run_spike_escalation(9)
        self.assertEqual(row.status, "done")
        self.assertEqual(row.attempts, 0)

    def test_a_symbolicated_signature_is_still_escalated(self):
        self.assertEqual(self._sweep([selection_row(32, [1, 0, 2], 21, signature=REAL)]), 1)
        self.assertEqual(self.created[0].signature, REAL)
        self.assertEqual(self.enqueued, [1])


class TestTheOnsetRule(unittest.TestCase):
    def test_a_gap_signature_is_shortlisted_but_never_fetched(self):
        rule = {"hours": 24, "baseline_days": 7, "first": {}, "second": {}}
        counts = {GAP: (50, 40), REAL: (50, 40)}
        with mock.patch.object(onset, "_signature_counts",
                               side_effect=lambda p, ch, s, e, recent: counts if recent else {}), \
                mock.patch.object(onset.report_bug, "meets_onset", return_value=True), \
                mock.patch.object(onset.report_bug, "fetch_channel_onsets",
                                  return_value={}) as fetch:
            onset._find("Firefox", rule, ["nightly", "release"], datetime(2026, 11, 16))
        asked = [c.args[0]["signature"] for c in fetch.call_args_list]
        self.assertEqual(asked, [REAL])


if __name__ == "__main__":
    unittest.main()
