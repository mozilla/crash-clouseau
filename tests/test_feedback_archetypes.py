# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# The feedback loop: what became of a bug we filed (`models.Feedback`), and the reusable
# investigation rules that come out of it (`models.Archetype`).
#
# Bug 2062119. The pipeline filed a shutdown-phase null deref, named a changeset from
# 2022-12-13 and needinfo'd its author. Jens Stutte: "I do not think bug 1768581 is the
# regressor" -- then found the real origin (bug 1412726 converted `gJarHandler` to a
# StaticRefPtr cleared by ClearOnShutdown), wrote the patches, and suggested the rule:
# "maybe a general 'is a singleton involved that may not have a good/complete shutdown
# handling?'".
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_feedback_archetypes
import json
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import archetypes, models  # noqa: E402
from crashclouseau.agent import triage  # noqa: E402


# THE PANEL IS REAL PRODUCTION INPUT, and that is the whole point of it. A matcher is a pure
# function of the dict `orchestrator._matching_archetypes` builds, so the only honest back-test
# is that dict: signature, `_stack_text` of the first 40 frames of
# `inspector.thread_for_analysis`, `crash_info.type or reason`, `crash_info.address or address`,
# plus the two context fields added 2026-08-21. Captured from unauthenticated Socorro
# (https://crash-stats.mozilla.org/api/ProcessedCrash/?crash_id=<uuid>) into
# tests/archetypes/shutdown_singleton_panel.json, so it can be regenerated and re-measured.
#
# WHAT IT REPLACES, because that test was the trap rather than the safety net: the previous
# back-test asserted the row matched "AsyncShutdownTimeout | profile-change-teardown |
# LoginStore::shutdown" and "#3 ...QuotaManager::Shutdown | shutdownhang" -- SIGNATURE text fed
# into the `stack` field, which `_matching_archetypes` cannot produce. Those tokens occur 0
# times in the stack field over 1051 nightly reports and 101 times in the SIGNATURE field, which
# this row does not test. So the row's only green test was green on an impossible input, while
# the row itself fired on 23 real crashes of which 21 contradicted its own guidance.
_PANEL_PATH = os.path.join(os.path.dirname(__file__), "archetypes",
                           "shutdown_singleton_panel.json")

with open(_PANEL_PATH, encoding="utf-8") as _fh:
    PANEL = json.load(_fh)


def _facts(entry):
    """The dict `orchestrator._matching_archetypes` hands to `Archetype.for_crash`."""
    return {
        "signature": entry["signature"],
        "stack": "\n".join(entry["stack_lines"]),
        "crash_type": entry["crash_type"],
        "fault_address": entry["fault_address"],
        "shutdown_progress": entry["shutdown_progress"],
        "moz_crash_reason": entry["moz_crash_reason"],
    }


def _panel(prefix):
    return next(e for e in PANEL if e["uuid"].startswith(prefix))


# The real crash behind bug 2062119.
_JENS = _facts(_panel("413d6058"))


def _archetype(**over):
    spec = dict(archetypes._SHUTDOWN_SINGLETON)
    spec.update(over)
    return models.Archetype(slug=spec["slug"], title=spec["title"],
                            guidance=spec["guidance"], matcher=spec["matcher"],
                            enabled=True)


def _hang_row():
    spec = archetypes._SHUTDOWN_HANG
    return models.Archetype(slug=spec["slug"], title=spec["title"],
                            guidance=spec["guidance"], matcher=spec["matcher"], enabled=True)


