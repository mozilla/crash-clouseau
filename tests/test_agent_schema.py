# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Run with a DB url set (the package builds a Flask app at import):
#   DATABASE_URL=sqlite:// python -m unittest tests.test_agent_schema
import copy
import json
import unittest

from pydantic import TypeAdapter, ValidationError

from crashclouseau.agent.schema import (
    Citation,
    Confidence,
    Decision,
    SearchfoxCitation,
    DiffLineCitation,
    StackFrameCitation,
    dossier_from_db_json,
    dossier_to_db_json,
    parse_and_validate,
    validate_dossier,
    validate_role_fragment,
)


def _searchfox():
    return {
        "kind": "searchfox",
        "permalink": "https://searchfox.org/mozilla-central/source/foo.cpp#10",
        "symbol_id": "_ZN3FooD1Ev",
        "repo": "mozilla-central",
        "rev": "deadbeef",
    }


def _diff_line():
    return {
        "kind": "diff_line",
        "node": "0123456789ab",
        "filename": "foo.cpp",
        "line": 10,
        "side": "added",
        "content": "Release(mObj);",
    }


def _stack_frame():
    return {
        "kind": "stack_frame",
        "uuid": "uuid-1",
        "stackpos": 0,
        "filename": "foo.cpp",
        "function": "Foo::Bar",
        "line": 42,
        "node": "0123456789ab",
    }


def _dossier():
    return {
        "crash": {
            "uuid": "uuid-1",
            "signature": "Foo::Bar",
            "failure_class": "uaf",
            "crashing_thread": 0,
            "frames": [
                {
                    "stackpos": 0,
                    "function": "Foo::Bar",
                    "filename": "foo.cpp",
                    "line": 42,
                    "node": "0123456789ab",
                }
            ],
        },
        "candidate": {"node": "0123456789ab", "bug": 123, "author": "dev"},
        "call_path": {
            "edges": [
                {
                    "caller_symbol": "A",
                    "callee_symbol": "B",
                    "via": "calls-from",
                    "citations": [_searchfox()],
                }
            ],
            "to_symbol": "B",
        },
        "hunks": [
            {
                "node": "0123456789ab",
                "filename": "foo.cpp",
                "header": "@@ -1 +1 @@",
                "lines": [_diff_line()],
                "citations": [_diff_line()],
            }
        ],
        "data_flow": {
            "summary": "UAF of mObj",
            "object_name": "mObj",
            "operation": "free",
            "crash_site": _stack_frame(),
            "citations": [_searchfox()],
        },
        "skeptic": [{"claim_ref": "edge0", "status": "pass", "note": "ok"}],
        "verdict": {
            "decision": "strong-evidence",
            "confidence": "high",
            "needinfo_draft": "please confirm",
            "mechanism": {"statement": "UAF", "citations": [_diff_line()]},
            "consistency": {"statement": "matches poison", "citations": [_stack_frame()]},
        },
    }


class TestFullyCited(unittest.TestCase):
    def test_fully_cited_dossier_validates(self):
        d = validate_dossier(_dossier())
        self.assertEqual(d.verdict.decision, Decision.strong_evidence)
        self.assertEqual(d.crash.failure_class.value, "uaf")
        self.assertEqual(len(d.call_path.edges), 1)


class TestCitationGate(unittest.TestCase):
    def test_uncited_call_edge_raises(self):
        obj = copy.deepcopy(_dossier())
        obj["call_path"]["edges"][0]["citations"] = []
        with self.assertRaises(ValidationError):
            validate_dossier(obj)

    def test_uncited_diff_hunk_raises(self):
        obj = copy.deepcopy(_dossier())
        obj["hunks"][0]["citations"] = []
        with self.assertRaises(ValidationError):
            validate_dossier(obj)

    def test_uncited_verdict_mechanism_raises(self):
        obj = copy.deepcopy(_dossier())
        obj["verdict"]["mechanism"]["citations"] = []
        with self.assertRaises(ValidationError):
            validate_dossier(obj)


class TestCitationRoundTrip(unittest.TestCase):
    def test_discriminated_union_round_trip(self):
        adapter = TypeAdapter(Citation)
        cases = [
            (_searchfox(), SearchfoxCitation, "searchfox"),
            (_diff_line(), DiffLineCitation, "diff_line"),
            (_stack_frame(), StackFrameCitation, "stack_frame"),
        ]
        for raw, cls, kind in cases:
            obj = adapter.validate_python(raw)
            self.assertIsInstance(obj, cls)
            dumped = adapter.dump_python(obj, mode="json")
            self.assertEqual(dumped["kind"], kind)
            self.assertIsInstance(adapter.validate_python(dumped), cls)

    def test_bad_kind_fails(self):
        adapter = TypeAdapter(Citation)
        with self.assertRaises(ValidationError):
            adapter.validate_python({"kind": "bogus", "permalink": "x"})


