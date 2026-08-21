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
import json
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

# Crash ec1ff67a-a835-4740-be14-572e50260818 (bug 2064436) in shape: 46 threads, 32 distinct
# names, 14 UNNAMED, and its absence claim was CORRECT. THE counter-example for every proposed
# tightening of the completeness predicate -- `unnamed == 0` would withdraw the claim here, and
# the family ceiling must not truncate this list. The 14 unnamed are Win32 thread-pool /
# NtWaitForMultipleObjects / win32u!ZwUserGetMessage threads with zero Gecko frames between them.
_2064436_NAMES = [
    "MainThread", "AudioIPC Server RPC", "AudioIPC Server Callback", "DeviceCollection RPC",
    "Shutdown Hang Terminator", "IPC I/O Parent", "Socket Thread", "Timer", "HTML5 Parser",
    "Compositor", "Renderer", "IPDL Background", "BgIOThreadPool #1", "BgIOThreadPool #2",
    "StyleThread#0", "StyleThread#1", "StyleThread#2", "StyleThread#3", "TaskCon~ller #0",
    "TaskCon~ller #1", "TaskCon~ller #2", "TaskCon~ller #3", "DNS Resolver #1", "DNS Resolver #2",
    "Cache2 I/O", "QuotaManager IO", "mozStorage #1", "mozStorage #2", "JS Watchdog", "GMPThread",
    "Netlink Monitor", "URL Classifier",
]


