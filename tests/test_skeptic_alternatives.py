# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Skeptic alternative detection and veto behavior.
# Run: python -m unittest tests.test_skeptic_alternatives
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402

from crashclouseau.agent.schema import (  # noqa: E402
    Candidate,
    Decision,
    SkepticResult,
    is_alternative_check,
    parse_and_validate,
)
from tests.test_actionable_verdict import _SF_DICT, _handoff  # noqa: E402

CAND = "507a4c21a8eb"     # the candidate in `_handoff`
OTHER = "6daf171a848b"


def _fail(claim_ref, note="unrelated to this crash", node=None):
    out = {"claim_ref": claim_ref, "status": "fail", "note": note, "citations": [_SF_DICT]}
    if node is not None:
        out["node"] = node
    return out


def _check(**kw):
    return SkepticResult.model_validate(dict(_fail(kw.pop("claim_ref", ""), **kw)))


class TestTheVeto(unittest.TestCase):
    def test_a_ruled_out_alternative_does_not_veto(self):
        skeptic = [_fail("window_candidate_" + OTHER,
                         "{} changes only GetBrowserParent; it cannot change the lookup".format(OTHER))]
        d = parse_and_validate(_handoff(skeptic=skeptic))
        self.assertEqual(d.verdict.decision, Decision.actionable)
        self.assertEqual(d.corroborations["skeptic_alternatives_unbound"],
                         ["window_candidate_" + OTHER])

    def test_a_fail_on_the_candidate_still_vetoes(self):
        for entry in (_fail("candidate_" + CAND), _fail("candidate", node=CAND),
                      _fail("mechanism")):
            with self.subTest(entry=entry):
                d = parse_and_validate(_handoff(skeptic=[entry]))
                self.assertEqual(d.verdict.decision, Decision.abstain)
                self.assertNotIn("skeptic_alternatives_unbound", d.corroborations)

    def test_an_alternative_does_not_shield_a_real_refutation(self):
        d = parse_and_validate(_handoff(skeptic=[_fail(OTHER), _fail("mechanism")]))
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertIn("failed: mechanism)", d.verdict.abstain_reason)
        self.assertEqual(d.corroborations["skeptic_alternatives_unbound"], [OTHER])

    def test_a_junk_node_does_not_unbind_a_refutation(self):
        for node in ("unknown", "n/a", {}, "candidate", CAND[:7]):
            with self.subTest(node=node):
                d = parse_and_validate(_handoff(skeptic=[_fail("mechanism", node=node)]))
                self.assertEqual(d.verdict.decision, Decision.abstain)

    def test_a_lead_keeps_its_rung(self):
        handoff = _handoff(skeptic=[_fail("seed:" + OTHER)])
        handoff["verdict"] = dict(handoff["verdict"], decision="lead")
        handoff["hunks"] = [{"node": CAND, "filename": "a.cpp", "header": "@@ -1 +1 @@",
                             "lines": ["+ 1: x"], "citations": [_SF_DICT]}]
        d = parse_and_validate(handoff)
        self.assertEqual(d.verdict.decision, Decision.lead)


class TestIsAlternativeCheck(unittest.TestCase):
    def test_node_decides_when_set(self):
        self.assertTrue(is_alternative_check(_check(claim_ref="candidate", node=OTHER), CAND))
        self.assertFalse(is_alternative_check(_check(claim_ref=OTHER, node=CAND), CAND))

    def test_hashes_in_the_claim_ref(self):
        for ref in ("candidate_" + OTHER, "hunk0-" + OTHER, "{}_96bba8579c32".format(OTHER)):
            with self.subTest(ref=ref):
                self.assertTrue(is_alternative_check(_check(claim_ref=ref), CAND))
        self.assertFalse(is_alternative_check(
            _check(claim_ref="{}/{}".format(OTHER, CAND)), CAND))

    def test_a_seed_label_reads_the_note(self):
        self.assertTrue(is_alternative_check(
            _check(claim_ref="seed_candidates", note="{} only renames a getter".format(OTHER)), CAND))
        self.assertFalse(is_alternative_check(
            _check(claim_ref="seed_candidates", note="{} is the cause".format(CAND)), CAND))
        self.assertFalse(is_alternative_check(
            _check(claim_ref="seed_candidates", note="the seeds touch no IPC code"), CAND))

    def test_a_buildid_is_not_a_changeset(self):
        self.assertFalse(is_alternative_check(
            _check(claim_ref="seed_candidates", note="first seen in build 20260825213824"), CAND))

    def test_other_claims_and_a_missing_candidate_bind(self):
        for ref in ("mechanism", "rate-claim", "candidate", "window-membership"):
            with self.subTest(ref=ref):
                self.assertFalse(is_alternative_check(
                    _check(claim_ref=ref, note="contradicts {}".format(OTHER)), CAND))
        self.assertFalse(is_alternative_check(_check(claim_ref=OTHER), ""))

    def test_a_node_that_is_not_a_hash_is_ignored(self):
        for node in ("unknown", "n/a", "{}", "candidate", "2060764"):
            with self.subTest(node=node):
                self.assertFalse(is_alternative_check(_check(claim_ref="mechanism", node=node), CAND))
        self.assertTrue(is_alternative_check(_check(claim_ref="x", node=OTHER[:7]), CAND))

    def test_the_candidates_abbreviation_git_commit_and_bug_bind(self):
        git = "bd2dcae484eeef7e607f3b7957031a413c8744de"
        cand = Candidate(node=CAND, bug=2010557, git_commit=git)
        self.assertFalse(is_alternative_check(_check(claim_ref="x", node=CAND[:7]), cand))
        self.assertFalse(is_alternative_check(_check(claim_ref="x", node=git), cand))
        self.assertFalse(is_alternative_check(_check(claim_ref="x", node=git[:12]), cand))
        self.assertFalse(is_alternative_check(
            _check(claim_ref="seed:" + OTHER, note="unlike bug 2010557, unrelated"), cand))
        self.assertTrue(is_alternative_check(_check(claim_ref="seed:" + OTHER), cand))

    def test_addresses_and_uuids_are_not_changesets(self):
        for note in ("fault at 0x7ff6fb034c43", "crash d0bcbd09-34af-471f-ba15-7dd300260930",
                     "a 13-digit run abcdef0123456 is not a node"):
            with self.subTest(note=note):
                self.assertFalse(is_alternative_check(
                    _check(claim_ref="seed_candidate", note=note), CAND))
        self.assertTrue(is_alternative_check(
            _check(claim_ref="seed_candidate", note="{} at 0x7ff6fb034c43".format(OTHER)), CAND))

    def test_a_null_node_is_empty(self):
        self.assertEqual(_check(claim_ref="x", node=None).node, "")
        self.assertEqual(SkepticResult.model_validate(
            {"status": "fail", "claim_ref": "x", "node": None}).node, "")


if __name__ == "__main__":
    unittest.main()
