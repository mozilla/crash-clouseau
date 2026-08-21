# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Code that was never compiled into the crashing binary cannot be the mechanism.
#
# Bug 2063782: we filed a needinfo on a mechanism guarded by `#ifdef JS_GC_CONCURRENT_MARKING`.
# The skeptic had NOTICED -- its note said the flag "could not be confirmed" -- but "cannot
# confirm" routes to `unverifiable`, which is advisory and KEEPS the lead, so the filing went out
# anyway and Jon Coppeard answered it by hand: "It does not. Is it possible to make Clouseau see
# this somehow?" It is: `js/moz.configure` declares `option("--enable-gc-concurrent-marking")`
# with no `default=`, which is OFF unless someone asks for it, and a searchfox query for the
# symbol lands on the `set_define` two lines below it. This is the single most common shape among
# the module-owner refutations -- 3 of 4.
#
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_compiled_out_guard
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402

from crashclouseau.agent import roles  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    Candidate,
    Claim,
    Confidence,
    Decision,
    Dossier,
    SearchfoxCitation,
    SkepticResult,
    SkepticStatus,
    Verdict,
)

_SF = SearchfoxCitation(
    permalink="https://searchfox.org/x#1", symbol_id="_Z1", repo="mozilla-central"
)
_SKEPTIC = roles._ROLES["skeptic"]


def _lead_with(status, note):
    """A model-emitted LEAD carrying one skeptic result -- the exact shape of bug 2063782."""
    return Dossier(
        crash={"uuid": "u", "signature": "sig", "frames": []},
        verdict=Verdict(
            decision=Decision.lead,
            confidence=Confidence.probable,
            mechanism=Claim(summary="concurrent marking barrier", citations=[_SF]),
        ),
        candidate=Candidate(node="a" * 12, bug=1, author="A", channel="nightly"),
        skeptic=[SkepticResult(status=status, claim_ref="mechanism", note=note)],
    )


class TestTheSkepticIsToldToCheckTheBuildFlag(unittest.TestCase):
    def test_the_clause_reaches_the_skeptic_prompt(self):
        self.assertIn("never compiled into this build", _SKEPTIC["prompt"])

    def test_it_asks_about_the_mechanism_symbols_not_only_the_cited_ones(self):
        """The refutations were checked against the bugs, and the obvious test is the wrong one.

        On all three (2062114, 2063782, 2063902) the CITED line is ordinary always-compiled code
        -- `CacheIRStubInfo::fieldType` at `js/src/jit/CacheIRCompiler.h#1366` has no enclosing
        `#ifdef` within 140 lines -- and neither named changeset so much as mentions
        `JS_GC_CONCURRENT_MARKING` in its diff. What is compiled out is the mechanism's PREMISE:
        `js::gc::AutoMarkingLock`, whose members, constructor body and destructor body are each
        wrapped in `#ifdef JS_GC_CONCURRENT_MARKING` and which documents itself "This is a no op
        outside concurrent marking builds". So a rule that looks only at the citation's own line
        fires on 0 of 3."""
        prompt = _SKEPTIC["prompt"]
        self.assertIn("every symbol the MECHANISM DEPENDS ON, not just the ones you cited", prompt)
        self.assertIn("compiled into the binary while doing NOTHING", prompt)
        self.assertIn("AutoMarkingLock", prompt)

    def test_it_names_the_default_rule_that_decides_the_answer(self):
        # The one thing a model gets wrong unaided: `--enable-X` with no `default=` is OFF.
        prompt = _SKEPTIC["prompt"]
        self.assertIn('`option("--enable-X")` with no `default=` is OFF', prompt)
        self.assertIn('`option("--disable-X")` is ON', prompt)

    def test_it_routes_to_fail_and_not_to_unverifiable(self):
        prompt = _SKEPTIC["prompt"]
        self.assertIn("demonstrably UNRELATED, so `fail` it", prompt)
        self.assertIn("a mechanism resting on it cannot be the cause", prompt)
        self.assertIn("`unverifiable` is the wrong answer here", prompt)

    def test_it_keeps_the_conservative_default_when_the_option_cannot_be_found(self):
        self.assertIn("Only when you cannot find the option at all is this `unverifiable`",
                      _SKEPTIC["prompt"])

    def test_the_skeptic_actually_has_the_two_tools_the_clause_names(self):
        """The clause tells the skeptic to run two specific tools. An instruction to use a tool
        the role was never granted is worse than no instruction: it reads as implemented and
        cannot fire. Pin the grant alongside the words."""
        for tool in ("mcp__searchfox__search", "mcp__source__raw_file"):
            self.assertIn(tool, _SKEPTIC["tools"])


class TestTheStatusDecidesWhetherWeFile(unittest.TestCase):
    """Why the clause had to change the STATUS rather than add a warning: the two statuses have
    opposite consequences, and nothing downstream reads the note."""

    def test_fail_on_a_lead_abstains(self):
        d = _lead_with(SkepticStatus.failed,
                       "JS_GC_CONCURRENT_MARKING is not defined in this build "
                       "(js/moz.configure:1077, no default=)")
        self.assertEqual(d.verdict.decision, Decision.abstain)

    def test_unverifiable_keeps_the_lead_which_is_how_2063782_went_out(self):
        d = _lead_with(SkepticStatus.unverifiable,
                       "whether this Nightly compiles JS_GC_CONCURRENT_MARKING "
                       "could not be confirmed")
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.verdict.confidence, Confidence.probable)

    def test_the_note_alone_changes_nothing(self):
        # Both dossiers above carry a note naming the flag; only the status moved the verdict.
        # So a prompt clause that merely asked the skeptic to MENTION the guard would be inert.
        said = _lead_with(SkepticStatus.unverifiable, "compiled out, JS_GC_CONCURRENT_MARKING")
        self.assertEqual(said.verdict.decision, Decision.lead)


if __name__ == "__main__":
    unittest.main()
