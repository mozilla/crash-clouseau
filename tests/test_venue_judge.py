# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Venue judge prompt, response parsing and failure handling tests."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import config  # noqa: E402
from crashclouseau.agent import venue_judge as vj  # noqa: E402
from tests import REAL_VENUE_CHECK  # noqa: E402

_CFG = {"enabled": True, "model": "claude-opus-5-5", "effort": "medium", "max_venues": 3}
_BUG = {"id": 5, "summary": "test.js | application crashed [@ Foo::Bar]", "product": "Toolkit",
        "component": "Telemetry", "creation_time": "2017-01-16T14:07:16Z",
        "cf_crash_signature": ""}
_COMMENTS = [{"count": 0, "author": "dev@moz.example", "when": "2017-01-16",
              "time": "2017-01-16T00:00:00Z", "text": "The try push shows an xpcshell failure."}]


def _result(text, is_error=False):
    return mock.Mock(result=text, is_error=is_error, total_cost_usd=0.09)


def _answer(verdict, reason="The bug is a test failure."):
    return '```json\n{{"verdict": "{}", "reason": "{}"}}\n```'.format(verdict, reason)


class TestThePrompt(unittest.TestCase):
    def test_fields_comments_and_draft_are_fenced_with_the_nonce(self):
        text = vj.user_prompt(_BUG, _COMMENTS, "the analysis", "abcd")
        for part in ("BEGIN BUG abcd", "Bug 5: test.js | application crashed [@ Foo::Bar]",
                     "Product: Toolkit :: Telemetry", "Filed: 2017-01-16",
                     "Crash signatures field: (none)", "xpcshell failure", "END BUG abcd",
                     "BEGIN DRAFT abcd", "the analysis", "END DRAFT abcd"):
            self.assertIn(part, text)

    def test_the_signature_field_is_listed(self):
        bug = dict(_BUG, cf_crash_signature="[@ Foo::Bar]\n[@ Foo::Baz]")
        self.assertIn("Crash signatures field: Foo::Bar Foo::Baz",
                      vj.user_prompt(bug, [], "d", "n"))

    def test_the_judge_has_no_tools(self):
        opts = vj.build_options(_CFG)
        self.assertEqual((opts.tools, opts.allowed_tools), ([], []))


class TestTheAnswer(unittest.TestCase):
    def test_a_known_verdict_is_required(self):
        self.assertEqual(vj.parse(_answer("different")),
                         {"verdict": "different", "reason": "The bug is a test failure."})
        self.assertIsNone(vj.parse(_answer("maybe")))
        self.assertIsNone(vj.parse("no block"))

    def test_the_reason_is_screened_flattened_and_capped(self):
        out = vj.parse(_answer("same", "see\\n https://evil.example/x " + "y" * 300))
        self.assertTrue(out["reason"].startswith("see (link removed) yyy"))
        self.assertEqual(len(out["reason"]), vj._MAX_REASON)


class TestTheCheck(unittest.TestCase):
    def _check(self, result=None, raises=None, bug=_BUG, comments=_COMMENTS, draft="draft"):
        ask = mock.AsyncMock(return_value=result, side_effect=raises)
        with mock.patch.object(vj, "_bug_fields", return_value=bug), \
                mock.patch.object(vj.comment_reader, "bug_comments", return_value=comments), \
                mock.patch.object(vj, "_ask", ask):
            return REAL_VENUE_CHECK(5, draft, _CFG), ask

    def test_the_verdict_and_its_cost(self):
        out, ask = self._check(result=_result(_answer("different")))
        self.assertEqual(out, {"verdict": "different", "reason": "The bug is a test failure.",
                               "cost_usd": 0.09})
        self.assertIn("BEGIN DRAFT", ask.call_args.args[0])

    def test_failures_give_no_verdict(self):
        self.assertIsNone(self._check(bug=None)[0])
        self.assertIsNone(self._check(comments=None)[0])
        self.assertIsNone(self._check(raises=RuntimeError("cli"))[0])
        self.assertIsNone(self._check(result=_result("", is_error=True))[0])
        self.assertIsNone(self._check(result=_result("no block"))[0])

    def test_an_empty_draft_reads_nothing(self):
        out, ask = self._check(draft="  ")
        self.assertIsNone(out)
        ask.assert_not_called()

    def test_a_reason_naming_a_hidden_bug_is_dropped(self):
        with mock.patch.object(vj.disclosure, "nonpublic", return_value={1996653}):
            out, _ = self._check(result=_result(_answer("different", "Tracked in bug 1996653.")))
        self.assertEqual((out["verdict"], out["reason"]), ("different", ""))
        with mock.patch.object(vj.disclosure, "nonpublic", return_value=set()):
            out, _ = self._check(result=_result(_answer("different", "Tracked in bug 1996653.")))
        self.assertEqual(out["reason"], "Tracked in bug 1996653.")


class TestTheComments(unittest.TestCase):
    def _prompt(self, comments):
        resp = mock.Mock(status_code=200, raise_for_status=lambda: None,
                         json=lambda: {"bugs": {"5": {"comments": comments}}})
        ask = mock.AsyncMock(return_value=_result(_answer("same")))
        with mock.patch.object(vj, "_bug_fields", return_value=_BUG), \
                mock.patch.object(vj.comment_reader.net, "get", return_value=resp), \
                mock.patch.object(vj, "_ask", ask):
            REAL_VENUE_CHECK(5, "draft", _CFG)
        return ask.call_args.args[0]

    def test_a_bot_description_is_kept_and_later_clouseau_comments_are_not(self):
        prompt = self._prompt([
            {"count": 0, "creator": "clouseau-bot@mozilla.tld", "text": "the bot description",
             "creation_time": "2026-09-01T00:00:00Z"},
            {"count": 1, "creator": "dev@moz.example", "text": "a person's comment",
             "creation_time": "2026-09-02T00:00:00Z"},
            {"count": 2, "creator": "clouseau-bot@mozilla.tld", "text": "our later analysis",
             "creation_time": "2026-09-03T00:00:00Z"},
            {"count": 3, "creator": "release-mgmt-account-bot@mozilla.tld",
             "text": "a status flag nag", "creation_time": "2026-09-04T00:00:00Z"},
        ])
        self.assertIn("the bot description", prompt)
        self.assertIn("a person's comment", prompt)
        self.assertNotIn("our later analysis", prompt)
        self.assertNotIn("a status flag nag", prompt)

    def test_an_automation_description_is_kept(self):
        prompt = self._prompt([
            {"count": 0, "creator": "intermittent-bug-filer@mozilla.bugs.tld",
             "text": "Filed by: a sheriff. Parsed log: test failure",
             "creation_time": "2026-09-01T00:00:00Z"}])
        self.assertIn("Parsed log: test failure", prompt)


class TestTheConfig(unittest.TestCase):
    def test_the_defaults(self):
        with mock.patch.object(config, "get_agent", return_value={}):
            self.assertEqual(config.get_agent_venue_judge(),
                             {"enabled": True, "model": "opus", "effort": "medium",
                              "max_venues": 3})


if __name__ == "__main__":
    unittest.main()