class TestParseAndValidate(unittest.TestCase):
    def test_missing_block_abstains(self):
        d = parse_and_validate("there is no json block here")
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertTrue(d.verdict.abstain_reason)

    def test_malformed_block_abstains(self):
        d = parse_and_validate("```json\n{not valid json,}\n```")
        self.assertEqual(d.verdict.decision, Decision.abstain)

    def test_uncited_supporting_edge_salvaged_not_abstained(self):
        # New salvage contract: an uncited call-path edge is DROPPED (not kept), but
        # the properly-cited strong-evidence verdict SURVIVES. parse_and_validate
        # never raises; validate_dossier on the same object still does.
        obj = copy.deepcopy(_dossier())
        obj["call_path"]["edges"][0]["citations"] = []
        text = "prose\n```json\n" + json.dumps(obj) + "\n```\n"
        d = parse_and_validate(text)
        self.assertEqual(d.verdict.decision, Decision.strong_evidence)  # verdict kept
        self.assertEqual(len(d.call_path.edges), 0)                     # uncited edge dropped
        with self.assertRaises(ValidationError):
            validate_dossier(obj)

    def test_uncited_hunk_dropped_verdict_kept(self):
        # A malformed/uncited supporting hunk must not discard the cited verdict.
        obj = copy.deepcopy(_dossier())
        obj["hunks"][0]["citations"] = []   # uncited hunk -> full validate fails
        d = parse_and_validate(obj)
        self.assertEqual(d.verdict.decision, Decision.strong_evidence)  # verdict survives
        self.assertEqual(len(d.hunks), 0)                               # bad hunk dropped
        self.assertEqual(len(d.call_path.edges), 1)                     # good evidence kept

    def test_invalid_verdict_forces_abstain_but_keeps_evidence(self):
        # An uncited strong-evidence verdict is unusable -> abstain (grounding gate
        # preserved), but valid evidence is still salvaged for the panel.
        obj = copy.deepcopy(_dossier())
        obj["verdict"]["mechanism"]["citations"] = []
        d = parse_and_validate(obj)
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertTrue(d.verdict.abstain_reason)
        self.assertEqual(len(d.call_path.edges), 1)   # evidence salvaged despite bad verdict

    def test_valid_block_in_text_parses(self):
        text = "here is my answer\n```json\n" + json.dumps(_dossier()) + "\n```\n"
        d = parse_and_validate(text)
        self.assertEqual(d.verdict.decision, Decision.strong_evidence)

    def test_last_block_wins(self):
        first = json.dumps({"verdict": {"decision": "abstain", "abstain_reason": "x"}})
        second = json.dumps(_dossier())
        text = f"```json\n{first}\n```\nmore\n```json\n{second}\n```"
        d = parse_and_validate(text)
        self.assertEqual(d.verdict.decision, Decision.strong_evidence)


