# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""Recent FIXED counts, triage-owner routing, and fallback behavior.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        python -m unittest tests.test_inactive_author
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import config, report_bug  # noqa: E402
from tests import test_actionable_verdict as tav  # noqa: E402
from tests.test_actionable_population import _OWNER, _Response  # noqa: E402
from tests.test_incomplete_fix import TestTheBugItWouldFile as _Fix  # noqa: E402

_OWNER_ASK = ":jimm, as triage owner, can you have a look please?"
_AUTHOR_ASK = ":bobowen, can you have a look please?"


def _lead():
    d = tav._preview_dossier()
    d["verdict"]["decision"] = "lead"
    return d


def _preview(dossier, fixes, owner="owner@moz.example", found=None):
    with mock.patch.object(report_bug, "_person_for_account",
                           return_value=dict(_OWNER) if found is None else found):
        return tav._build_preview(dossier, owner=owner, fixes=fixes)


class TestRecentFixes(unittest.TestCase):
    def setUp(self):
        report_bug._FIXED_CACHE.clear()
        self.addCleanup(report_bug._FIXED_CACHE.clear)

    def test_the_query_counts_fixed_assignments_in_the_window(self):
        with mock.patch.object(report_bug.net, "get",
                               return_value=_Response({"bug_count": 13})) as get:
            self.assertEqual(report_bug._recent_fixes("a@x", 182), 13)
            self.assertEqual(report_bug._recent_fixes("A@x", 182), 13)
        get.assert_called_once()
        self.assertTrue(get.call_args.args[0].endswith("/rest/bug"))
        self.assertEqual(get.call_args.kwargs["params"], {
            "assigned_to": "a@x", "resolution": "FIXED", "count_only": 1,
            "f1": "cf_last_resolved", "o1": "greaterthaneq", "v1": "-182d"})

    def test_a_failed_or_odd_answer_is_unknown_and_not_cached(self):
        for resp in (_Response({}, fail=True), _Response({"error": True}),
                     _Response({"bug_count": "3"})):
            with self.subTest(resp.payload), \
                    mock.patch.object(report_bug.net, "get", return_value=resp):
                self.assertIsNone(report_bug._recent_fixes("c@x", 182))
        self.assertEqual(report_bug._FIXED_CACHE, {})


class TestInactiveAuthor(unittest.TestCase):
    _BOB = {"nick": "bobowen", "account": "bob@x.com"}

    def _check(self, count, person=None, days=182, minimum=7):
        with mock.patch.object(report_bug, "_recent_fixes", return_value=count) as fixes:
            got = report_bug._inactive_author(self._BOB if person is None else person,
                                              days, minimum)
        return got, fixes

    def test_the_threshold_is_more_than_six(self):
        self.assertTrue(self._check(0)[0])
        self.assertTrue(self._check(6)[0])
        self.assertFalse(self._check(7)[0])

    def test_unknown_counts_keep_the_author(self):
        self.assertFalse(self._check(None)[0])

    def test_nothing_is_read_without_an_account_or_with_a_knob_off(self):
        for name, over in (("no account", {"person": {"nick": "bob"}}),
                           ("no person", {"person": {}}),
                           ("days off", {"days": 0}),
                           ("minimum off", {"minimum": None})):
            with self.subTest(name):
                got, fixes = self._check(0, **over)
                self.assertFalse(got)
                fixes.assert_not_called()

    def test_the_defaults(self):
        policy = config.get_agent_autofile("nightly")
        self.assertEqual((policy["author_fixed_days"], policy["author_fixed_min"]), (182, 7))


