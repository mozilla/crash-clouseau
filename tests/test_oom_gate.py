# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""The OOM gate and mechanism-only actionable filing.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        uv run python -m unittest tests.test_oom_gate

Bug 2073760's OOM-unsafe `Zone::New` abort was closed WONTFIX. Bug 2071557's recorded large
COLRFonts allocation was fixed and uplifted. The fixtures pin the policy distinction.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import inspect  # noqa: E402
import pathlib  # noqa: E402
import unittest  # noqa: E402

from crashclouseau.agent import orchestrator as orch  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    AbstainKind,
    Candidate,
    Claim,
    Confidence,
    Decision,
    Dossier,
    RefCitation,
    Verdict,
)
# The module, not its classes: binding a TestCase name here would run its tests twice.
from tests import test_actionable_verdict as tav  # noqa: E402

_ZONE_SIG = ("OOM | unknown | js::AutoEnterOOMUnsafeRegion::crash_impl | "
             "js::AutoEnterOOMUnsafeRegion::crash | v8::internal::Zone::New<T>")
_COLR_SIG = "OOM | large | nsTArray_Impl<T>::SetCapacity | mozilla::gfx::COLRFonts::CreateColorPalette"
_MECH = "`Zone::New` allocates one parser node and calls `oomUnsafe.crash` when that returns null"
# Bug 2073760's consistency statement, verbatim: one fact and two arguments about what is not.
_CONS_2073760 = (
    "This crash's frame #1 (`v8::internal::Zone::New<T>`, `ZoneShim.h:34`) matches the cited "
    "crashing line exactly, and `mcp__history__blame` names `6e6fbe10aa30` (bug 1690142) as the "
    "origin of that line — used here only to route to the code owner/component. The signature "
    "is ~1129 days old (first seen 2023-08-01) and its rate per 1000 reports is flat across recent "
    "release versions (153.0.4: 1.05, 154.0: 0.92, 154.0.1: 1.09, 155.0: 0.79, 155.0.1: 0.97, "
    "156.0: 0.93) with no step at 155.0.1; none of the 155.0.1 pushlog window's changesets touch "
    "`js/src`, irregexp, or content-process memory allocation. This is consistent with a "
    "long-standing, pre-existing deliberate-OOM-crash design rather than a newly introduced "
    "regression from this window.")


def _raw(signature, reason, **mem):
    """A processed-crash seed using bug 2073760's memory values by default."""
    raw = {"signature": signature, "moz_crash_reason": reason, "process_type": "content",
           "release_channel": "release", "available_page_file": 230998016,
           "total_virtual_memory": 140737488224256, "total_physical_memory": 17111031808,
           "system_memory_use_percentage": 66,
           "json_dump": {"crash_info": {"type": "EXCEPTION_BREAKPOINT", "crashing_thread": 15}}}
    raw.update(mem)
    return raw


def _seed(raw, signature=None):
    return {"uuid": "03d25350", "signature": signature or raw.get("signature"),
            "channel": "release", "product": "Firefox", "raw_crash": raw}


def _dossier(decision=Decision.actionable, paths=("js/src/irregexp/util/ZoneShim.h",),
             title="Uncapped `Zone::New` allocation OOM-crashes during regex parsing", **over):
    cits = [RefCitation(filename=p, line=34) for p in paths]
    fields = dict(decision=decision, title=title,
                  confidence=Confidence.high if decision == Decision.strong_evidence
                  else Confidence.probable,
                  mechanism=Claim(statement=_MECH, citations=cits),
                  consistency=Claim(statement=_CONS_2073760, citations=cits))
    if decision in (Decision.lead, Decision.strong_evidence):
        fields["needinfo_draft"] = "look?"
    if decision == Decision.abstain:
        fields.update(abstain_reason="r", abstain_kind=AbstainKind.noise)
    fields.update(over)
    return Dossier(
        candidate=Candidate(node="6e6fbe10aa30", bug=1690142, author="Iain Ireland"),
        verdict=Verdict(**fields),
    )


