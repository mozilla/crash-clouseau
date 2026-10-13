# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Tests for mailmap parsing, caching and author lookups.

    uv run python -m unittest tests.test_mailmap
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import db, hang, mailmap, models, report_bug  # noqa: E402
from crashclouseau.agent.tools import history  # noqa: E402

MAILMAP = """\
# A comment line.
Alex Thayer <dothayer@mozilla.com>
Nika Layzell <nika@thelayzells.com> <michael@thelayzells.com>
<proper@example.com> <old@example.com>
Pat Doe <pat@example.com> Patrick Doe <shared@example.com>
Shared Name <shared@example.com>  # trailing comment
"""


def _with(text=MAILMAP):
    return mock.patch.dict(mailmap._cache, entries=mailmap.parse(text), until=float("inf"))


class TestParse(unittest.TestCase):
    def test_the_four_forms(self):
        self.assertEqual(mailmap.parse(MAILMAP), {
            ("dothayer@mozilla.com", None): ("Alex Thayer", None),
            ("michael@thelayzells.com", None): ("Nika Layzell", "nika@thelayzells.com"),
            ("old@example.com", None): (None, "proper@example.com"),
            ("shared@example.com", "patrick doe"): ("Pat Doe", "pat@example.com"),
            ("shared@example.com", None): ("Shared Name", None),
        })

    def test_repeated_simple_entries_merge(self):
        self.assertEqual(mailmap.parse("A <a@x> <old@x>\nB <old@x>\n"),
                         {("old@x", None): ("B", "a@x")})

    def test_junk_is_ignored(self):
        self.assertEqual(mailmap.parse("\n# x <a@b>\nno email\nName <>\n"), {})


class TestCanonical(unittest.TestCase):
    def test_entries(self):
        with _with():
            for (name, email), want in [
                (("Doug Thayer", "dothayer@mozilla.com"), ("Alex Thayer", "dothayer@mozilla.com")),
                (("Doug Thayer", "DoThayer@Mozilla.com"), ("Alex Thayer", "DoThayer@Mozilla.com")),
                (("Michael Layzell", "michael@thelayzells.com"),
                 ("Nika Layzell", "nika@thelayzells.com")),
                (("Someone", "old@example.com"), ("Someone", "proper@example.com")),
                (("Patrick Doe", "shared@example.com"), ("Pat Doe", "pat@example.com")),
                (("Other", "shared@example.com"), ("Shared Name", "shared@example.com")),
                (("Jane Doe", "jane@x.org"), ("Jane Doe", "jane@x.org")),
                (("Doug Thayer", ""), ("Doug Thayer", "")),
            ]:
                with self.subTest(name=name, email=email):
                    self.assertEqual(mailmap.canonical(name, email), want)

    def test_author_keeps_the_shape(self):
        with _with():
            for args, want in [
                (("Doug Thayer <dothayer@mozilla.com>",), "Alex Thayer <dothayer@mozilla.com>"),
                (("Michael Layzell <michael@thelayzells.com>",),
                 "Nika Layzell <nika@thelayzells.com>"),
                (("Doug Thayer", "dothayer@mozilla.com"), "Alex Thayer"),
                (("michael@thelayzells.com",), "nika@thelayzells.com"),
                (("Doug Thayer",), "Doug Thayer"),
                (("A <a@x.org>, Doug Thayer <dothayer@mozilla.com>",),
                 "A <a@x.org>, Doug Thayer <dothayer@mozilla.com>"),
                ((None,), ""),
            ]:
                with self.subTest(args=args):
                    self.assertEqual(mailmap.author(*args), want)


class TestAliases(unittest.TestCase):
    def test_the_commit_addresses(self):
        with _with():
            self.assertEqual(mailmap.aliases("Nika@TheLayzells.com"),
                             {"michael@thelayzells.com", "nika@thelayzells.com"})
            self.assertEqual(mailmap.aliases("pat@example.com"),
                             {"pat@example.com", "shared@example.com"})
            self.assertEqual(mailmap.aliases("dothayer@mozilla.com"), {"dothayer@mozilla.com"})
            self.assertEqual(mailmap.aliases(""), set())


def _is_postgres():
    try:
        return db.engine.dialect.name == "postgresql"
    except Exception:
        return False


