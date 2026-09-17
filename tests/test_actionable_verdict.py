# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""The `actionable` verdict (2026-09-17): a crash worth filing on its own facts.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        uv run python -m unittest tests.test_actionable_verdict

Crash 0015b3bf (release, `CheckLogMessage::~CheckLogMessage`, 75-day-old signature, 110
installations) was analysed twice on the same evidence and came out culprit 85 then abstain 25,
because the pipeline had no box for "the mechanism is established, the code has an owner, and no
changeset in the window caused it". These tests pin the third box end to end: what the schema
requires of it, what the gates do with it, what the bug says, and what the filer checks before
posting it -- all affirmative, nothing about what the crash is not.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import bugzilla_apply, report_bug  # noqa: E402
from crashclouseau.agent import orchestrator as orch, triage  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    AbstainKind,
    Candidate,
    Claim,
    Confidence,
    Decision,
    Dossier,
    SearchfoxCitation,
    Verdict,
    parse_and_validate,
)
from tests.test_autofile import _Base, _bug  # noqa: E402
# The module, not its classes: binding a TestCase name here would run its tests twice.
from tests import test_product_wiring as tpw  # noqa: E402
from tests.test_prompt_schema_drift import _quoted_tokens  # noqa: E402
from tests.test_signature_age_gate import _SEED, _seed  # noqa: E402

_SF_DICT = {"kind": "searchfox", "permalink": "https://searchfox.org/x#392",
            "symbol_id": "_ZN7sandbox19InterceptionManager10PatchNtdllE", "repo": "mozilla-central"}
_SF = SearchfoxCitation(**{k: v for k, v in _SF_DICT.items() if k != "kind"})
_MECH = ("`CHECK(thunk_base)` in `sandbox::InterceptionManager::PatchNtdll` (interception.cc:392) "
         "fires when `VirtualAllocEx` fails to commit the thunk region in the new child process")
_FACT = "85 crashes from 77 installations on 155.0.1, at a flat 0.5 per 1000 across four versions"


def _handoff(**over):
    """A model handoff that says `actionable`, as the principal would emit it."""
    obj = {
        "candidate": {"node": "507a4c21a8eb", "bug": 2010557, "author": "Bob Owen"},
        "verdict": {"decision": "actionable", "confidence": "probable",
                    "mechanism": {"statement": _MECH, "citations": [_SF_DICT]},
                    "consistency": {"statement": _FACT, "citations": [_SF_DICT]}},
    }
    for key, value in over.items():
        if value is None:
            obj.pop(key, None)
        else:
            obj[key] = value
    return obj


def _actionable(confidence=Confidence.probable, node="abc123def456"):
    return Dossier(
        candidate=Candidate(node=node, bug=42, author="Bob Owen") if node else None,
        verdict=Verdict(decision=Decision.actionable, confidence=confidence,
                        mechanism=Claim(statement=_MECH, citations=[_SF]),
                        consistency=Claim(statement=_FACT, citations=[_SF])),
    )