class TestShutdownSingletonMatcher(unittest.TestCase):
    """The one archetype we ship, against real crashes in both directions."""

    def test_the_whole_panel_lands_where_it_should(self):
        row = _archetype()
        for entry in PANEL:
            with self.subTest(uuid=entry["uuid"]):
                self.assertIs(row.matches(_facts(entry)), entry["want"], entry["why"])

    def test_it_matches_the_crash_it_came_from(self):
        self.assertTrue(_archetype().matches(_JENS))

    def test_a_wild_pointer_is_not_this_archetype(self):
        # The rule's whole claim is "a small address during shutdown is a cleared global, not a
        # wild pointer". Above a page it has nothing to say.
        self.assertFalse(_archetype().matches({**_JENS, "fault_address": "0x00007f9c1a2b3c40"}))

    def test_a_null_deref_with_no_shutdown_on_the_stack_is_not_this_archetype(self):
        self.assertFalse(_archetype().matches(
            {**_JENS, "stack": "#0 mozilla::dom::Element::UnsetAttr dom/base/Element.cpp:4273"}))

    def test_signature_text_is_not_stack_text(self):
        # The three strings the deleted back-test fed into `stack`. They are SIGNATURE text and
        # `_matching_archetypes` puts `_stack_text` there and nothing else (measured 0/1051 in
        # the stack field). Pinned in the FALSE direction so the shortcut cannot come back.
        for bogus in ("#3 mozilla::dom::quota::QuotaManager::Shutdown | shutdownhang",
                      "AsyncShutdownTimeout | profile-change-teardown | LoginStore::shutdown",
                      "#7 mozJSModuleLoader::UnloadLoaders"):
            with self.subTest(stack=bogus):
                self.assertFalse(_archetype().matches({**_JENS, "stack": bogus}))

    def test_a_deliberate_abort_is_not_a_null_deref(self):
        # The defect this row shipped with: 21 of its 23 firings over 1051 nightly reports
        # carried a `moz_crash_reason`, so its opening sentence ("a GLOBAL/SINGLETON was read
        # after it was cleared") was false on 91% of the prompts it reached.
        for prefix in ("424b0ab0", "58ffaf90", "60681b60", "61228138"):
            with self.subTest(uuid=prefix):
                self.assertFalse(_archetype().matches(_facts(_panel(prefix))))
        # ...and it is the ABORT RECORD that discriminates, not the address and not the
        # shutdown tokens: clear the field on the same crash and the row fires again.
        self.assertTrue(_archetype().matches(
            {**_facts(_panel("58ffaf90")), "moz_crash_reason": None}))

    def test_a_crash_that_is_not_in_shutdown_at_all_is_not_this_archetype(self):
        # 61228138 is a MOZ_Crash during STARTUP (`nsXREDirProvider::DoStartup` ->
        # `ProfileStarted`) matched only because the crashing FUNCTION is named
        # `GetShutdownPhase`. Both new conditions reject it; pin the shutdown one alone.
        startup = {**_facts(_panel("61228138")), "moz_crash_reason": None}
        self.assertIsNone(startup["shutdown_progress"])
        self.assertFalse(_archetype().matches(startup))

    def test_a_genuine_read_at_exactly_zero_still_matches(self):
        # Against the obvious fix, a non-zero `min_fault_address` ("0x0 means nothing was
        # dereferenced"): 50 of the 96 nightly in-shutdown ACCESS_VIOLATION_READ crashes fault
        # at exactly 0x0, all with a recorded memory access and none with a moz_crash_reason,
        # so a floor deletes 40% of this row's correct firings. 032c9db1 is the mechanism
        # verbatim at 0x0 -- `URLQueryStringStripper::Shutdown()` <- the `GetSingleton` lambda
        # <- `mozilla::KillClearOnShutdown(mozilla::ShutdownPhase)`.
        for prefix in ("e23bec95", "032c9db1"):
            with self.subTest(uuid=prefix):
                facts = _facts(_panel(prefix))
                self.assertEqual(int(facts["fault_address"], 16), 0)
                self.assertTrue(_archetype().matches(facts))

    def test_the_mechanism_is_not_on_the_stack_of_the_crash_that_taught_us_it(self):
        # Against the second obvious fix ("shrink the alternation to the tokens that name the
        # mechanism"): bug 2062119's own crashes are the counter-example, because the singleton
        # is read through an inlined accessor. That predicate scores 1 firing per 1051 nightly
        # reports and 0 on the crash it was learned from.
        for prefix in ("413d6058", "b65b3c02"):
            stack = _facts(_panel(prefix))["stack"]
            for token in ("ClearOnShutdown", "StaticRefPtr"):
                with self.subTest(uuid=prefix, token=token):
                    self.assertNotIn(token, stack)

    def test_it_no_longer_fires_beside_the_shutdown_hang_row(self):
        # Both rows go into the SAME prompt. On a shutdownhang the sibling said "A shutdown
        # hang is not a fault: nothing crashed" while this one said a cleared global had been
        # read. Fixed here without touching the sibling, by `no_moz_crash_reason`. NOT because
        # "every shutdownhang carries a moz_crash_reason" -- measured over 3 months of nightly,
        # 116 of 3332 `^shutdownhang` reports do not. It holds because 0 of those 116 has BOTH
        # a small fault address and `shutdown_progress` set, which this row also requires, so
        # the double-fire is gone at 0 per 3 months rather than by an absolute rule.
        hang, singleton = _hang_row(), _archetype()
        hangs = [e for e in PANEL if hang.matches(_facts(e))]
        self.assertTrue(hangs, "the panel must contain a shutdown hang")
        for entry in hangs:
            with self.subTest(uuid=entry["uuid"]):
                self.assertFalse(singleton.matches(_facts(entry)))

    def test_the_dead_branches_are_gone_from_the_alternation(self):
        # Each measured 0 hits in the STACK field over 1051 nightly reports; `XPCOMShutdown`
        # never could match, the symbol is `ShutdownXPCOM`. Three of them are SIGNATURE tokens
        # (101 hits there) and relocating them to a `signature` key would re-admit exactly the
        # 18 deliberate-abort firings this fix removes, so the absence of that key is pinned
        # too.
        alternation = " ".join(archetypes._SHUTDOWN_SINGLETON["matcher"]["stack"])
        for dead in ("XPCOMShutdown", "::Teardown", "UnloadLoaders", "AsyncShutdownTimeout",
                     "shutdownhang", "profile-change-teardown"):
            with self.subTest(dead=dead):
                self.assertNotIn(dead, alternation)
        self.assertNotIn("signature", archetypes._SHUTDOWN_SINGLETON["matcher"])

    def test_an_unparseable_fault_address_does_not_satisfy_the_bound(self):
        # An unknown must never satisfy a condition -- otherwise the archetype fires on every
        # crash whose address Socorro did not give us.
        for addr in (None, "", "not-an-address"):
            with self.subTest(addr=addr):
                self.assertFalse(_archetype().matches({**_JENS, "fault_address": addr}))

    def test_an_unset_shutdown_phase_does_not_satisfy_its_condition(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertFalse(_archetype().matches({**_JENS, "shutdown_progress": value}))
        self.assertFalse(_archetype().matches(
            {k: v for k, v in _JENS.items() if k != "shutdown_progress"}))

    def test_a_caller_that_never_looked_up_moz_crash_reason_does_not_satisfy_it(self):
        # The subtle half. An ABSENT key is not "there was no abort", it is "nobody checked",
        # and reading it as the former would silently restore the old behaviour for every
        # caller and fixture written before the field existed.
        blind = {k: v for k, v in _JENS.items() if k != "moz_crash_reason"}
        self.assertFalse(_archetype().matches(blind))
        for empty in (None, "", "  "):
            with self.subTest(value=empty):
                self.assertTrue(_archetype().matches({**blind, "moz_crash_reason": empty}))
        self.assertFalse(_archetype().matches({**blind, "moz_crash_reason": "MOZ_CRASH(oops)"}))

    def test_an_unspecified_key_is_not_a_constraint(self):
        row = models.Archetype(slug="s", title="t", guidance="g",
                               matcher={"signature": ["Foo::bar"]})
        self.assertTrue(row.matches({"signature": "mozilla::Foo::bar", "stack": "anything"}))
        self.assertFalse(row.matches({"signature": "Other::thing"}))
        # Including the two keys added 2026-08-21: a row that does not name them must behave
        # exactly as it did, whatever the crash's shutdown_progress / moz_crash_reason say.
        self.assertTrue(row.matches({"signature": "mozilla::Foo::bar",
                                     "moz_crash_reason": "MOZ_CRASH(x)"}))

    def test_an_empty_matcher_matches_everything(self):
        # Deliberate: it is how you write a rule that always applies. Worth pinning so nobody
        # "fixes" it into matching nothing, which would silently disable such a row.
        self.assertTrue(models.Archetype(slug="s", title="t", guidance="g",
                                         matcher={}).matches(_JENS))

    def test_a_broken_pattern_disables_its_own_row_rather_than_breaking_a_run(self):
        row = models.Archetype(slug="s", title="t", guidance="g",
                               matcher={"signature": ["*unclosed(["]})
        self.assertFalse(row.matches(_JENS))

    def test_an_absurd_pattern_is_refused(self):
        # No timeout is available inside `re`, so length is the only cheap guard against a
        # pathological pattern hanging a worker.
        row = models.Archetype(slug="s", title="t", guidance="g",
                               matcher={"signature": ["(a+)+" * 100]})
        self.assertFalse(row.matches({"signature": "a" * 50}))


class TestTheFactsTheMatcherIsGiven(unittest.TestCase):
    """`models.Archetype.matches` is a pure function of one dict, so a fact that is not in it
    cannot be a condition. Two were missing and they were the two that decide whether a small
    fault address during shutdown means anything at all."""

    @staticmethod
    def _facts_built_for(raw_crash):
        from crashclouseau.agent import orchestrator

        seen = {}
        with mock.patch.object(models.Archetype, "for_crash",
                               side_effect=lambda f: seen.update(f) or []):
            orchestrator._matching_archetypes({"signature": "S"}, "#0 f", raw_crash)
        return seen

    def test_the_shutdown_phase_and_the_abort_record_reach_the_matcher(self):
        facts = self._facts_built_for({
            "shutdown_progress": "XPCOMShutdownFinal",
            "moz_crash_reason": "Shutdown hanging at step AppShutdownQM.",
            "json_dump": {"crash_info": {"type": "SIGSEGV", "address": "0x28"}},
        })
        self.assertEqual(facts["shutdown_progress"], "XPCOMShutdownFinal")
        self.assertEqual(facts["moz_crash_reason"], "Shutdown hanging at step AppShutdownQM.")
        self.assertEqual(facts["fault_address"], "0x28")

    def test_moz_crash_reason_is_read_the_way_the_prompt_reads_it(self):
        # triage.py's MOZ_CRASH_REASON line falls back to `json_dump`; a matcher reading only
        # the top level would contradict the fact printed into the same prompt.
        facts = self._facts_built_for(
            {"json_dump": {"moz_crash_reason": "MOZ_CRASH(nope)", "crash_info": {}}})
        self.assertEqual(facts["moz_crash_reason"], "MOZ_CRASH(nope)")

    def test_both_keys_are_present_even_when_the_crash_has_neither(self):
        # `no_moz_crash_reason` refuses an ABSENT key, so the caller must always put one there
        # or the row silently stops firing on the crashes it exists for.
        facts = self._facts_built_for({"json_dump": {"crash_info": {}}})
        self.assertIn("moz_crash_reason", facts)
        self.assertIn("shutdown_progress", facts)
        self.assertIsNone(facts["moz_crash_reason"])

    def test_a_missing_processed_crash_satisfies_neither_condition(self):
        # `build_seed` carries on when the ProcessedCrash fetch fails (raw_crash=None). An
        # unknown must not satisfy a condition, so the row simply does not fire.
        facts = self._facts_built_for(None)
        self.assertIsNone(facts["shutdown_progress"])
        self.assertIsNone(facts["moz_crash_reason"])
        self.assertFalse(_archetype().matches({**_JENS, "shutdown_progress": None}))


class TestGuidanceReachesTheAgent(unittest.TestCase):
    def test_the_hint_is_in_the_prompt_and_labelled_as_a_prior(self):
        crash = {"uuid": "u-1", "signature": "S", "channel": "nightly", "stack": "#0 f",
                 "archetypes": [{"slug": "shutdown-singleton", "title": "Shutdown singleton",
                                 "guidance": "check the ClearOnShutdown conversion"}]}
        lines = triage._archetype_lines(crash)
        text = "\n".join(lines)
        self.assertIn("KNOWN ARCHETYPES", text)
        self.assertIn("check the ClearOnShutdown conversion", text)
        # It must never read as a finding: these rows are added without the review a patch gets,
        # so the grounding rule has to keep doing the work.
        self.assertIn("PRIOR TO TEST, not a finding", text)

    def test_no_matching_archetype_adds_nothing(self):
        for crash in ({}, {"archetypes": []}, {"archetypes": [{"slug": "x"}]},
                      {"archetypes": ["junk"]}):
            with self.subTest(crash=crash):
                self.assertEqual(triage._archetype_lines(crash), [])

    def test_the_second_opinion_is_NOT_given_the_hints(self):
        """FACTS are shared with the blind reviewer; HYPOTHESES are not.

        The opposite of the bit-flip fix, and deliberately. There, both models were blind to
        something TRUE about the crash and the fix was to tell both -- a fact constrains two
        analyses toward the same right answer. An archetype is a suggested direction, and
        priming the independent reviewer with the same prior the first analysis used correlates
        their mistakes: it would agree because it was pointed the same way, and the SO's whole
        measured value (it refutes 74% of leads, specificity 1.00) is that it was not.
        """
        from crashclouseau.agent import second_opinion

        crash = {"signature": "S", "channel": "nightly", "stack": "#0 f",
                 "archetypes": [{"slug": "a", "title": "T", "guidance": "look at G"}]}
        self.assertNotIn("look at G", second_opinion._user_prompt(crash, {"node": "n"}))
        # ...while a FACT about the crash still reaches it.
        facts = {"json_dump": {"crash_info": {"instruction": "mov rax, [rax + 0xd0]"}}}
        self.assertIn("mov rax, [rax + 0xd0]",
                      second_opinion._user_prompt({**crash, "raw_crash": facts},
                                                  {"node": "n"}))


class TestAttributionClassifier(unittest.TestCase):
    """The verdict on our verdict."""

    def test_the_2062119_case_is_wrong_attribution_not_a_bad_bug(self):
        # A real crash we were useful about, with the wrong changeset named -- the outcome the
        # worth-investigating pivot says is acceptable. Collapsing it into "wrong" would make
        # the pipeline look worse than it is; into "correct" would hide a real problem.
        self.assertEqual(
            models.Feedback.classify(None, named_bug=1768581, regressed_by=[1412726]), "wrong")

    def test_a_confirmed_attribution(self):
        self.assertEqual(
            models.Feedback.classify(None, named_bug=1412726, regressed_by=[1412726]),
            "correct")
        self.assertEqual(
            models.Feedback.classify("FIXED", named_bug=7, regressed_by=[3, 7]), "correct")

    def test_invalid_beats_everything(self):
        # Bug 2061961: a hardware bit flip. There was no bug, so attribution is not the story.
        for res in ("INVALID", "WORKSFORME", "INCOMPLETE", "invalid"):
            with self.subTest(res=res):
                self.assertEqual(
                    models.Feedback.classify(res, named_bug=1, regressed_by=[2]),
                    "crash_invalid")

    def test_silence_is_not_agreement(self):
        # Nobody setting regressed_by is the common case and it means nothing either way.
        for rb in (None, [], ["junk"]):
            with self.subTest(rb=rb):
                self.assertEqual(models.Feedback.classify(None, 1768581, rb), "unknown")
        self.assertEqual(models.Feedback.classify("FIXED", None, [1412726]), "wrong")

    def test_our_own_regressed_by_is_not_a_verdict_on_itself(self):
        # The filer now SETS regressed_by. Scoring the field we wrote as "correct" would have
        # every archetype reading 100% from the day it first fired.
        self.assertEqual(
            models.Feedback.classify(None, named_bug=7, regressed_by=[7], claimed=[7]),
            "unconfirmed")
        # Same field, put there by somebody else -> a real endorsement.
        self.assertEqual(
            models.Feedback.classify(None, named_bug=7, regressed_by=[7], claimed=[]),
            "correct")
        self.assertEqual(
            models.Feedback.classify(None, named_bug=7, regressed_by=[7], claimed=None),
            "correct")

    def test_a_reviewer_who_replaces_ours_still_counts_as_wrong(self):
        # The half of the loop that improves anything: writing the field invites the correction
        # that prose in a comment can absorb silently. 2062119 with the claim now made by us.
        self.assertEqual(
            models.Feedback.classify(None, named_bug=1768581, regressed_by=[1412726],
                                     claimed=[1768581]),
            "wrong")

    def test_a_second_regressor_added_beside_ours_is_still_unconfirmed(self):
        # Somebody adding another cause did not necessarily look at ours; the module's rule is
        # that silence is not agreement, and leaving it in ours is silence.
        self.assertEqual(
            models.Feedback.classify(None, named_bug=7, regressed_by=[7, 9], claimed=[7]),
            "unconfirmed")

    def test_a_field_nobody_wrote_a_word_about_is_not_an_endorsement(self):
        # Bugs 2061969 and 2061691. dmeehan (release management) set `regressed_by` in a batch on
        # 2026-08-17, answering BugBot's "could you fill (if possible) the regressed_by field?" by
        # copying the value out of OUR comment. No human but the filer has ever written on either
        # bug. 10 of the 15 non-filer settings across the 52 filings are that one account, so the
        # loop was reading most of its wins off its own prose.
        self.assertEqual(
            models.Feedback.classify(None, named_bug=1998600, regressed_by=[1998600],
                                     claimed=[], independent_comment=False),
            "unconfirmed")

    def test_an_adjudicated_bug_still_scores_correct(self):
        # Bug 2062806, the wrong-direction case: ryanvm set the field and never commented, while
        # hzhao ("Confirming the mechanism - this is correct") had already landed the backout. The
        # question is about the BUG, not about who touched the field.
        self.assertEqual(
            models.Feedback.classify("FIXED", named_bug=2057317, regressed_by=[2057317],
                                     claimed=[], independent_comment=True),
            "correct")
        # ...and an UNCHECKED row (None) is scored exactly as it was before this existed, so no
        # historical row is relabelled by a caller that never looked.
        self.assertEqual(
            models.Feedback.classify("FIXED", named_bug=2057317, regressed_by=[2057317],
                                     claimed=[], independent_comment=None),
            "correct")

    def test_there_is_no_length_floor_on_an_adjudication(self):
        # Bug 2063862's only independent comment is 42 characters -- "This will be fixed by
        # backout bug 2059597." -- and a threshold tuned to exclude it would be fit on the single
        # case it was invented for. The predicate is "somebody wrote", not "somebody wrote a lot".
        self.assertEqual(
            models.Feedback.classify("FIXED", named_bug=2059597, regressed_by=[2059597],
                                     claimed=[], independent_comment=True),
            "correct")

    def test_the_check_cannot_rescue_a_wrong_attribution_or_soften_our_own_write(self):
        # 2062119 (jstutte replaced ours) stays `wrong` however lively the bug is, and a field we
        # set ourselves stays `unconfirmed` however lively it is.
        self.assertEqual(
            models.Feedback.classify(None, named_bug=1768581, regressed_by=[1412726],
                                     claimed=[1768581], independent_comment=True),
            "wrong")
        self.assertEqual(
            models.Feedback.classify(None, named_bug=7, regressed_by=[7], claimed=[7],
                                     independent_comment=True),
            "unconfirmed")

    def test_an_llm_agent_is_not_an_independent_reviewer(self):
        # Why `feedback._independent_reviewers` reuses `ReviewNote.classify_author` rather than
        # `experts._is_bot`: the bot-marker list scores firefoxmanagerdev@gmail.com HUMAN, and
        # that account is an LLM whose three appearances in the panel (bugs 2060922, 2061973,
        # 2062335) all REFUTE our attribution. Counting a refutation as an endorsement is the failure the
        # check exists to stop. `hackbot@mozilla.tld` -- 2560 characters of our own analysis on
        # bug 2061691 -- is the same shape, and happens to be caught by both.
        from crashclouseau.agent.experts import _is_bot

        self.assertEqual(
            models.ReviewNote.classify_author("firefoxmanagerdev@gmail.com", "not right"),
            "agent")
        self.assertFalse(_is_bot("firefoxmanagerdev@gmail.com", "", ""))
        self.assertEqual(
            models.ReviewNote.classify_author("hackbot@mozilla.tld", "Confirmed"), "agent")
        # ...and a real reviewer stays a real reviewer, including the @gmail.com ones.
        for email in ("hzhao@mozilla.com", "kershaw@mozilla.com", "ryanvm@gmail.com"):
            with self.subTest(email=email):
                self.assertEqual(
                    models.ReviewNote.classify_author(email, "Confirming the mechanism"),
                    "human")


class TestSeeding(unittest.TestCase):
    def test_the_shipped_archetype_names_the_bug_that_taught_it(self):
        # A row is never anonymous folklore.
        self.assertEqual(archetypes._SHUTDOWN_SINGLETON["source_bug"], 2062119)
        guidance = archetypes._SHUTDOWN_SINGLETON["guidance"]
        # The two things Jens actually established, and the reason the pipeline missed them.
        self.assertIn("ClearOnShutdown", guidance)
        self.assertIn("will NOT be in this build's pushlog window", guidance)
        self.assertIn("2062119", guidance)

    def test_the_guidance_opens_with_a_condition_rather_than_an_assertion(self):
        # It used to open "A small fault address during shutdown usually means a
        # GLOBAL/SINGLETON was read after it was cleared" -- an assertion, false on 21 of the
        # 23 crashes it reached, because those had a MOZ_CRASH_REASON and had dereferenced
        # nothing. The matcher now excludes them; the text still has to say how to tell, since
        # rows are DB-editable and a relaxed matcher must not silently revive the claim.
        guidance = archetypes._SHUTDOWN_SINGLETON["guidance"]
        self.assertIn("MOZ_CRASH_REASON", guidance)
        self.assertIn("this row does not apply", guidance)

    def test_leaving_the_pushlog_window_is_gated_on_finding_the_declaration(self):
        # "the origin will NOT be in this build's pushlog window" is the most expensive thing
        # this row can ask for, and a searchfox hit on the `StaticRefPtr`/`ClearOnShutdown`
        # declaration is its only evidence. It was unconditional, on 21 crashes a week where no
        # pointer had been read at all.
        guidance = archetypes._SHUTDOWN_SINGLETON["guidance"]
        self.assertLess(guidance.index("SEARCHFOX THE DECLARATION"),
                        guidance.index("will NOT be in this build's pushlog window"))
        self.assertIn("If you did NOT find such a declaration", guidance)

    def test_seeding_never_clobbers_an_edited_row(self):
        # The table is DB-editable on purpose; a deploy reverting tuned text (or re-enabling a
        # row somebody turned off after it misfired) would make it untrustworthy.
        existing = mock.Mock(enabled=False)
        query = mock.Mock()
        query.filter.return_value.one_or_none.return_value = existing
        with mock.patch.object(models.db, "session", mock.Mock(query=mock.Mock(return_value=query))), \
             mock.patch.object(models.Archetype, "upsert") as upsert:
            self.assertEqual(archetypes.seed(), [])
            upsert.assert_not_called()

    def test_overwrite_restores_the_shipped_text_but_keeps_the_switch(self):
        existing = mock.Mock(enabled=False)
        query = mock.Mock()
        query.filter.return_value.one_or_none.return_value = existing
        with mock.patch.object(models.db, "session", mock.Mock(query=mock.Mock(return_value=query))), \
             mock.patch.object(models.Archetype, "upsert") as upsert:
            # Every shipped row, not a frozen literal: adding one must not need this edit.
            self.assertEqual(archetypes.seed(overwrite=True),
                             [s["slug"] for s in archetypes.SEED_ARCHETYPES])
        self.assertIs(upsert.call_args.kwargs["enabled"], False)

    def test_seed_quietly_never_raises(self):
        with mock.patch.object(archetypes, "seed", side_effect=RuntimeError("no table")), \
             mock.patch.object(models.db, "session", mock.Mock()):
            self.assertEqual(archetypes.seed_quietly(), [])

    def test_the_deploy_path_seeds_them(self):
        """The whole feature was dead in prod for want of this call.

        `seed_quietly()` was reachable only from bin/init.py — the docker-compose entrypoint —
        so no Heroku release ever wrote the rows: measured 2026-08-12, prod's `archetypes` table
        held 0 rows and every dossier recorded `"archetypes": []`. Nothing failed, nothing
        logged, and Jens Stutte's rule from bug 2062119 had never been in front of a single
        production run. Read as source rather than executed because bin/release.py is a script
        that talks to the database at import time."""
        import ast
        import os

        path = os.path.join(os.path.dirname(__file__), "..", "bin", "release.py")
        with open(path) as f:
            src = f.read()
        calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)]

        def names(call):
            f = call.func
            return ast.dump(f)

        self.assertTrue(
            any("seed_quietly" in names(c) for c in calls),
            "bin/release.py must seed archetypes, or the table stays empty in prod forever",
        )
        # AFTER models.create(): `archetypes` is a post-deploy table (models._ADDED_TABLES), so
        # seeding first would write to a relation that does not exist yet on a long-lived DB —
        # swallowed by seed_quietly into a warning, i.e. straight back to an empty table.
        self.assertLess(
            src.index("models.create()"), src.index("seed_quietly"),
            "seed archetypes AFTER models.create(), which is what creates the table",
        )


