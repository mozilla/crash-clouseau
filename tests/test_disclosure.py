# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Tests for bug reference screening and filing disclosure policy.

Run with ``uv run python -m unittest tests.test_disclosure``.
The tests set default SQLite and Redis URLs.

Visibility is mocked unless the test explicitly checks the anonymous HTTP request."""
import os
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau import bugzilla_apply, disclosure, report_bug  # noqa: E402
from tests.test_autofile import _PREVIEW, _Base, _bug  # noqa: E402

_HIDDEN = 2072467


def _public_except(*hidden):
    """Mock visibility: every requested ID is public except ``hidden``."""
    return lambda ids: {int(i) for i in ids if i and int(i) not in hidden}


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self._payload


class TestReferences(unittest.TestCase):
    def test_the_forms_a_bug_is_named_in(self):
        text = ("Bug 2072467 and bug #2073969; bugs 2068438 and 2067971, bugs 2000001, "
                "2000002, and 2000003; https://bugzilla.mozilla.org/show_bug.cgi?id=1620171 "
                "and bugzil.la/1505660; Bug2059195")
        self.assertEqual(disclosure.bug_refs(text), {
            2072467, 2073969, 2068438, 2067971, 2000001, 2000002, 2000003, 1620171, 1505660,
            2059195})

    def test_numbers_that_are_not_bug_references(self):
        self.assertEqual(disclosure.bug_refs(
            "debug 1234567, build 20260923115834, 12345 reports, bug 1234"), set())

    def test_a_changeset_hash_names_its_bug(self):
        bugs = {"0a49d5b304b4": _HIDDEN, "2222222bbbbcccc": 22}
        text = "`0a49d5b304b4` adds a call; 2222222bbbb is older; cf7f10260925 is a uuid part"
        self.assertEqual(disclosure.node_refs(text, bugs), {_HIDDEN, 22})
        self.assertEqual(disclosure.node_refs("0a49d5b304b4c2d1e0f9", bugs), {_HIDDEN})
        self.assertEqual(disclosure.node_refs(text, None), set())


class TestThePublicRead(unittest.TestCase):
    def test_the_read_is_anonymous_and_batched(self):
        calls = []

        def get(url, **kw):
            calls.append(kw)
            ids = [int(i) for i in kw["params"]["id"].split(",")]
            return _Resp({"bugs": [{"id": i} for i in ids if i != _HIDDEN]})

        with mock.patch.object(disclosure.net, "get", side_effect=get), \
                mock.patch.object(bugzilla_apply.config, "get_bugzilla_token",
                                  return_value="tok"):
            public = disclosure.public_bugs(list(range(3000000, 3000150)) + [_HIDDEN])
        self.assertEqual(len(public), 150)
        self.assertNotIn(_HIDDEN, public)
        self.assertEqual(len(calls), 2)
        for kw in calls:
            self.assertNotIn("X-Bugzilla-API-Key", kw.get("headers") or {})

    def test_an_unanswered_read_is_none(self):
        with mock.patch.object(disclosure.net, "get", side_effect=RuntimeError("503")):
            self.assertIsNone(disclosure.public_bugs([1]))
            self.assertIsNone(disclosure.nonpublic([1]))
        with mock.patch.object(disclosure.net, "get", return_value=_Resp({"error": True})):
            self.assertIsNone(disclosure.public_bugs([1]))

    def test_nothing_to_ask_asks_nothing(self):
        with mock.patch.object(disclosure.net, "get", side_effect=AssertionError("asked")):
            self.assertEqual(disclosure.public_bugs([]), set())
            disclosure.check_public_write("no reference here", 55)


class TestWithdraw(unittest.TestCase):
    TEXT = ("Clouseau analysis: the rise starts with 157.0b4.\n\n"
            "Checked:\n- facets by version (socorro)\n- `a61331c8205c` (bug 2064287) changes "
            "drag code only\n  and nothing on the stack\n- blame of foo.cpp\n\n"
            "Alternatives considered:\n- bug 2064287 as the culprit: disfavored\n\n"
            "```\n0 xul.dll bug 2064287 frame\n```\n\nPlease have a look.")

    def test_the_items_that_name_a_hidden_bug_go(self):
        out, withdrawn = disclosure.withdraw(self.TEXT, {2064287})
        self.assertEqual(withdrawn, {2064287})
        self.assertIn("- facets by version (socorro)\n- blame of foo.cpp", out)
        self.assertNotIn("drag code", out)
        self.assertNotIn("nothing on the stack", out, "a continuation line goes with its item")
        self.assertNotIn("Alternatives considered", out, "an emptied list goes with its lead-in")
        self.assertIn("0 xul.dll bug 2064287 frame", out, "fenced blocks are kept")
        self.assertTrue(out.endswith("Please have a look."))
        self.assertEqual(disclosure.refs(out) & {2064287}, {2064287}, "the fence still names it")

    def test_nothing_withdrawn_is_byte_identical(self):
        self.assertEqual(disclosure.withdraw(self.TEXT + "\n", {1}), (self.TEXT + "\n", set()))

    def test_a_hash_in_an_item_counts(self):
        out, withdrawn = disclosure.withdraw("Checked:\n- `a61331c8205c` reads\n- other",
                                             {2064287}, {"a61331c8205c": 2064287})
        self.assertEqual((out, withdrawn), ("Checked:\n- other", {2064287}))


class TestScreen(unittest.TestCase):
    def test_prose_is_left_and_items_are_withdrawn(self):
        text = ("Bug 2046734 promoted the assert.\n\nAlternatives considered:\n"
                "- bug 2068438: not supported\n- bug 1855742: backed out")
        with mock.patch.object(disclosure, "public_bugs", side_effect=_public_except(2046734, 2068438)):
            out = disclosure.screen(text)
        self.assertEqual((out["left"], out["withdrawn"]), ([2046734], [2068438]))
        self.assertNotIn("2068438", out["text"])
        self.assertIn("bug 1855742", out["text"])

    def test_also_names_bugs_outside_the_text(self):
        with mock.patch.object(disclosure, "public_bugs", side_effect=_public_except(_HIDDEN)):
            self.assertEqual(disclosure.screen("nothing", also=[_HIDDEN])["left"], [_HIDDEN])

    def test_an_unanswered_read_is_none(self):
        with mock.patch.object(disclosure, "public_bugs", return_value=None):
            self.assertIsNone(disclosure.screen("bug 1234567"))


class TestTheLastCheckBeforeAPublicWrite(unittest.TestCase):
    def test_a_public_bug_may_not_carry_a_hidden_reference(self):
        with mock.patch.object(disclosure, "public_bugs", side_effect=_public_except(_HIDDEN)):
            with self.assertRaises(disclosure.DisclosureRefused) as ctx:
                disclosure.check_public_write("see bug {}".format(_HIDDEN), 1620171)
            # The reason reaches public pages, so it names no bug.
            self.assertNotIn(str(_HIDDEN), str(ctx.exception))
            with self.assertRaises(disclosure.DisclosureRefused):
                disclosure.check_public_write("see bug {}".format(_HIDDEN))
            disclosure.check_public_write("see bug 1855742", 1620171)

    def test_a_restricted_bug_may_carry_anything(self):
        with mock.patch.object(disclosure, "public_bugs", side_effect=_public_except(_HIDDEN, 2099999)):
            disclosure.check_public_write("see bug {}".format(_HIDDEN), 2099999)

    def test_an_unanswered_read_refuses(self):
        with mock.patch.object(disclosure, "public_bugs", return_value=None):
            with self.assertRaises(disclosure.DisclosureRefused):
                disclosure.check_public_write("see bug 1855742", 55)

    def test_the_write_helpers_refuse_before_posting(self):
        with mock.patch.object(disclosure, "public_bugs", side_effect=_public_except(_HIDDEN)), \
                mock.patch.object(bugzilla_apply.net, "post") as post, \
                mock.patch.object(bugzilla_apply.net, "put") as put:
            with self.assertRaises(disclosure.DisclosureRefused):
                bugzilla_apply._post_comment(55, "bug {}".format(_HIDDEN), False, "tok")
            with self.assertRaises(disclosure.DisclosureRefused):
                bugzilla_apply._put_bug(55, {"comment": {"body": "bug {}".format(_HIDDEN)}}, "tok")
            with self.assertRaises(disclosure.DisclosureRefused):
                bugzilla_apply._create_bug({"summary": "Crash in [@ S]",
                                            "description": "bug {}".format(_HIDDEN)}, "tok")
            post.assert_not_called()
            put.assert_not_called()
            post.return_value = _Resp({"id": 7})
            bugzilla_apply._create_bug({"summary": "Crash in [@ S]", "groups": ["core-security"],
                                        "description": "bug {}".format(_HIDDEN)}, "tok")
            bugzilla_apply._post_comment(55, "bug {}".format(_HIDDEN), True, "tok")
        self.assertEqual(post.call_count, 2)


class _Restricting:
    """Mock the security group and CC fields added by ``restrict=True``."""

    @staticmethod
    def preview(*args, **kw):
        out = dict(_PREVIEW)
        if kw.get("restrict"):
            out.update(groups=["core-security"], cc=[out["needinfo_email"]])
        return out


class TestTheOrdinaryFiler(_Base):
    DOSSIER = {"candidate": {"node": "0a49d5b304b4", "bug": _HIDDEN}}

    def setUp(self):
        super().setUp()
        report_bug.build_bug_preview.side_effect = _Restricting.preview
        p = mock.patch.object(report_bug, "security_group", return_value="core-security")
        p.start()
        self.addCleanup(p.stop)

    def _hide(self, *bugs):
        disclosure.public_bugs.side_effect = _public_except(*bugs)

    def test_a_restricted_regressor_files_a_restricted_bug(self):
        self._hide(_HIDDEN)
        res = self._file(dossier=self.DOSSIER)
        self.assertTrue(res["filed"])
        self.assertEqual((res["mode"], res["restricted"]), ("new_bug", "regressor"))
        self.assertEqual(res["security_groups"], ["core-security"])
        self.assertNotIn(str(_HIDDEN), repr(res), "the record is served publicly")
        payload = self.created[0]
        self.assertEqual((payload["groups"], payload["cc"]), (["core-security"], ["dev@moz.example"]))
        self.assertIn("_Filed restricted because bug {}, the bug of the changeset named above, "
                      "is not public._".format(_HIDDEN), payload["description"])
        self.assertTrue(report_bug.build_bug_preview.call_args.kwargs["restrict"])

    def test_it_declines_a_public_venue_on_every_channel(self):
        self._hide(_HIDDEN)
        for mode in ("comment", "skip"):
            self.created.clear()
            with self.subTest(mode=mode), \
                    mock.patch.object(bugzilla_apply, "_open_bugs_for_signature",
                                      return_value=[_bug(55)]):
                res = self._file(dossier=self.DOSSIER, comment_on_existing=mode)
                self.assertEqual((res["mode"], res["public_venue_declined"]), ("new_bug", 55))
                self.assertEqual(self.comments, [])
                self.assertIn("_Probably a duplicate of bug 55, which is open on this same "
                              "signature. This bug was filed separately, and restricted, because "
                              "bug {}, the bug of the changeset named above, is not public, and "
                              "bug 55 is public._".format(_HIDDEN), self.created[0]["description"])

    def test_an_unanswered_read_files_nothing(self):
        disclosure.public_bugs.side_effect = None
        disclosure.public_bugs.return_value = None
        res = self._file(dossier=self.DOSSIER)
        self.assertFalse(res["filed"])
        self.assertIn("not risking a disclosure", res["skipped"])
        self.assertEqual((self.created, self.comments), ([], []))

    def test_a_list_item_naming_a_hidden_bug_is_removed(self):
        text = ("the whole bug opener\n\nWhat the automated skeptic pass checked:\n"
                "- **pass** window — bug 2059195 touches the compositor only\n"
                "- **pass** mechanism — confirmed")
        report_bug.build_bug_preview.side_effect = lambda *a, **k: dict(_PREVIEW, comment=text)
        self._hide(2059195)
        res = self._file(dossier={"candidate": {"node": "n", "bug": 42}})
        self.assertEqual((res["mode"], res["withdrawn_refs"]), ("new_bug", 1))
        self.assertNotIn("security_groups", res)
        body = self.created[0]["description"]
        self.assertNotIn("2059195", body)
        self.assertIn("- **pass** mechanism — confirmed", body)
        self.assertNotIn("groups", self.created[0])

    def test_a_hidden_bug_in_the_analysis_prose_restricts_the_filing(self):
        text = "the whole bug opener\n\nBug 2068336 converted the invariant check."
        report_bug.build_bug_preview.side_effect = lambda *a, **k: dict(_PREVIEW, comment=text)
        self._hide(2068336)
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature", return_value=[_bug(55)]):
            res = self._file(dossier={"candidate": {"node": "n", "bug": 42}})
        self.assertEqual((res["mode"], res["restricted"]), ("new_bug", "analysis"))
        self.assertEqual(res["public_venue_declined"], 55)
        self.assertEqual(self.comments, [])
        self.assertEqual(self.created[0]["groups"], ["core-security"])
        self.assertIn("because the analysis above names bug 2068336, which is not public, and "
                      "bug 55 is public._", self.created[0]["description"])

    def test_no_security_group_files_nothing(self):
        self._hide(_HIDDEN)
        report_bug.build_bug_preview.side_effect = lambda *a, **k: dict(_PREVIEW)
        res = self._file(dossier=self.DOSSIER)
        self.assertFalse(res["filed"])
        self.assertEqual(res["skipped"], "the regressor bug is not public and no security group "
                                         "for product 'Core'")

    def test_a_public_regressor_changes_nothing(self):
        res = self._file(dossier={"candidate": {"node": "n", "bug": 42}})
        self.assertEqual(res["mode"], "new_bug")
        self.assertNotIn("restricted", res)
        self.assertNotIn("groups", self.created[0])
        self.assertEqual(self.created[0]["description"], _PREVIEW["comment"])


class TestTheNote(unittest.TestCase):
    def test_both_reasons(self):
        self.assertEqual(bugzilla_apply._restricted_note("analysis", [2, 1]),
                         "_Filed restricted because the analysis above names bug 1, bug 2, "
                         "which are not public._")
        self.assertEqual(bugzilla_apply._restricted_note("regressor", [7], declined=55),
                         "_Probably a duplicate of bug 55, which is open on this same signature. "
                         "This bug was filed separately, and restricted, because bug 7, the bug "
                         "of the changeset named above, is not public, and bug 55 is public._")


if __name__ == "__main__":
    unittest.main()