class TestWhatTheSchemaRequires(unittest.TestCase):
    def test_a_cited_mechanism_and_an_origin_make_an_actionable_verdict(self):
        d = parse_and_validate(_handoff())
        self.assertEqual(d.verdict.decision, Decision.actionable)
        self.assertEqual(d.verdict.confidence, Confidence.probable)
        self.assertEqual(d.candidate.node, "507a4c21a8eb")
        self.assertIn("CHECK(thunk_base)", d.verdict.mechanism.statement)

    def test_high_is_clamped_to_probable_like_a_lead(self):
        # No deterministic corroborator applies to this verdict, so `high` is not a rung it can
        # self-assert; `probable` is the filing floor and "the skeptic could not contradict it".
        v = parse_and_validate(_handoff(verdict={**_handoff()["verdict"], "confidence": "high"}))
        self.assertEqual(v.verdict.decision, Decision.actionable)
        self.assertEqual(v.verdict.confidence, Confidence.probable)

    def test_without_a_cited_mechanism_there_is_no_actionable_verdict(self):
        verdict = {"decision": "actionable", "confidence": "probable",
                   "consistency": {"statement": _FACT, "citations": [_SF_DICT]}}
        d = parse_and_validate(_handoff(verdict=verdict))
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertEqual(d.verdict.abstain_kind, AbstainKind.pipeline_error)
        # The uncited claim was the whole verdict, so nothing survived to file on.
        self.assertIn("verdict unusable", d.verdict.abstain_reason)

    def test_without_an_origin_it_is_a_pre_existing_abstain_that_keeps_the_mechanism(self):
        # Nobody to route it to is not an error; it is exactly `pre_existing` ("you DID find the
        # mechanism and it is old"), and the page still shows what was found.
        d = parse_and_validate(_handoff(candidate=None))
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertEqual(d.verdict.abstain_kind, AbstainKind.pre_existing)
        self.assertIn("no origin changeset", d.verdict.abstain_reason)
        self.assertIn("CHECK(thunk_base)", d.verdict.mechanism.statement)

    def test_a_stray_abstain_reason_is_dropped_not_fatal(self):
        verdict = {**_handoff()["verdict"], "abstain_reason": "it is old"}
        d = parse_and_validate(_handoff(verdict=verdict))
        self.assertEqual(d.verdict.decision, Decision.actionable)
        self.assertIsNone(d.verdict.abstain_reason)

    def test_a_skeptic_fail_on_the_mechanism_is_noise(self):
        skeptic = [{"claim_ref": "mechanism", "status": "fail",
                    "note": "the cited line is a DCHECK compiled out of release builds",
                    "citations": [_SF_DICT]}]
        d = parse_and_validate(_handoff(skeptic=skeptic))
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertEqual(d.verdict.abstain_kind, AbstainKind.noise)
        self.assertIn("this actionable as noise", d.verdict.abstain_reason)

    def test_a_presence_objection_does_not_bind(self):
        # (1d) as for a lead: "already in the build" is the precondition, not a refutation.
        skeptic = [{"claim_ref": "candidate", "status": "fail",
                    "note": "507a4c21a8eb is already present in this build, so it cannot be new",
                    "citations": [_SF_DICT]}]
        d = parse_and_validate(_handoff(skeptic=skeptic))
        self.assertEqual(d.verdict.decision, Decision.actionable)
        self.assertEqual(d.corroborations["skeptic_presence_unbound"], ["candidate"])

    def test_the_prompt_offers_exactly_the_schemas_decisions(self):
        toks = _quoted_tokens(triage._system_prompt(), "decision")
        self.assertIn("actionable", toks)
        self.assertLessEqual(toks, {d.value for d in Decision})


class TestWhatTheGatesDoWithIt(unittest.TestCase):
    def test_the_verdict_row_carries_its_own_label_and_the_mechanism(self):
        row = orch._verdict_row(SimpleNamespace(dossier=_actionable()))
        self.assertEqual((row["verdict"], row["confidence"]), ("actionable", 70))
        self.assertIn("CHECK(thunk_base)", row["rationale"])

    def test_no_calibrated_probability_is_read_off_the_regressor_table(self):
        d = _actionable()
        with mock.patch.object(orch.config, "get_agent_calibration",
                               return_value={70: 0.7234, 85: 0.7234}):
            orch._apply_worth_investigating(d, {"channel": "nightly", "product": "Firefox"})
        self.assertIsNone(d.verdict.p_worth_investigating)

    def test_an_origin_that_postdates_the_signature_is_not_the_origin(self):
        # The age gate's sign flips: for a lead a late candidate is a downweight (it may still
        # have made an old crash frequent); for `actionable` the candidate is the ORIGIN and a
        # late one routes the bug off the wrong changeset.
        d = _actionable()
        orch._apply_signature_age_gate(d, _seed(178.0))
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertEqual(d.verdict.abstain_kind, AbstainKind.pre_existing)
        self.assertIn("178 days after", d.verdict.abstain_reason)
        self.assertEqual(d.corroborations["actionable_origin_postdates_signature"], 178.0)
        self.assertNotIn("stale_signature_clamped", d.corroborations)
        self.assertIn("CHECK(thunk_base)", d.verdict.mechanism.statement)

    def test_an_origin_that_predates_the_signature_stands(self):
        d = _actionable()
        orch._apply_signature_age_gate(d, _seed(-4.0))
        self.assertEqual(d.verdict.decision, Decision.actionable)
        self.assertNotIn("actionable_origin_postdates_signature", d.corroborations or {})

    def test_the_blind_second_opinion_is_not_bought_for_it(self):
        with mock.patch.object(orch.config, "get_agent_second_opinion",
                               return_value={"enabled": True, "min_confidence": 25,
                                             "min_boost_confidence": 50}):
            so, status = orch._maybe_run_second_opinion(
                SimpleNamespace(dossier=_actionable()), _SEED)
        self.assertIsNone(so)
        self.assertEqual(status, "skipped_actionable")


