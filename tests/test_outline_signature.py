# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Duplicate lookup for Fenix signatures whose frame is an R8 outline."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import bugzilla_apply, utils  # noqa: E402
from crashclouseau.agent import spike_escalation as se  # noqa: E402
from tests.test_autofile import _INFO, _Base  # noqa: E402
from tests.test_spike_escalation import _FilerBase, _esc  # noqa: E402

OUTLINE = "java.lang.RuntimeException: at okio.Buffer$$ExternalSyntheticBUOutline0.m$1(R8$$SyntheticClass:3)"
ALLOCATE = ("java.lang.RuntimeException: at org.mozilla.gecko.process.ServiceAllocator"
            "$DefaultContentPolicy.allocate(ServiceAllocator.java)")


def _frame(original, internal):
    return {"original": original, "internal": internal}


FRAMES = [
    _frame("at okio.Buffer$$ExternalSyntheticBUOutline0.m$1(R8$$SyntheticClass:3)", False),
    _frame("at androidx.fragment.app.Fragment.requireContext(Fragment.java:12)", False),
    _frame("at org.mozilla.gecko.process.ServiceAllocator$DefaultContentPolicy.allocate"
           "(ServiceAllocator.java:44)", True),
    _frame("at org.mozilla.gecko.process.ServiceAllocator$InstanceInfo.<init>"
           "(ServiceAllocator.java:64)", True),
]


