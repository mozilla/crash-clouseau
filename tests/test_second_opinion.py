# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Blind second-opinion agent: options guardrail (tight allowlist), prompt modes, parsing,
# config. No SDK/network — build_options gets a dummy searchfox client.
#   DATABASE_URL=sqlite:// python -m unittest tests.test_second_opinion
import os
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite://")

from crashclouseau import config  # noqa: E402
from crashclouseau.agent.second_opinion import (  # noqa: E402
    build_options,
    parse_second_opinion,
    _user_prompt,
)

_CRASH = {"uuid": "u-1", "signature": "mozilla::Foo::Bar", "channel": "nightly",
          "product": "Firefox", "stack": "0 Foo::Bar foo.cpp:1", "pin_rev": ""}


class TestSecondOpinionOptions(unittest.TestCase):
    def test_tight_allowlist_has_no_shell(self):
        opts = build_options(_CRASH, {"node": "abc", "bug": 1}, searchfox_client=object())
        allowed = set(opts.allowed_tools)
        for t in ("mcp__searchfox__define", "mcp__patch__diff", "mcp__history__blame",
                  "mcp__source__raw_file", "mcp__bugzilla__bug", "mcp__socorro__crash_stats"):
            self.assertIn(t, allowed)
        # No shell / builtins / subagents -> the agent cannot GET hg json-pushes (no pushlog).
        for banned in ("Bash", "Read", "Grep", "Glob", "Task"):
            self.assertNotIn(banned, allowed)
        self.assertEqual(
            set(opts.mcp_servers),
            {"searchfox", "patch", "history", "source", "bugzilla", "socorro"})
        self.assertEqual(opts.model, "claude-opus-4-8")   # Opus 4.8
        self.assertEqual(opts.effort, "high")             # measured better than max; see config


class TestSecondOpinionParse(unittest.TestCase):
    def test_verify_mode(self):
        text = ('analysis...\n```json\n{"corroborates": true, "confidence": "high", '
                '"mechanism": "UAF of mFoo", "refutation": ""}\n```')
        so = parse_second_opinion(text, {"node": "abc"})
        self.assertEqual(so.mode, "verify")
        self.assertTrue(so.corroborates)
        self.assertEqual(so.confidence, "high")
        self.assertIn("UAF", so.mechanism)

    def test_mechanism_mode_no_candidate(self):
        text = '```json\n{"corroborates": null, "confidence": "medium", "mechanism": "reused id"}\n```'
        so = parse_second_opinion(text, None)
        self.assertEqual(so.mode, "mechanism")
        self.assertIsNone(so.corroborates)

    def test_refutation_captured(self):
        text = ('```json\n{"corroborates": false, "confidence": "high", "mechanism": "", '
                '"refutation": "the assert is debug-only; this is a release crash"}\n```')
        so = parse_second_opinion(text, {"node": "abc"})
        self.assertFalse(so.corroborates)
        self.assertIn("debug-only", so.refutation)

    def test_no_or_bad_block(self):
        self.assertIsNone(parse_second_opinion("no json here", None))
        self.assertIsNone(parse_second_opinion(None, None))
        self.assertIsNone(parse_second_opinion("```json\n{not valid,}\n```", None))


class TestSecondOpinionPrompt(unittest.TestCase):
    def test_verifier_prompt_is_neutral_and_names_candidate(self):
        p = _user_prompt(_CRASH, {"node": "deadbeef", "bug": 42})
        self.assertIn("deadbeef", p)
        self.assertIn("bug 42", p)
        self.assertIn("may be unrelated", p)          # neutral / anti-confirmation
        self.assertIn("mozilla::Foo::Bar", p)

    def test_generator_prompt_asks_for_mechanism(self):
        p = _user_prompt(_CRASH, None)
        self.assertIn("No candidate", p)
        self.assertIn("MECHANISM", p)
        self.assertNotIn("candidate regressor changeset has been proposed", p)


class TestSecondOpinionConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = config.get_agent_second_opinion()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["model"], "opus")
        # `high` beat `max` head-to-head on 51 ground-truth corpus cases (equal-or-better
        # sensitivity/specificity at half the cost and 2.6x the speed), so max is not the default.
        self.assertEqual(cfg["effort"], "high")
        # 25 (= Confidence.low), NOT 50: any `lead` is REPORTED, so a threshold of 50 left the
        # weakest shown leads with no second opinion at all (4 of 31 over the first prod days).
        self.assertEqual(cfg["min_confidence"], 25)

    def test_env_enable(self):
        with mock.patch.dict(os.environ, {"SECOND_OPINION_ENABLED": "1"}):
            self.assertTrue(config.get_agent_second_opinion()["enabled"])


if __name__ == "__main__":
    unittest.main()
