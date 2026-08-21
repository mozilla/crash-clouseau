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
# THE WALK THAT SENTENCE DESCRIBES IS NO LONGER IN THE PROMPT, and this file is now largely the
# record of why. Measured over 1996 prod dossiers (2026-07-06..08-05; 8901 skeptic claims, 1765
# `fail`): 39 `fail`s reason about a build guard, the `set_define` -> `option()` -> `default=`
# instrument serves 2 of them, and both of those filings are suppressed with no LLM at all by
# `_apply_compiled_out_gate`. What the clause keeps is the CONTEXT -- code that is not in THIS
# build is `fail` -- plus the three limits the reviewer's context actually implies: the
# `GUARD_DENY` macros, the report's own `OS:` line, and prefs. The BINDING decision moved into
# `Dossier._skeptic_veto` (1c) / `compiled_out.is_build_flag_ground`, because an abstain is the
# harshest thing we do and the least visible one.
#
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_compiled_out_guard
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402

from crashclouseau import compiled_out as co  # noqa: E402
from crashclouseau.agent import roles, triage  # noqa: E402
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


def _dossier(skeptic, decision=Decision.lead, corroborations=None):
    """A dossier carrying `skeptic` results -- a model-emitted LEAD by default, which is the
    exact shape of bug 2063782."""
    extra = {"confidence": Confidence.probable}
    if decision is Decision.strong_evidence:
        extra = {"confidence": Confidence.high,
                 "consistency": Claim(summary="only candidate in the window", citations=[_SF])}
    return Dossier(
        crash={"uuid": "u", "signature": "sig", "frames": []},
        verdict=Verdict(
            decision=decision,
            mechanism=Claim(summary="concurrent marking barrier", citations=[_SF]),
            **extra,
        ),
        candidate=Candidate(node="a" * 12, bug=1, author="A", channel="nightly"),
        skeptic=skeptic,
        corroborations=corroborations or {},
    )


def _lead_with(status, note, citations=None, corroborations=None):
    return _dossier([SkepticResult(status=status, claim_ref="mechanism", note=note,
                                   citations=list(citations or []))],
                    corroborations=corroborations)


# The two notes everything below turns on. Both are real prod skeptic notes. The first is the
# shape of the three jcoppeard refutations; the second is the verbatim ground of a BINDING veto
# we got RIGHT, on crash 560c0f2f-07cc-46c6-950c-1d8240260731.
_FLAG_NOTE = ("`gc::AutoMarkingLock`/`JS_GC_CONCURRENT_MARKING` is disabled by default "
              "(`--enable-gc-concurrent-marking`, x86_64 CI-only variant) so the lock is a "
              "no-op on official Nightly")
_GTK_NOTE = "GTK-gated Linux ibus/fcitx key-event plumbing, not compiled into Windows builds"


