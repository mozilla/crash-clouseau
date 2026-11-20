# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""Venue search under shorter forms of the signature.

Run: DATABASE_URL=sqlite:// uv run python -m unittest tests.test_shorter_signature_venue
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import bugzilla_apply  # noqa: E402
from tests.ipc_signatures import (  # noqa: E402
    IDB_ANNOTATION, IDB_MESSAGE, IDB_SIGNATURE, IPC_SEND, LS_ANNOTATION, LS_MESSAGE, LS_SIGNATURE,
)

VARIANT = "mozilla::Variant<T>::as"
SETCOLOR = "mozilla::layers::AnimatedValue::SetColor"
SIG = "{} | {}".format(VARIANT, SETCOLOR)
SAFE = ("mozilla::freestanding::SafeThreadLocal<T>::get | "
        "mozilla::freestanding::TakePendingFileHandle | "
        "mozilla::freestanding::patched_NtMapViewOfSection | cyinjct.dll | "
        "RtlHeapTrkInitialize | BaseThreadInitThunk")


def _bug(id_, summary, cf, creation="2023-12-26T00:00:00Z"):
    return {"id": id_, "summary": summary, "cf_crash_signature": cf, "creation_time": creation,
            "product": "Core", "keywords": [], "regressed_by": []}


OLD = _bug(1871890, "Crash in [@ mozilla::Variant<T>::as] via AnimatedValue::SetColor",
           "[@ mozilla::Variant<T>::as]")
OTHER_CALLER = _bug(1902941, "Crash in [@ mozilla::Variant<T>::as] from DirectoryLockImpl::Overlaps",
                    "[@ mozilla::Variant<T>::as]")
OWN = _bug(2087941, "Crash in [@ {}]".format(SIG), "[@ {}]".format(SIG), "2026-11-02T04:33:07Z")


def _search(bugs, signature=SIG, product="Firefox"):
    response = mock.Mock()
    response.json.return_value = {"bugs": bugs}
    with mock.patch.object(bugzilla_apply.net, "get", return_value=response) as get, \
            mock.patch.object(bugzilla_apply, "_duplicate_targets_for_signature", return_value=[]):
        rows = bugzilla_apply._open_bugs_for_signature(signature, product=product)
    return rows, get.call_args.kwargs["params"]


class TestTruncations(unittest.TestCase):
    def test_the_prefix_listed_head_is_the_old_name_and_the_call_site_is_cut(self):
        self.assertEqual(bugzilla_apply._signature_truncations(SIG), {VARIANT: SETCOLOR})

    def test_a_thread_entry_is_not_what_was_cut(self):
        t = bugzilla_apply._signature_truncations(SAFE)
        short = "mozilla::freestanding::SafeThreadLocal<T>::get | " \
                "mozilla::freestanding::TakePendingFileHandle"
        self.assertEqual(t[short], "mozilla::freestanding::patched_NtMapViewOfSection")
        self.assertNotIn("BaseThreadInitThunk", t.values())

    def test_a_frame_the_shorter_form_holds_is_not_a_cut(self):
        sig = "core::ptr::drop_in_place<T> | core::ptr::drop_in_place<T> | core::ptr::drop_in_place<T>"
        self.assertEqual(bugzilla_apply._signature_truncations(sig), {})
        sig = ("OOM | large | NS_ABORT_OOM | IPC::ParamTraits<JSStructuredCloneData>::Read | "
               "IPC::ParamTraits<mozilla::SerializedStructuredCloneBuffer>::Read")
        self.assertEqual(bugzilla_apply._signature_truncations(sig), {})

    def test_no_shorter_form_of_crash_words_a_single_frame_or_a_shutdown_timeout(self):
        for sig in ("JSScript::getAtom", "OOM | large | mozilla::dom::Foo::Bar",
                    "AsyncShutdownTimeout | profile-before-change | Blocker: flushing"):
            self.assertEqual(bugzilla_apply._signature_truncations(sig), {}, sig)

    def test_at_most_the_longest_few(self):
        sig = " | ".join("mozilla::Variant<T>::f{}".format(i) for i in range(8)) + " | " + SETCOLOR
        t = bugzilla_apply._signature_truncations(sig)
        self.assertEqual(len(t), bugzilla_apply._MAX_TRUNCATIONS)
        self.assertEqual(max(t, key=len), sig.rsplit(" | ", 1)[0])


IPC_CALLER = "DecryptingInputStream::Read"
IPC_FRAME = f"mozilla::dom::quota::{IPC_CALLER}"
BARE_IPC = f"{IPC_SEND} | {IPC_FRAME}"
IDB_LONG = f"{BARE_IPC} | {IDB_ANNOTATION}"
LS_LONG = f"{BARE_IPC} | {LS_ANNOTATION}"