class TestTheSizeClass(unittest.TestCase):
    def test_the_three_socorro_classes_and_nothing_else(self):
        self.assertEqual(orch._oom_kind(_ZONE_SIG), "unknown")
        self.assertEqual(orch._oom_kind(_COLR_SIG), "large")
        self.assertEqual(orch._oom_kind("OOM | small"), "small")
        self.assertIsNone(orch._oom_kind(
            "js::AutoEnterOOMUnsafeRegion::crash_impl | v8::internal::Zone::New<T>"))
        self.assertIsNone(orch._oom_kind("OOM | huge | x"))
        self.assertIsNone(orch._oom_kind(None))

    def test_the_memory_picture_reads_the_annotations_that_are_there(self):
        self.assertEqual(orch._memory_picture(_raw(_ZONE_SIG, "")),
                         "231 MB of commit space available, 66% of system memory in use")
        self.assertEqual(orch._memory_picture({
            "available_page_file": 230998016, "total_page_file": 34359738368,
            "available_physical_memory": 5707038720, "total_physical_memory": 17111031808,
            "total_virtual_memory": 140737488224256, "system_memory_use_percentage": 66}),
            "231 MB of commit space available of 34.4 GB limit, "
            "5.7 GB of 17.1 GB physical memory free, "
            "66% of system memory in use")
        self.assertEqual(orch._memory_picture({"total_virtual_memory": 4294836224,
                                               "available_virtual_memory": 2992349184}),
                         "4 GB total virtual address space with 2.8 GB free")
        self.assertEqual(orch._memory_picture({"total_virtual_memory": 4294836224}),
                         "4 GB total virtual address space")
        self.assertEqual(orch._memory_picture({"available_page_file": "230998016"}), "")
        self.assertEqual(orch._memory_picture({}), "")


