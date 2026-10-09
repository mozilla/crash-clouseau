# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Tests for bug reference screening and filing disclosure policy.

Run with ``uv run python -m unittest tests.test_disclosure``.
The tests set default SQLite and Redis URLs.

Visibility is mocked unless the test explicitly checks the anonymous HTTP request."""
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau import bugzilla_apply, disclosure, html, report_bug  # noqa: E402
from crashclouseau.agent import spike_escalation as se  # noqa: E402
from crashclouseau.agent.spike_agent import SpikeFindings  # noqa: E402
from tests.test_autofile import _PREVIEW, _Base, _bug  # noqa: E402
from tests.test_spike_escalation import _FilerBase, _esc  # noqa: E402

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

    def test_the_collapsed_skeptic_block_keeps_its_tags(self):
        block = report_bug.build_skeptic_block({"skeptic": [
            {"status": "fail", "claim_ref": "seeds", "note": "bug 2059195 is unrelated"},
            {"status": "pass", "claim_ref": "mechanism", "note": "confirmed"}]})
        text = "Opener.\n\n" + block + "\n\nPlease have a look."
        out, _ = disclosure.withdraw(text, {2059195})
        self.assertNotIn("2059195", out)
        self.assertIn("- **pass** mechanism — confirmed\n\n</details>\n\nPlease", out)
        self.assertTrue(out.startswith("Opener.\n\n<details>\n<summary>"))
        only = text.replace("mechanism — confirmed", "mechanism — bug 2059195 too")
        out, _ = disclosure.withdraw(only, {2059195})
        self.assertEqual(out, "Opener.\n\nPlease have a look.", "an emptied block goes entirely")

    def test_an_empty_details_block_in_a_fence_is_kept(self):
        fence = "```\n<details>\n<summary>s</summary>\n</details>\n```"
        out, _ = disclosure.withdraw("Checked:\n- bug 2059195 x\n- other\n\n" + fence,
                                     {2059195})
        self.assertEqual(out, "Checked:\n- other\n\n" + fence)


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

    def test_a_known_hidden_set_asks_nothing(self):
        with mock.patch.object(disclosure, "public_bugs", side_effect=AssertionError("asked")):
            out = disclosure.screen("Checked:\n- bug 2068438\n- other", hidden={2068438})
        self.assertEqual((out["withdrawn"], out["left"]), ([2068438], []))

    def test_every_string_of_a_structure(self):
        self.assertEqual(disclosure.strings(
            {"a": "x", "b": [1, "y", {"c": "z"}], "d": None}).split(), ["x", "y", "z"])


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
        # Render checks are covered in test_links.py.
        with mock.patch.object(disclosure, "public_bugs", side_effect=_public_except(_HIDDEN)), \
                mock.patch.object(bugzilla_apply, "_render_comment", return_value="<p>ok</p>"), \
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

    def test_a_restricted_bug_filed_instead_of_a_comment_keeps_the_filing_footer(self):
        text = ("the whole bug opener\n\nBug 2068336 converted the invariant check.\n\n"
                + report_bug._provenance("nightly"))
        report_bug.build_bug_preview.side_effect = lambda *a, **k: dict(_PREVIEW, comment=text)
        self._hide(2068336)
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature", return_value=[_bug(55)]):
            res = self._file(dossier={"candidate": {"node": "n", "bug": 42}})
        self.assertEqual(res["public_venue_declined"], 55)
        self.assertIn(report_bug._provenance("nightly"), self.created[0]["description"])
        self.assertNotIn("_Posted automatically", self.created[0]["description"])

    def test_a_decline_or_a_failed_write_carries_the_reason(self):
        self._hide(_HIDDEN)
        report_bug.build_bug_preview.side_effect = lambda *a, **k: dict(_PREVIEW)
        self.assertEqual(self._file(dossier=self.DOSSIER)["restricted"], "regressor")
        report_bug.build_bug_preview.side_effect = _Restricting.preview
        errors = []
        with mock.patch.object(bugzilla_apply, "_create_bug", side_effect=RuntimeError("boom")), \
                mock.patch.object(bugzilla_apply.models.Dossier, "record_filing_error",
                                  side_effect=lambda u, i: errors.append(i)):
            res = self._file(dossier=self.DOSSIER)
        self.assertEqual((res["filed"], res["restricted"]), (False, "regressor"))
        self.assertEqual(errors[0]["restricted"], "regressor")

    def test_a_failed_write_keeps_the_withdrawn_count(self):
        """A failed write must retain the flag that withholds removed references."""
        text = ("the whole bug opener\n\nWhat the automated skeptic pass checked:\n"
                "- **pass** window — bug 2059195 touches the compositor only\n"
                "- **pass** mechanism — confirmed")
        report_bug.build_bug_preview.side_effect = lambda *a, **k: dict(_PREVIEW, comment=text)
        self._hide(2059195)
        errors = []
        with mock.patch.object(bugzilla_apply, "_create_bug", side_effect=RuntimeError("boom")), \
                mock.patch.object(bugzilla_apply.models.Dossier, "record_filing_error",
                                  side_effect=lambda u, i: errors.append(i)):
            res = self._file(dossier={"candidate": {"node": "n", "bug": 42}})
        self.assertEqual((res["filed"], res["withdrawn_refs"]), (False, 1))
        self.assertNotIn("restricted", res)
        self.assertEqual(errors[0]["withdrawn_refs"], 1)
        self.assertTrue(disclosure.withheld_filing(errors[0]))

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


class TestTheSpikeFiler(_FilerBase):
    """Exercise restricted regressors and other nonpublic references in spike filings."""

    def setUp(self):
        super().setUp()
        self.findings = SpikeFindings(
            summary="The rise starts with 157.0b4, the first beta with `0a49d5b304b4`.",
            product="Core", component="Graphics: Canvas2D",
            culprit={"node": "0a49d5b304b4", "bug": _HIDDEN, "confidence": "medium",
                     "why": "The diff adds a call on the failure path."},
            evidence=[{"claim": "facets by version", "source": "socorro"}],
            ruled_out=["Bug 1855742 (`2028c7018977`): backed out in 2023"])

    def _hide(self, *bugs):
        disclosure.public_bugs.side_effect = _public_except(*bugs)

    def _old_bug(self, bid=1620171, created="2020-03-05T00:00:00Z"):
        return {"id": bid, "creation_time": created, "product": "Core", "keywords": [],
                "regressed_by": []}

    def test_a_restricted_culprit_files_a_restricted_bug_past_an_old_public_one(self):
        self._hide(_HIDDEN)
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature",
                               return_value=[self._old_bug()]):
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual((res["mode"], res["restricted"]), ("spike_new_bug", "regressor"))
        self.assertEqual(res["predating_bugs"], [1620171])
        self.assertEqual(self.comments, [], "nothing is written on the public bug")
        self.assertNotIn(str(_HIDDEN), repr(res), "the record is served publicly")
        payload = self.created[0]
        self.assertEqual(payload["groups"], ["core-security"])
        self.assertEqual(payload["cc"], ["dev@moz.example"])
        body = payload["description"]
        self.assertIn("Filed as a new bug rather than a comment on bug 1620171", body)
        self.assertIn("_Filed restricted because bug {}, the bug of the changeset named above, "
                      "is not public._".format(_HIDDEN), body)

    def test_a_restricted_culprit_declines_a_venue_about_the_regression(self):
        self._hide(_HIDDEN)
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature",
                               return_value=[self._old_bug(55, "2026-08-25T00:00:00Z")]):
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual((res["mode"], res["public_venue_declined"]), ("spike_new_bug", 55))
        self.assertEqual(self.comments, [])
        self.assertIn("_Probably a duplicate of bug 55", self.created[0]["description"])

    def test_a_hidden_bug_in_a_list_item_is_removed_from_a_public_comment(self):
        findings = self.findings.model_copy(update={"culprit": None, "ruled_out": [
            "Bug 1855742 (`2028c7018977`): backed out in 2023",
            "`a61331c8205c` (bug 2064287, drag hardening): not supported"]})
        self._hide(2064287)
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature",
                               return_value=[self._old_bug()]):
            res = se.file_spike_bug(_esc(), self.brief, findings, grounded=True)
        self.assertEqual((res["mode"], res["bug"], res["withdrawn_refs"]),
                         ("spike_comment", 1620171, 1))
        text = self.comments[0][1]
        self.assertNotIn("2064287", text)
        self.assertNotIn("a61331c8205c", text)
        self.assertIn("Bug 1855742", text)

    def test_a_hidden_bug_in_the_summary_restricts_the_filing(self):
        findings = self.findings.model_copy(update={
            "culprit": None,
            "summary": "The abort exists on beta because bug 2046734 made it a release assert."})
        self._hide(2046734)
        res = se.file_spike_bug(_esc(), self.brief, findings, grounded=True)
        self.assertEqual((res["mode"], res["restricted"]), ("spike_new_bug", "analysis"))
        self.assertEqual(self.created[0]["groups"], ["core-security"])

    def test_an_unanswered_read_is_retried_and_writes_nothing(self):
        disclosure.public_bugs.side_effect = None
        disclosure.public_bugs.return_value = None
        res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertTrue(res["retry"])
        self.assertIn("not risking a disclosure", res["skipped"])
        self.assertEqual(res["restricted"], "unchecked")
        self.assertNotIn("screened", res)
        self.assertEqual((self.created, self.comments), ([], []))

    def test_every_exit_after_the_decision_carries_it(self):
        """Restriction flags survive missing groups, failed writes and disabled filing."""
        self._hide(_HIDDEN)
        with mock.patch.object(report_bug, "security_group", return_value=None):
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertFalse(res["filed"])
        self.assertEqual((res["restricted"], res["screened"]), ("regressor", True))
        with mock.patch.object(bugzilla_apply, "_create_bug_keeping_the_bug",
                               side_effect=RuntimeError("boom")):
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertIn("bugzilla write failed", res["skipped"])
        self.assertEqual(res["restricted"], "regressor")
        with mock.patch.object(se.config, "autofile_globally_enabled", return_value=False):
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual((res["skipped"], res["restricted"]), ("autofile disabled", "regressor"))

    def test_what_the_comment_does_not_render_is_screened_too(self):
        """Screen stored fields and list entries omitted from the rendered comment."""
        public_culprit = self.findings.culprit.model_copy(update={"bug": 1855742})
        cases = {
            "component_reason": self.findings.model_copy(update={
                "culprit": public_culprit,
                "component_reason": "The candidate's bug {} was not readable.".format(_HIDDEN)}),
            "past the cap": self.findings.model_copy(update={
                "culprit": public_culprit,
                "ruled_out": ["bug 1855742: backed out"] * 6 + [
                    "bug {}: not supported".format(_HIDDEN)]}),
        }
        self._hide(_HIDDEN)
        for name, findings in cases.items():
            self.created.clear()
            with self.subTest(name):
                res = se.file_spike_bug(_esc(), self.brief, findings, grounded=True)
                self.assertEqual(res["mode"], "spike_new_bug")
                self.assertNotIn("restricted", res, "the bug text does not name it")
                self.assertNotIn(str(_HIDDEN), self.created[0]["description"])
                self.assertTrue(res["findings_withheld"])
                self.assertFalse(disclosure.public_findings(res))

    def test_an_ungrounded_culprit_is_screened(self):
        self._hide(_HIDDEN)
        res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=False)
        self.assertEqual(res["mode"], "spike_new_bug")
        self.assertNotIn("restricted", res)
        self.assertNotIn(str(_HIDDEN), self.created[0]["description"])
        self.assertTrue(res["findings_withheld"])
        findings = self.findings.model_copy(update={"culprit": self.findings.culprit.model_copy(
            update={"bug": None})})
        with mock.patch("crashclouseau.sigage.json_rev", return_value={}):
            res = se.file_spike_bug(_esc(), self.brief, findings, grounded=False)
        self.assertEqual((res["mode"], res["findings_withheld"]), ("spike_new_bug", True),
                         "an unreadable culprit withholds the findings, not the filing")

    def test_a_clean_spike_is_marked_screened(self):
        res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertTrue(res["screened"])
        self.assertNotIn("restricted", res)
        self.assertTrue(disclosure.public_findings(res))

    def test_a_culprit_without_a_bug_is_read_off_its_commit_message(self):
        findings = self.findings.model_copy(update={"culprit": self.findings.culprit.model_copy(
            update={"bug": None})})
        self._hide(_HIDDEN)
        with mock.patch("crashclouseau.sigage.json_rev", return_value={
                "desc": "Bug {} - Update the display on failure. r=someone".format(_HIDDEN)}):
            res = se.file_spike_bug(_esc(), self.brief, findings, grounded=True)
        self.assertEqual(res["restricted"], "regressor")
        with mock.patch("crashclouseau.sigage.json_rev", return_value={}):
            res = se.file_spike_bug(_esc(), self.brief, findings, grounded=True)
        self.assertTrue(res["retry"])

    def test_a_restricted_spike_held_by_a_meta_with_no_bucket_is_not_posted(self):
        self._hide(_HIDDEN)
        meta = dict(self._old_bug(1866944), keywords=["meta"])
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature", return_value=[meta]), \
                mock.patch.object(se.spike_report, "spike_bucket_title", return_value=""):
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertFalse(res["filed"])
        self.assertIn("held by [meta] bug 1866944", res["skipped"])
        self.assertEqual((self.created, self.comments), ([], []))

    def test_a_memory_safety_spike_declines_our_public_bucket_bug(self):
        self.brief["raw_crash"] = {"json_dump": {"crash_info": {"address": "0xe5e5e5e5e5e5e5e5"}}}
        with mock.patch.object(se, "resolve_venue_below_public", return_value={
                "id": 2071528, "kind": "own_bucket", "assigned_to": ""}):
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual((res["mode"], res["public_venue_declined"]), ("spike_new_bug", 2071528))
        self.assertEqual(self.comments, [])
        self.assertEqual(res["restricted"], "memory_safety")


class TestThePublicPages(unittest.TestCase):
    EV = {"uuid": "u-1", "status": "done", "verdict": "lead", "confidence": 70,
          "dossier": {"corroborations": {},
                      "verdict": {"mechanism": {"statement": "the diff adds a call"}},
                      "candidate": {"node": "0a49d5b304b4", "bug": _HIDDEN}},
          "actions": []}

    def _evidence(self, filed_bug, public=True):
        with mock.patch.object(bugzilla_apply.models.Verdict, "get_evidence",
                               return_value=dict(self.EV, filed_bug=filed_bug)):
            return bugzilla_apply.build_evidence("u-1", public=public)

    def test_a_restricted_filing_withholds_the_analysis(self):
        for filed in ({"filed": True, "bug": 2099999, "security_groups": ["core-security"],
                       "restricted": "regressor"},
                      {"filed": True, "bug": 2099998, "withdrawn_refs": 1}):
            ev = self._evidence(filed)
            self.assertEqual((ev["withheld"], ev["withheld_kind"], ev["withheld_reasons"]),
                             (True, "restricted_bug", []))
            self.assertNotIn(str(_HIDDEN), repr(ev))
            self.assertNotIn("0a49d5b304b4", repr(ev))
        self.assertEqual(self._evidence({"filed": True, "restricted": "regressor"},
                                        public=False)["verdict"], "lead")
        self.assertNotIn("withheld", self._evidence({"filed": True, "bug": 2099997}))

    def test_the_spike_table_withholds_the_same_findings(self):
        now = datetime(2026, 9, 28, tzinfo=timezone.utc)
        row = {"id": 1, "status": "done", "created": now.isoformat(), "updated": now.isoformat(),
               "findings": {"assessment": "regression",
                            "culprit": {"node": "0a49d5b304b4", "bug": _HIDDEN}}}
        for filing in ({"filed": True, "bug": 2099999, "restricted": "regressor"},
                       {"filed": True, "bug": 1620171, "withdrawn_refs": 2}):
            rows, _ = html._spike_view([dict(row, filing=filing)], 3900, now)
            self.assertTrue(rows[0]["withheld"])
            self.assertIsNone(rows[0]["culprit_bug"])
        rows, _ = html._spike_view([dict(row, filing={"filed": True, "bug": 5, "screened": True})],
                                   3900, now)
        self.assertEqual(rows[0]["culprit_bug"], _HIDDEN)


class TestWhatAnonymousViewersSee(unittest.TestCase):
    """Anonymous responses must redact both findings and sensitive filing metadata."""

    FILING = {"filed": True, "bug": 2099999, "mode": "spike_new_bug", "screened": True,
              "restricted": "regressor", "security_groups": ["core-security"],
              "regressed_by": [_HIDDEN], "needinfo": "dev@moz.example", "product": "Core",
              "component": "Graphics: Canvas2D", "keywords": ["crash", "regression"],
              "bucket_title": "OffscreenCanvas::GetContext clears the worker ref"}

    def test_a_withheld_filing_record_keeps_only_safe_fields(self):
        out = disclosure.public_filing(self.FILING, True)
        self.assertEqual(out, {"filed": True, "bug": 2099999, "mode": "spike_new_bug",
                               "screened": True, "restricted": "regressor",
                               "security_groups": ["core-security"]})
        self.assertEqual(disclosure.public_filing({"bug": 72, "skipped": "bug 72 was fixed"},
                                                  True)["skipped"], "bug 72 was fixed")
        self.assertEqual(disclosure.public_filing({"skipped": "regressor bug {} is excluded".format(
            _HIDDEN)}, True)["skipped"], "not filed (reason withheld)")
        self.assertIs(disclosure.public_filing(self.FILING, False), self.FILING)

    def test_the_spike_feed(self):
        from crashclouseau import app
        row = {"id": 1, "signature": "S", "status": "done", "filing": dict(self.FILING),
               "findings": {"culprit": {"node": "0a49d5b304b4", "bug": _HIDDEN}}}
        client = app.test_client()
        with mock.patch.object(bugzilla_apply.models.SpikeEscalation, "recent",
                               side_effect=lambda **kw: [dict(row, filing=dict(self.FILING))]), \
                mock.patch.dict(os.environ, {"API_WRITE_TOKEN": "s3cret"}, clear=False):
            anonymous = client.get("/api/spikes").get_json()["rows"][0]
            authorized = client.get("/api/spikes",
                                    headers={"X-Clouseau-Token": "s3cret"}).get_json()["rows"][0]
        self.assertIsNone(anonymous["findings"])
        self.assertNotIn(str(_HIDDEN), repr(anonymous))
        self.assertNotIn("dev@moz.example", repr(anonymous))
        self.assertEqual(authorized["filing"]["regressed_by"], [_HIDDEN])

    def test_rows_without_a_screen_are_withheld(self):
        now = datetime(2026, 9, 28, tzinfo=timezone.utc)
        row = {"id": 1, "status": "done", "created": now.isoformat(), "updated": now.isoformat(),
               "findings": {"assessment": "regression", "culprit": {"node": "0a49d5b304b4"}}}
        for filing in (None, {"filed": True, "bug": 5, "needinfo": "dev@moz.example",
                              "component": "Graphics"}):
            rows, _ = html._spike_view([dict(row, filing=filing)], 3900, now)
            self.assertTrue(rows[0]["withheld"])
            self.assertIsNone(rows[0]["culprit_node"])
            self.assertIsNone(rows[0]["needinfo"])
            self.assertIsNone(rows[0]["component"])
        rows, _ = html._spike_view([dict(row, filing=None)], 3900, now, public=False)
        self.assertEqual(rows[0]["culprit_node"], "0a49d5b304b4")
        rows, _ = html._spike_view([dict(row, findings=None, filing=None)], 3900, now)
        self.assertFalse(rows[0]["withheld"], "nothing to withhold")

    def test_an_ordinary_decline_or_error_withholds_the_analysis(self):
        ev = dict(TestThePublicPages.EV, filed_bug=None)
        for key in ("filing_declined", "filing_error"):
            with mock.patch.object(bugzilla_apply.models.Verdict, "get_evidence",
                                   return_value=dict(ev, **{key: {"restricted": "regressor"}})):
                out = bugzilla_apply.build_evidence("u-1")
            self.assertTrue(out["withheld"], key)
            self.assertNotIn(str(_HIDDEN), repr(out))

    def test_the_tasks_table_hides_who_was_asked_on_a_restricted_filing(self):
        from tests.test_tasks_view import NOW, _row
        row = _row(filed_bug="2099999", filed_mode="new_bug", filed_needinfo="dev@moz.example",
                   filed_restricted="regressor")
        tasks, _ = html._task_view([row], 3900, NOW)
        self.assertIsNone(tasks[0]["filed_needinfo"])
        self.assertEqual(tasks[0]["filed_bug"], "2099999")
        tasks, _ = html._task_view([row], 3900, NOW, public=False)
        self.assertEqual(tasks[0]["filed_needinfo"], "dev@moz.example")


class TestTheDeclineRecord(unittest.TestCase):
    def test_it_keeps_the_reason(self):
        from crashclouseau.agent import orchestrator
        seen = []
        with mock.patch.object(orchestrator.models.CrashStack, "get_by_uuid",
                               return_value=({}, {"channel": "nightly", "product": "Firefox",
                                                  "signature": "S", "buildid": None})), \
                mock.patch.object(bugzilla_apply, "autofile_bug", return_value={
                    "filed": False, "skipped": "product/component unresolved",
                    "restricted": "regressor", "withdrawn_refs": 2,
                    "findings_withheld": True}), \
                mock.patch.object(orchestrator.models.Dossier, "record_filing_decline",
                                  side_effect=lambda u, i: seen.append(i)):
            orchestrator._autofile("u-1", {"dossier": {}}, {"verdict": "lead", "confidence": 70})
        self.assertEqual((seen[0]["restricted"], seen[0]["withdrawn_refs"],
                          seen[0]["findings_withheld"]), ("regressor", 2, True))


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