def _bug_2064436():
    threads = [{"thread_name": n, "frames": []} for n in _2064436_NAMES]
    threads += [{"frames": [{"function": "ntdll!ZwWaitForWorkViaWorkerFactory"}]}
                for _ in range(14)]
    # `process_type` is set on 840 of 840 sampled nightly crashes and this one is a PARENT crash,
    # which is the whole point: `MediaTrackGrph` is a CONTENT-process thread (p=0.00 parent /
    # 0.05 content), so its absence here is exactly the cross-process shape the licence now
    # conditions on. A fixture without the field would not exercise the thing it exists for.
    return _hang(process_type="parent",
                 json_dump={"crash_info": {"crashing_thread": 0}, "threads": threads})


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

    def test_the_absence_licence_is_scoped_to_this_process(self):
        # Andreas Pehrson's caveat, generalised. 113 of the 159 thread families seen >=10x over
        # 840 nightly crashes (71%) are >=95% confined to ONE process type, so on a PARENT crash
        # the absence of a media-graph thread (p=0.00 parent / 0.05 content) is the base rate and
        # not evidence. The CODE always kept the caveat (a clamp, never an abstain); this text
        # said "REFUTED ... look elsewhere" flat, and via `_crash_facts` that absolute reached the
        # blind second opinion -- the specificity-1.00 instrument used to SUPPRESS leads.
        facts = self._facts()
        self.assertIn("COMPLETE for the NAMED threads of THIS process", facts)
        self.assertIn("BUT THE LICENCE IS ABOUT THIS PROCESS ONLY", facts)
        self.assertIn("BASE RATE, not evidence", facts)
        self.assertIn("cross the process boundary", facts)
        # The conclusion is still available -- gated on the second step, not withdrawn.
        self.assertIn("REFUTED, not merely unproven", facts)

    def test_the_process_type_is_named_beside_the_list(self):
        # The denominator belongs next to the list it qualifies; absent from the payload it is
        # simply not asserted.
        self.assertIn("THREADS IN THIS PROCESS (content process, 3 threads",
                      self._facts(_hang(process_type="content")))
        self.assertIn("THREADS IN THIS PROCESS (3 threads", self._facts())

    def test_the_unnamed_count_is_in_the_rule_not_just_the_header(self):
        # 347 of the 810 sampled inventories (42.8%; 44.1% of the 786 the old ceiling called
        # complete) contain unnamed threads, median 14.5% of the process, p90 57.7%. The header
        # said ", 14 unnamed" and the rule then said COMPLETE with no qualifier. NOT
        # `unnamed == 0`, which is refuted: -44% of the rule's reach for 0.2% leakage, and it
        # would withdraw the claim on bug 2064436 itself.
        facts = self._facts(_bug_2064436())
        self.assertIn("(parent process, 46 threads, 32 distinct names in 23 families, "
                      "14 unnamed)", facts)
        self.assertIn("This list is COMPLETE", facts)
        self.assertIn("14 of these 46 threads carry no name", facts)
        self.assertIn("only 13 of 5,982 of them (0.2%) held any Gecko frame", facts)
        # ...and it stays quiet when there is nothing to qualify.
        self.assertNotIn("carry no name", self._facts())

    def test_an_unnamed_analysed_thread_says_so(self):
        # 47 of 821 sampled crashes (5.7%): "Analysed thread: 12" above a list advertised as
        # COMPLETE that has no thread 12 in it reads as a contradiction unless we say which it is.
        data = _hang()
        data["json_dump"]["threads"][0] = {"frames": []}
        self.assertIn("Analysed thread (the stack below is THIS thread): 0 (unnamed)",
                      self._facts(data))
        # An index with no thread object behind it is not known to be unnamed, so it stays bare.
        self.assertIn(
            "Analysed thread (the stack below is THIS thread): 7",
            self._facts({"json_dump": {"crash_info": {"crashing_thread": 7}, "threads": []}}))

    def test_instance_numbering_does_not_cost_the_absence_claim(self):
        # The ceiling was `<= 120 distinct NAMES`, an n=1 number off one hang report, and what
        # pushed a crash past it was instance numbering rather than subsystems: `FSBroker<pid>`
        # alone contributes 582 names across the 24 of 840 nightly crashes it clipped (all 24
        # parent-process = 11.8% of parent crashes; 0 of 116 hang reports). 130 brokers are ONE
        # family, and past the rendering cap they print as one line instead of 130 pids.
        data = _hang()
        data["json_dump"]["threads"] += [
            {"thread_name": "FSBroker%d" % (4000 + i), "frames": []} for i in range(130)]
        # ...plus one name carried by SEVEN threads, because the fold label counts THREADS and not
        # distinct names. `Renderer` (1 name, 7 threads) and `firefo:traceq` (1 name, 18) are real
        # shapes: in 24 of the 24 sampled crashes that fold, at least one family had more threads
        # than names, and a distinct-name count would have printed them as a bare single entry.
        data["json_dump"]["threads"] += [
            {"thread_name": "Renderer", "frames": []} for _ in range(7)]
        facts = self._facts(data)
        self.assertIn("This list is COMPLETE", facts)
        self.assertIn("(140 threads, 134 distinct names in 5 families)", facts)
        self.assertIn("FSBroker x130", facts)
        self.assertIn("Renderer x7", facts)
        self.assertNotIn("FSBroker4000,", facts)
        # ...and the header says what the folding means, so "FSBroker4123" is still findable.
        self.assertIn("Numbered instances are folded into their family", facts)
        self.assertNotIn("Numbered instances are folded", self._facts())

    def test_a_short_stem_is_not_a_family(self):
        # The other direction. "T0".."T124" must NOT collapse to the single family "T": a
        # one-character stem is a parse failure, not a subsystem, and treating it as one would let
        # a 125-name list claim completeness.
        data = _hang()
        data["json_dump"]["threads"] = [
            {"thread_name": "T%d" % i, "frames": []} for i in range(125)]
        self.assertIn("This list is TRUNCATED", self._facts(data))
        self.assertEqual(triage._thread_family("T7"), "T7")
        self.assertEqual(triage._thread_family("FSBroker4242"), "FSBroker")
        self.assertEqual(triage._thread_family("TaskCon~ller #7"), "TaskCon~ller")
        self.assertEqual(triage._thread_family("StyleThread#5"), "StyleThread")
        self.assertEqual(triage._thread_family("DNS Resolver #4"), "DNS Resolver")
        self.assertEqual(triage._thread_family("Cache2 I/O"), "Cache2 I/O")

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
        # with our encouragement. The ceiling counts FAMILIES now, so the case that must still
        # truncate is a process with that many genuinely DIFFERENT subsystems -- the collapse must
        # not buy completeness for one of those.
        data = _hang()
        data["json_dump"]["threads"] = [
            {"thread_name": "Subsystem%dWorker" % i, "frames": []}
            for i in range(triage._MAX_THREAD_FAMILIES + 5)]
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
        # And the CONDITION on the absence licence, not just the licence: this prompt is the
        # instrument that suppresses leads, so an absolute here suppresses on a base rate.
        self.assertIn("BUT THE LICENCE IS ABOUT THIS PROCESS ONLY", prompt)


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

    def test_the_2064436_crash_still_fires_over_its_14_unnamed_threads(self):
        # THE counter-example. 46 threads, 32 distinct names, 14 unnamed, 23 families -- complete,
        # and the absence claim on it was CORRECT. Any tightening that stops this firing is wrong,
        # and any ceiling that truncates 23 families is wrong.
        dossier = _dossier(_FILED_MECHANISM)
        orchestrator._apply_absent_thread_gate(
            dossier, {"uuid": "ec1ff67a-a835-4740-be14-572e50260818",
                      "raw_crash": _bug_2064436()})
        self.assertEqual(dossier.verdict.confidence, Confidence.medium)
        self.assertEqual(dossier.corroborations["absent_named_threads"], ["MediaTrackGrph"])

    def test_a_collapsed_family_still_satisfies_the_gate(self):
        # Past the rendering cap the agent is shown "FSBroker x300", not 300 pids. The gate still
        # matches against every raw name, so a mechanism naming the family is not called absent.
        raw = {"json_dump": {"threads": [{"thread_name": "FSBroker%d" % i} for i in range(300)]}}
        dossier = _dossier('the `"FSBroker"` thread holds the lock')
        orchestrator._apply_absent_thread_gate(dossier, {"uuid": "u-1", "raw_crash": raw})
        self.assertEqual(dossier.corroborations, {})
        self.assertEqual(dossier.verdict.confidence, Confidence.probable)

    def test_a_truncated_inventory_cannot_prove_absence(self):
        names = [{"thread_name": "Subsystem%dWorker" % i}
                 for i in range(triage._MAX_THREAD_FAMILIES + 5)]
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


