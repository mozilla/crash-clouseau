"""Who gets the needinfo when the author's account cannot be found or cannot be asked."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import report_bug  # noqa: E402
from tests import test_actionable_verdict as tav  # noqa: E402


def _person(email, real="", nick="", role="assignee"):
    return {"email": email, "real": real, "nick": nick, "role": role}


HARRISON = _person("hoglesby@mozilla.com", "[:harrisono]", "harrisono")
ARYX = _person("aryx.bugmail@gmx-topmail.de", "Sebastian Hengst [:aryx]", "aryx", "creator")


class TestAssigneeByName(unittest.TestCase):
    def test_a_surname_or_handle_in_the_assignee_login_or_nick(self):
        for people, name, email in (
                ([HARRISON, ARYX], "Harrison Oglesby", "oglesby.harrison@gmail.com"),
                ([_person("gmalekpour@mozilla.com", ":Gela", "Gela"),
                  _person("jonalmeida942@gmail.com", "Jonathan Almeida", "jonalmeida", "creator")],
                 "gela", "gela.malekpour@gmail.com"),
                ([_person("ogonzalez@mozilla.com", ":omarg", "omarg"), ARYX],
                 "Omar Gonzalez", "s9tpepper@apache.org"),
                ([_person("krosylight@mozilla.com", "Kagami Rosylight", "saschanaz"), ARYX],
                 "Kagami Sascha Rosylight", "krosylight@proton.me")):
            self.assertIs(report_bug._assignee_by_name(people, name, email), people[0], name)

    def test_no_name_evidence_means_no_one(self):
        self.assertIsNone(report_bug._assignee_by_name(
            [_person("jdoe@mozilla.com", "Jane Doe", "jdoe"), ARYX], "Sebastian Hengst",
            "archaeopteryx@coole-files.de"))

    def test_a_first_name_alone_is_not_enough(self):
        self.assertIsNone(report_bug._assignee_by_name(
            [_person("fabio.kaminski@codethink.co.uk", "Fabio", "fabio.kaminski"), ARYX],
            "Fabio Rossi", "frossi@example.org"))
        self.assertIsNone(report_bug._assignee_by_name(
            [_person("fabio@mozilla.com", "Fabio Kaminski", "fabio"), ARYX],
            "Fabio Rossi", "frossi@example.org"))

    def test_only_the_assignee_and_never_a_bot(self):
        self.assertIsNone(report_bug._assignee_by_name(
            [ARYX, dict(HARRISON, role="creator")], "Harrison Oglesby", "oglesby.harrison@gmail.com"))
        self.assertIsNone(report_bug._assignee_by_name(
            [_person("wptsync@mozilla.com", "moz-wptsync-bot", "")], "wptsync", "wptsync@mozilla.com"))


class TestTheAccountLadder(unittest.TestCase):
    def setUp(self):
        report_bug._USER_CACHE.clear()
        self.addCleanup(report_bug._USER_CACHE.clear)

    def _account(self, people):
        def user(email):
            if email.endswith("@mozilla.com"):
                return {"exists": True, "nick": "harrisono", "real": "[:harrisono]", "askable": True}
            return {"exists": False, "nick": "", "real": "", "askable": False}
        with mock.patch.object(report_bug, "_bugzilla_user", side_effect=user), \
                mock.patch.object(report_bug, "_bug_people", return_value={2086228: people}), \
                mock.patch("crashclouseau.models.Node.recent_bugs_by_author", return_value=[]):
            return report_bug._needinfo_account({"bug": 2086228}, "beta",
                                                "oglesby.harrison@gmail.com", "Harrison Oglesby")

    def test_a_personal_hg_address_reaches_the_named_assignee(self):
        self.assertEqual(self._account([HARRISON, ARYX])["email"], "hoglesby@mozilla.com")

    def test_an_unrelated_assignee_is_not_asked(self):
        self.assertEqual(self._account([_person("jdoe@mozilla.com", "Jane Doe", "jdoe"), ARYX]), {})

    def test_a_namesake_by_first_name_is_not_asked(self):
        def user(email):
            return ({"exists": True, "nick": "fabio", "real": "Fabio Kaminski", "askable": True}
                    if email == "fabio@mozilla.com" else {"exists": False, "askable": False})
        people = [_person("fabio@mozilla.com", "Fabio Kaminski", "fabio"), ARYX]
        with mock.patch.object(report_bug, "_bugzilla_user", side_effect=user), \
                mock.patch.object(report_bug, "_bug_people", return_value={2086228: people}), \
                mock.patch("crashclouseau.models.Node.recent_bugs_by_author", return_value=[]):
            got = report_bug._needinfo_account({"bug": 2086228}, "nightly",
                                               "frossi@example.org", "Fabio Rossi")
        self.assertEqual(got, {})


class TestAnAuthorWhoCannotBeAsked(unittest.TestCase):
    def setUp(self):
        report_bug._USER_CACHE.clear()
        self.addCleanup(report_bug._USER_CACHE.clear)

    def _preview(self, decision, author_left=True):
        d = tav._preview_dossier(actionable_origin_age={
            "landed": "2026-08-25", "days_before_build": 10.0, "predates_signature": True})
        d["verdict"]["decision"] = decision

        def user(email):
            if email == "bob@x.com":
                return {"exists": True, "nick": "bobowen", "real": "Bob Owen",
                        "askable": not author_left}
            return {"exists": True, "nick": "jimm", "real": "Jim", "askable": True}

        with mock.patch.object(report_bug, "resolve_product_component",
                               return_value=("Core", "Security: Process Sandboxing")), \
                mock.patch.object(report_bug, "fetch_crash_reason", return_value={}), \
                mock.patch.object(report_bug, "fetch_signature_stats",
                                  return_value=(False, {"count": 85, "installs": 77})), \
                mock.patch.object(report_bug, "fetch_recent_channel_stats", return_value=None), \
                mock.patch("crashclouseau.models.Node.authors_for", return_value={}), \
                mock.patch("crashclouseau.models.Node.recent_bugs_by_author", return_value=[]), \
                mock.patch.object(report_bug, "_bug_people", return_value={}), \
                mock.patch.object(report_bug, "_bugzilla_user", side_effect=user), \
                mock.patch.object(report_bug, "_component_activity", return_value=True), \
                mock.patch.object(report_bug, "_recent_fixes", return_value=50), \
                mock.patch.object(report_bug, "_component_triage_owner",
                                  return_value="owner@moz.example"):
            return report_bug.build_bug_preview(tav._UI, {"frames": []}, d)

    def test_the_triage_owner_is_asked_instead_of_nobody(self):
        for decision in ("actionable", "lead"):
            p = self._preview(decision)
            self.assertEqual(p["needinfo_email"], "owner@moz.example", decision)
            self.assertIn(":jimm, as triage owner, can you have a look please?", p["comment"])

    def test_an_askable_author_is_still_asked(self):
        p = self._preview("actionable", author_left=False)
        self.assertEqual(p["needinfo_email"], "bob@x.com")


if __name__ == "__main__":
    unittest.main()
