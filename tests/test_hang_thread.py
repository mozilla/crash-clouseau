# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Bug 2064436: the pipeline analysed a shutdown hang's WATCHDOG thread, never showed any model the
# process's thread list, and filed a mechanism about a "MediaTrackGrph" thread the process never
# had. Three fixes, tested here:
#
#   1. `inspector.thread_for_analysis` -- analyse the hung main thread, not the watchdog.
#   2. `triage._thread_inventory` + the shutdown facts -- put the thread list and the blocked
#      spin-event-loop stack in front of BOTH the agent and the blind second opinion.
#   3. `orchestrator._apply_absent_thread_gate` -- a backstop clamp when 1 and 2 are ignored.
#
#     DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#         uv run python -m unittest tests.test_hang_thread
import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau import inspector  # noqa: E402
from crashclouseau.agent import orchestrator, triage  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    Candidate, Claim, Confidence, Decision, Dossier, RefCitation, Verdict,
)

# The two threads that matter on crash ec1ff67a-a835-4740-be14-572e50260818, verbatim in shape:
# thread 0 is the hung main thread, thread 2 the watchdog that crashed on purpose.
_MAIN = {
    "thread_name": "MainThread",
    "frames": [
        {"frame": 0, "module": "ntdll.dll", "function": "ZwWaitForAlertByThreadId"},
        {"frame": 1, "module": "xul.dll", "function": "nsThreadPool::ShutdownWithTimeout(int)",
         "file": "hg:hg.mozilla.org/mozilla-central:xpcom/threads/nsThreadPool.cpp:abcdef123456",
         "line": 615, "inlines": [{"function": "SpinEventLoopUntil"}]},
    ],
}
_WATCHDOG = {
    "thread_name": "Shutdown Hang Terminator",
    "frames": [
        {"frame": 0, "module": "xul.dll",
         "function": "mozilla::(anonymous namespace)::RunWatchdog(void*)",
         "file": "hg:hg.mozilla.org/mozilla-central:toolkit/components/terminator/"
                 "nsTerminator.cpp:abcdef123456", "line": 300,
         "inlines": [{"function": "MOZ_Crash(char const*, int, char const*)"}]},
        {"frame": 1, "module": "nss3.dll", "function": "_PR_NativeRunThread(void*)"},
    ],
}
_OTHER = {"thread_name": "AudioIPC Server RPC", "frames": [{"frame": 0, "function": "recv"}]}


def _hang(**over):
    """A shutdownhang processed crash: watchdog at index 2, hung main thread at 0."""
    data = {
        "report_type": "hang",
        "crashing_thread": 0,                 # Socorro's own answer: the main thread
        "crashing_thread_name": "Shutdown Hang Terminator",
        "shutdown_progress": "xpcom-shutdown-threads",
        "shutdown_reason": "AppClose",
        "xpcom_spin_event_loop_stack": "default: nsThreadPool::ShutdownWithTimeout BgIOThreadPool",
        "moz_crash_reason": "Shutdown hanging at step XPCOMShutdownThreads.",
        "json_dump": {
            "crash_info": {"crashing_thread": 2, "type": "EXCEPTION_BREAKPOINT"},
            "threads": [_MAIN, _OTHER, _WATCHDOG],
        },
    }
    data.update(over)
    return data


