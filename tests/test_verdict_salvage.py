# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""An uncitable optional claim must cost the claim, not the whole verdict.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        uv run python -m unittest tests.test_verdict_salvage

`_salvage`'s contract has always been "nothing uncited SURVIVES ... while a single malformed
optional field no longer discards the whole (properly-cited) verdict". The verdict's own
sub-claims were the one place it was not applied: `Verdict` validated atomically, so a
`consistency` the model could not cite took a correctly-cited `mechanism` down with it.

Of the 45 dossiers prod lost to validation in 30 days, 37 (82%) name `verdict.consistency`.
Both live shapes are reproduced below, verbatim from prod's own error text.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import json  # noqa: E402
import unittest  # noqa: E402

from crashclouseau.agent.schema import (  # noqa: E402
    AbstainKind, Decision, _salvage_verdict, parse_and_validate,
)

CITE = {"kind": "stack_frame", "uuid": "u", "stackpos": 0, "function": "F"}
CITED = {"statement": "s", "citations": [CITE]}


def _dossier(verdict):
    return {"crash": {"uuid": "u", "signature": "S", "failure_class": "other",
                      "crashing_thread": 0, "frames": []},
            "candidate": {"node": "0123456789ab", "bug": 1, "author": "d"},
            "verdict": verdict}


def _parse(verdict):
    return parse_and_validate("```json\n" + json.dumps(_dossier(verdict)) + "\n```")


class TestTheTwoLiveShapes(unittest.TestCase):
    """Both produce prod's exact message, "1 malformed field: verdict.consistency"."""

    def test_consistency_with_an_empty_citations_list(self):
        # "Claim needs >= 1 citation(s), got 0"
        v = _parse({"decision": "lead", "confidence": "probable", "mechanism": CITED,
                    "consistency": {"statement": "c", "citations": []}}).verdict
        self.assertEqual(v.decision, Decision.lead)
        self.assertIsNotNone(v.mechanism)      # the cited claim survives
        self.assertIsNone(v.consistency)       # the uncited one does not

    def test_consistency_written_as_a_bare_string(self):
        # "Input should be a valid dictionary or instance of Claim"
        v = _parse({"decision": "lead", "confidence": "probable", "mechanism": CITED,
                    "consistency": "the crash matches the poison pattern"}).verdict
        self.assertEqual(v.decision, Decision.lead)
        self.assertIsNone(v.consistency)


class TestItEnforcesTheGroundingRuleRatherThanRelaxingIt(unittest.TestCase):
    def test_the_uncited_claim_is_dropped_never_kept(self):
        _, dropped = _salvage_verdict({"decision": "lead", "confidence": "medium",
                                       "mechanism": CITED,
                                       "consistency": {"statement": "c", "citations": []}})
        self.assertEqual(dropped, ["verdict.consistency"])

    def test_an_uncited_mechanism_goes_the_same_way(self):
        v = _parse({"decision": "lead", "confidence": "probable",
                    "mechanism": {"statement": "m", "citations": []},
                    "consistency": CITED}).verdict
        self.assertEqual(v.decision, Decision.lead)
        self.assertIsNone(v.mechanism)


class TestStrongEvidenceIsNotTouched(unittest.TestCase):
    """The strictest gate in the schema — claim the strongest thing and fail to cite it and
    the verdict is unusable — is left exactly as it was. Repairing it would also be
    unmeasurable: prod has emitted 0 strong-evidence verdicts in 2,560 done runs."""

    def test_an_uncited_consistency_still_bins_a_strong_evidence_verdict(self):
        v = _parse({"decision": "strong-evidence", "confidence": "high", "mechanism": CITED,
                    "consistency": {"statement": "c", "citations": []}}).verdict
        self.assertEqual(v.decision, Decision.abstain)

    def test_an_uncited_mechanism_too(self):
        v = _parse({"decision": "strong-evidence", "confidence": "high",
                    "mechanism": {"statement": "m", "citations": []},
                    "consistency": CITED}).verdict
        self.assertEqual(v.decision, Decision.abstain)

    def test_it_returns_before_inspecting_the_claims(self):
        verdict, dropped = _salvage_verdict(
            {"decision": "strong-evidence", "confidence": "high", "mechanism": CITED,
             "consistency": {"statement": "c", "citations": []}})
        self.assertIsNone(verdict)
        self.assertEqual(dropped, ["verdict"])


class TestNothingIsEverPromoted(unittest.TestCase):
    def test_nothing_is_ever_promoted(self):
        for decision in ("lead", "abstain"):
            payload = {"decision": decision, "confidence": "medium",
                       "consistency": {"statement": "c", "citations": []}}
            if decision == "abstain":
                payload["abstain_reason"] = "r"
            else:
                payload["mechanism"] = CITED
            v = _parse(payload).verdict
            self.assertEqual(v.decision.value, decision, decision)


class TestWhatItRefusesToRepair(unittest.TestCase):
    def test_a_verdict_broken_for_another_reason_still_abstains(self):
        # strong-evidence below the confidence floor: not a citation problem, not ours to fix.
        v = _parse({"decision": "strong-evidence", "confidence": "low",
                    "mechanism": CITED, "consistency": CITED}).verdict
        self.assertEqual(v.decision, Decision.abstain)
        self.assertEqual(v.abstain_kind, AbstainKind.pipeline_error)

    def test_a_non_dict_verdict_is_dropped(self):
        verdict, dropped = _salvage_verdict("not a verdict at all")
        self.assertIsNone(verdict)
        self.assertEqual(dropped, ["verdict"])

    def test_an_intact_verdict_is_untouched(self):
        verdict, dropped = _salvage_verdict(
            {"decision": "lead", "confidence": "medium", "mechanism": CITED})
        self.assertEqual(dropped, [])
        self.assertEqual(verdict.decision, Decision.lead)


class TestTheEvidenceAroundItIsUnaffected(unittest.TestCase):
    def test_the_rest_of_the_dossier_still_arrives(self):
        d = _parse({"decision": "lead", "confidence": "probable", "mechanism": CITED,
                    "consistency": {"statement": "c", "citations": []}})
        self.assertEqual(d.candidate.node, "0123456789ab")
        self.assertEqual(d.crash.signature, "S")


if __name__ == "__main__":
    unittest.main()