class TestTheSpelling(unittest.TestCase):
    def test_the_first_internal_frame_in_socorros_form(self):
        self.assertEqual(utils.outline_signature(OUTLINE, FRAMES), ALLOCATE)

    def test_a_kotlin_line_is_kept(self):
        frames = [FRAMES[0], _frame("at org.mozilla.fenix.Foo.bar(Foo.kt:7)", True)]
        self.assertEqual(utils.outline_signature(OUTLINE, frames),
                         "java.lang.RuntimeException: at org.mozilla.fenix.Foo.bar(Foo.kt:7)")

    def test_a_frame_without_a_file_is_an_unknown_source(self):
        for stored, socorro in (("(:30)", "(Unknown Source:30)"), ("()", "(Unknown Source)")):
            frames = [_frame("at org.mozilla.geckoview.GeckoRuntime$1.handleMessage" + stored,
                             True)]
            with self.subTest(stored):
                self.assertEqual(utils.outline_signature(OUTLINE, frames),
                                 "java.lang.RuntimeException: at org.mozilla.geckoview."
                                 "GeckoRuntime$1.handleMessage" + socorro)

    def test_a_merged_enum_class_counts(self):
        sig = ("java.lang.RuntimeException: at com.airbnb.lottie.model.content.PolystarShape"
               "$Type$EnumUnboxingLocalUtility.onShutdown(Unknown Source:4)")
        frames = [_frame("at org.mozilla.geckoview.GeckoRuntime.shutdown(GeckoRuntime.java:9)",
                         True)]
        self.assertEqual(utils.outline_signature(sig, frames), "java.lang.RuntimeException: at "
                         "org.mozilla.geckoview.GeckoRuntime.shutdown(GeckoRuntime.java)")

    def test_what_gets_no_spelling(self):
        lambda_sig = ("java.lang.RuntimeException: at mozilla.components.browser.engine.gecko."
                      "GeckoEngine$$ExternalSyntheticLambda36.onShutdown(R8$$SyntheticClass:5)")
        for name, sig, frames in (("a lambda frame", lambda_sig, FRAMES),
                                  ("a native signature", "mozilla::Foo::Bar", FRAMES),
                                  ("no internal frame", OUTLINE, FRAMES[:2]),
                                  ("no frames", OUTLINE, None),
                                  ("internal outline only", OUTLINE,
                                   [dict(FRAMES[0], internal=True)])):
            with self.subTest(name):
                self.assertIsNone(utils.outline_signature(sig, frames))


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class TestTheVenueSearch(unittest.TestCase):
    BUG = {"id": 2005069, "summary": "Handle running out of Android ContentServices",
           "creation_time": "2025-12-09T00:00:00Z", "product": "GeckoView", "keywords": [],
           "cf_crash_signature": "[@ {}]".format(ALLOCATE), "regressed_by": []}

    def test_an_extra_spelling_is_a_sibling_venue(self):
        seen = {}

        def fake_get(url, params=None, timeout=None):
            seen.update(params or {})
            return _Resp({"bugs": [self.BUG]})

        with mock.patch.object(bugzilla_apply.net, "get", side_effect=fake_get), \
                mock.patch.object(bugzilla_apply, "_duplicate_targets_for_signature",
                                  return_value=[]):
            rows = bugzilla_apply._open_bugs_for_signature(OUTLINE, extra=(ALLOCATE,))
        self.assertIn(ALLOCATE, {v for k, v in seen.items() if k.startswith("v")})
        self.assertEqual([r["id"] for r in rows], [2005069])
        self.assertEqual((rows[0]["via_signature"], rows[0]["via_relation"]),
                         (ALLOCATE, "outline"))

    def _lookup(self, bugs, frame):
        with mock.patch.object(bugzilla_apply.net, "get", return_value=_Resp({"bugs": bugs})), \
                mock.patch.object(bugzilla_apply, "_duplicate_targets_for_signature",
                                  return_value=[]):
            return bugzilla_apply._open_bugs_for_signature(OUTLINE, extra=(frame,))

    def test_the_callers_bug_beats_a_bug_that_carries_the_shared_outline(self):
        tab = "java.lang.RuntimeException: at mozilla.components.browser.session.Reader.tab()"
        both = "[@ {}]\n[@ {}]".format(ALLOCATE, OUTLINE)
        carries_both = dict(self.BUG, cf_crash_signature=both)
        other_caller = dict(self.BUG, id=2090000, cf_crash_signature="[@ {}]".format(tab))
        rows = self._lookup([carries_both, other_caller], tab)
        self.assertEqual([r["id"] for r in rows], [2090000])
        self.assertEqual(rows[0]["via_relation"], "outline")
        rows = self._lookup([carries_both, other_caller], ALLOCATE)
        self.assertEqual([r["id"] for r in rows], [2005069])
        self.assertNotIn("via_relation", rows[0])

    def test_a_frame_named_only_in_a_summary_does_not_displace_the_venue(self):
        own = dict(self.BUG, id=2080031, cf_crash_signature="[@ {}]".format(OUTLINE))
        frame = dict(self.BUG, id=2090000, cf_crash_signature="",
                     summary="Crash in [@ {}]".format(ALLOCATE))
        rows = self._lookup([own, frame], ALLOCATE)
        self.assertEqual([(r["id"], r.get("summary_only")) for r in rows],
                         [(2080031, None), (2090000, True)])

    def _lookup_with_dups(self, bugs, dups, targets, frame):
        def fake_get(url, params=None, timeout=None):
            params = params or {}
            if params.get("resolution") == "DUPLICATE":
                return _Resp({"bugs": dups})
            if "id" in params:
                return _Resp({"bugs": targets})
            return _Resp({"bugs": bugs})

        with mock.patch.object(bugzilla_apply.net, "get", side_effect=fake_get):
            return bugzilla_apply._open_bugs_for_signature(OUTLINE, extra=(frame,))

    def test_a_callers_bug_reached_through_a_duplicate_still_wins(self):
        outline_only = dict(self.BUG, id=2080031, cf_crash_signature="[@ {}]".format(OUTLINE))
        dup = {"id": 2006000, "summary": "dup", "dupe_of": 2005069,
               "cf_last_resolved": "2025-12-10T00:00:00Z",
               "cf_crash_signature": "[@ {}]".format(ALLOCATE)}
        target = dict(self.BUG, status="NEW", resolution="", dupe_of=None)
        rows = self._lookup_with_dups([outline_only], [dup], [target], ALLOCATE)
        self.assertEqual([r["id"] for r in rows], [2005069])
        self.assertEqual((rows[0]["via_relation"], rows[0]["via_duplicates"]),
                         ("outline", [2006000]))
        self.assertTrue(rows[0]["carries_frame"])

    def test_an_outline_bug_that_is_the_callers_duplicate_target_stays(self):
        outline_only = dict(self.BUG, cf_crash_signature="[@ {}]".format(OUTLINE))
        dup = {"id": 2006000, "summary": "dup", "dupe_of": 2005069,
               "cf_last_resolved": "2025-12-10T00:00:00Z",
               "cf_crash_signature": "[@ {}]".format(ALLOCATE)}
        target = dict(self.BUG, status="NEW", resolution="", dupe_of=None)
        rows = self._lookup_with_dups([outline_only], [dup], [target], ALLOCATE)
        self.assertEqual([r["id"] for r in rows], [2005069])
        self.assertTrue(rows[0]["carries_frame"])

    def test_a_frame_already_in_the_family_keeps_its_provenance(self):
        family = {"predecessors": [], "siblings": [
            {"signature": ALLOCATE, "relation": "frame-variant", "status": "younger"}]}
        outline_only = dict(self.BUG, id=2080031, cf_crash_signature="[@ {}]".format(OUTLINE))
        with mock.patch.object(bugzilla_apply.net, "get",
                               return_value=_Resp({"bugs": [outline_only, self.BUG]})), \
                mock.patch.object(bugzilla_apply, "_duplicate_targets_for_signature",
                                  return_value=[]) as dups:
            rows = bugzilla_apply._open_bugs_for_signature(OUTLINE, family=family,
                                                           extra=(ALLOCATE,))
        self.assertEqual([r["id"] for r in rows], [2005069])
        self.assertEqual(rows[0]["via_relation"], "sibling")
        self.assertTrue(rows[0]["carries_frame"])
        self.assertFalse(bugzilla_apply._attaches_signature(rows[0], OUTLINE))
        self.assertEqual(dups.call_args.kwargs["frames"], [ALLOCATE])

    def test_a_duplicate_through_a_family_frame_carries_it(self):
        dup = {"id": 2006000, "summary": "dup", "dupe_of": 2005069,
               "cf_last_resolved": "2025-12-10T00:00:00Z",
               "cf_crash_signature": "[@ {}]".format(ALLOCATE)}
        target = dict(self.BUG, status="NEW", resolution="", dupe_of=None)

        def fake_get(url, params=None, timeout=None):
            return _Resp({"bugs": [dup] if (params or {}).get("resolution") == "DUPLICATE"
                          else [target]})

        with mock.patch.object(bugzilla_apply.net, "get", side_effect=fake_get):
            rows = bugzilla_apply._duplicate_targets_for_signature(
                OUTLINE, spellings=[ALLOCATE], relations={ALLOCATE: "sibling"},
                frames=[ALLOCATE])
        self.assertEqual((rows[0]["id"], rows[0]["via_relation"]), (2005069, "sibling"))
        self.assertTrue(rows[0]["carries_frame"])

    def test_a_foreign_frame_match_does_not_displace_the_venue(self):
        outline_only = dict(self.BUG, id=2080031, cf_crash_signature="[@ {}]".format(OUTLINE))
        desktop = dict(self.BUG, id=2090000, product="Firefox")
        with mock.patch.object(bugzilla_apply.net, "get",
                               return_value=_Resp({"bugs": [outline_only, desktop]})), \
                mock.patch.object(bugzilla_apply, "_duplicate_targets_for_signature",
                                  return_value=[]):
            rows = bugzilla_apply._open_bugs_for_signature(OUTLINE, product="Fenix",
                                                           extra=(ALLOCATE,))
        self.assertEqual([r["id"] for r in rows], [2080031, 2090000])
        venues, other_app = bugzilla_apply._split_by_application(rows, "Fenix")
        self.assertEqual(([r["id"] for r in venues], [r["id"] for r in other_app]),
                         ([2080031], [2090000]))

    def test_a_same_application_frame_match_still_displaces_it(self):
        outline_only = dict(self.BUG, id=2080031, cf_crash_signature="[@ {}]".format(OUTLINE))
        desktop = dict(self.BUG, id=2090000, product="Firefox")
        with mock.patch.object(bugzilla_apply.net, "get",
                               return_value=_Resp({"bugs": [outline_only, desktop, self.BUG]})), \
                mock.patch.object(bugzilla_apply, "_duplicate_targets_for_signature",
                                  return_value=[]):
            rows = bugzilla_apply._open_bugs_for_signature(OUTLINE, product="Fenix",
                                                           extra=(ALLOCATE,))
        venues, _other = bugzilla_apply._split_by_application(rows, "Fenix")
        self.assertEqual([r["id"] for r in venues], [2005069])

    def test_without_a_caller_match_the_outline_bug_stays(self):
        outline_only = dict(self.BUG, cf_crash_signature="[@ {}]".format(OUTLINE))
        rows = self._lookup([outline_only], ALLOCATE)
        self.assertEqual([r["id"] for r in rows], [2005069])
        self.assertNotIn("carries_frame", rows[0])