class TestThreadForAnalysis(unittest.TestCase):
    def test_a_hang_is_analysed_on_the_thread_socorro_points_at(self):
        # THE bug. `crash_info.crashing_thread` is the watchdog, which crashed deliberately and
        # whose stack is pure boilerplate; Socorro's top-level field names the hung main thread,
        # and that is the thread the `shutdownhang | ...` signature we triage is generated from.
        self.assertEqual(inspector.thread_for_analysis(_hang()), 0)

    def test_an_ordinary_crash_is_untouched(self):
        # Narrow on purpose: only `report_type == "hang"` may override the faulting thread.
        self.assertEqual(inspector.thread_for_analysis(_hang(report_type="crash")), 2)
        self.assertEqual(inspector.thread_for_analysis(_hang(report_type=None)), 2)

    def test_a_hang_whose_crashing_thread_is_not_the_watchdog_is_untouched(self):
        # All three conditions are required. Without the RunWatchdog check this would follow the
        # top-level field on any hang, including ones where the two disagree for another reason.
        data = _hang()
        data["json_dump"]["threads"][2] = {
            "thread_name": "Some Other Thread", "frames": [{"function": "Foo::Bar"}]}
        self.assertEqual(inspector.thread_for_analysis(data), 2)

    def test_the_watchdog_is_matched_on_its_frame_not_its_name(self):
        # Linux caps a pthread name at 15 bytes and Socorro elides the middle, so
        # "Shutdown Hang Terminator" arrives as "Shutdow~minator" -- a name match would miss it
        # on every Linux hang. The symbolized frame is stable.
        data = _hang(crashing_thread_name="Shutdow~minator")
        data["json_dump"]["threads"][2] = dict(_WATCHDOG, thread_name="Shutdow~minator")
        self.assertEqual(inspector.thread_for_analysis(data), 0)

    def test_agreeing_fields_and_bad_indices_fall_back(self):
        for over in ({"crashing_thread": 2}, {"crashing_thread": 99},
                     {"crashing_thread": None}, {"crashing_thread": "0"}):
            self.assertEqual(inspector.thread_for_analysis(_hang(**over)), 2, over)

    def test_no_threads_or_no_dump_never_raises(self):
        self.assertIsNone(inspector.thread_for_analysis({}))
        self.assertIsNone(inspector.thread_for_analysis({"json_dump": {}}))
        self.assertEqual(
            inspector.thread_for_analysis(
                {"json_dump": {"crash_info": {"crashing_thread": 7}, "threads": []}}), 7)

    def test_the_stack_scored_is_the_hung_thread(self):
        # End to end through the function that feeds file scoring, the crashstack hash and the
        # agent's stack: the watchdog contributes only `nsTerminator.cpp`, which no window
        # changeset touches, so the whole crash went off-stack with no stack signal at all.
        frames, files = inspector.inspect_stacktrace(_hang(), "abcdef123456")
        self.assertEqual([f["function"] for f in frames],
                         ["ZwWaitForAlertByThreadId", "nsThreadPool::ShutdownWithTimeout(int)"])
        self.assertEqual(files, {"xpcom/threads/nsThreadPool.cpp"})
        _, old_files = inspector.inspect_stacktrace(_hang(report_type="crash"), "abcdef123456")
        self.assertEqual(old_files, {"toolkit/components/terminator/nsTerminator.cpp"})

    def test_inlines_come_from_the_analysed_thread(self):
        # A wrong fact dressed as a grounded one: inlines are keyed by stack POSITION, so reading
        # them off the watchdog would graft `MOZ_Crash` onto the main thread's frame 0. Socorro's
        # real payload expands `json_dump.crashing_thread` into the full thread OBJECT, so the
        # dict shape has to lose to the index.
        data = _hang()
        data["json_dump"]["crashing_thread"] = _WATCHDOG
        self.assertEqual(orchestrator._inlines_by_stackpos(data), {1: ["SpinEventLoopUntil"]})

    def test_inlines_still_read_the_legacy_shapes(self):
        # No `threads` list to index into: fall back to the expanded dict, then to the int index.
        self.assertEqual(
            orchestrator._inlines_by_stackpos({"json_dump": {"crashing_thread": _WATCHDOG}}),
            {0: ["MOZ_Crash(char const*, int, char const*)"]})
        self.assertEqual(
            orchestrator._inlines_by_stackpos(
                {"json_dump": {"crashing_thread": 1, "threads": [_OTHER, _MAIN]}}),
            {1: ["SpinEventLoopUntil"]})