class TestTheGate(unittest.TestCase):
    def test_bug_2073760_is_resource_exhaustion_and_keeps_its_mechanism(self):
        d = _dossier()
        seed = _seed(_raw(_ZONE_SIG, "[unhandlable oom] Irregexp Zone::New"))
        orch._apply_oom_gate(d, seed)
        v = d.verdict
        self.assertEqual((v.decision, v.abstain_kind, v.confidence),
                         (Decision.abstain, AbstainKind.resource_exhaustion, Confidence.low))
        self.assertIn("`[unhandlable oom] Irregexp Zone::New`", v.abstain_reason)
        self.assertIn("OOM-unsafe region", v.abstain_reason)
        self.assertIn("231 MB of commit space available", v.abstain_reason)
        # The mechanism and the title stay on the page; only the filing is gone.
        self.assertEqual(v.mechanism.statement, _MECH)
        self.assertEqual(v.title, "Uncapped `Zone::New` allocation OOM-crashes during regex parsing")
        self.assertEqual(d.corroborations["oom_not_actionable"],
                         {"kind": "unknown", "reason": "[unhandlable oom] Irregexp Zone::New",
                          "memory": "231 MB of commit space available, "
                                    "66% of system memory in use"})

    def test_an_unhandlable_oom_abstains_whatever_the_name_says(self):
        d = _dossier()
        orch._apply_oom_gate(d, _seed(_raw(
            "OOM | large | js::AutoEnterOOMUnsafeRegion::crash_impl | js::gc::FreeSpan::allocate",
            "[unhandlable oom] GC FreeSpan")))
        self.assertEqual(d.verdict.abstain_kind, AbstainKind.resource_exhaustion)
        # Also gate an unprefixed signature when the reason identifies the OOM.
        d = _dossier()
        orch._apply_oom_gate(d, _seed(_raw(
            "js::AutoEnterOOMUnsafeRegion::crash_impl | v8::internal::Zone::New<T>",
            "[Unhandlable OOM] Irregexp Zone::New")))
        self.assertEqual(d.verdict.decision, Decision.abstain)

    def test_unknown_and_small_abstain_on_the_class_alone(self):
        # nsDynamicAtom::Create (captured dossier 22773).
        d = _dossier(paths=("xpcom/ds/nsAtomTable.cpp",))
        orch._apply_oom_gate(d, _seed(_raw("OOM | unknown | nsDynamicAtom::Create",
                                           "MOZ_CRASH(Out of memory atomizing)",
                                           available_page_file=100663296)))
        self.assertEqual(d.verdict.abstain_kind, AbstainKind.resource_exhaustion)
        self.assertIn("size class is `unknown`", d.verdict.abstain_reason)
        self.assertIn("101 MB of commit space available", d.verdict.abstain_reason)
        d = _dossier()
        orch._apply_oom_gate(d, _seed(_raw("OOM | small", None)))
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertEqual(d.corroborations["oom_not_actionable"]["reason"], None)

    def test_bug_2071557_stands(self):
        # A recorded large request is not suppressed.
        d = _dossier(paths=("gfx/thebes/COLRFonts.cpp",), title="kept")
        orch._apply_oom_gate(d, _seed(_raw(_COLR_SIG,
                                           "out of memory: 0x00000000000FF958 bytes requested",
                                           oom_allocation_size=1046872, available_page_file=1e8)))
        self.assertEqual(d.verdict.decision, Decision.actionable)
        self.assertNotIn("oom_not_actionable", d.corroborations)

    def test_a_large_earned_by_the_js_annotation_alone_is_not_a_recorded_request(self):
        # Socorro checks `Reporting` before the size and can assign `large` without recording it.
        d = _dossier(paths=("memory/build/mozjemalloc.cpp",))
        orch._apply_oom_gate(d, _seed(_raw("OOM | large | new", "MOZ_CRASH(OOM)",
                                           oom_allocation_size=None,
                                           js_large_allocation_failure="Reporting")))
        self.assertEqual(d.verdict.abstain_kind, AbstainKind.resource_exhaustion)
        self.assertIn("`JSLargeAllocationFailure: Reporting`", d.verdict.abstain_reason)
        # A recorded large size passes.
        d = _dossier(paths=("memory/build/mozjemalloc.cpp",))
        orch._apply_oom_gate(d, _seed(_raw("OOM | large | new", "MOZ_CRASH(OOM)",
                                           oom_allocation_size=1048576,
                                           js_large_allocation_failure="Reporting")))
        self.assertEqual(d.verdict.decision, Decision.actionable)
        # `Recovered` means GC satisfied the allocation and does not trigger this gate.
        d = _dossier(paths=("memory/build/mozjemalloc.cpp",))
        orch._apply_oom_gate(d, _seed(_raw("OOM | large | new", "MOZ_CRASH(OOM)",
                                           oom_allocation_size=None,
                                           js_large_allocation_failure="Recovered")))
        self.assertEqual(d.verdict.decision, Decision.actionable)

    def test_a_large_class_stands_even_on_a_machine_at_the_edge(self):
        # The deterministic gate uses the recorded request, not the available-memory value.
        d = _dossier(paths=("dom/base/nsContentUtils.cpp", "ipc/glue/BigBuffer.h"))
        orch._apply_oom_gate(d, _seed(_raw(
            "OOM | large | NS_ABORT_OOM | mozilla::ipc::BigBuffer::AllocBuffer", "MOZ_CRASH(OOM)",
            oom_allocation_size=12582912, available_page_file=4096)))
        self.assertEqual(d.verdict.decision, Decision.actionable)

    def test_only_actionable(self):
        # Changeset verdicts have separate gates.
        for decision in (Decision.lead, Decision.strong_evidence):
            d = _dossier(decision=decision)
            orch._apply_oom_gate(d, _seed(_raw(
                "OOM | unknown | js::gc::detail::ChunkPtrHasStoreBuffer", None)))
            self.assertEqual(d.verdict.decision, decision)
        d = _dossier(decision=Decision.abstain)
        orch._apply_oom_gate(d, _seed(_raw(_ZONE_SIG, "[unhandlable oom] x")))
        self.assertEqual(d.verdict.abstain_kind, AbstainKind.noise)

    def test_an_oom_reason_under_an_unprefixed_name_is_classed_from_the_size(self):
        # `OOMSignature` does not inspect `moz_crash_reason`, so the name may be unprefixed.
        d = _dossier(paths=("xpcom/string/nsTSubstring.cpp",))
        orch._apply_oom_gate(d, _seed(_raw("nsTSubstring<T>::Append | TextToNode",
                                           "MOZ_CRASH(OOM)", oom_allocation_size=None)))
        self.assertEqual(d.verdict.abstain_kind, AbstainKind.resource_exhaustion)
        self.assertIn("the crash reason is `MOZ_CRASH(OOM)` and the report recorded no allocation "
                      "size", d.verdict.abstain_reason)
        self.assertEqual(d.corroborations["oom_not_actionable"]["kind"], "unknown")
        d = _dossier(paths=("xpcom/string/nsTSubstring.cpp",))
        orch._apply_oom_gate(d, _seed(_raw("nsTSubstring<T>::Append | TextToNode",
                                           "out of memory: 0x10000 bytes requested",
                                           oom_allocation_size=65536)))
        self.assertIn("recorded a request of 65,536 bytes", d.verdict.abstain_reason)
        # A recorded large request is Socorro's `large`, whatever the name: stands.
        d = _dossier(paths=("gfx/thebes/COLRFonts.cpp",))
        orch._apply_oom_gate(d, _seed(_raw("mozalloc_abort | moz_xmalloc | CreateColorPalette",
                                           "out of memory: 0x00000000000FF958 bytes requested",
                                           oom_allocation_size=1046872)))
        self.assertEqual(d.verdict.decision, Decision.actionable)

    def test_a_crash_that_is_not_an_oom_is_untouched(self):
        d = _dossier(paths=("security/sandbox/chromium/sandbox/win/src/interception.cc",))
        orch._apply_oom_gate(d, _seed(_raw(
            "logging::CheckLogMessage::~CheckLogMessage", "Check failed: thunk_base.")))
        self.assertEqual(d.verdict.decision, Decision.actionable)

    def test_offline_it_is_a_no_op(self):
        d = _dossier()
        orch._apply_oom_gate(d, {"uuid": "u"})
        orch._apply_oom_gate(d, None)
        orch._apply_oom_gate(Dossier(), _seed(_raw(_ZONE_SIG, "[unhandlable oom] x")))
        self.assertEqual(d.verdict.decision, Decision.actionable)
        # The signature may come from the seed alone (the raw crash without one) or the reason
        # from the dump.
        d = _dossier()
        orch._apply_oom_gate(d, {"signature": _ZONE_SIG, "raw_crash": {}})
        self.assertEqual(d.verdict.decision, Decision.abstain)
        d = _dossier()
        orch._apply_oom_gate(
            d, {"raw_crash": {"json_dump": {"moz_crash_reason": "[unhandlable oom] y"}}})
        self.assertEqual(d.verdict.decision, Decision.abstain)

    def test_the_gates_run_it_beside_the_hang_wait_gate(self):
        src = inspect.getsource(orch.apply_deterministic_gates)
        wait = "_apply_hang_wait_gate(result.dossier, seed)"
        oom = "_apply_oom_gate(result.dossier, seed)"
        fold = "_fold_second_opinion("
        self.assertIn(oom, src)
        self.assertLess(src.index(wait), src.index(oom))
        self.assertLess(src.index(oom), src.index(fold))