@unittest.skipUnless(_is_postgres(), "needs a disposable Postgres")
class TestRecentBugsByAlias(unittest.TestCase):
    ROWS = [("0a11a5000001", 1367406, ("michael@thelayzells.com", "Michael Layzell", "")),
            ("0a11a5000002", 101, ("shared@example.com", "Patrick Doe", "")),
            ("0a11a5000003", 102, ("shared@example.com", "Someone Else", ""))]

    def setUp(self):
        models.create()
        self._clean()
        for day, (node, bug, author) in enumerate(self.ROWS, 1):
            db.session.add(models.Node("nightly", {
                "node": node, "date": datetime(2026, 10, day, tzinfo=timezone.utc),
                "backedout": False, "merge": False, "bug": bug, "author": [author]}))
        db.session.commit()

    def tearDown(self):
        self._clean()

    def _clean(self):
        db.session.query(models.Node).filter(models.Node.node.like("0a11a5%")).delete(
            synchronize_session=False)
        db.session.commit()

    def test_a_row_stored_before_the_entry(self):
        with _with():
            for email in ("michael@thelayzells.com", "nika@thelayzells.com"):
                with self.subTest(email=email):
                    self.assertEqual(models.Node.recent_bugs_by_author(email, "nightly"),
                                     [1367406])
        self.assertEqual(models.Node.recent_bugs_by_author("nika@thelayzells.com", "nightly"), [])

    def test_a_name_qualified_entry_matches_only_that_name(self):
        with _with():
            self.assertEqual(models.Node.recent_bugs_by_author("pat@example.com", "nightly"), [101])
            self.assertEqual(models.Node.recent_bugs_by_author("shared@example.com", "nightly"),
                             [102])

    def test_a_default_entry_leaves_out_the_name_qualified_commits(self):
        text = ("Default User <default@example.com> <shared@example.com>\n"
                "Pat Doe <pat@example.com> Patrick Doe <shared@example.com>\n")
        with _with(text):
            for args, want in [
                (("default@example.com",), [102]),
                (("shared@example.com",), [102]),
                (("pat@example.com",), [101]),
                (("shared@example.com", "Patrick Doe"), [101]),
            ]:
                with self.subTest(args=args):
                    email, *name = args
                    self.assertEqual(models.Node.recent_bugs_by_author(
                        email, "nightly", name=name[0] if name else ""), want)


class TestFetch(unittest.TestCase):
    def test_one_fetch_per_day(self):
        resp = mock.Mock(text=MAILMAP)
        with mock.patch.dict(mailmap._cache, entries={}, until=0.0), \
                mock.patch.object(mailmap.net, "get", return_value=resp) as get:
            self.assertEqual(mailmap.canonical("Doug Thayer", "dothayer@mozilla.com")[0],
                             "Alex Thayer")
            mailmap.canonical("Doug Thayer", "dothayer@mozilla.com")
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], mailmap.URL)

    def test_a_failed_fetch_keeps_the_last_copy(self):
        with mock.patch.dict(mailmap._cache, entries=mailmap.parse(MAILMAP), until=0.0), \
                mock.patch.object(mailmap.net, "get", side_effect=OSError("down")) as get:
            self.assertEqual(mailmap.canonical("Doug Thayer", "dothayer@mozilla.com")[0],
                             "Alex Thayer")
            mailmap.canonical("Doug Thayer", "dothayer@mozilla.com")
            get.assert_called_once()
            self.assertGreater(mailmap._cache["until"], 0.0)


class TestWhereNamesAreUsed(unittest.TestCase):
    def test_the_actionable_comment_without_a_bugzilla_name(self):
        dossier = {"verdict": {"decision": "actionable",
                               "mechanism": {"statement": "The lock is held across I/O."}},
                   "candidate": {"node": "eb8741186210", "bug": 1594577, "author": "Doug Thayer",
                                 "author_email": "dothayer@mozilla.com"}}
        info = {"uuid": "u-1", "signature": "Foo::Bar", "channel": "nightly",
                "product": "Firefox"}
        with _with(), mock.patch.object(report_bug, "build_stats_sentence", return_value=None), \
                mock.patch.object(report_bug, "changeset_links", return_value="eb8741186210"):
            text = report_bug.build_actionable_comment(info, {}, dossier)
        self.assertIn("comes from eb8741186210 (bug 1594577) by Alex Thayer.", text)
        self.assertNotIn("Doug", text)

    def test_the_explanation_comment_without_a_bugzilla_name(self):
        with _with():
            exp = report_bug._explanation_comment(
                {}, {"node": "abc", "author": "Doug Thayer", "author_email": "dothayer@mozilla.com"},
                corroborations={"candidate_in_pushlog_window": True})
        self.assertIn("Suspected regressor: abc by Alex Thayer.", exp)

    def test_the_needinfo_ladder_starts_from_the_mapped_address(self):
        cand = {"node": "70f65afbc48a", "bug": 1367406,
                "author": "Michael Layzell <michael@thelayzells.com>"}
        with _with(), mock.patch.object(report_bug.models.Node, "authors_for", return_value={}), \
                mock.patch.object(report_bug, "_needinfo_account",
                                  return_value={"email": "nika@thelayzells.com", "nick": "nika",
                                                "real": "Nika Layzell [:nika]"}) as account:
            person = report_bug._needinfo_person(cand, "nightly")
        self.assertEqual(account.call_args.args[2:], ("nika@thelayzells.com", "Nika Layzell"))
        self.assertEqual(person["name"], "Nika Layzell")

    def test_the_history_tool(self):
        with _with():
            self.assertEqual(history._author("Doug Thayer <dothayer@mozilla.com>"), "Alex Thayer")
            self.assertEqual(history._author("michael@thelayzells.com"), "nika")

    def test_the_hang_origin(self):
        row = {"node": "eb8741186210aaaa", "author": "Doug Thayer <dothayer@mozilla.com>",
               "desc": "Bug 1594577 - Record BHR hangs"}
        with _with():
            origin = hang.origin_from_row(row, {"file": "x.cpp", "line": 3})
        self.assertEqual(origin["author"], "Alex Thayer <dothayer@mozilla.com>")
        self.assertEqual(origin["author_email"], "dothayer@mozilla.com")


if __name__ == "__main__":
    unittest.main()