class TestHangFacts(unittest.TestCase):
    def _facts(self, raw=None):
        return "\n".join(triage._crash_facts({"raw_crash": raw if raw is not None else _hang()}))

    def test_the_hang_fields_reach_the_prompt(self):
        # None of these had ever reached a prompt. The spin-loop stack is the highest-value line
        # available on a shutdown hang: Socorro naming the pool the main thread is parked on.
        facts = self._facts()
        self.assertIn("Report type: hang", facts)
        self.assertIn("Shutdown phase reached: xpcom-shutdown-threads", facts)
        self.assertIn("Why shutdown started: AppClose", facts)
        self.assertIn("default: nsThreadPool::ShutdownWithTimeout BgIOThreadPool", facts)
        self.assertIn("BLOCKED SPIN-EVENT-LOOP STACK", facts)

    def test_the_analysed_thread_is_labelled_not_the_crashing_one(self):
        # Printing "45" above thread 0's frames is worse than printing nothing.
        self.assertIn("Analysed thread (the stack below is THIS thread): 0 (MainThread)",
                      self._facts())

    def test_the_thread_inventory_reaches_the_prompt(self):
        # Andreas Pehrson's whole refutation is a lookup in this list.
        facts = self._facts()
        self.assertIn("THREADS IN THIS PROCESS (3 threads, 3 distinct names)", facts)
        self.assertIn("MainThread, AudioIPC Server RPC, Shutdown Hang Terminator", facts)
        self.assertIn("This list is COMPLETE", facts)
        self.assertIn("REFUTED, not merely unproven", facts)

    def test_unnamed_threads_are_counted_not_listed(self):
        data = _hang()
        data["json_dump"]["threads"] += [{"frames": []}, {"thread_name": None, "frames": []}]
        self.assertIn("(5 threads, 3 distinct names, 2 unnamed)", self._facts(data))

    def test_duplicate_names_are_listed_once(self):
        data = _hang()
        data["json_dump"]["threads"] += [dict(_OTHER), dict(_OTHER)]
        facts = self._facts(data)
        self.assertIn("(5 threads, 3 distinct names)", facts)
        self.assertEqual(facts.count("AudioIPC Server RPC"), 1)

    def test_a_truncated_inventory_withdraws_the_absence_claim(self):
        # The absence is the load-bearing half, and it is only sound over a complete list. An
        # agent reasoning from a silently clipped list would make bug 2064436's mistake again,
        # with our encouragement.
        data = _hang()
        data["json_dump"]["threads"] = [
            {"thread_name": "T%d" % i, "frames": []}
            for i in range(triage._MAX_THREAD_NAMES + 5)]
        facts = self._facts(data)
        self.assertIn("This list is TRUNCATED", facts)
        self.assertIn("CANNOT be used to argue that a thread is absent", facts)
        self.assertNotIn("This list is COMPLETE", facts)

    def test_no_inventory_when_there_is_nothing_to_list(self):
        for raw in ({}, {"json_dump": {}}, {"json_dump": {"threads": []}},
                    {"json_dump": {"threads": [{"frames": []}]}}):
            self.assertEqual(triage._thread_inventory(raw), [], raw)

    def test_the_second_opinion_sees_all_of_it(self):
        # These are FACTS both models were blind to, not an archetype hint: the blind second
        # opinion is the calibrated refuter (specificity 1.00) and was equally blind here. It
        # shares `_crash_facts` verbatim, so this pins that the sharing is real.
        from crashclouseau.agent import second_opinion

        prompt = second_opinion._user_prompt(
            {"uuid": "u-1", "signature": "shutdownhang | x", "raw_crash": _hang()}, None)
        self.assertIn("THREADS IN THIS PROCESS", prompt)
        self.assertIn("BLOCKED SPIN-EVENT-LOOP STACK", prompt)
        self.assertNotIn("KNOWN ARCHETYPES", prompt)


_CITE = [RefCitation(filename="dom/media/MediaTrackGraph.cpp", line=2042)]
# The mechanism as actually filed on bug 2064436, verbatim.
_FILED_MECHANISM = (
    "`XPCOMShutdownThreads` joins every `nsThread`-based pool including the "
    "`\"MediaTrackGrph\"` thread owned by `ThreadedDriver`; that thread is only told to shut "
    "down once `MediaTrackGraphImpl::RunInStableState` decides the queues are empty."
)


def _dossier(statement, decision=Decision.lead, confidence=Confidence.probable):
    return Dossier(
        candidate=Candidate(node="e7ad1bf72931", bug=1993981),
        verdict=Verdict(decision=decision, confidence=confidence, needinfo_draft="please look",
                        mechanism=Claim(statement=statement, citations=_CITE),
                        consistency=Claim(statement="fits the evidence", citations=_CITE)))