# Replay fixtures with crash-stats annotations captured on 2026-09-21. Each row is
# (dossier, filed bug, signature, reason, allocation size, available commit space, total virtual
# memory, original verdict, expected verdict). Expected values encode the gate policy.
_OOM_REPLAY = [
    (11320, 2067511, 'OOM | unknown | js::gc::detail::ChunkPtrHasStoreBuffer',
     None, None, 96653312, 140737488224256, 'strong-evidence', 'strong-evidence'),
    (11746, 2069830, 'OOM | unknown | nsGlobalWindowInner::ClearDocumentDependentSlots',
     'MOZ_CRASH(Unhandlable OOM while clearing document dependent slots.)', None, 173305856, 140737488224256, 'lead', 'lead'),
    (12338, None, 'OOM | large | InfallibleAllocPolicy::pod_malloc | mozilla::BufferList<T>::AllocateSegment',
     'out of memory: 0x000000000004E200 bytes requested', 320000, 1360338944, 140737488224256, 'lead', 'lead'),
    (12789, 2069460, 'OOM | large | NS_ABORT_OOM | nsTArray_Impl<T>::SetCapacity<T> | mozilla::dom::EditContext::GetCharacterBounds',
     'MOZ_CRASH(OOM)', 68719463200, None, None, 'strong-evidence', 'strong-evidence'),
    (12829, None, 'OOM | large | NS_ABORT_OOM | nsTArray_Impl<T>::SetCapacity<T> | mozilla::dom::EditContext::GetCharacterBounds',
     'MOZ_CRASH(OOM)', 68719476624, None, None, 'strong-evidence', 'strong-evidence'),
    (13579, None, 'OOM | large | NS_ABORT_OOM | nsTArray_Impl<T>::SetCapacity<T> | mozilla::dom::EditContext::GetCharacterBounds',
     'MOZ_CRASH(OOM)', 68719453648, None, None, 'strong-evidence', 'strong-evidence'),
    (13687, None, 'OOM | large | nsTArray_Impl<T>::SetCapacity | mozilla::gfx::COLRFonts::CreateColorPalette',
     'out of memory: 0x00000000000FF958 bytes requested', 1046872, 1028943872, 140737488224256, 'lead', 'lead'),
    (21473, None, 'OOM | unknown | js::AutoEnterOOMUnsafeRegion::crash_impl | js::AutoEnterOOMUnsafeRegion::crash | js::Nursery::maybeMoveRawNurseryOrMallocBufferOnPromotion',
     '[unhandlable oom] Nursery::maybeMoveRawNurseryOrMallocBufferOnPromotion', None, 786436096, 140737488224256, 'actionable', 'abstain'),
    (21508, None, 'OOM | large | NS_ABORT_OOM | nsTSubstring<T>::AllocFailed | nsTSubstring<T>::Append | TextToNode',
     'MOZ_CRASH(OOM)', 4194296, 72351744, 140737488224256, 'actionable', 'actionable'),
    (21513, None, 'OOM | large | NS_ABORT_OOM | nsTSubstring<T>::AllocFailed | nsTSubstring<T>::Append | TextToNode',
     'MOZ_CRASH(OOM)', 8388600, 2726244352, 2147352576, 'actionable', 'actionable'),
    (21515, 2073327, 'OOM | large | NS_ABORT_OOM | nsTSubstring<T>::AllocFailed | nsTSubstring<T>::Append | TextToNode',
     'MOZ_CRASH(OOM)', 524280, 2355658752, 2147352576, 'actionable', 'actionable'),
    (21664, None, 'OOM | unknown | js::AutoEnterOOMUnsafeRegion::crash_impl | v8::internal::Zone::AllocateArray',
     '[unhandlable oom] Irregexp Zone::New', None, 679960576, 140737488224256, 'actionable', 'abstain'),
    (21755, None, 'OOM | large | nsTArray_Impl<T>::SetCapacity | mozilla::gfx::COLRFonts::CreateColorPalette',
     'out of memory: 0x00000000000FF958 bytes requested', 1046872, 99434496, 140737488224256, 'actionable', 'actionable'),
    (21954, None, 'OOM | unknown | js::AutoEnterOOMUnsafeRegion::crash_impl | v8::internal::Zone::AllocateArray',
     '[unhandlable oom] Irregexp Zone::New', None, 698810368, 140737488224256, 'actionable', 'abstain'),
    (22158, 2073760, 'OOM | unknown | js::AutoEnterOOMUnsafeRegion::crash_impl | js::AutoEnterOOMUnsafeRegion::crash | v8::internal::Zone::New<T>',
     '[unhandlable oom] Irregexp Zone::New', None, 230998016, 140737488224256, 'actionable', 'abstain'),
    (22485, None, 'OOM | unknown | mozilla::wr::Moz2DRenderCallback',
     'MOZ_RELEASE_ASSERT(false)', None, 378773504, 140737488224256, 'actionable', 'abstain'),
    (22535, None, 'OOM | large | NS_ABORT_OOM | nsTSubstring<T>::AllocFailed | nsTSubstring<T>::SetLength | mozilla::dom::SnappyUncompress',
     'MOZ_CRASH(OOM)', 1108666, 824979456, 140737488224256, 'actionable', 'actionable'),
    (22773, None, 'OOM | unknown | nsDynamicAtom::Create',
     'MOZ_CRASH(Out of memory atomizing)', None, 50384896, 140737488224256, 'actionable', 'abstain'),
    (22801, 2073879, 'OOM | large | NS_ABORT_OOM | mozilla::ipc::BigBuffer::AllocBuffer',
     'MOZ_CRASH(OOM)', 7680000, 3099361280, 4294836224, 'actionable', 'actionable'),
    (22808, None, 'OOM | large | NS_ABORT_OOM | mozilla::ipc::BigBuffer::AllocBuffer',
     'MOZ_CRASH(OOM)', 12582912, 38203392, 140737488224256, 'actionable', 'actionable'),
    (22810, None, 'OOM | large | NS_ABORT_OOM | mozilla::ipc::BigBuffer::AllocBuffer',
     'MOZ_CRASH(OOM)', 67380836, 108503040, 140737488224256, 'actionable', 'actionable'),
    (22812, None, 'OOM | large | NS_ABORT_OOM | mozilla::ipc::BigBuffer::AllocBuffer',
     'MOZ_CRASH(OOM)', 4915200, 605982720, 4294836224, 'actionable', 'actionable'),
    (22822, None, 'OOM | large | NS_ABORT_OOM | mozilla::ipc::BigBuffer::AllocBuffer',
     'MOZ_CRASH(OOM)', 1347906552, 1414414336, 140737488224256, 'actionable', 'actionable'),
    (23101, None, 'OOM | unknown | js::AutoEnterOOMUnsafeRegion::crash_impl | v8::internal::Zone::New<T>',
     '[unhandlable oom] Irregexp Zone::New', None, 2749665280, 4294836224, 'actionable', 'abstain'),
    (23414, None, 'OOM | large | NS_ABORT_OOM | nsTSubstring<T>::AllocFailed | nsTSubstring<T>::SetLength | mozilla::dom::SnappyUncompress',
     'MOZ_CRASH(OOM)', 1227749, 633651200, 140737488224256, 'actionable', 'actionable'),
]


