# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""A commit's author string is frozen; a Bugzilla account is not. Bugzilla wins.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        uv run python -m unittest tests.test_stale_hg_identity

Bug 2067059. We named a 2017 changeset's hg author, ``Michael Layzell
<michael@thelayzells.com>``, in two places in the filed comment -- the "Starting point" line and
the needinfo ask -- and set no needinfo flag at all, because that address is not a Bugzilla
account (BMO: "There is no user named ..."). The person is ``Nika Layzell [:nika]
<nika@thelayzells.com>``, the assignee of the bug the changeset landed for, and the comment
published a name she does not use while asking nobody.

Both halves are one root cause: we read the human off Mercurial when Bugzilla knows better.
These tests pin the ladder rung that resolves it (`_sole_bug_person`), the display rule that
prefers the account over the commit string (`_person_display`), and the end-to-end result on
this bug's own data.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import report_bug  # noqa: E402
from crashclouseau.agent.experts import _is_bot  # noqa: E402

# Bug 2067059's candidate, verified against hg and BMO on 2026-08-27.
HG_NAME = "Michael Layzell"
HG_EMAIL = "michael@thelayzells.com"
NODE = "70f65afbc48a"
BUG = 1367406
NIKA = {"email": "nika@thelayzells.com",
        "real": "Nika Layzell [:nika] (ni? for response)", "nick": "nika",
        "role": "assignee"}


def _person(email, real, nick="", role="assignee"):
    return {"email": email, "real": real, "nick": nick, "role": role}


class TestSoleBugPerson(unittest.TestCase):
    def test_the_assignee_is_the_person(self):
        self.assertEqual(report_bug._sole_bug_person([NIKA, dict(NIKA, role="creator")]), NIKA)

    def test_two_distinct_humans_resolve_nobody(self):
        self.assertIsNone(report_bug._sole_bug_person(
            [_person("a@x.com", "A"), _person("b@x.com", "B", role="creator")]))

    def test_a_bot_beside_the_assignee_does_not_make_it_ambiguous(self):
        # An intermittent-filed bug reassigned to a human: the filer must neither be picked
        # nor block the pick.
        self.assertEqual(report_bug._sole_bug_person(
            [NIKA, _person("intermittent-bug-filer@mozilla.bugs", "Treeherder Bug Filer",
                           "intermittent-bug-filer", role="creator")]), NIKA)

    def test_a_bot_alone_resolves_nobody(self):
        self.assertIsNone(report_bug._sole_bug_person(
            [_person("intermittent-bug-filer@mozilla.bugs", "Treeherder Bug Filer",
                     "intermittent-bug-filer")]))

    def test_creator_only_resolves_nobody(self):
        self.assertIsNone(report_bug._sole_bug_person(
            [_person("reporter@x.com", "A Reporter", role="creator")]))

    def test_nothing_resolves_nobody(self):
        for people in (None, [], [{}], [{"email": ""}]):
            self.assertIsNone(report_bug._sole_bug_person(people), people)


class TestTheBmoSideBots(unittest.TestCase):
    """`experts._is_bot` was measured on a year of mozilla-central AUTHOR strings, and no hg
    author has ever had a ``@mozilla.bugs`` address -- so the panel could not see these. All
    three accounts on that domain across 1,200 recent prod bugs are automation, and two were
    reaching `_bug_people` as needinfo candidates."""

    def test_the_bmo_pseudo_domain_is_automation(self):
        for email in ("intermittent-bug-filer@mozilla.bugs", "telemetry-probes@mozilla.bugs",
                      "wptsync@mozilla.bugs"):
            self.assertTrue(_is_bot(email), email)

    def test_it_is_the_exact_domain_and_not_a_suffix(self):
        self.assertFalse(_is_bot("someone@notmozilla.bugs"))