class TestIpcMessageTruncations(unittest.TestCase):
    def test_truncating_keeps_the_message_and_requires_a_code_frame(self):
        self.assertEqual(bugzilla_apply._signature_truncations(IDB_LONG), {IDB_SIGNATURE: IPC_FRAME})
        self.assertEqual(bugzilla_apply._signature_truncations(IDB_SIGNATURE), {})
        module = f"xul.dll | {IPC_FRAME} | {IDB_ANNOTATION}"
        self.assertEqual(bugzilla_apply._signature_truncations(module), {})

    def test_the_bare_prefix_is_not_searched_for_an_annotated_crash(self):
        bug = _bug(1, f"Crash in [@ {LS_LONG}]", f"[@ {IPC_SEND}]\n[@ {LS_LONG}]")
        rows, params = _search([bug], signature=IDB_LONG)
        self.assertEqual(rows, [])
        self.assertNotIn(IPC_SEND, {v for k, v in params.items() if k.startswith("v")})

    def test_an_unannotated_crash_needs_unannotated_frame_evidence(self):
        prefix = f"[@ {IPC_SEND}]"
        cases = (
            (f"Crash in [@ {LS_LONG}]", prefix, False),
            ("Crash", f"{prefix}\n[@ {LS_LONG}]", False),
            (f"Crash with {LS_ANNOTATION} via {IPC_CALLER}", prefix, False),
            (f"Crash sending {LS_MESSAGE} from {IPC_CALLER}", prefix, False),
            (f"Crash in {prefix} via {IPC_CALLER}", prefix, True),
            ("Crash", f"{prefix}\n[@ {BARE_IPC} | Other::Caller]", True),
        )
        for title, signatures, matches in cases:
            with self.subTest(title=title, signatures=signatures):
                rows, _ = _search([_bug(1, title, signatures)], signature=BARE_IPC)
                self.assertEqual([row["via_relation"] for row in rows], ["truncation"] if matches else [])

    def test_the_frame_and_message_must_come_from_the_same_signature(self):
        prefix = f"[@ {IDB_SIGNATURE}]"
        cases = (
            (f"Crash in {prefix} and [@ {LS_LONG}]", False),
            (f"Crash in {prefix} and [@ {LS_SIGNATURE}] via {IPC_CALLER}", False),
            (f"Crash in [@ {IDB_LONG} | Other::Caller] and [@ {LS_LONG}]", True),
            (f"Crash in {prefix} via {IPC_CALLER} [Windows]", True),
            (f"Crash sending {IDB_MESSAGE} from {IPC_CALLER}", True),
        )
        for title, matches in cases:
            with self.subTest(title=title):
                rows, _ = _search([_bug(1, title, prefix)], signature=IDB_LONG)
                evidence = [(row["via_relation"], row["via_signature"], row["via_frame"]) for row in rows]
                self.assertEqual(evidence, [("truncation", IDB_SIGNATURE, IPC_FRAME)] if matches else [])

    def test_summary_parts_keep_nested_brackets(self):
        signature = "mozilla::Array<T>::operator[] | <[T] as core::Foo>::bar"
        signatures, prose = bugzilla_apply._summary_parts(
            f"Crash in [@ {signature}] via Baz::Qux [Windows] and [@ X::y]")
        self.assertEqual(signatures, [signature, "X::y"])
        self.assertEqual(prose.split(), "Crash in via Baz::Qux [Windows] and".split())