class TestTheReplay(unittest.TestCase):
    def test_the_captured_verdicts_follow_the_gate_policy(self):
        for did, bug, sig, reason, alloc, page, vm, decision, expected in _OOM_REPLAY:
            with self.subTest(dossier=did, bug=bug, sig=sig[:60]):
                d = _dossier(decision=Decision(decision))
                raw = {"signature": sig, "moz_crash_reason": reason, "oom_allocation_size": alloc,
                       "available_page_file": page, "total_virtual_memory": vm, "json_dump": {}}
                orch._apply_oom_gate(d, _seed(raw))
                self.assertEqual(d.verdict.decision, Decision(expected))
                if expected == "abstain":
                    self.assertEqual(d.verdict.abstain_kind, AbstainKind.resource_exhaustion)

    def test_the_replay_contains_the_documented_examples(self):
        bugs = {row[1] for row in _OOM_REPLAY}
        self.assertIn(2073760, bugs)   # dropped
        self.assertIn(2073879, bugs)   # kept
        # Dossier 21755 captures the same signature and allocation size as bug 2071557.
        by_id = {row[0]: row for row in _OOM_REPLAY}
        self.assertEqual(by_id[21755][8], "actionable")
        self.assertEqual(by_id[22158][8], "abstain")
        kept = [r for r in _OOM_REPLAY if r[7] == "actionable" and r[8] == "actionable"]
        dropped = [r for r in _OOM_REPLAY if r[7] == "actionable" and r[8] == "abstain"]
        self.assertEqual((len(kept), len(dropped)), (11, 7))
        self.assertTrue(all(r[2].startswith("OOM | large | ") for r in kept))