_UI = {"uuid": "u-1", "signature": "logging::CheckLogMessage::~CheckLogMessage",
       "channel": "release", "product": "Firefox", "version": "155.0.1",
       "buildid": "20260903215306"}


def _preview_dossier(**corro):
    return {
        "candidate": {"node": "507a4c21a8eb", "bug": 2010557, "author": "Bob Owen <bob@x.com>",
                      "git_commit": "def44d35"},
        "corroborations": {"candidate_in_pushlog_window": True,
                           "signature_first_seen_ever": "20260621080854",
                           "signature_age_days_ever": 75.0, **corro},
        "verdict": {"decision": "actionable", "confidence": "probable",
                    "mechanism": {"statement": _MECH, "citations": [_SF_DICT]},
                    "consistency": {"statement": _FACT, "citations": [_SF_DICT]}},
    }


def _build_preview(dossier, ui=_UI):
    with mock.patch.object(report_bug, "resolve_product_component",
                           return_value=("Core", "Security: Process Sandboxing")), \
            mock.patch.object(report_bug, "fetch_crash_reason",
                              return_value={"reason": "EXCEPTION_BREAKPOINT",
                                            "address": "0x7ff6fb034c43"}), \
            mock.patch.object(report_bug, "fetch_signature_stats",
                              return_value=(False, {"count": 85, "installs": 77})), \
            mock.patch("crashclouseau.models.Node.authors_for", return_value={}), \
            mock.patch.object(report_bug, "_bugzilla_user",
                              return_value={"exists": True, "nick": "bobowen"}):
        return report_bug.build_bug_preview(ui, {"frames": [
            {"stackpos": 0, "function": "logging::LogMessage::~LogMessage()",
             "filename": "security/sandbox/chromium-shim/base/logging.cpp", "line": 101,
             "module": "firefox.exe"}]}, dossier)


# Every phrase the regressor filing uses to say what the crash is NOT, or to hedge a claim this
# bug does not make. Calixte, 2026-09-17: "no need to say what this bug isn't ... just be
# affirmative".
_FORBIDDEN = ("NOT a suspected cause", "did not land", "not a regression", "not new",
              "not a spike", "Suspected regressor", "worth investigating", "Skeptic",
              "not proven end-to-end", "Starting point")