class TestSeedReachesADatabaseThatAlreadyHasTheRow(unittest.TestCase):
    """A shipped fix to a ROW has to be able to reach prod, and until 2026-08-21 none could.

    `seed(overwrite=False)` skipped on SLUG alone, so a database that already held the row kept
    the text it was first seeded with for ever -- and no page renders this table, so the
    divergence from archetypes.py was invisible too. The obvious fix, `overwrite=True`, is
    wrong in the measured direction: it reverts a `guidance` an operator tuned after a misfire
    and re-seeds a row somebody switched off, which is exactly what seed() promises not to do.
    So a row is replaced only when its stored text fingerprints to a version this file has
    SUPERSEDED -- i.e. only when nobody has touched it."""

    def _seed(self, row, superseded):
        query = mock.Mock()
        query.filter.return_value.one_or_none.return_value = row
        with mock.patch.dict(archetypes._SUPERSEDED, superseded, clear=True), \
             mock.patch.object(models.db, "session",
                               mock.Mock(query=mock.Mock(return_value=query))), \
             mock.patch.object(models.Archetype, "upsert") as upsert:
            return archetypes.seed(), upsert

    def test_an_untouched_older_seed_is_upgraded(self):
        row = mock.Mock(enabled=True, guidance="old text", matcher={"stack": ["Old"]})
        written, upsert = self._seed(
            row, {"shutdown-singleton":
                  frozenset({archetypes._fingerprint("old text", {"stack": ["Old"]})})})
        self.assertEqual(written, ["shutdown-singleton"])
        self.assertEqual(upsert.call_args.kwargs["matcher"],
                         archetypes._SHUTDOWN_SINGLETON["matcher"])

    def test_a_hand_edited_row_is_still_never_touched(self):
        row = mock.Mock(enabled=True, guidance="an operator tuned this",
                        matcher={"stack": ["Old"]})
        written, upsert = self._seed(
            row, {"shutdown-singleton":
                  frozenset({archetypes._fingerprint("old text", {"stack": ["Old"]})})})
        self.assertEqual(written, [])
        upsert.assert_not_called()

    def test_the_text_shipping_today_is_not_listed_as_superseded(self):
        # Otherwise a release rewrites a row that is already correct, and an operator's edit
        # gets reverted on the next deploy instead of being kept.
        for spec in archetypes.SEED_ARCHETYPES:
            with self.subTest(slug=spec["slug"]):
                self.assertNotIn(archetypes._fingerprint(spec["guidance"], spec["matcher"]),
                                 archetypes._SUPERSEDED.get(spec["slug"], ()))

    def test_only_the_text_prod_can_actually_hold_is_listed(self):
        # `shutdown-singleton` was seeded by 312e153 and unchanged until this fix, so exactly
        # one stored text is known not to be somebody's edit. `shutdown-hang` is unchanged and
        # lists nothing, which is what keeps this from becoming a blanket overwrite.
        self.assertEqual(len(archetypes._SUPERSEDED["shutdown-singleton"]), 1)
        self.assertNotIn("shutdown-hang", archetypes._SUPERSEDED)

    def test_a_row_that_will_not_serialise_is_left_alone_not_raised_on(self):
        # bin/release.py runs this on every deploy; a weird row must not fail a release.
        self.assertEqual(archetypes._fingerprint("g", {"bad": object()}), "")
        row = mock.Mock(enabled=True, guidance="g", matcher={"bad": object()})
        written, upsert = self._seed(
            row, {"shutdown-singleton": frozenset({archetypes._fingerprint("g", {})})})
        self.assertEqual(written, [])
        upsert.assert_not_called()


