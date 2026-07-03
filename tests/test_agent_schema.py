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

    def test_uncited_claim_abstains_but_validate_raises(self):
        obj = copy.deepcopy(_dossier())
        obj["call_path"]["edges"][0]["citations"] = []
        text = "prose\n```json\n" + json.dumps(obj) + "\n```\n"
        # parse_and_validate must NEVER raise -> abstain
        d = parse_and_validate(text)
        self.assertEqual(d.verdict.decision, Decision.abstain)
        # validate_dossier on the same object DOES raise
        with self.assertRaises(ValidationError):
            validate_dossier(obj)

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

    def test_strong_without_mechanism_raises(self):
        obj = copy.deepcopy(_dossier())
        v = self._verdict()
        v.pop("mechanism")
        obj["verdict"] = v
        with self.assertRaises(ValidationError):
            validate_dossier(obj)

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


if __name__ == "__main__":
    unittest.main()