class TestTheClauseKeepsTheContextNotTheInstrument(unittest.TestCase):
    """What survived the overfitting audit and what did not.

    SCOPE vs EVIDENCE was the finding. The clause was written from 3 refutations by one reviewer
    in one subsystem, and applied to every mechanism resting on a lock/barrier/assert/counter --
    384 of the 842 reportable verdicts in a month (45%; "assert" alone 167). Its instrument, the
    `set_define` -> `option()` -> `default=` walk, serves 2 of the 39 build-guard `fail`s in that
    month, and those 2 filings (2063782, 2063902) now need no LLM at all: at a real pinned node
    the diff ranker puts `gc::AutoMarkingLock` #1 of 8 and `hollow_symbols` fires on it and on
    none of its 7 siblings."""

    def test_the_context_still_reaches_the_prompt(self):
        self.assertIn("code that is not in THIS build", _SKEPTIC["prompt"])

    def test_it_still_asks_about_the_mechanism_symbols_not_only_the_cited_ones(self):
        """9216f51's finding stands: on all three refutations the CITED line is ordinary
        always-compiled code and neither changeset mentions the flag. What is compiled out is
        `js::gc::AutoMarkingLock`, whose every function body is `#ifdef`'d."""
        prompt = _SKEPTIC["prompt"]
        self.assertIn("every symbol the MECHANISM DEPENDS ON, not just the ones you cited", prompt)
        self.assertIn("AutoMarkingLock", prompt)

    def test_compiled_out_is_still_fail_and_not_unverifiable(self):
        # defe860 is NOT reverted. The PLATFORM shape is 15 of the 39 build-guard fails and 3 of
        # the 8 binding vetoes in the month, and it is correct every time; routing it back to
        # `unverifiable` would turn ~38% of the correct noise-kills into filed leads.
        self.assertIn("demonstrably UNRELATED: that is `fail`", _SKEPTIC["prompt"])

    def test_the_moz_configure_walk_is_gone(self):
        prompt = _SKEPTIC["prompt"]
        for gone in ("set_define", '`option("--enable-X")`', "`option()` above it",
                     "Only when you cannot find the option at all"):
            self.assertNotIn(gone, prompt)

    def test_the_citation_line_sub_clause_is_gone(self):
        """0-of-3 by its own author (9216f51), and its trigger is noise: 4 of 44 corpus_ship
        top-frame crash lines sit inside an `#if` and 3 of those 4 are include guards
        (GLCONTEXT_H_, SANDBOX_WIN_SRC_POLICY_ENGINE_PARAMS_H_) or MOZ_HAS_MOZGLUE."""
        self.assertNotIn("cited line itself sits inside", _SKEPTIC["prompt"])

    def test_the_prompt_now_carries_the_deny_lists_the_code_already_had(self):
        """21 of the 39 build-guard `fail`s name a macro in `GUARD_DENY` -- the 20 the CODE
        refuses to touch -- and the prompt named none of them. Rendered from `compiled_out` so
        the two cannot drift."""
        prompt = _SKEPTIC["prompt"]
        for macro in co.GUARD_DENY:
            self.assertIn(macro, prompt, macro)

    def test_it_splits_the_always_on_macros_from_the_build_type_ones(self):
        """The brief's literal sentence ("never conclude off for DEBUG / ... / NIGHTLY_BUILD")
        is false for half the list, and the corpus says so: 4 of the 21 real notes the code
        predicate fires on are "`#ifdef DEBUG` / `MOZ_ASSERT` is compiled out of the opt
        Nightly", which is TRUE (mfbt/Assertions.h:563). MOZ_DIAGNOSTIC_ASSERT_ENABLED is the
        one that is ON (moz.configure:174, `when=... milestone.is_nightly ...`)."""
        prompt = _SKEPTIC["prompt"]
        self.assertIn("9-11% of the crashes we analyse are MOZ_DIAGNOSTIC_ASSERT", prompt)
        self.assertIn("is not in the shipped binary", prompt)
        for macro in co.CHANNEL_ON_DENY:
            self.assertIn(macro, prompt, macro)

    def test_ndebug_is_told_to_the_model_as_ON_because_an_opt_build_defines_it(self):
        """THE WRONG-DIRECTION CASE INSIDE LIMIT (1), and it reads as a typo until you check.

        Splitting the deny list by build type invites putting `NDEBUG` next to `DEBUG`. It is
        the other way round: `moz.configure`'s `debug_defines` returns `["DEBUG", ...]` for a
        debug build and `["NDEBUG", "TRIMMED"]` otherwise, so the official opt Nightly DEFINES
        `NDEBUG` and `#ifdef NDEBUG` code is precisely what shipped. The one claim in the whole
        8901-claim dump that mentions it agrees -- "Nightly defines NDEBUG" -- so a prompt
        saying the opposite would talk the model out of a read it already gets right, and the
        resulting `fail` would BIND (`_BUILD_TYPE_GROUND` would veto the unbind) into an
        abstain no scoreboard can see."""
        prompt = _SKEPTIC["prompt"]
        always_on = prompt.split('Never conclude "off" for ')[1].split(": they are ON")[0]
        the_opposite = prompt.split(" are the opposite")[0].rsplit(". ", 1)[-1]
        self.assertIn("NDEBUG", always_on)
        self.assertNotIn("NDEBUG", the_opposite)
        self.assertIn("DEBUG,", the_opposite)          # the sentence really is the off list
        self.assertIn("NDEBUG", co.CHANNEL_ON_DENY)
        self.assertNotIn("NDEBUG", co.BUILD_TYPE_DENY - co.CHANNEL_ON_DENY)

    def test_the_platform_limit_points_at_a_fact_the_prompt_really_carries(self):
        """A cross-file claim, so it is pinned: `triage._crash_facts` is what puts the `OS:`
        line in front of the skeptic. If that ever stops being emitted, this clause is telling
        the model to read something that is not there."""
        self.assertIn("`OS:` line above", _SKEPTIC["prompt"])
        self.assertIn("OS: Windows 10",
                      triage._crash_facts({"raw_crash": {"os_pretty_version": "Windows 10"}}))

    def test_prefs_are_unverifiable_never_fail(self):
        # A `StaticPrefList.yaml` default is not what shipped: 16 prefs whose YAML value is
        # `false` ship `true` from firefox.js/all.js, and 82 more carry a build template as
        # their default (66 @IS_NIGHTLY_BUILD@, 11 @IS_EARLY_BETA_OR_EARLIER@, ...). This lives
        # in the PROMPT only -- the code predicate had a pref arm and the replay killed it.
        self.assertIn("pref is on is `unverifiable`, NEVER `fail`", _SKEPTIC["prompt"])

    def test_it_says_the_configure_default_is_not_the_last_word(self):
        # Walking js/moz.configure at build node 8e966e6c894a labels 9 macros default-off and 3
        # of them are ON in official Nightly (MOZ_RUST_SIMD, MOZ_INSTRUMENTS, MOZ_PROFILING).
        # The walk never reads a mozconfig.
        self.assertIn("evidence, not proof", _SKEPTIC["prompt"])

    def test_the_skeptic_still_has_the_tool_the_clause_names(self):
        """An instruction to use a tool the role was never granted reads as implemented and
        cannot fire. Pin the grant alongside the words."""
        self.assertIn("mcp__source__raw_file", _SKEPTIC["prompt"])
        self.assertIn("mcp__source__raw_file", _SKEPTIC["tools"])

    def test_the_clause_is_shorter_than_the_walk_it_replaced(self):
        # 279 words before, three gates after where there was one instrument. A regression
        # guard, not a style rule.
        self.assertLess(len(roles._COMPILED_OUT.split()), 279)