class TestTheBugItFiles(unittest.TestCase):
    def test_the_bug_argues_from_what_is(self):
        p = _build_preview(_preview_dossier())
        c = p["comment"]
        self.assertIn("**This bug looks actionable because:**", c)
        self.assertIn("- " + _MECH, c)
        self.assertIn("- " + _FACT, c)
        self.assertIn("- The failing code comes from [507a4c21a8eb](", c)
        self.assertIn("(bug 2010557) by :bobowen.", c)
        self.assertIn("There are 85 crashes (from 77 installations) in 155.0.1 starting with "
                      "buildid 20260903215306.", c)
        self.assertIn("This signature has been reported since build 20260621080854 (2026-06-21), "
                      "75 days before the build above.", c)
        self.assertIn(":bobowen, can you have a look please?", c)
        self.assertTrue(c.endswith(report_bug._provenance("release")))
        for phrase in _FORBIDDEN:
            self.assertNotIn(phrase, c, phrase)

    def test_the_sections_come_in_the_reading_order(self):
        c = _build_preview(_preview_dossier())["comment"]
        order = ["Crash report:", "Crash Reason:", "Top 1 frames:", "There are 85 crashes",
                 "has been reported since", "looks actionable because", "The failing code",
                 ":bobowen, can you have a look please?", "Filed automatically"]
        at = [c.find(x) for x in order]
        self.assertNotIn(-1, at, list(zip(order, at)))
        self.assertEqual(at, sorted(at))

    def test_it_claims_no_regression_even_when_the_origin_is_in_the_window(self):
        # `candidate_in_pushlog_window` is True in the fixture on purpose: the window says
        # nothing about a verdict that makes no causal claim.
        p = _build_preview(_preview_dossier())
        self.assertEqual(p["keywords"], ["crash"])
        self.assertEqual(p["regressed_by"], [])
        self.assertEqual(p["blocked"], ["clouseau"])

    def test_release_marks_are_regression_marks_and_stay_off(self):
        p = _build_preview(_preview_dossier())
        self.assertEqual(p["title"], "Crash in [@ logging::CheckLogMessage::~CheckLogMessage]")
        self.assertIsNone(p["tracking_flag"])
        # ...and the routing still comes from the origin, as for a regressor filing.
        self.assertEqual((p["product"], p["component"]), ("Core", "Security: Process Sandboxing"))
        self.assertEqual(p["needinfo_email"], "bob@x.com")

    def test_a_regressor_filing_is_untouched(self):
        d = _preview_dossier()
        d["verdict"]["decision"] = "lead"
        p = _build_preview(d)
        self.assertEqual(p["title"], "[new in release] Crash in [@ logging::CheckLogMessage::"
                                     "~CheckLogMessage]")
        self.assertEqual(p["keywords"], ["crash", "regression"])
        self.assertIn("Suspected regressor:", p["comment"])

    def test_the_onset_line_in_its_three_shapes(self):
        c = {"signature_first_seen_ever": "20260903215306", "signature_age_days_ever": 0.0}
        self.assertEqual(report_bug.build_signature_since_note(c, "20260903215306"),
                         "This signature's first report anywhere is in the build above.")
        c = {"signature_first_seen_ever": "20260903100000", "signature_age_days_ever": 0.4}
        self.assertEqual(report_bug.build_signature_since_note(c, "20260903215306"),
                         "This signature has been reported since build 20260903100000 "
                         "(2026-09-03), less than a day before the build above.")
        c = {"signature_first_seen_ever": "20260621080854", "signature_age_days_ever": 75.0,
             "signature_clock_drift_days": 300.0, "signature_rename_suspected": True}
        self.assertIn("under an earlier name", report_bug.build_signature_since_note(c))
        self.assertEqual(report_bug.build_signature_since_note({}), "")