class TestNamesFrame(unittest.TestCase):
    def test_the_summary_or_another_entry_names_it(self):
        self.assertTrue(bugzilla_apply._truncation_venue(OLD, VARIANT, SETCOLOR))
        entry = dict(OTHER_CALLER, cf_crash_signature="[@ {}]\n[@ {} | {}]".format(
            VARIANT, VARIANT, SETCOLOR))
        self.assertTrue(bugzilla_apply._truncation_venue(entry, VARIANT, SETCOLOR))

    def test_another_caller_or_a_longer_word_is_not_it(self):
        self.assertFalse(bugzilla_apply._truncation_venue(OTHER_CALLER, VARIANT, SETCOLOR))
        longer = dict(OLD, summary="Crash in [@ mozilla::Variant<T>::as] via AnimatedValue::SetColorFoo")
        self.assertFalse(bugzilla_apply._truncation_venue(longer, VARIANT, SETCOLOR))

    def test_a_lambda_is_keyed_by_its_owner(self):
        foo = "mozilla::dom::Foo::Run::<T>::operator()"
        cf = "[@ {}]".format(VARIANT)
        for owner, matches in (("Foo", True), ("Bar", False)):
            caller = "mozilla::dom::{}::Run::$::operator()".format(owner)
            bugs = (_bug(77, "Crash via " + caller, cf),
                    _bug(78, "Crash", "{}\n[@ {}]".format(cf, caller)))
            for bug in bugs:
                with self.subTest(owner=owner, bug=bug):
                    self.assertEqual(bugzilla_apply._truncation_venue(bug, VARIANT, foo), matches)
        self.assertNotEqual(
            bugzilla_apply._frame_key("mozilla::A::Init::<T>::operator()::<T>::operator()"),
            bugzilla_apply._frame_key("mozilla::B::Init::<T>::operator()::<T>::operator()"))

    def test_a_closure_named_in_a_summary_is_found(self):
        cf = "[@ {}]".format(VARIANT)
        bug = _bug(77, "Crash via webrender::scene::build::{{closure}}", cf)
        for cut in ("webrender::scene::build::{{closure}}", "webrender::scene::build::{closure#0}"):
            with self.subTest(cut=cut):
                self.assertTrue(bugzilla_apply._truncation_venue(bug, VARIANT, cut))
        self.assertFalse(bugzilla_apply._truncation_venue(bug, VARIANT, "webrender::scene::other::{{closure}}"))

    def test_only_an_exact_entry_carries_it(self):
        summary_only = dict(OLD, cf_crash_signature="")
        self.assertFalse(bugzilla_apply._truncation_venue(summary_only, VARIANT, SETCOLOR))
        longer = dict(OLD, cf_crash_signature="[@ mozilla::Variant<T>::as | Foo::Bar]")
        self.assertFalse(bugzilla_apply._truncation_venue(longer, VARIANT, SETCOLOR))


class TestTheVenueSearch(unittest.TestCase):
    def test_the_old_bug_is_asked_for_and_found(self):
        rows, seen = _search([OLD, OTHER_CALLER])
        values = {seen[k] for k in seen if k.startswith("v")}
        self.assertEqual(values, {SIG, "[@ " + SIG, VARIANT})
        self.assertEqual([r["id"] for r in rows], [1871890])
        row = rows[0]
        self.assertEqual((row["via_signature"], row["via_relation"], row["via_frame"]),
                         (VARIANT, "truncation", SETCOLOR))
        self.assertNotIn("summary_only", row)

    def test_a_spaced_or_bare_entry_is_found(self):
        for cf in ("[@ mozilla::Variant<T>::as ]", "mozilla::Variant<T>::as"):
            with self.subTest(cf=cf):
                rows, _ = _search([dict(OLD, cf_crash_signature=cf)])
                self.assertEqual([(r["id"], r["via_relation"]) for r in rows],
                                 [(1871890, "truncation")])

    def test_another_lambda_spelling_is_asked_for_and_found(self):
        sig = "mozilla::dom::Foo::Run::<T>::operator() | mozilla::dom::Callback::Notify"
        dollar = "mozilla::dom::Foo::Run::$::operator()"
        bug = _bug(77, "Crash in [@ {}] via Callback::Notify".format(dollar), "[@ {}]".format(dollar))
        rows, seen = _search([bug], sig)
        self.assertIn(dollar, {seen[k] for k in seen if k.startswith("v")})
        self.assertEqual((rows[0]["id"], rows[0]["via_signature"]), (77, dollar))

    def test_the_signatures_own_bug_wins(self):
        rows, _ = _search([OLD, OWN])
        self.assertEqual([r["id"] for r in rows], [2087941])
        self.assertNotIn("via_relation", rows[0])

    def test_another_applications_own_bug_does_not_evict_it(self):
        desktop = dict(OWN, id=2000000, product="Firefox")
        rows, _ = _search([OLD, desktop], product="Fenix")
        venues, other_app = bugzilla_apply._split_by_application(rows, "Fenix")
        self.assertEqual(([r["id"] for r in venues], [r["id"] for r in other_app]),
                         ([1871890], [2000000]))

    def test_a_single_frame_signature_asks_what_it_did(self):
        _, seen = _search([], signature="JSScript::getAtom")
        self.assertEqual({k for k in seen if k.startswith("v")}, {"v1", "v2"})


class TestTheComment(unittest.TestCase):
    VENUE = {"id": 1871890, "via_signature": VARIANT, "via_relation": "truncation",
             "via_frame": SETCOLOR}

    def test_the_note_says_why_and_the_signature_is_attached(self):
        self.assertTrue(bugzilla_apply._attaches_signature(self.VENUE, SIG))
        self.assertEqual(
            bugzilla_apply._venue_via_signature_note(SIG, self.VENUE, {}),
            "_This bug carries the shorter signature `[@ {}]` and names `{}` from this "
            "crash's signature `{}`._".format(VARIANT, SETCOLOR, SIG))
        self.assertEqual(bugzilla_apply._via_clause(self.VENUE),
                         " (on this crash's shorter signature `{}`)".format(VARIANT))


if __name__ == "__main__":
    unittest.main()