class TestPersonDisplay(unittest.TestCase):
    def test_the_nick_wins(self):
        self.assertEqual(report_bug._person_display(
            {"nick": "nika", "account_name": "Nika Layzell", "name": HG_NAME}), ":nika")

    def test_the_account_name_beats_the_commit_string(self):
        """THE FIX, in one assertion. With no nick to fall back on, the account's display name
        still outranks the name frozen in the commit -- which is where the deadname was."""
        self.assertEqual(report_bug._person_display(
            {"nick": "", "account_name": "Nika Layzell", "name": HG_NAME}), "Nika Layzell")

    def test_the_commit_string_is_used_when_nothing_resolved(self):
        # Still better than silence: a triager who reads a name can set the flag in one click.
        self.assertEqual(report_bug._person_display({"name": HG_NAME}), HG_NAME)

    def test_the_address_is_the_last_resort(self):
        self.assertEqual(report_bug._person_display({"email": HG_EMAIL}), HG_EMAIL)

    def test_nothing_at_all(self):
        for p in (None, {}, {"nick": " ", "name": "", "email": None}):
            self.assertEqual(report_bug._person_display(p), "", p)


class TestBug2067059EndToEnd(unittest.TestCase):
    """The real data: hg says Michael, BMO says there is no such account, bug 1367406 says
    Nika is both its assignee and its creator."""

    def setUp(self):
        report_bug._USER_CACHE.clear()
        report_bug._BUG_CACHE.clear()
        self.addCleanup(report_bug._USER_CACHE.clear)
        self.addCleanup(report_bug._BUG_CACHE.clear)

    def _ctx(self):
        def user(email):
            if email == NIKA["email"]:
                return {"exists": True, "nick": "nika", "real": NIKA["real"], "askable": True}
            return {"exists": False, "nick": "", "real": "", "askable": False}
        return (mock.patch.object(report_bug, "_bugzilla_user", side_effect=user),
                mock.patch.object(report_bug, "_bug_people",
                                  side_effect=lambda ids: {b: ([NIKA, dict(NIKA, role="creator")]
                                                               if b == BUG else []) for b in ids}),
                mock.patch("crashclouseau.models.Node.authors_for",
                           return_value={NODE: {"email": HG_EMAIL, "real": HG_NAME}}),
                mock.patch("crashclouseau.models.Node.recent_bugs_by_author", return_value=[]))

    def _person(self):
        a, b, c, d = self._ctx()
        with a, b, c, d:
            return report_bug._needinfo_person({"node": NODE, "bug": BUG}, "nightly")

    def test_the_flag_goes_to_the_account_that_exists(self):
        # It was set to nothing at all: BMO rejects an unknown requestee, so the filed bug
        # carried no needinfo and nobody was asked.
        self.assertEqual(self._person()["account"], NIKA["email"])

    def test_the_ask_uses_her_nick(self):
        self.assertEqual(report_bug._needinfo_line(self._person()),
                         ":nika, can you have a look please?")
        self.assertNotIn(HG_NAME, report_bug._needinfo_line(self._person()))

    def test_the_starting_point_line_names_the_same_person_as_the_ask(self):
        """The two lines are built in different places and used to disagree: the ask took the
        resolved account and the attribution took `candidate["author"]` straight from hg."""
        person = self._person()
        text = report_bug._explanation_comment(
            {"mechanism": {"statement": "m"}},
            {"node": NODE, "bug": BUG, "author": HG_NAME, "author_email": HG_EMAIL},
            "nightly", corroborations={},
            author_display=report_bug._person_display(person))
        line = [x for x in text.split("\n") if "Starting point" in x][0]
        self.assertIn("by :nika.", line)
        self.assertNotIn(HG_NAME, text)

    def test_the_hg_string_survives_when_nothing_resolves(self):
        # No account anywhere -> the attribution falls back rather than going anonymous.
        text = report_bug._explanation_comment(
            {"mechanism": {"statement": "m"}},
            {"node": NODE, "bug": BUG, "author": HG_NAME}, "nightly",
            corroborations={}, author_display="")
        self.assertIn("by {}.".format(HG_NAME), text)


if __name__ == "__main__":
    unittest.main()