class TestWhatTheFilerChecks(_Base):
    """`autofile_bug` on an `actionable` verdict: the two gates of its own, and the ordinary
    ones it shares. `_Base` fakes every Bugzilla and DB collaborator; the preview is `_PREVIEW`."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(report_bug, "fetch_signature_stats",
                              return_value=(True, {"count": 30, "installs": 12}))
        self.stats = p.start()
        self.addCleanup(p.stop)

    def _actionable(self, **cfg_over):
        return self._file(verdict="actionable", confidence=70, **cfg_over)

    def test_a_probable_actionable_verdict_files(self):
        res = self._actionable()
        self.assertTrue(res["filed"], res)
        self.assertEqual((res["bug"], res["mode"]), (999, "new_bug"))
        self.assertEqual(len(self.created), 1)

    def test_below_the_rung_it_does_not(self):
        res = self._file(verdict="actionable", confidence=50)
        self.assertFalse(res["filed"])
        self.assertIn("below 70", res["skipped"])
        self.assertEqual(self.created, [])

    def test_one_installation_is_below_the_floor(self):
        # The shipped floor on Firefox nightly (`spike.real_installs`, the spike path's own bar):
        # one imaged fleet can mint two "installations" in a minute, so it asks for three.
        self.stats.return_value = (True, {"count": 4, "installs": 1})
        res = self._actionable()
        self.assertFalse(res["filed"])
        floor = bugzilla_apply.config.get_spike("real_installs", "Firefox", "nightly")
        self.assertGreaterEqual(floor, 3)
        self.assertEqual(res["skipped"], "1 installation on this signature, below the actionable "
                                         "floor of {}".format(floor))
        self.assertEqual(self.created, [])

    def test_an_unknown_population_does_not_file(self):
        self.stats.return_value = (True, {})
        res = self._actionable()
        self.assertFalse(res["filed"])
        self.assertIn("population unknown", res["skipped"])

    def test_the_floor_is_the_spike_paths_real_installs(self):
        with mock.patch.object(bugzilla_apply.config, "get_spike", return_value=20) as spike:
            res = self._actionable()
        self.assertFalse(res["filed"])
        self.assertIn("below the actionable floor of 20", res["skipped"])
        spike.assert_called_once_with("real_installs", "Firefox", "nightly")

    def test_an_open_bug_declines_on_every_channel_policy(self):
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(5)]
        for mode in ("comment", "skip", "file_new"):
            with self.subTest(mode=mode):
                res = self._actionable(comment_on_existing=mode)
                self.assertFalse(res["filed"])
                self.assertEqual(res["bug"], 5)
                self.assertEqual(res["skipped"],
                                 "open bug 5 exists; an actionable crash is filed only where no "
                                 "bug is")
        self.assertEqual((self.created, self.comments), ([], []))

    def test_a_meta_tracker_or_another_application_is_not_an_open_bug(self):
        bugzilla_apply._open_bugs_for_signature.return_value = [
            _bug(6, keywords=("meta",)), _bug(7, product="MailNews Core")]
        res = self._actionable()
        self.assertTrue(res["filed"], res)

    def test_a_prior_filing_on_the_signature_stops_it_even_where_comments_are_allowed(self):
        bugzilla_apply.models.Dossier.already_filed_for_signature.return_value = {
            "bug": 7, "uuid": "u-0"}
        res = self._actionable(comment_on_existing="comment")
        self.assertFalse(res["filed"])
        self.assertIn("already filed bug 7", res["skipped"])

    def test_the_verdict_can_be_taken_out_of_the_filing_list(self):
        res = self._actionable(verdicts=["lead", "culprit"])
        self.assertFalse(res["filed"])
        self.assertEqual(res["skipped"], "verdict actionable not fileable")


class TestThePage(unittest.TestCase):
    def test_the_badge_and_the_headings_speak_in_its_own_voice(self):
        ev = tpw._evidence(verdict="actionable", confidence=70)
        ev["dossier"]["verdict"]["decision"] = "actionable"
        panel = tpw.TestCrashstackPanel(methodName="setUp")
        panel.setUp()
        html = panel._get(ev).get_data(as_text=True)
        self.assertIn("ACTIONABLE", html)
        self.assertIn("70%", html)
        self.assertIn("Where the failing code comes from", html)
        self.assertIn("What fails and where", html)
        self.assertNotIn("Suspected regressor", html)
        self.assertNotIn("Working hypothesis", html)
        # The bug preview is built for it, in its own words.
        self.assertIn("This bug looks actionable because", html)


if __name__ == "__main__":
    unittest.main()