class TestTheWording(unittest.TestCase):
    VENUE = {"id": 2005069, "via_signature": ALLOCATE, "via_relation": "outline"}

    def test_the_clause_and_the_note(self):
        self.assertEqual(bugzilla_apply._via_clause(self.VENUE),
                         " (on this crash's first internal frame `{}`)".format(ALLOCATE))
        note = bugzilla_apply._venue_via_signature_note(OUTLINE, self.VENUE, {})
        self.assertIn("first internal frame `{}` is on this bug".format(ALLOCATE), note)
        self.assertIn("R8 outline frame shared by unrelated crashes, so it was not added", note)
        self.assertNotIn("has been added", note)

    def test_a_family_match_that_carries_the_frame_says_it_was_not_added(self):
        venue = dict(self.VENUE, via_relation="sibling", carries_frame=True)
        note = bugzilla_apply._venue_via_signature_note(OUTLINE, venue, {})
        self.assertIn("first internal frame `{}` is on this bug".format(ALLOCATE), note)
        self.assertNotIn("has been added", note)

    def test_an_outline_signature_is_never_attached(self):
        plain = "mozilla::Foo::Bar"
        for name, venue, signature, attaches in (
                ("outline relation", self.VENUE, plain, False),
                ("sibling", dict(self.VENUE, via_relation="sibling"), plain, True),
                ("handoff", dict(self.VENUE, via_relation="handoff"), plain, True),
                ("sibling carrying the frame",
                 dict(self.VENUE, via_relation="sibling", carries_frame=True), plain, False),
                ("sibling of an outline signature",
                 dict(self.VENUE, via_relation="sibling"), OUTLINE, False),
                ("no other name", {"id": 1}, plain, False)):
            with self.subTest(name):
                self.assertIs(bugzilla_apply._attaches_signature(venue, signature), attaches)