class TestTheFilerUsesOnlyTheMechanism(unittest.TestCase):
    def test_the_consistency_claim_stays_in_the_dossier_not_the_bug(self):
        dossier = tav._preview_dossier()
        dossier["verdict"]["consistency"]["statement"] = _CONS_2073760
        dossier["verdict"]["consistency"]["citations"] = [{
            "kind": "searchfox", "permalink": "https://consistency-only.example/source#1",
            "symbol_id": "consistency_only",
        }]
        c = tav._build_preview(dossier)["comment"]
        self.assertIn("- " + tav._MECH, c)
        self.assertNotIn("matches the cited crashing line", c)
        self.assertNotIn("pre-existing", c)
        self.assertNotIn("rather than a newly introduced regression", c)
        self.assertNotIn("consistency-only.example", c)
        self.assertNotIn("consistency_only", c)
        self.assertIn("- The failing code comes from", c)
        # Rendering is not allowed to rewrite the dossier kept for audit.
        self.assertEqual(dossier["verdict"]["consistency"]["statement"], _CONS_2073760)

    def test_even_an_affirmative_consistency_claim_is_not_copied(self):
        dossier = tav._preview_dossier()
        c = tav._build_preview(dossier)["comment"]
        self.assertIn("- " + tav._MECH, c)
        self.assertNotIn("- " + tav._FACT, c)
        self.assertIn("- The failing code comes from", c)


