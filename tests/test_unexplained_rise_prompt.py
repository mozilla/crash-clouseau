# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Telling the agent that a rise with no candidate is a finding, not a failure.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        uv run python -m unittest tests.test_unexplained_rise_prompt

Crash 84794f8d: the rate had risen, the window explained nothing, and the agent reached for the
closest changeset anyway — which was the FIX for the same signature, already in the build. Its
own skeptic refuted the window claim, `schema` rule (1b) read that as "noise", and a run with a
source-verified call path published nothing.

The block is deliberately small in reach and careful in wording, because the population that
looks like this and SHOULD stay silent is much bigger than the one that should not: over 1,646
model-authored abstains in 30 days, 80.7% sit on a signature whose rate is not even measurable,
17.1% are measurably not rising, and most of the remainder are third-party driver code or memory
exhaustion. So the last paragraph — abstaining is still right when the crash is not ours — is
load-bearing, and these tests pin it.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402

from crashclouseau.agent import second_opinion, triage  # noqa: E402

RISING = {"signature_trend_window_days": 7, "signature_trend_installs": 9,
          "signature_trend_reports": 9, "signature_trend_baseline_days": 56,
          "signature_trend_baseline_installs": 3,
          "signature_trend_expected_installs": 0.46, "signature_trend_ratio": 19.36}
QUIET = {**RISING, "signature_trend_ratio": 1.1}


def _crash(**over):
    c = {"uuid": "u-1", "signature": "libc.so.6 | cuEGLApiInit", "channel": "nightly",
         "product": "Firefox", "buildid": "20260826091205", "stack": "frame 0",
         "is_offstack": True, "signature_trend": RISING,
         "candidates": [{"node": "bbdaf4e3b2c2", "score": None, "bug": 2063678,
                         "desc": "Bug 2063678 - Allow setsockopt", "noise": False}]}
    c.update(over)
    return c


class TestWhenItFires(unittest.TestCase):
    def test_a_rise_with_the_undifferentiated_window(self):
        self.assertTrue(triage._unexplained_rise_lines(_crash()))

    def test_a_rise_with_nothing_scored_onto_a_frame(self):
        # On-stack in name only: no candidate carries a proximity score.
        self.assertTrue(triage._unexplained_rise_lines(
            _crash(is_offstack=False, candidates=[{"node": "n", "score": None}])))

    def test_a_real_proximity_score_means_the_ordinary_hunt(self):
        # With something scored onto a crash frame the agent has a defensible candidate to
        # work; this block would only invite it to give up early.
        self.assertFalse(triage._unexplained_rise_lines(
            _crash(is_offstack=False, candidates=[{"node": "n", "score": 9}])))

    def test_no_rise_no_block(self):
        for trend in ({}, None, QUIET):
            self.assertFalse(triage._unexplained_rise_lines(_crash(signature_trend=trend)),
                             trend)

    def test_an_unmeasurable_rate_is_not_a_rise(self):
        """80.7% of the abstain panel: the signature is not in the rollup at all. That must
        read as "not measured", never as "flat" and never as "rising"."""
        self.assertFalse(triage._unexplained_rise_lines(_crash(signature_trend={})))


class TestWhatItSays(unittest.TestCase):
    def setUp(self):
        self.text = "\n".join(triage._unexplained_rise_lines(_crash()))

    def test_it_releases_the_agent_from_naming_a_changeset(self):
        self.assertIn("NO CHANGESET IS REQUIRED", self.text)

    def test_it_names_the_failure_mode_it_exists_to_stop(self):
        self.assertIn("DO NOT REACH", self.text)
        self.assertIn("refuted by your own skeptic", self.text)

    def test_it_says_what_a_good_empty_answer_contains(self):
        for want in ("component", "what you searched and ruled out"):
            self.assertIn(want, self.text)

    def test_it_still_blesses_a_correct_abstain(self):
        """The load-bearing paragraph. Most crashes that reach this block genuinely are
        somebody else's, and the block must not turn those correct silences into noise."""
        for want in ("third-party", "unsymbolicated", "memory exhaustion",
                     "abstaining IS the finding", "Do not manufacture"):
            self.assertIn(want, self.text)


class TestItIsTriageOnly(unittest.TestCase):
    """`_crash_facts` is shared verbatim with the blind second opinion. A FACT both models
    lack has to reach both; a suggested direction must not prime the reviewer."""

    def test_the_rate_itself_reaches_both(self):
        facts = "\n".join(triage._crash_facts(_crash()))
        self.assertIn("SIGNATURE CRASH RATE HAS RISEN", facts)

    def test_the_instruction_does_not(self):
        facts = "\n".join(triage._crash_facts(_crash()))
        self.assertNotIn("NO CHANGESET IS REQUIRED", facts)
        self.assertNotIn("DO NOT REACH", facts)

    def test_the_second_opinion_prompt_is_clean(self):
        so = second_opinion._user_prompt(_crash(), {"node": "bbdaf4e3b2c2"})
        self.assertNotIn("NO CHANGESET IS REQUIRED", so)
        self.assertNotIn("DO NOT REACH", so)

    def test_but_the_triage_prompt_carries_it(self):
        self.assertIn("NO CHANGESET IS REQUIRED", triage._user_prompt(_crash()))


class TestWhereItSits(unittest.TestCase):
    def test_after_the_candidate_list(self):
        # It is about what to do when that list does not answer the question, so it has to
        # read after it.
        p = triage._user_prompt(_crash())
        self.assertLess(p.index("bbdaf4e3b2c2"), p.index("NO CHANGESET IS REQUIRED"))

    def test_after_the_rate_it_refers_to(self):
        p = triage._user_prompt(_crash())
        self.assertLess(p.index("SIGNATURE CRASH RATE HAS RISEN"),
                        p.index("NO CHANGESET IS REQUIRED"))


if __name__ == "__main__":
    unittest.main()
