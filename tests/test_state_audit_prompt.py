# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Prompt coverage for state audits, absence checks, and narrowed claims."""
import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite://")

from crashclouseau.agent import roles, triage  # noqa: E402


class TestTheTracerAuditsTheState(unittest.TestCase):
    def test_the_tracer_asks_for_setters_and_resets(self):
        prompt = roles.make_role("data-flow-tracer").prompt
        self.assertIn(roles._STATE_AUDIT, prompt)
        self.assertIn("one writer among several", prompt)
        self.assertIn("List the other sites that set or reset that state", prompt)
        self.assertIn("leaves the value the crash found", prompt)

    def test_the_audit_is_scoped_to_state_that_outlives_the_call(self):
        self.assertIn("STATE THAT OUTLIVES THE CALL", roles._STATE_AUDIT)
        self.assertIn("a read finds it null, stale or out of range", roles._STATE_AUDIT)
        self.assertIn("computed inside the crashing function has no writers", roles._STATE_AUDIT)

    def test_a_java_tracer_keeps_it(self):
        self.assertIn(roles._STATE_AUDIT, roles.make_role("data-flow-tracer", java=True).prompt)


class TestTheSkepticLooksForTheCounterExample(unittest.TestCase):
    def test_an_absence_needs_a_search(self):
        prompt = roles._ROLES["skeptic"]["prompt"]
        self.assertIn(roles._ABSENCE, prompt)
        self.assertIn("could have found the counter-example", prompt)
        self.assertIn("Reading one function is `unverifiable` at best", prompt)

    def test_a_reset_that_misses_the_crashing_object_narrows_the_claim(self):
        self.assertIn("a `fail` only if it reaches the crashing object", roles._ABSENCE)
        self.assertIn("`pass`, with the narrowed claim in the note", roles._ABSENCE)

    def test_every_channel_rendering_keeps_it(self):
        for channel in ("beta", "release", "esr153"):
            self.assertIn(roles._ABSENCE, roles.make_role("skeptic", channel=channel).prompt,
                          channel)
        self.assertIn(roles._ABSENCE, roles.make_role("skeptic", java=True).prompt)

    def test_the_clauses_stay_short(self):
        self.assertLess(len(roles._STATE_AUDIT.split()), 110)
        self.assertLess(len(roles._ABSENCE.split()), 100)


class TestThePrincipalIsTold(unittest.TestCase):
    def test_the_narrowed_claim(self):
        for prompt in (triage._system_prompt(), triage._system_prompt("beta"),
                       triage._system_prompt(None, True)):
            self.assertIn("the mechanism states the narrowed claim", prompt)


if __name__ == "__main__":
    unittest.main()