class TestTheGateAndThePromptAgree(unittest.TestCase):
    """`orchestrator._process_thread_names` used to RE-IMPLEMENT the completeness ceiling over
    SQUASHED names while `triage._thread_inventory` applied it to RAW distinct names, under a
    comment claiming they used "the SAME ceiling ... so the gate and the agent cannot disagree".
    Above the ceiling that was false, and the direction of the disagreement is the harmful one:
    the agent is told the list is TRUNCATED and the gate clamps its verdict for an absence from
    that same list anyway. Both sides now call `triage._inventory_complete`."""

    def _both(self, threads):
        raw = {"json_dump": {"threads": threads}}
        said = "This list is COMPLETE" in "\n".join(triage._thread_inventory(raw))
        _, gated = orchestrator._process_thread_names({"raw_crash": raw})
        return said, gated

    def test_they_agree_on_every_shape(self):
        cases = {
            "small": [{"thread_name": "MainThread"}, {"thread_name": "Timer"}],
            # 121 raw names that squash to 119. Under the old code the prompt said TRUNCATED
            # (121 > 120) and the gate said complete (119 <= 120).
            "squash collision": [{"thread_name": "Worker %d" % i} for i in range(119)] + [
                {"thread_name": "Worker-0"}, {"thread_name": "Worker-1"}],
            "one family, many instances": [
                {"thread_name": "FSBroker%d" % i} for i in range(300)],
            "genuinely wide": [
                {"thread_name": "Subsystem%dWorker" % i}
                for i in range(triage._MAX_THREAD_FAMILIES + 5)],
            "bug 2064436": _bug_2064436()["json_dump"]["threads"],
        }
        for label, threads in cases.items():
            said, gated = self._both(threads)
            self.assertEqual(said, gated, label)
        self.assertTrue(self._both(cases["one family, many instances"])[0])
        self.assertFalse(self._both(cases["genuinely wide"])[0])
        self.assertTrue(self._both(cases["bug 2064436"])[0])


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

    def test_the_clauses_the_panel_validated_survive_verbatim(self):
        # Only the closer was replaced. Everything else scored 3/3 on our own shutdown hangs:
        # the spin-loop annotation named the subsystem on the two crashes that carry one, and
        # the thread list refuted the mechanism on both of the wrong ones.
        from crashclouseau import archetypes

        guidance = archetypes._SHUTDOWN_HANG["guidance"]
        for kept in ("A shutdown hang is not a fault",
                     "BLOCKED SPIN-EVENT-LOOP STACK",
                     "Shutdown phase reached",
                     "BEFORE YOU NAME A SUBSYSTEM, find its thread"):
            with self.subTest(kept=kept):
                self.assertIn(kept, guidance)

    def test_it_carries_no_prior_about_whether_a_regressor_exists(self):
        # The row used to close "Finally, expect NO regressor ... better than naming a
        # changeset because the window had to contain one" -- generalised from one INVALID bug
        # (2064436) the day after it closed, and the only sentence in the row with neither a
        # fact nor a counter-example. What made apehrson right there was an UNGROUNDED
        # MECHANISM, not the absence of a regressor; he never said there wasn't one.
        # NOT INVERTED EITHER: 40 of the 144 panel bugs closed WORKSFORME/INCOMPLETE/INVALID,
        # so the clause's world is real, it is just a quarter of this one, and the closer says
        # that out loud beside the rates.
        from crashclouseau import archetypes

        guidance = archetypes._SHUTDOWN_HANG["guidance"]
        for gone in ("expect NO regressor", "better than naming a changeset",
                     "usually a latent ordering"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, guidance)
        self.assertIn("carry NO prior about whether a regressor exists", guidance)
        self.assertIn("Both endings are legitimate", guidance)
        self.assertNotIn("expect a regressor", guidance)

    def test_the_refutation_clause_does_not_restate_the_absence_absolute(self):
        # `triage._thread_inventory` deliberately conditions "REFUTED" on a cross-process step,
        # because 71% of thread families are >=95% confined to one process type and so the
        # absence of a content-process thread from a parent crash is the BASE RATE. The closer
        # points back at "the thread check above", which carries that caveat two sentences
        # earlier, rather than re-asserting a flat absolute in a second place where it would
        # quietly undo that fix.
        from crashclouseau import archetypes

        guidance = archetypes._SHUTDOWN_HANG["guidance"]
        self.assertIn("when a mechanism fails the thread check above", guidance)
        self.assertLess(guidance.index("BEFORE YOU NAME A SUBSYSTEM"),
                        guidance.index("when a mechanism fails the thread check above"))

    def test_the_numbers_in_the_closer_recompute_from_the_committed_panel(self):
        # Every figure in the closer, against the bugs it was measured on.
        panel = _shutdownhang_bug_panel()
        fixed = [b for b in panel if b["resolution"] == "FIXED"]
        rb = [b for b in panel if b["regressed_by"]]
        rb_fixed = [b for b in fixed if b["regressed_by"]]
        old_fixed = [b for b in fixed if b["signature_age_days_at_filing"] >= 365]
        old_fixed_rb = [b for b in old_fixed if b["regressed_by"]]
        self.assertEqual(
            (len(panel), len(fixed), len(rb), len(rb_fixed), len(old_fixed), len(old_fixed_rb)),
            (144, 42, 19, 15, 25, 7))
        self.assertEqual(round(100 * len(rb) / len(panel)), 13)
        self.assertEqual(round(100 * len(rb_fixed) / len(fixed)), 36)
        self.assertEqual(round(100 * len(old_fixed_rb) / len(old_fixed)), 28)
        oldest = max(old_fixed_rb, key=lambda b: b["signature_age_days_at_filing"])
        self.assertEqual((oldest["id"], oldest["signature_age_days_at_filing"],
                          oldest["regressed_by"]), (2037923, 3855, [2026686]))
        # Nobody backfills `regressed_by`, so 19/144 is a floor, not an estimate; the closer
        # says "carry NO prior", never "there is one", and it says the floor out loud so the
        # smallest of the three rates cannot be read back as the sentence it replaced.
        self.assertLess(len(rb), len(panel))
        # The other direction, also quoted: the deleted clause's world is real, just not the
        # whole world. INACTIVE is not in the panel at all, so it is not in the sentence.
        negative = [b for b in panel
                    if b["resolution"] in ("WORKSFORME", "INCOMPLETE", "INVALID")]
        self.assertEqual(len(negative), 40)

        from crashclouseau import archetypes

        guidance = archetypes._SHUTDOWN_HANG["guidance"]
        for quoted in ("Of 144 Gecko `shutdownhang` crash bugs filed since 2020",
                       "19 (13%)", "of the 42 that reached RESOLVED FIXED, 15 (36%)",
                       "7 of 25 (28%)",
                       "Those rates are FLOORS",
                       "40 of the same 144 closed WORKSFORME/INCOMPLETE/INVALID",
                       "bug 2037923 is a 3855-day-old signature regressed by bug 2026686"):
            with self.subTest(quoted=quoted):
                self.assertIn(quoted, guidance)

    def test_an_age_gated_version_of_the_clause_is_refuted_by_the_panel(self):
        # THE OBVIOUS GRADED REPAIR, killed: keep "expect NO regressor" and fire it only on an
        # old signature. The rate does fall with age (<30d 34.8% of n=23 carry a regressor,
        # 30-365d 12.9% of n=31, >365d 7.8% of n=90) and it still does not license the clause,
        # because 7 of the 25 FIXED bugs in that oldest slice name one -- signatures 876 to
        # 3855 days old at filing. The gate would also have contradicted its neighbour in the
        # same prompt: `triage._signature_age_lines` appends exactly ONE of three closers, and
        # on an old signature that one is `_OLD_SIGNATURE_GUIDANCE` ("a new patch can perfectly
        # well start crashing code that has crashed under this name for years"), while the
        # undated branch bans the inference in as many words.
        panel = _shutdownhang_bug_panel()
        eaten = [b for b in panel
                 if b["resolution"] == "FIXED" and b["regressed_by"]
                 if b["signature_age_days_at_filing"] >= 365]
        self.assertEqual(sorted(b["id"] for b in eaten),
                         [1676851, 1704391, 1752326, 1772281, 1801819, 2019599, 2037923])
        self.assertEqual(min(b["signature_age_days_at_filing"] for b in eaten), 876)
        # And 365 is not where the fit is: the same rate is 23-35% at every cut from 90 to
        # 1460 days, so the repair dies wherever the line is drawn rather than at one lucky
        # boundary.
        rates = []
        for cut in (90, 180, 365, 548, 730, 1095, 1460):
            old = [b for b in panel if b["resolution"] == "FIXED"
                   if b["signature_age_days_at_filing"] >= cut]
            rates.append(len([b for b in old if b["regressed_by"]]) / len(old))
        self.assertGreater(min(rates), 0.2)
        self.assertLess(max(rates), 0.36)
        # The youngest slice quoted in the comment above carries 7 bugs whose signature age is
        # NEGATIVE -- Socorro's first_date postdates the filing, so the age is not usable.
        # Dropping them leaves 5 of 16, which is why the gradient is not their artefact.
        young = [b for b in panel if b["signature_age_days_at_filing"] < 30]
        dated = [b for b in young if b["signature_age_days_at_filing"] >= 0]
        self.assertEqual((len(young), len(dated),
                          sum(1 for b in dated if b["regressed_by"])), (23, 16, 5))

    def test_the_deterministic_backstop_does_not_cover_what_the_clause_covered(self):
        # THE HONEST COST OF THE DELETION, pinned so the comment above cannot drift into a
        # promise. `orchestrator._apply_absent_thread_gate` is the only DETERMINISTIC check of
        # a named-but-absent thread, and it reads QUOTED names only. Bug 2061969's mechanism
        # wrote "(IO thread)" in prose and quoted no thread, so the gate is a no-op on exactly
        # the filing whose accidental brake the deleted sentence was; what is left there is
        # this row's thread check and `triage._thread_inventory`, which are advice. Bug
        # 2064436, which wrote "the `MediaTrackGrph` thread", is caught.
        mechanism_2061969 = (
            "`QuotaManager::InitializeRepository`'s per-origin directory walk (IO thread) is "
            "deliberately not short-circuited by a shutdown request. `QuotaManager::Shutdown` "
            "on PBackground blocks the main thread's shutdown spin-wait on this IO thread "
            "finishing (`shutdownAndJoinIOThread`).")
        self.assertEqual(
            [m.group(1) or m.group(2)
             for m in orchestrator._QUOTED_THREAD_RE.finditer(mechanism_2061969)], [])
        self.assertEqual(
            [m.group(1) or m.group(2) for m in orchestrator._QUOTED_THREAD_RE.finditer(
                "the `MediaTrackGrph` thread owned by `ThreadedDriver`")], ["MediaTrackGrph"])

    def test_it_fires_on_all_three_of_our_own_shutdown_hang_filings(self):
        # A guidance change only matters where the row fires. Signatures verbatim from the
        # ProcessedCrash of each filing's crash uuid; 2063892 is the one this pipeline got
        # right (abienner accepted the mechanism 11h after filing and attached a patch at 22h),
        # and it is the crash the deleted sentence was reaching.
        from crashclouseau import archetypes, models

        spec = archetypes._SHUTDOWN_HANG
        row = models.Archetype(slug=spec["slug"], title=spec["title"],
                               guidance=spec["guidance"], matcher=spec["matcher"])
        for bug, uuid, signature in (
            (2064436, "ec1ff67a-a835-4740-be14-572e50260818",
             "shutdownhang | RtlWaitOnAddress | WaitOnAddress"),
            (2063892, "80e01888-f10a-4a4b-9120-b2aac0260816",
             "shutdownhang | RtlpWaitOnAddressWithTimeout | RtlpWaitOnAddress | "
             "RtlWaitOnAddress | kernelbase.dll | DispatchMessageWorker"),
            (2061969, "424b0ab0-af81-4b33-b045-83c5b0260808",
             "shutdownhang | __fstatat"),
        ):
            with self.subTest(bug=bug, uuid=uuid):
                self.assertTrue(row.matches({"signature": signature, "stack": "",
                                             "crash_type": "", "fault_address": None}))