class TestWhatABuildFlagFailMayDo(unittest.TestCase):
    """Rule (1c) of `Dossier._skeptic_veto`: a `fail` whose stated GROUND is a configure-switch
    compile-flag claim no longer turns a lead into an abstain.

    WHY IT IS A CODE RULE AND NOT A PROMPT RULE. That `fail` is the one place where the cheapest
    model tier walks the longest multi-hop chain (symbol -> `#ifdef` -> `set_define` -> `option`)
    for the harshest consequence we have. An abstain files nothing, skips the second opinion
    (orchestrator.py:2268) and never reaches `Feedback`, so a wrong one is invisible forever --
    which is also why this rule was audited by REPLAY (0 of 216 stored binding vetoes change on
    the 1996-dossier dump) rather than by outcome.

    THE INVARIANT: such a `fail` costs a verdict its STRONG-EVIDENCE status, never its
    existence."""

    def test_an_ordinary_fail_still_abstains_a_lead(self):
        d = _lead_with(SkepticStatus.failed,
                       "the cited diff line 2165 is not present in changeset ff789e9f149e")
        self.assertEqual(d.verdict.decision, Decision.abstain)

    def test_a_configure_switch_fail_no_longer_abstains_a_lead(self):
        d = _lead_with(SkepticStatus.failed, _FLAG_NOTE)
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.corroborations["skeptic_build_flag_unbound"], ["mechanism"])

    def test_the_gtk_on_windows_veto_keeps_every_tooth(self):
        """THE COUNTER-EXAMPLE. Crash 560c0f2f-07cc-46c6-950c-1d8240260731 (Firefox nightly
        20260730132738, Windows NT 10.0.19044, `mozilla::FileBlockCache::Flush`): its only
        candidate ff789e9f149e touches 4 of 6 files under `widget/gtk/`, the skeptic failed it
        on exactly this note, and that fail was binding and right. 15 of the 39 build-guard
        fails and 3 of the 8 binding vetoes in the month look like this. The deterministic gate
        is designed never to see it -- `_option_is_default_off` returns False for all 20
        GUARD_DENY macros -- so if this predicate ate it, nothing would catch it."""
        d = _lead_with(SkepticStatus.failed, _GTK_NOTE)
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertIn("noise / unrelated", d.verdict.abstain_reason)

    def test_an_ordinary_fail_beside_a_configure_switch_fail_still_binds(self):
        # Per RESULT, not per dossier: the skeptic emits one entry per claim, so a genuine
        # contradiction keeps its teeth even when a sibling entry cites a configure switch.
        d = _dossier([
            SkepticResult(status=SkepticStatus.failed, claim_ref="mechanism", note=_FLAG_NOTE),
            SkepticResult(status=SkepticStatus.failed, claim_ref="edge0",
                          note="searchfox has no such edge; the caller does not exist"),
        ])
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertIn("edge0", d.verdict.abstain_reason)
        self.assertNotIn("mechanism", d.verdict.abstain_reason)

    def test_the_deterministic_gate_can_still_make_it_bind(self):
        # `corroborations["compiled_out_suppressed"]` is the escape hatch. Live it is never set
        # this early (`_apply_compiled_out_gate` runs later, in apply_deterministic_gates, and
        # abstains by itself); it earns its keep on a RE-VALIDATED stored payload.
        d = _lead_with(SkepticStatus.failed, _FLAG_NOTE,
                       corroborations={"compiled_out_suppressed": True})
        self.assertEqual(d.verdict.decision, Decision.abstain)

    def test_strong_evidence_still_falls_all_the_way_to_a_lead(self):
        d = _dossier([SkepticResult(status=SkepticStatus.failed, claim_ref="mechanism",
                                    note=_FLAG_NOTE)],
                     decision=Decision.strong_evidence)
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.verdict.confidence, Confidence.medium)

    def test_unverifiable_keeps_the_lead_which_is_how_2063782_went_out(self):
        d = _lead_with(SkepticStatus.unverifiable,
                       "whether this Nightly compiles JS_GC_CONCURRENT_MARKING "
                       "could not be confirmed")
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.verdict.confidence, Confidence.probable)

    def test_nothing_is_recorded_when_the_rule_changed_nothing(self):
        # The flag exists to make a rule whose failure mode is a FALSE ABSTAIN countable, so it
        # must mark the runs where it actually kept a lead alive and no others.
        d = _lead_with(SkepticStatus.unverifiable, _FLAG_NOTE)
        self.assertNotIn("skeptic_build_flag_unbound", d.corroborations)


if __name__ == "__main__":
    unittest.main()