class TestScoreboard(unittest.TestCase):
    def test_it_tallies_per_archetype(self):
        rows = [
            mock.Mock(attribution="wrong", archetypes=["shutdown-singleton"]),
            mock.Mock(attribution="correct", archetypes=["shutdown-singleton"]),
            mock.Mock(attribution="crash_invalid", archetypes=[]),
            mock.Mock(attribution="unknown", archetypes=["shutdown-singleton"]),
            # A row whose regressed_by is only OUR claim. It must show up under its own name
            # rather than vanishing from the archetype's tally.
            mock.Mock(attribution="unconfirmed", archetypes=["shutdown-singleton"]),
        ]
        query = mock.Mock()
        query.all.return_value = rows
        with mock.patch.object(models.db, "session", mock.Mock(query=mock.Mock(return_value=query))):
            board = models.Feedback.scoreboard()
        self.assertEqual(board["total"], 5)
        self.assertEqual(board["by_attribution"]["wrong"], 1)
        self.assertEqual(board["by_archetype"]["shutdown-singleton"],
                         {"filed": 4, "correct": 1, "wrong": 1, "unconfirmed": 1,
                          "crash_invalid": 0})


_EMPTY_BOARD = {"total": 0, "by_attribution": {}, "by_archetype": {}}


class TestRefresh(unittest.TestCase):
    def test_an_unreadable_bug_is_left_alone_not_blanked(self):
        # A restricted bug is ABSENT from BMO's query form rather than an error. Wiping the
        # verdict we already recorded about it would be the worst reading of that silence.
        from crashclouseau import feedback as fb

        filed = [{"bug_id": 1, "uuid": "u-1", "named_bug": 7, "named_node": "n",
                  "archetypes": [], "claimed": [], "filed_at": "2026-08-10T00:00:00+00:00"},
                 {"bug_id": 2, "uuid": "u-2", "named_bug": 8, "named_node": "m",
                  "archetypes": [], "claimed": [], "filed_at": None}]
        with mock.patch.object(fb, "_filed_bugs", return_value=filed), \
             mock.patch.object(fb, "_fetch", return_value={
                 1: {"id": 1, "status": "RESOLVED", "resolution": "INVALID",
                     "regressed_by": []}}), \
             mock.patch.object(models.Feedback, "record") as record, \
             mock.patch.object(models.Feedback, "scoreboard", return_value=_EMPTY_BOARD):
            summary = fb.refresh()
        self.assertEqual(summary["updated"], 1)
        self.assertEqual(record.call_args.args[0], 1)
        self.assertEqual(record.call_args.kwargs["resolution"], "INVALID")

    def test_the_archetypes_that_fired_are_carried_onto_the_outcome(self):
        # This join is the entire reason a learned rule can ever be scored.
        from crashclouseau import feedback as fb

        filed = [{"bug_id": 9, "uuid": "u", "named_bug": 1, "named_node": "n",
                  "archetypes": ["shutdown-singleton"], "claimed": [], "filed_at": None}]
        with mock.patch.object(fb, "_filed_bugs", return_value=filed), \
             mock.patch.object(fb, "_fetch", return_value={9: {"id": 9, "status": "NEW"}}), \
             mock.patch.object(models.Feedback, "record") as record, \
             mock.patch.object(models.Feedback, "scoreboard", return_value=_EMPTY_BOARD):
            fb.refresh()
        self.assertEqual(record.call_args.kwargs["archetypes"], ["shutdown-singleton"])

    def test_what_the_filer_claimed_reaches_the_classifier(self):
        # Without this the loop scores our own regressed_by write as a reviewer agreeing with us.
        from crashclouseau import feedback as fb

        filed = [{"bug_id": 9, "uuid": "u", "named_bug": 42, "named_node": "n",
                  "archetypes": [], "claimed": [42], "filed_at": None}]
        with mock.patch.object(fb, "_filed_bugs", return_value=filed), \
             mock.patch.object(fb, "_fetch", return_value={
                 9: {"id": 9, "status": "NEW", "regressed_by": [42]}}), \
             mock.patch.object(models.Feedback, "record") as record, \
             mock.patch.object(models.Feedback, "scoreboard", return_value=_EMPTY_BOARD):
            fb.refresh()
        self.assertEqual(record.call_args.kwargs["claimed_regressed_by"], [42])

    def test_the_independence_question_is_asked_only_where_it_can_change_the_answer(self):
        # `correct` is the only verdict it can move, so bug 2 (a value WE set -> `unconfirmed`)
        # and bug 3 (nobody set anything -> `unknown`) are never asked about. 14 of the 52
        # filings on the panel are eligible; 2 move.
        from crashclouseau import feedback as fb

        filed = [{"bug_id": 1, "uuid": "u1", "named_bug": 7, "named_node": "n",
                  "archetypes": [], "claimed": [], "filed_at": None},
                 {"bug_id": 2, "uuid": "u2", "named_bug": 7, "named_node": "n",
                  "archetypes": [], "claimed": [7], "filed_at": None},
                 {"bug_id": 3, "uuid": "u3", "named_bug": 7, "named_node": "n",
                  "archetypes": [], "claimed": [], "filed_at": None}]
        fetched = {1: {"id": 1, "status": "NEW", "regressed_by": [7],
                       "creator": "cdenizet@mozilla.com"},
                   2: {"id": 2, "status": "NEW", "regressed_by": [7],
                       "creator": "cdenizet@mozilla.com"},
                   3: {"id": 3, "status": "NEW", "regressed_by": []}}
        with mock.patch.object(fb, "_filed_bugs", return_value=filed), \
             mock.patch.object(fb, "_fetch", return_value=fetched), \
             mock.patch.object(fb, "_independent_reviewers",
                               return_value={1: False}) as indep, \
             mock.patch.object(models.Feedback, "record") as record, \
             mock.patch.object(models.Feedback, "scoreboard", return_value=_EMPTY_BOARD):
            fb.refresh()
        indep.assert_called_once_with({1: "cdenizet@mozilla.com"})
        self.assertEqual([c.kwargs["independent_comment"] for c in record.call_args_list],
                         [False, None, None])

    def test_an_unswept_bug_is_unchecked_rather_than_unendorsed(self):
        # `_ingest_notes` runs AFTER the verdicts, so a bug's first scan and its first
        # independence answer are one tick apart -- and on a database with no note corpus the
        # answer is absent for everything. Absent must read as "nobody looked", never as "nobody
        # wrote", or the first run after deploy relabels every win as unconfirmed.
        from crashclouseau import feedback as fb

        self.assertEqual(fb._independent_reviewers({}), {})
        with mock.patch.object(models, "db") as db:
            db.session.query.side_effect = RuntimeError("no such table")
            self.assertEqual(fb._independent_reviewers({1: "cdenizet@mozilla.com"}), {})

    def test_the_claim_is_read_off_the_filing_record(self):
        # `filed_bug["regressed_by"]` is what `bugzilla_apply` recorded as actually linked.
        from crashclouseau import feedback as fb

        rows = [{"uuid": "u-1",
                 "filed_bug": {"bug": 9, "filed": True, "regressed_by": [42], "at": None},
                 "dossier": {"candidate": {"bug": 42, "node": "n"}}},
                # An older filing, from before the filer set the field at all.
                {"uuid": "u-2",
                 "filed_bug": {"bug": 8, "filed": True, "at": None},
                 "dossier": {"candidate": {"bug": 7, "node": "m"}}}]
        with mock.patch.object(models.Dossier, "filed_bug_rows", return_value=rows):
            got = fb._filed_bugs()
        self.assertEqual([e["claimed"] for e in got], [[42], []])


if __name__ == "__main__":
    unittest.main()
