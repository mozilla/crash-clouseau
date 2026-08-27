# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""An abstain is usually a conclusion, not an absence. Say which kind it is.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        uv run python -m unittest tests.test_abstain_kind

`system.md` said `abstain` was "ONLY for genuine noise". Over 30 days of prod, 1,646 of 2,325
abstains (71%) are model-authored and full of specific conclusions — a driver fault, an
unsymbolicated stack, commit-charge exhaustion, "I searched the window and nothing explains it".
Most of those are correctly silent; a few are not; and nothing could tell them apart, because it
was all free text.

The taxonomy is model-emitted rather than classified after the fact for a measured reason: a
keyword pass over all 1,648 scored `hardware` at 26.8% by catching agents RULING OUT hardware,
and left 22% unclassified. A category the writer knows and the reader has to guess has to be
written down.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import json  # noqa: E402
import unittest  # noqa: E402

from crashclouseau import models  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    AbstainKind, Decision, Verdict, dossier_from_db_json, dossier_to_db_json,
    parse_and_validate,
)


class TestTheVocabularyIsTotal(unittest.TestCase):
    def test_an_unrecognised_word_never_raises(self):
        # The `FailureClass._missing_` pattern: a value the model invents must not be able to
        # destroy a verdict, which is what a bare `Literal` would do.
        for junk in ("wat", "", None, "NOISE!!", "third party crash", 7):
            self.assertEqual(AbstainKind(junk), AbstainKind.other, junk)

    def test_spelling_variants_land_on_the_member(self):
        for raw, want in (("third-party", AbstainKind.third_party),
                          ("Third Party", AbstainKind.third_party),
                          ("no candidate explains it", AbstainKind.no_candidate_explains_it),
                          ("PRE_EXISTING", AbstainKind.pre_existing)):
            self.assertEqual(AbstainKind(raw), want, raw)

    def test_absent_is_not_other_and_not_noise(self):
        """Three distinct facts: not stated, stated-but-unrecognised, stated-as-nothing-here."""
        v = Verdict(decision=Decision.abstain, abstain_reason="r")
        self.assertIsNone(v.abstain_kind)
        self.assertEqual(
            Verdict(decision=Decision.abstain, abstain_reason="r",
                    abstain_kind="wat").abstain_kind, AbstainKind.other)
        self.assertEqual(
            Verdict(decision=Decision.abstain, abstain_reason="r",
                    abstain_kind="noise").abstain_kind, AbstainKind.noise)


class TestItIsNeverRequired(unittest.TestCase):
    """A field the model may omit must not be able to cost a verdict — the 2026-08-05
    citation-kind losses are what that mistake costs."""

    def test_an_abstain_without_it_still_validates(self):
        v = Verdict(decision=Decision.abstain, abstain_reason="because")
        self.assertEqual(v.decision, Decision.abstain)

    def test_a_lead_may_carry_it_without_complaint(self):
        # Not validated against the decision either: a spurious kind on a lead is harmless
        # noise, whereas a rule rejecting it would throw the lead away.
        v = Verdict(decision=Decision.lead, abstain_kind="noise")
        self.assertEqual(v.decision, Decision.lead)


class TestThePipelineTagsItsOwnFailures(unittest.TestCase):
    """The class that hid best: our machinery failing, in the same bucket as a driver crash.
    45 such runs in 30 days, evidence intact, verdict discarded, mean $3.00."""

    def test_an_unreadable_handoff(self):
        d = parse_and_validate("there is no json block here")
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertEqual(d.verdict.abstain_kind, AbstainKind.pipeline_error)

    def test_unparseable_json(self):
        d = parse_and_validate("```json\n{not valid json,}\n```")
        self.assertEqual(d.verdict.abstain_kind, AbstainKind.pipeline_error)

    def test_a_salvaged_dossier_whose_verdict_died(self):
        obj = {"crash": {"uuid": "u", "signature": "S", "failure_class": "other",
                         "crashing_thread": 0, "frames": []},
               "verdict": {"decision": "strong-evidence", "confidence": "high"}}
        d = parse_and_validate("```json\n" + json.dumps(obj) + "\n```")
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertEqual(d.verdict.abstain_kind, AbstainKind.pipeline_error)

    def test_the_word_is_not_offered_to_the_model(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        prompt = open(os.path.join(here, "crashclouseau", "agent", "prompts",
                                   "system.md"), encoding="utf-8").read()
        self.assertNotIn("pipeline_error", prompt)
        # ...but every kind the model IS expected to choose from is.
        for member in AbstainKind:
            if member is not AbstainKind.pipeline_error:
                self.assertIn(member.value, prompt, member.value)


class TestTheGuardrailAbstainsSayNoise(unittest.TestCase):
    def test_every_schema_authored_abstain_carries_a_kind(self):
        """Otherwise the counting has a hole exactly where the schema, not the model, decided."""
        import re
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(here, "crashclouseau", "agent", "schema.py"),
                   encoding="utf-8").read()
        blocks = re.findall(r"Verdict\((?:[^()]|\([^()]*\))*\)", src, re.S)
        abstains = [b for b in blocks if "Decision.abstain" in b]
        self.assertTrue(abstains)
        for b in abstains:
            self.assertIn("abstain_kind=", b, b[:120])


class TestItSurvivesPersistence(unittest.TestCase):
    def test_round_trip(self):
        d = parse_and_validate("no json here")
        again = dossier_from_db_json(dossier_to_db_json(d))
        self.assertEqual(again.verdict.abstain_kind, AbstainKind.pipeline_error)

    def test_an_older_dossier_without_the_field_reads_back(self):
        raw = dossier_to_db_json(parse_and_validate("no json here"))
        del raw["verdict"]["abstain_kind"]
        self.assertIsNone(dossier_from_db_json(raw).verdict.abstain_kind)


class TestTheBrokenRunDetectorReadsIt(unittest.TestCase):
    """Giving the field a reader on day one: `_unusable_verdict` decides whether a broken run
    permanently closes its proto-signature cluster, and it used to match prose prefixes only."""

    def test_the_sql_consults_both_the_prefix_and_the_kind(self):
        sql = str(models._unusable_verdict().compile(
            compile_kwargs={"literal_binds": True}))
        self.assertIn("abstain_reason", sql)
        self.assertIn("abstain_kind", sql)
        self.assertIn("pipeline_error", sql)

    def test_both_arms_are_false_not_null(self):
        """The docstring's own hazard: `FALSE OR NULL` is NULL, which would drop a successful
        run out of BOTH arms of the caller's query and reopen every closed cluster."""
        sql = str(models._unusable_verdict().compile(
            compile_kwargs={"literal_binds": True}))
        self.assertEqual(sql.count("IS NOT NULL"), 2)

    def test_the_prefixes_still_match_what_the_schema_writes(self):
        # Pinned elsewhere too, but restated here because the kind must be an ADDITION to the
        # prefixes, never a replacement: ~2,200 persisted dossiers have no kind at all.
        d = parse_and_validate("nothing parseable")
        self.assertTrue(any(d.verdict.abstain_reason.startswith(p)
                            for p in models._UNUSABLE_VERDICT_PREFIXES))


if __name__ == "__main__":
    unittest.main()