class TestTheTriageFiler(_Base):
    def test_the_lookup_asks_for_the_internal_frame(self):
        info = dict(_INFO, signature=OUTLINE, product="Fenix")
        bugzilla_apply.autofile_bug("u-1", info, {"frames": FRAMES},
                                    {"candidate": {"node": "n"}}, "lead", 70)
        call = bugzilla_apply._open_bugs_for_signature.call_args
        self.assertEqual((call.args[0], call.kwargs["extra"]), (OUTLINE, (ALLOCATE,)))

    def test_an_ordinary_crash_asks_as_before(self):
        self._file()
        call = bugzilla_apply._open_bugs_for_signature.call_args
        self.assertNotIn("extra", call.kwargs)


class TestTheSpikeFiler(_FilerBase):
    def test_a_comment_through_the_frame_attaches_nothing(self):
        venue = {"id": 2005069, "creation_time": "2025-12-09T00:00:00Z", "product": "GeckoView",
                 "keywords": [], "regressed_by": [], "via_signature": ALLOCATE,
                 "via_relation": "outline", "carries_frame": True}
        brief = dict(self.brief, signature=OUTLINE, stack_frames={"frames": FRAMES})
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature",
                               return_value=[venue]), \
                mock.patch.object(bugzilla_apply, "_attach_signature") as attach:
            res = se.file_spike_bug(_esc(signature=OUTLINE, product="Fenix"), brief,
                                    self.findings.model_copy(update={"culprit": None}))
        self.assertEqual((res["filed"], res["bug"], res["mode"]), (True, 2005069, "spike_comment"))
        self.assertEqual(res["venue_via_relation"], "outline")
        self.assertNotIn("signature_attached", res)
        attach.assert_not_called()
        self.assertIn("so it was not added to this bug", self.comments[0][1])

    def test_a_family_venue_carrying_the_frame_attaches_nothing(self):
        venue = {"id": 2005069, "creation_time": "2025-12-09T00:00:00Z", "product": "GeckoView",
                 "keywords": [], "regressed_by": [], "via_signature": ALLOCATE,
                 "via_relation": "sibling", "carries_frame": True}
        brief = dict(self.brief, signature=OUTLINE, stack_frames={"frames": FRAMES})
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature",
                               return_value=[venue]), \
                mock.patch.object(bugzilla_apply, "_attach_signature") as attach:
            res = se.file_spike_bug(_esc(signature=OUTLINE, product="Fenix"), brief,
                                    self.findings.model_copy(update={"culprit": None}))
        self.assertEqual((res["filed"], res["bug"]), (True, 2005069))
        self.assertNotIn("signature_attached", res)
        attach.assert_not_called()
        self.assertIn("so it was not added to this bug", self.comments[0][1])

    def test_the_lookup_asks_for_the_internal_frame(self):
        brief = dict(self.brief, signature=OUTLINE, stack_frames={"frames": FRAMES})
        se.file_spike_bug(_esc(signature=OUTLINE, product="Fenix"), brief, self.findings)
        call = bugzilla_apply._open_bugs_for_signature.call_args
        self.assertEqual((call.args[0], call.kwargs["extra"]), (OUTLINE, (ALLOCATE,)))


if __name__ == "__main__":
    unittest.main()