class TestThePage(unittest.TestCase):
    def test_crashstack_has_an_abstain_branch_for_it(self):
        # Like the compiled-out and hang-wait suppressions: a suppressed verdict is not an
        # empty one, and "Insufficient evidence" there would read as a failure to look.
        here = pathlib.Path(__file__).resolve().parent.parent
        text = (here / "templates" / "crashstack.html").read_text()
        self.assertIn("corrob.get('oom_not_actionable')", text)
        self.assertIn("lacks enough caller-specific evidence", text)
        self.assertIn("corrob['oom_not_actionable']['memory']", text)
        self.assertIn("corrob['oom_not_actionable']['reason']", text)


class TestTheModelSeesTheEvidence(unittest.TestCase):
    def test_the_crash_facts_carry_the_memory_annotations(self):
        # The prompt gates `actionable` on these; until now none reached ordinary triage, which
        # has no per-report crash-stats tool.
        from crashclouseau.agent import triage
        facts = "\n".join(triage._crash_facts({"raw_crash": {
            "moz_crash_reason": "[unhandlable oom] Irregexp Zone::New",
            "available_page_file": 230998016, "total_page_file": 34359738368,
            "system_memory_use_percentage": 66}}))
        self.assertIn("Memory at crash (what the machine had left): 231 MB of commit space "
                      "available of 34.4 GB limit, 66% of system memory in use", facts)
        self.assertNotIn("OOM allocation size", facts)
        facts = "\n".join(triage._crash_facts({"raw_crash": {
            "moz_crash_reason": "out of memory: 0x00000000000FF958 bytes requested",
            "oom_allocation_size": 1046872}}))
        self.assertIn("OOM allocation size (bytes): 1,046,872", facts)
        # The signature alone also identifies an OOM.
        facts = "\n".join(triage._crash_facts({"signature": _ZONE_SIG, "raw_crash": {
            "available_page_file": 230998016}}))
        self.assertIn("Memory at crash (what the machine had left): 231 MB of commit space "
                      "available", facts)

    def test_a_null_deref_on_windows_keeps_its_prompt(self):
        # Memory annotations also occur on unrelated Windows crashes, so hide them for non-OOMs.
        from crashclouseau.agent import triage
        raw = {"reason": "EXCEPTION_ACCESS_VIOLATION_READ", "moz_crash_reason": None,
               "available_page_file": 230998016, "total_page_file": 34359738368,
               "system_memory_use_percentage": 66, "oom_allocation_size": None}
        facts = "\n".join(triage._crash_facts(
            {"signature": "mozilla::dom::Document::GetDocShell", "raw_crash": raw}))
        self.assertNotIn("Memory at crash", facts)
        self.assertNotIn("OOM allocation size", facts)
        self.assertFalse(triage._is_oom({"signature": "mozilla::Foo"}, raw))
        self.assertTrue(triage._is_oom({}, {"moz_crash_reason": "MOZ_CRASH(OOM)"}))
        self.assertTrue(triage._is_oom({}, {"moz_crash_reason": "[unhandlable oom] x"}))
        self.assertTrue(triage._is_oom({}, {"oom_allocation_size": 65536}))
        self.assertTrue(triage._is_oom({}, {"js_large_allocation_failure": "Reporting"}))
        # `Recovered`: the GC satisfied the allocation; a later unrelated crash is not an OOM.
        self.assertFalse(triage._is_oom({"signature": "mozilla::Foo"},
                                        {"js_large_allocation_failure": "Recovered"}))
        facts = "\n".join(triage._crash_facts({"signature": "OOM | large | new", "raw_crash": {
            "js_large_allocation_failure": "Reporting"}}))
        self.assertIn("JS large allocation failure (`Reporting` = crash while responding to a "
                      "large-allocation OOM; Socorro assigns `OOM | large` even with no size): "
                      "Reporting", facts)
        self.assertFalse(triage._is_oom({}, {"moz_crash_reason": "MOZ_CRASH(Bloom filter)"}))

    def test_a_small_oom_shows_its_size_and_the_gate_says_so(self):
        # Socorro: `OOM | small` is a recorded OOMAllocationSize of at most 256 KB, not a missing
        # one -- the label and the abstain reason must not contradict the number.
        from crashclouseau.agent import triage
        facts = "\n".join(triage._crash_facts({"signature": "OOM | small", "raw_crash": {
            "oom_allocation_size": 65536}}))
        self.assertIn("OOM allocation size (bytes): 65,536", facts)
        d = _dossier()
        orch._apply_oom_gate(d, _seed(_raw("OOM | small", "MOZ_CRASH(OOM)", oom_allocation_size=65536)))
        self.assertIn("size class is `small`: the report recorded a request of 65,536 bytes",
                      d.verdict.abstain_reason)
        d = _dossier()
        orch._apply_oom_gate(d, _seed(_raw("OOM | unknown | nsDynamicAtom::Create", "MOZ_CRASH(x)")))
        self.assertIn("recorded no allocation size", d.verdict.abstain_reason)


class TestTheSpikeReportTool(unittest.TestCase):
    def test_the_report_prints_the_allocation_size_and_the_js_annotation(self):
        from crashclouseau.agent.tools import crashstats
        for key in ("oom_allocation_size", "js_large_allocation_failure", "available_page_file",
                    "total_virtual_memory"):
            self.assertIn(key, crashstats._REPORT_KEYS, key)
        self.assertIn("js_large_allocation_failure", crashstats.TERM_FIELDS)
        self.assertIn("oom_allocation_size", crashstats.NUMERIC_FIELDS)