class TestVerdictRules(unittest.TestCase):
    def _verdict(self, **over):
        base = {
            "decision": "strong-evidence",
            "confidence": "high",
            "mechanism": {"statement": "m", "citations": [_diff_line()]},
            "consistency": {"statement": "c", "citations": [_stack_frame()]},
        }
        base.update(over)
        return base

    def test_strong_low_confidence_raises(self):
        obj = copy.deepcopy(_dossier())
        obj["verdict"] = self._verdict(confidence="low")
        with self.assertRaises(ValidationError):
            validate_dossier(obj)

    def test_strong_medium_confidence_raises(self):
        # The confidence floor (config abstain_below_confidence=0.85) enforces
        # system.md's "strong-evidence REQUIRES confidence:high": medium (0.5) is
        # below the floor, so it must be rejected just like low.
        obj = copy.deepcopy(_dossier())
        obj["verdict"] = self._verdict(confidence="medium")
        with self.assertRaises(ValidationError):
            validate_dossier(obj)

    def test_strong_without_mechanism_raises(self):
        obj = copy.deepcopy(_dossier())
        v = self._verdict()
        v.pop("mechanism")
        obj["verdict"] = v
        with self.assertRaises(ValidationError):
            validate_dossier(obj)

    def test_skeptic_fail_downgrades_to_abstain(self):
        # A skeptic `fail` on any culprit-chain claim vetoes a strong-evidence
        # verdict: it is downgraded to abstain IN PLACE, and the evidence survives.
        obj = copy.deepcopy(_dossier())
        obj["skeptic"] = [{"claim_ref": "edge0", "status": "fail", "note": "no edge"}]
        d = validate_dossier(obj)
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertEqual(d.verdict.confidence, Confidence.low)  # not the old high
        self.assertIn("skeptic", d.verdict.abstain_reason)
        self.assertIn("edge0", d.verdict.abstain_reason)
        self.assertTrue(d.hunks)              # evidence preserved, not dropped
        self.assertTrue(d.call_path.edges)

    def test_skeptic_unverifiable_keeps_strong(self):
        # `unverifiable` (a searchfox hole) is advisory, not a veto.
        obj = copy.deepcopy(_dossier())
        obj["skeptic"] = [{"claim_ref": "edge0", "status": "unverifiable", "note": "hole"}]
        d = validate_dossier(obj)
        self.assertEqual(d.verdict.decision, Decision.strong_evidence)

    def test_skeptic_fail_with_malformed_citation_still_vetoes(self):
        # A `fail` carrying a malformed citation must NOT be dropped: the veto keys
        # on status, so the strong-evidence verdict is still downgraded.
        obj = copy.deepcopy(_dossier())
        obj["skeptic"] = [{
            "claim_ref": "edge0", "status": "fail",
            "citations": [{"kind": "searchfox", "permalink": "x"}],  # missing symbol_id/repo
        }]
        d = parse_and_validate(obj)
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertIn("skeptic", d.verdict.abstain_reason)

    def test_skeptic_fail_survives_salvage_path(self):
        # Force parse_and_validate's salvage path (an uncited call edge) while a
        # failing skeptic result is present: the veto must still fire on the
        # Dossier(**kwargs) rebuild rather than being bypassed.
        obj = copy.deepcopy(_dossier())
        obj["call_path"]["edges"].append({"caller_symbol": "X", "callee_symbol": "Y"})
        obj["skeptic"] = [{"claim_ref": "edge0", "status": "fail"}]
        d = parse_and_validate(obj)
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertIn("skeptic", d.verdict.abstain_reason)

    def test_abstain_requires_reason(self):
        obj = copy.deepcopy(_dossier())
        obj["verdict"] = {"decision": "abstain", "confidence": "low"}
        with self.assertRaises(ValidationError):
            validate_dossier(obj)

    def test_abstain_forbids_needinfo(self):
        obj = copy.deepcopy(_dossier())
        obj["verdict"] = {
            "decision": "abstain",
            "abstain_reason": "not enough evidence",
            "needinfo_draft": "hi",
        }
        with self.assertRaises(ValidationError):
            validate_dossier(obj)

    def test_abstain_valid(self):
        obj = copy.deepcopy(_dossier())
        obj["verdict"] = {"decision": "abstain", "abstain_reason": "not enough evidence"}
        d = validate_dossier(obj)
        self.assertEqual(d.verdict.decision, Decision.abstain)


class TestRoleFragments(unittest.TestCase):
    def test_call_graph_explorer_fragment(self):
        frag = validate_role_fragment(
            "call-graph-explorer",
            {"edges": [{"caller_symbol": "A", "callee_symbol": "B",
                        "via": "calls-from", "citations": [_searchfox()]}]},
        )
        self.assertEqual(len(frag.edges), 1)

    def test_patch_scout_fragment_is_list(self):
        frags = validate_role_fragment(
            "patch-scout",
            [{"node": "0123456789ab", "filename": "f.cpp",
              "lines": [_diff_line()], "citations": [_diff_line()]}],
        )
        self.assertEqual(len(frags), 1)

    def test_patch_scout_uncited_raises(self):
        with self.assertRaises(ValidationError):
            validate_role_fragment(
                "patch-scout",
                [{"node": "0123456789ab", "filename": "f.cpp", "citations": []}],
            )

    def test_unknown_role_raises(self):
        with self.assertRaises(ValueError):
            validate_role_fragment("nope", {})


class TestDbJson(unittest.TestCase):
    def test_round_trip(self):
        d = validate_dossier(_dossier())
        j = dossier_to_db_json(d)
        # JSON-serializable
        json.dumps(j)
        self.assertIn("schema_version", j)
        self.assertEqual(dossier_from_db_json(j), d)

    def test_future_version_rejected(self):
        j = dossier_to_db_json(validate_dossier(_dossier()))
        j["schema_version"] = j["schema_version"] + 999
        with self.assertRaises(ValueError):
            dossier_from_db_json(j)


class TestDataFlowOperationFreeform(unittest.TestCase):
    """Regression: a live run emitted data_flow.operation='null_deref', which a
    narrow Literal rejected -> false-abstain masking a valid dossier. operation is
    a free-form label now (citations are the grounding gate)."""

    def test_role_fragment_accepts_arbitrary_operation(self):
        frag = validate_role_fragment(
            "data-flow-tracer",
            {"summary": "s", "object_name": "o", "operation": "null_deref",
             "citations": [_searchfox()]},
        )
        self.assertEqual(frag.operation, "null_deref")

    def test_full_dossier_accepts_arbitrary_operation(self):
        obj = copy.deepcopy(_dossier())
        obj["data_flow"]["operation"] = "double_free"
        d = validate_dossier(obj)
        self.assertEqual(d.data_flow.operation, "double_free")


if __name__ == "__main__":
    unittest.main()