class TestAbsentThreadGate(unittest.TestCase):
    def _run(self, statement, **kw):
        dossier = _dossier(statement, **kw)
        orchestrator._apply_absent_thread_gate(dossier, {"uuid": "u-1", "raw_crash": _hang()})
        return dossier

    def test_the_filed_mechanism_is_clamped_below_the_filing_threshold(self):
        # Bug 2064436 shipped at rung 70 (`probable`), which is what files a bug.
        dossier = self._run(_FILED_MECHANISM)
        self.assertEqual(dossier.verdict.confidence, Confidence.medium)
        self.assertEqual(dossier.verdict.decision, Decision.lead)
        self.assertEqual(dossier.corroborations["absent_named_threads"], ["MediaTrackGrph"])
        self.assertIs(dossier.corroborations["absent_thread_clamped"], True)

    def test_strong_evidence_is_downgraded_to_a_lead(self):
        dossier = self._run(_FILED_MECHANISM, decision=Decision.strong_evidence,
                            confidence=Confidence.high)
        self.assertEqual(dossier.verdict.decision, Decision.lead)
        self.assertEqual(dossier.verdict.confidence, Confidence.medium)
        self.assertIs(dossier.corroborations["downgraded_from_strong"], True)

    def test_never_below_a_reportable_lead(self):
        # A clamp, not a suppression: the check reads prose and can be wrong about a thread that
        # legitimately lives in ANOTHER process, so a false fire must cost the automatic FILING
        # and not the lead. The finding is still recorded.
        dossier = self._run(_FILED_MECHANISM, confidence=Confidence.medium)
        self.assertEqual(dossier.verdict.confidence, Confidence.medium)
        self.assertEqual(dossier.verdict.decision, Decision.lead)
        self.assertIn("absent_named_threads", dossier.corroborations)
        self.assertNotIn("absent_thread_clamped", dossier.corroborations)

    def test_a_thread_that_is_present_does_not_fire(self):
        for statement in ('the `"MainThread"` thread is blocked in shutdown',
                          'the `"AudioIPC Server RPC"` thread is still serving',
                          'the `"Shutdown Hang Terminator"` thread fired the MOZ_CRASH',
                          'thread named `"AudioIPC Server"` was busy',
                          'the "main" thread never returned'):
            dossier = self._run(statement)
            self.assertEqual(dossier.verdict.confidence, Confidence.probable, statement)
            self.assertEqual(dossier.corroborations, {}, statement)

    def test_unquoted_prose_about_a_subsystem_is_not_gated(self):
        # Quoting is the model asserting a literal runtime identifier, which is checkable.
        # Reasoning about a subsystem in prose is not, and gating it would punish ordinary
        # writing -- including the correct sentence "no MediaTrackGraph exists here".
        for statement in ("a MediaTrackGraph would be torn down at this phase",
                          "the MediaTrackGrph thread owned by ThreadedDriver",
                          "no MediaTrackGraph runs in this process, so this is not the cause"):
            self.assertEqual(self._run(statement).verdict.confidence, Confidence.probable,
                             statement)

    def test_an_x_thread_compound_is_not_a_claimed_thread(self):
        # Bug 2062286, verbatim: the exception NAME was read as a claimed thread because a word
        # boundary sits after the hyphen in "main-thread". Absent from an 86-thread complete
        # inventory, it clamped 97% to medium -- below `autofile.min_confidence` -- on the one
        # filing BMO resolves FIXED with `regressed_by` naming the changeset we named. 10 of 52
        # filings use an X-thread compound.
        for statement in (
                "Crash is a main-thread `EXCEPTION_ACCESS_VIOLATION_READ` reading a "
                "`char16_t` buffer at a computed offset",
                "the off-thread `nsThread` parse completes after teardown",
                "a background-thread `MediaTrackGraph` reference outlives the driver"):
            dossier = self._run(statement)
            self.assertEqual(dossier.verdict.confidence, Confidence.probable, statement)
            self.assertEqual(dossier.corroborations, {}, statement)

    def test_a_truncated_inventory_cannot_prove_absence(self):
        names = [{"thread_name": "T%d" % i} for i in range(triage._MAX_THREAD_NAMES + 5)]
        raw = {"json_dump": {"threads": names}}
        dossier = _dossier(_FILED_MECHANISM)
        orchestrator._apply_absent_thread_gate(dossier, {"uuid": "u-1", "raw_crash": raw})
        self.assertEqual(dossier.verdict.confidence, Confidence.probable)
        self.assertEqual(dossier.corroborations, {})

    def test_a_linux_truncated_name_still_matches(self):
        raw = {"json_dump": {"threads": [{"thread_name": "Shutdow~minator"},
                                         {"thread_name": "firefox-bin"}]}}
        dossier = _dossier('the `"Shutdown Hang Terminator"` thread fired')
        orchestrator._apply_absent_thread_gate(dossier, {"uuid": "u-1", "raw_crash": raw})
        self.assertEqual(dossier.verdict.confidence, Confidence.probable)

    def test_no_op_without_a_thread_list(self):
        # Offline eval seeds, Java crashes and any payload without `json_dump.threads`.
        for seed in ({}, {"raw_crash": {}}, {"raw_crash": {"json_dump": {}}},
                     {"raw_crash": {"json_dump": {"threads": [{"frames": []}]}}}):
            dossier = _dossier(_FILED_MECHANISM)
            orchestrator._apply_absent_thread_gate(dossier, seed)
            self.assertEqual(dossier.verdict.confidence, Confidence.probable, seed)
            self.assertEqual(dossier.corroborations, {}, seed)

    def test_no_verdict_and_abstain_never_raise(self):
        orchestrator._apply_absent_thread_gate(None, {"raw_crash": _hang()})
        orchestrator._apply_absent_thread_gate(Dossier(), {"raw_crash": _hang()})
        dossier = Dossier(verdict=Verdict(decision=Decision.abstain, confidence=Confidence.low,
                                          abstain_reason="nothing credible"))
        orchestrator._apply_absent_thread_gate(dossier, {"raw_crash": _hang()})
        self.assertEqual(dossier.verdict.decision, Decision.abstain)

    def test_the_data_flow_summary_is_scanned_too(self):
        dossier = _dossier("something innocuous")
        dossier.verdict = dossier.verdict.model_copy(
            update={"mechanism": Claim(statement="innocuous", citations=_CITE)})
        from crashclouseau.agent.schema import DataFlowHypothesis

        dossier.data_flow = DataFlowHypothesis(
            summary='the `"GraphRunner"` thread holds the last reference', citations=_CITE)
        orchestrator._apply_absent_thread_gate(dossier, {"uuid": "u-1", "raw_crash": _hang()})
        self.assertEqual(dossier.corroborations["absent_named_threads"], ["GraphRunner"])

    def test_the_gate_runs_inside_the_shipped_pipeline(self):
        # A gate nobody calls is the `_ensure_enum_values` mistake. Pin the wiring.
        import inspect

        src = inspect.getsource(orchestrator.apply_deterministic_gates)
        self.assertIn("_apply_absent_thread_gate(result.dossier, seed)", src)