class TestTheAsk(unittest.TestCase):
    def test_a_lead_by_an_inactive_author_asks_the_triage_owner(self):
        p = _preview(_lead(), fixes=0)
        self.assertEqual(p["needinfo_email"], "owner@moz.example")
        self.assertEqual(p["needinfo"], _OWNER_ASK)
        self.assertIn(_OWNER_ASK, p["comment"])
        self.assertIn("(bug 2010557) by :bobowen.", p["comment"])
        self.assertNotIn(_AUTHOR_ASK, p["comment"])

    def test_an_actionable_recent_origin_by_an_inactive_author_asks_the_triage_owner(self):
        # A recent origin bypasses the age rule; the FIXED-count rule still applies.
        d = tav._preview_dossier(actionable_origin_age={"days_before_build": 30.0})
        p = _preview(d, fixes=2)
        self.assertEqual(p["needinfo_email"], "owner@moz.example")
        self.assertEqual(p["needinfo"], _OWNER_ASK)

    def test_what_keeps_the_author(self):
        cases = {
            "seven fixed": {"fixes": 7},
            "count unknown": {"fixes": None},
            "owner unreadable": {"fixes": 0, "owner": None},
            "no owner": {"fixes": 0, "owner": ""},
            "owner is the author": {"fixes": 0, "owner": "BOB@x.com"},
            "owner not askable": {"fixes": 0, "found": {}},
        }
        for name, over in cases.items():
            with self.subTest(name):
                p = _preview(_lead(), **over)
                self.assertEqual(p["needinfo_email"], "bob@x.com")
                self.assertEqual(p["needinfo"], _AUTHOR_ASK)

    def test_a_stale_wake_keeps_its_triage_owner_without_a_count(self):
        stale = {"since": "2025-04-24", "person": dict(_OWNER)}
        with mock.patch.object(report_bug, "_recent_fixes", return_value=0) as fixes, \
                mock.patch.object(report_bug, "_component_activity", return_value=True), \
                mock.patch.object(report_bug, "_component_triage_owner", return_value=None), \
                mock.patch.object(report_bug, "fetch_recent_channel_stats", return_value=None), \
                mock.patch.object(report_bug, "resolve_product_component",
                                  return_value=("Core", "DOM")), \
                mock.patch.object(report_bug, "fetch_crash_reason", return_value={}), \
                mock.patch.object(report_bug, "fetch_signature_stats",
                                  return_value=(False, {"count": 85, "installs": 77})), \
                mock.patch("crashclouseau.models.Node.authors_for", return_value={}), \
                mock.patch.object(report_bug, "_bugzilla_user",
                                  return_value={"exists": True, "nick": "bobowen"}):
            p = report_bug.build_bug_preview(tav._UI, {"frames": []}, tav._preview_dossier(),
                                             stale=stale)
        self.assertEqual(p["needinfo_email"], "owner@moz.example")
        fixes.assert_not_called()

    def test_an_inactive_fixer_of_an_incomplete_fix_gives_the_triage_owner(self):
        users = {"tboiko@nvidia.com": {"exists": True, "nick": "tboiko", "askable": True},
                 "owner@moz.example": {"exists": True, "nick": "jimm", "askable": True}}
        with mock.patch.object(report_bug, "fetch_signature_stats", return_value=(True, "")), \
                mock.patch.object(report_bug, "fetch_crash_reason", return_value={}), \
                mock.patch.object(report_bug, "_bugzilla_user", side_effect=users.get), \
                mock.patch.object(report_bug, "_recent_fixes", return_value=0) as fixes, \
                mock.patch.object(report_bug, "_component_triage_owner",
                                  return_value="owner@moz.example") as own, \
                mock.patch.object(report_bug.models.UUID, "get_info",
                                  return_value={"version": "156.0a1"}):
            p = report_bug.build_bug_preview(_Fix.UUID_INFO, _Fix.STACK,
                                             {"verdict": {"decision": "abstain"}},
                                             incomplete_fix=_Fix.FIX)
        fixes.assert_called_once_with("tboiko@nvidia.com", 182)
        own.assert_called_once_with("Core", "Audio/Video: Playback")
        self.assertEqual(p["needinfo_email"], "owner@moz.example")
        self.assertEqual(p["needinfo"], _OWNER_ASK)


if __name__ == "__main__":
    unittest.main()
