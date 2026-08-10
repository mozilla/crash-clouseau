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
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import archetypes, models  # noqa: E402
from crashclouseau.agent import triage  # noqa: E402


# The real crash behind bug 2062119.
_JENS = {
    "signature": "nsJARProtocolHandler::MimeService",
    "stack": ("#0 nsJARChannel::GetContentTypeGuess modules/libjar/nsJARChannel.cpp:765\n"
              "#19 mozilla::AppShutdown::AdvanceShutdownPhaseInternal "
              "xpcom/base/AppShutdown.cpp:427"),
    "crash_type": "EXCEPTION_ACCESS_VIOLATION_READ",
    "fault_address": "0x0000000000000028",
}


def _archetype(**over):
    spec = dict(archetypes._SHUTDOWN_SINGLETON)
    spec.update(over)
    return models.Archetype(slug=spec["slug"], title=spec["title"],
                            guidance=spec["guidance"], matcher=spec["matcher"],
                            enabled=True)


class TestShutdownSingletonMatcher(unittest.TestCase):
    """The one archetype we ship, against the crash that taught it to us."""

    def test_it_matches_the_crash_it_came_from(self):
        self.assertTrue(_archetype().matches(_JENS))

    def test_a_wild_pointer_is_not_this_archetype(self):
        # The rule's whole claim is "a small address during shutdown is a cleared global, not a
        # wild pointer". Above a page it has nothing to say.
        self.assertFalse(_archetype().matches({**_JENS, "fault_address": "0x00007f9c1a2b3c40"}))

    def test_a_null_deref_with_no_shutdown_on_the_stack_is_not_this_archetype(self):
        self.assertFalse(_archetype().matches(
            {**_JENS, "stack": "#0 mozilla::dom::Element::UnsetAttr dom/base/Element.cpp:4273"}))

    def test_the_other_shutdown_crashes_in_the_corpus_match(self):
        # 4 of the canary's first 20 filings carry shutdown machinery; this is not a one-off.
        for stack in ("#3 mozilla::dom::quota::QuotaManager::Shutdown | shutdownhang",
                      "AsyncShutdownTimeout | profile-change-teardown | LoginStore::shutdown",
                      "#7 mozJSModuleLoader::UnloadLoaders"):
            with self.subTest(stack=stack):
                self.assertTrue(_archetype().matches({**_JENS, "stack": stack}))

    def test_an_unparseable_fault_address_does_not_satisfy_the_bound(self):
        # An unknown must never satisfy a condition -- otherwise the archetype fires on every
        # crash whose address Socorro did not give us.
        for addr in (None, "", "not-an-address"):
            with self.subTest(addr=addr):
                self.assertFalse(_archetype().matches({**_JENS, "fault_address": addr}))

    def test_an_unspecified_key_is_not_a_constraint(self):
        row = models.Archetype(slug="s", title="t", guidance="g",
                               matcher={"signature": ["Foo::bar"]})
        self.assertTrue(row.matches({"signature": "mozilla::Foo::bar", "stack": "anything"}))
        self.assertFalse(row.matches({"signature": "Other::thing"}))

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


class TestSeeding(unittest.TestCase):
    def test_the_shipped_archetype_names_the_bug_that_taught_it(self):
        # A row is never anonymous folklore.
        self.assertEqual(archetypes._SHUTDOWN_SINGLETON["source_bug"], 2062119)
        guidance = archetypes._SHUTDOWN_SINGLETON["guidance"]
        # The two things Jens actually established, and the reason the pipeline missed them.
        self.assertIn("ClearOnShutdown", guidance)
        self.assertIn("will NOT be in this build's pushlog window", guidance)
        self.assertIn("2062119", guidance)

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
            self.assertEqual(archetypes.seed(overwrite=True), ["shutdown-singleton"])
        self.assertIs(upsert.call_args.kwargs["enabled"], False)

    def test_seed_quietly_never_raises(self):
        with mock.patch.object(archetypes, "seed", side_effect=RuntimeError("no table")), \
             mock.patch.object(models.db, "session", mock.Mock()):
            self.assertEqual(archetypes.seed_quietly(), [])


class TestScoreboard(unittest.TestCase):
    def test_it_tallies_per_archetype(self):
        rows = [
            mock.Mock(attribution="wrong", archetypes=["shutdown-singleton"]),
            mock.Mock(attribution="correct", archetypes=["shutdown-singleton"]),
            mock.Mock(attribution="crash_invalid", archetypes=[]),
            mock.Mock(attribution="unknown", archetypes=["shutdown-singleton"]),
        ]
        query = mock.Mock()
        query.all.return_value = rows
        with mock.patch.object(models.db, "session", mock.Mock(query=mock.Mock(return_value=query))):
            board = models.Feedback.scoreboard()
        self.assertEqual(board["total"], 4)
        self.assertEqual(board["by_attribution"]["wrong"], 1)
        self.assertEqual(board["by_archetype"]["shutdown-singleton"],
                         {"filed": 3, "correct": 1, "wrong": 1, "crash_invalid": 0})


_EMPTY_BOARD = {"total": 0, "by_attribution": {}, "by_archetype": {}}


class TestRefresh(unittest.TestCase):
    def test_an_unreadable_bug_is_left_alone_not_blanked(self):
        # A restricted bug is ABSENT from BMO's query form rather than an error. Wiping the
        # verdict we already recorded about it would be the worst reading of that silence.
        from crashclouseau import feedback as fb

        filed = [{"bug_id": 1, "uuid": "u-1", "named_bug": 7, "named_node": "n",
                  "archetypes": [], "filed_at": "2026-08-10T00:00:00+00:00"},
                 {"bug_id": 2, "uuid": "u-2", "named_bug": 8, "named_node": "m",
                  "archetypes": [], "filed_at": None}]
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
                  "archetypes": ["shutdown-singleton"], "filed_at": None}]
        with mock.patch.object(fb, "_filed_bugs", return_value=filed), \
             mock.patch.object(fb, "_fetch", return_value={9: {"id": 9, "status": "NEW"}}), \
             mock.patch.object(models.Feedback, "record") as record, \
             mock.patch.object(models.Feedback, "scoreboard", return_value=_EMPTY_BOARD):
            fb.refresh()
        self.assertEqual(record.call_args.kwargs["archetypes"], ["shutdown-singleton"])


if __name__ == "__main__":
    unittest.main()