class TestShutdownHangArchetype(unittest.TestCase):
    def test_it_matches_shutdownhang_signatures_only(self):
        from crashclouseau import archetypes, models

        spec = archetypes._SHUTDOWN_HANG
        row = models.Archetype(slug=spec["slug"], title=spec["title"],
                               guidance=spec["guidance"], matcher=spec["matcher"])
        for signature, want in (
            ("shutdownhang | ntdll.dll | mozilla::FutexImpl<T>::wait", True),
            ("shutdownhang | OpenTypeCharacterMap::Read", True),
            ("AsyncShutdownTimeout | profile-change-teardown", False),
            ("mozilla::dom::Foo::Bar", False),
        ):
            self.assertIs(row.matches({"signature": signature, "stack": "",
                                       "crash_type": "", "fault_address": None}), want, signature)

    def test_it_carries_what_the_reviewer_established(self):
        from crashclouseau import archetypes

        spec = archetypes._SHUTDOWN_HANG
        self.assertEqual(spec["source_bug"], 2064436)
        guidance = spec["guidance"]
        # The spin-loop stack is the lead; the other two are Andreas Pehrson's own caveats.
        self.assertIn("BLOCKED SPIN-EVENT-LOOP STACK", guidance)
        self.assertIn("CONTENT process", guidance)
        self.assertIn("TIMER", guidance)
        self.assertIn("2064436", guidance)


if __name__ == "__main__":
    unittest.main()


class TestHangCommentLabelsTheThread(unittest.TestCase):
    """The reviewer on bug 2064436 was shown seven frames of watchdog boilerplate under a bare
    "Top 9 frames:", with nothing saying which thread they belonged to."""

    _STACK = {"frames": [
        {"stackpos": 0, "module": "ntdll.dll", "function": "ZwWaitForAlertByThreadId"},
        {"stackpos": 1, "module": "xul.dll", "function": "nsThreadPool::ShutdownWithTimeout",
         "filename": "xpcom/threads/nsThreadPool.cpp", "line": 615},
    ]}

    def test_a_hang_says_the_frames_are_the_hung_main_thread(self):
        from crashclouseau import report_bug

        block = report_bug.build_frames_block(self._STACK, details={"report_type": "hang"})
        self.assertIn("Top 2 frames of the hung main thread", block)
        self.assertIn("nothing crashed here", block)
        self.assertIn("waiting on", block)

    def test_an_ordinary_crash_keeps_the_socorro_wording(self):
        from crashclouseau import report_bug

        for details in ({}, None, {"report_type": "crash"}):
            block = report_bug.build_frames_block(self._STACK, details=details)
            self.assertIn("Top 2 frames:", block)
            self.assertNotIn("hung main thread", block)

    def test_report_type_is_fetched_or_the_label_can_never_fire(self):
        from crashclouseau import report_bug

        self.assertIn("report_type", report_bug._REASON_COLUMNS)