def _shutdownhang_bug_panel():
    """The 144 bugs behind the `shutdown-hang` closer.

    BMO `cf_crash_signature` substring `shutdownhang |` is 613 bugs all-time; created
    >= 2020-01-01 (before that `regressed_by` does not exist) and product in
    Core/Toolkit/Firefox with our own filings excluded leaves 146, of which 144 have a Socorro
    `SignatureFirstDate` for the matched signature. Regenerate end to end with
    `spike/_shutdownhang_regressor_panel.py`."""
    path = os.path.join(os.path.dirname(__file__), "archetypes",
                        "shutdownhang_bug_panel.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


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


# The two crash facts the 300-char cap actually damages, and the panel that says so:
# `spike/CRASH_FACT_RENDERERS_PANEL.json` (9,251 spin values / 4,880 async_shutdown_timeout
# values, Firefox nightly 2026-06-12..2026-08-21) rebuilt by
# `spike/crash_fact_renderers_panel.py`. The cap is a no-op on the other 19 fields (0 of 2,800
# control reports over 300 chars for any of them), so these two renderers are the whole change.

# uuid defd3256-091a-4e73-b575-c57f80260614 in shape (10,018 chars here; the real one is
# 10,000, Socorro's annotation cap, which severed its last entry mid-name): 323 entries, 3
# distinct -- the reason tail-preserving truncation LOSES. The innermost slot carries a real
# name from the panel, `nsThread::Shutdown: GraphRunner`, which head-300 ate on e3a7c479.
_SPIN_ENTRIES = ["tab: nsThread::Shutdown: DOM Worker"]
_SPIN_ENTRIES += ["nsThread::Shutdown: DOM Worker"] * 321
_SPIN_ENTRIES += ["nsThread::Shutdown: GraphRunner"]
_SPIN_REPEATED = "|".join(_SPIN_ENTRIES)
# uuid fcb7058e-210c-47a9-954a-219bc0260807 in shape (875 chars here, 876 there): 22 entries,
# ALL DISTINCT -- THE counter-example collapse cannot shrink. Its three subsystems survive only
# because the cap is 400 and not 300.
_DISTINCT_ENTRIES = ["default: nsThread::Shutdown: sqldb:Login Data #18"]
_DISTINCT_ENTRIES += ["nsThread::Shutdown: sqldb:%s #%d" % (db, n) for n, db
                      in enumerate(["History", "Web Data", "Login Data"] * 7, start=19)]
_SPIN_DISTINCT = "|".join(_DISTINCT_ENTRIES)
# uuid 89910485-48e7-4fa2-ab18-23b050260812, VERBATIM: 163 chars, so today's head-300 leaves it
# untouched -- and its innermost entry (`GraphRunner`) also appears three times earlier, which is
# what makes a first-occurrence collapse re-order it. 4 of the 9,251 panel values are like this.
_SPIN_INNERMOST_RECURS = (
    "tab: nsThread::Shutdown: GraphRunner|nsThread::Shutdown: GraphRunner|"
    "nsThread::Shutdown: GraphRunner|nsThread::Shutdown: DOM Worker|"
    "nsThread::Shutdown: GraphRunner")
# uuid d29ac23d-eb9c-45f0-bf2c-8129b0260809 (bug 2062062, REOPENED), verbatim: 356 chars, and
# today's head-300 cuts the second frame off the end.
_AST_BUG_2062062 = (
    '{"phase":"profile-change-teardown","conditions":[{"name":"LoginManagerRustStorage: '
    'Interrupt IO operations on login store","state":"(none)","filename":'
    '"resource://gre/modules/storage-rust.sys.mjs","lineNumber":413,"stack":'
    '["resource://gre/modules/storage-rust.sys.mjs:_registerShutdownBlocker:413",'
    '"resource://gre/modules/storage-rust.sys.mjs:null:375"]}]}')
# uuid 69f5115c-83a8-4524-99aa-4588e0260612, byte-for-byte: 693 chars, a DICT `state` whose
# `shutdownStates` repeats one phrase 12 times, and a `stack` that is a bare string.
_AST_DICT_STATE = (
    '{"phase":"profile-change-teardown","conditions":[{"name":"ServiceWorkerShutdownBlocker: '
    'shutting down Service Workers","state":{"shutdownStates":"%s","pendingPromises":12,'
    '"acceptingPromises":false},"filename":'
    '"./../../../../checkouts/gecko/dom/serviceworkers/ServiceWorkerShutdownBlocker.cpp",'
    '"lineNumber":107,"stack":"Service Workers shutdown"}]}'
) % ("content process main thread, " * 12)
# uuid b43e9c0d-7465-4acd-bfe8-2546d0260708, byte-for-byte: 422 chars, and its `state` is a bare
# STRING, not a dict. Today's head-300 keeps it (it sits before the 300th char); a renderer that
# tests `isinstance(state, dict)` drops it, and the dict-only state metric cannot see that.
_AST_STRING_STATE = (
    '{"phase":"Sqlite.sys.mjs: wait until all clients have completed their task",'
    '"conditions":[{"name":"PlacesUtils wrapped connection must be closed before '
    'Sqlite.sys.mjs","state":"1. Service has initiated shutdown","filename":'
    '"resource://gre/modules/PlacesUtils.sys.mjs","lineNumber":3102,"stack":'
    '["resource://gre/modules/PlacesUtils.sys.mjs:setupDbForShutdown:3102",'
    '"resource://gre/modules/PlacesUtils.sys.mjs:null:3131"]}]}')


class TestSpinStackRenderer(unittest.TestCase):
    """`xpcom_spin_event_loop_stack` reached the prompt with bug 2064436 and the 300-char head
    then ate the end of it -- on a field whose own label says "innermost last"."""

    def _fact(self, value):
        facts = "\n".join(triage._crash_facts(
            {"raw_crash": _hang(xpcom_spin_event_loop_stack=value)}))
        return [ln for ln in facts.split("\n") if ln.startswith("BLOCKED SPIN")][0]

    def test_a_short_stack_is_byte_identical_to_today(self):
        # p50 is 58 chars and 98.55% of the 9,251 never truncate, so the renderer has to be
        # near-invisible on them or it is buying its 6 rescues with 9,117 regressions. Measured:
        # it changes 58 of those 9,117, every one of them a repeat collapsed to `(xN)` -- which
        # is lossless and shorter. A value with no repeat at all comes through byte-for-byte.
        for value in ("default: nsThreadPool::ShutdownWithTimeout BgIOThreadPool",
                      "default: QuotaManager::Observer::Observe profile-before-change-qm",
                      "a|b|c"):
            self.assertEqual(triage._render_spin_stack(value), value)
        self.assertEqual(triage._render_spin_stack(""), "")
        self.assertEqual(triage._render_spin_stack(None), "")

    def test_repetition_collapses_and_the_count_survives(self):
        # "322 workers were stuck" is information, so the repeat is not deduped away: it costs
        # 6 bytes as `(x321)` instead of the 9,900 Socorro spent on it.
        rendered = triage._render_spin_stack(_SPIN_REPEATED)
        self.assertIn("nsThread::Shutdown: DOM Worker (x321)", rendered)
        self.assertLess(len(rendered), 150)

    def test_the_innermost_subsystem_survives_the_cap(self):
        # What head-300 lost on 4 of the 134 over-300 values, and the whole point of the field.
        self.assertIn("nsThread::Shutdown: GraphRunner",
                      triage._render_spin_stack(_SPIN_REPEATED))
        self.assertNotIn("GraphRunner", triage._short_value(_SPIN_REPEATED))

    def test_a_value_with_no_repeats_keeps_every_subsystem(self):
        # THE COUNTER-EXAMPLE collapse cannot help: 22 distinct entries. Only the per-thread
        # `#N` suffixes fall off the 400-char cap, and those name nothing.
        rendered = self._fact(_SPIN_DISTINCT)
        for db in ("sqldb:Login Data", "sqldb:History", "sqldb:Web Data"):
            self.assertIn(db, rendered)

    def test_the_innermost_entry_still_ends_the_line(self):
        # "Innermost last" is the field's own prompt label, so ending on the wrong entry is a
        # WRONG FACT, not a cosmetic one. Collapsing by first occurrence does exactly that when
        # the innermost entry recurs earlier: on this real 163-char value -- which today's
        # head-300 does not touch at all -- it would end on `DOM Worker` instead of
        # `GraphRunner`. The panel's `innermost_lost` counter is a containment test and scores 0
        # either way, which is why the panel also reports `mis_ordered_innermost` (0 shipped,
        # 4 for a plain first-occurrence collapse).
        rendered = triage._render_spin_stack(_SPIN_INNERMOST_RECURS)
        self.assertTrue(rendered.endswith("nsThread::Shutdown: GraphRunner (x3)"), rendered)
        self.assertIn("nsThread::Shutdown: DOM Worker", rendered)
        self.assertLess(len(rendered), len(_SPIN_INNERMOST_RECURS))
        # ... and a value whose innermost does NOT recur keeps plain first-occurrence order.
        self.assertEqual(triage._render_spin_stack("a|a|b"), "a (x2)|b")

    def test_the_cap_is_400_and_the_budget_is_why(self):
        # 500/600/800 recover no further subsystem name on the panel, so the extra bytes would
        # be spent for nothing against an 8,994-13,136-char prompt.
        self.assertEqual(triage._SPIN_STACK_LIMIT, 400)
        self.assertGreater(len(_SPIN_DISTINCT), 400)
        self.assertEqual(len(triage._render_spin_stack(_SPIN_DISTINCT)), 400)


class TestAsyncShutdownRenderer(unittest.TestCase):
    """`async_shutdown_timeout` is the ONE field the 300-char cap destroys: 98.8% of 4,880
    values are over it, and the head keeps 9.6% of the blocker source files."""

    def test_the_blocker_file_and_the_cut_frame_both_survive(self):
        # Bug 2062062's own crash: `storage-rust.sys.mjs:null:375` fell off the head-300.
        rendered = triage._render_async_shutdown(_AST_BUG_2062062)
        self.assertIn("phase=profile-change-teardown", rendered)
        self.assertIn('blocker "LoginManagerRustStorage: Interrupt IO operations on login '
                      'store" @ resource://gre/modules/storage-rust.sys.mjs:413', rendered)
        self.assertIn("storage-rust.sys.mjs:null:375", rendered)
        self.assertNotIn("null:375", triage._short_value(_AST_BUG_2062062))
        # And it is SHORTER than what it replaces, which is why the budget survives it.
        self.assertLess(len(rendered), len(triage._short_value(_AST_BUG_2062062)))

    def test_state_is_kept_because_the_head_300_already_kept_it(self):
        # THE COUNTER-EXAMPLE. `state` sits right after the blocker name, so today's head keeps
        # 3,272 of 4,118 state objects (79.5%). The first extractor dropped it -- source files
        # 97.6%, state 0.0% -- and that is why this renderer carries a state budget at all.
        rendered = triage._render_async_shutdown(_AST_DICT_STATE)
        self.assertIn('state={"shutdownStates":"content process main thread', rendered)
        self.assertIn('ServiceWorkerShutdownBlocker.cpp:107', rendered)

    def test_a_state_that_is_not_a_dict_is_kept_too(self):
        # THE SECOND WAY TO EAT THE SAME COUNTER-EXAMPLE. `state` is a dict on 4,118 of the
        # panel's conditions, a bare string on 925 and a list on 158; an `isinstance(state,
        # dict)` test drops 1,083 of 5,201 states, and the dict-only metric that scored the
        # renderer 79.5% -> 80.0% cannot see it (on all 5,201 it reads 79.7% -> 63.4%). Here
        # today's head-300 keeps the string and the renderer has to as well.
        rendered = triage._render_async_shutdown(_AST_STRING_STATE)
        self.assertIn("state=1. Service has initiated shutdown", rendered)
        self.assertIn("1. Service has initiated shutdown",
                      triage._short_value(_AST_STRING_STATE))
        self.assertIn("PlacesUtils.sys.mjs:null:3131", rendered)

    def test_state_is_capped_at_160_the_floor_that_clears_today(self):
        # 100 -> 73.7%, 120 -> 73.9%, 140 -> 78.7% state coverage, all BELOW today's 79.5%;
        # 160 -> 80.0%. The number is a floor read off the sweep, not a round guess.
        self.assertEqual(triage._ASYNC_SHUTDOWN_STATE_LIMIT, 160)
        rendered = triage._render_async_shutdown(_AST_DICT_STATE)
        state = rendered.split("state=", 1)[1]
        self.assertLessEqual(len(state), 160)
        self.assertTrue(state.endswith("..."), state)

    def test_the_frame_list_is_deduped_and_bounded(self):
        # The repetition is in the frames too, and a blocker list is unbounded in the payload.
        value = json.dumps({"phase": "p", "conditions": [
            {"name": "n%d" % i, "filename": "f%d.mjs" % i, "lineNumber": i,
             "stack": ["a.mjs:x:1"] * 3 + ["b%d.mjs:y:%d" % (j, j) for j in range(9)]}
            for i in range(5)]})
        rendered = triage._render_async_shutdown(value)
        # 3 copies inside one blocker collapse to 1; the 3 rendered blockers each keep theirs.
        self.assertEqual(rendered.split('blocker "n1"')[0].count("a.mjs:x:1"), 1)
        self.assertEqual(rendered.count("a.mjs:x:1"), 3)
        self.assertIn("(+2 more blockers)", rendered)
        self.assertNotIn("f3.mjs", rendered)
        self.assertIn("b4.mjs:y:4", rendered)      # 6 frames per blocker = a.mjs + b0..b4
        self.assertNotIn("b5.mjs", rendered)

    def test_a_severed_value_falls_back_to_the_old_truncation(self):
        # 1 of the 4,880 (uuid d44e2101-...) is not JSON: Socorro's own 32,766-char annotation
        # cap cut it mid-string. The fallback keeps that case exactly as it is today.
        severed = _AST_BUG_2062062[:200]
        self.assertEqual(triage._render_async_shutdown(severed),
                         triage._short_value(severed))
        for junk in ("[]", "null", '"a string"', "not json at all"):
            self.assertEqual(triage._render_async_shutdown(junk),
                             triage._short_value(junk))

    def test_it_is_wired_to_the_field_and_reaches_the_second_opinion(self):
        # Keyed on the FIELD, not on the string's shape -- and `_crash_facts` is shared verbatim
        # with the blind reviewer, which is correct for a fact Socorro sent us.
        from crashclouseau.agent import second_opinion

        crash = {"uuid": "u-1", "signature": "AsyncShutdownTimeout | x",
                 "raw_crash": _hang(async_shutdown_timeout=_AST_BUG_2062062)}
        self.assertIn("Async shutdown timeout: phase=profile-change-teardown",
                      "\n".join(triage._crash_facts(crash)))
        self.assertIn("storage-rust.sys.mjs:null:375", second_opinion._user_prompt(crash, None))

    def test_the_other_facts_still_go_through_short_value(self):
        # The 2-tuple path has to keep working: 21 of the 23 facts have no renderer.
        facts = "\n".join(triage._crash_facts(
            {"raw_crash": _hang(moz_crash_reason="x" * 400)}))
        self.assertIn("MOZ_CRASH_REASON: " + "x" * 297 + "...", facts)
