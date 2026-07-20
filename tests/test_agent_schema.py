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
    SkepticStatus,
    SearchfoxCitation,
    DiffLineCitation,
    StackFrameCitation,
    StructLayoutCitation,
    _normalize_citations,
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


def _struct_layout():
    return {
        "kind": "struct_layout",
        "type_name": "mozilla::detail::nsTStringRepr",
        "field": "mLength",
        "offset": 8,
        "repo": "mozilla-central",
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
            (_struct_layout(), StructLayoutCitation, "struct_layout"),
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


class TestCitationNormalization(unittest.TestCase):
    """The model routinely writes prose citation spellings ("stack-frame" with a
    hyphen; a "removed" diff line) instead of the schema token. A live run had a
    genuine, fully-cited LEAD downgraded to a FALSE abstain because such spellings in
    the verdict's own mechanism/consistency citations made salvage drop the verdict.
    parse_and_validate now normalizes these unambiguous variants at the parse boundary
    (spelling only — never inventing a citation)."""

    def test_normalize_maps_variants_recursively(self):
        obj = {
            "a": {"kind": "stack-frame", "side": None},
            "b": [{"kind": "diff-line", "side": "removed"},
                  {"kind": "SearchFox"}],
            "c": {"phc_kind": "stack-frame"},  # NOT a citation field -> untouched
        }
        _normalize_citations(obj)
        self.assertEqual(obj["a"]["kind"], "stack_frame")
        self.assertEqual(obj["b"][0]["kind"], "diff_line")
        self.assertEqual(obj["b"][0]["side"], "deleted")
        self.assertEqual(obj["b"][1]["kind"], "searchfox")   # case-insensitive
        self.assertEqual(obj["c"]["phc_kind"], "stack-frame")  # left alone

    def test_normalize_passes_through_unknown_and_canonical(self):
        obj = {"kind": "bogus", "side": "sideways"}
        _normalize_citations(obj)
        self.assertEqual(obj["kind"], "bogus")   # unknown -> unchanged (still invalid)
        self.assertEqual(obj["side"], "sideways")

    def test_hyphen_stack_frame_citation_validates(self):
        adapter = TypeAdapter(Citation)
        raw = _stack_frame()
        raw["kind"] = "stack-frame"
        with self.assertRaises(ValidationError):   # raw variant is invalid pre-normalize
            adapter.validate_python(raw)
        self.assertIsInstance(
            adapter.validate_python(_normalize_citations(raw)), StackFrameCitation
        )

    def test_field_layout_alias_normalizes_to_struct_layout(self):
        adapter = TypeAdapter(Citation)
        for spelling in ("field-layout", "field_layout", "struct-layout"):
            raw = _struct_layout()
            raw["kind"] = spelling
            self.assertIsInstance(
                adapter.validate_python(_normalize_citations(raw)),
                StructLayoutCitation,
            )

    def test_lead_with_hyphen_and_removed_spellings_survives(self):
        # The exact live-run failure mode: a lead whose mechanism/consistency (and
        # supporting evidence) cite "stack-frame"/"removed" must SURVIVE as a lead, not
        # get salvaged to abstain.
        sf = _stack_frame()
        sf["kind"] = "stack-frame"
        dl = _diff_line()
        dl["side"] = "removed"
        obj = copy.deepcopy(_dossier())
        obj["verdict"] = {
            "decision": "lead",
            "confidence": "medium",
            "needinfo_draft": "could you take a look?",
            "mechanism": {"statement": "stale ptr UAF", "citations": [sf, dl]},
            "consistency": {"statement": "landed+reverted before build",
                            "citations": [dl]},
        }
        obj["hunks"][0]["citations"] = [copy.deepcopy(dl)]
        d = parse_and_validate(obj)
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertTrue(d.verdict.mechanism and d.verdict.mechanism.citations)
        self.assertTrue(d.verdict.consistency and d.verdict.consistency.citations)
        self.assertEqual(len(d.hunks), 1)   # supporting hunk kept, not dropped

    def test_lead_with_variant_citations_survives_the_salvage_path(self):
        # Regression lock for the LOAD-BEARING invariant (parse_and_validate normalizes
        # obj IN PLACE, so the _salvage fallback sees the normalized citations too — see
        # the schema.py comment). The exact live bug went THROUGH salvage: the bad
        # spellings made validate_dossier fail, and salvage dropped the whole verdict.
        # Here the verdict citations use "stack-frame"/"removed" AND an unrelated uncited
        # edge forces validate_dossier to fail -> salvage runs; the lead must SURVIVE
        # (not collapse to a false abstain). Without normalization reaching salvage the
        # verdict would be dropped.
        sf = _stack_frame()
        sf["kind"] = "stack-frame"
        dl = _diff_line()
        dl["side"] = "removed"
        obj = copy.deepcopy(_dossier())
        obj["verdict"] = {
            "decision": "lead",
            "confidence": "medium",
            "needinfo_draft": "could you take a look?",
            "mechanism": {"statement": "stale ptr UAF", "citations": [sf, dl]},
            "consistency": {"statement": "landed+reverted before build",
                            "citations": [dl]},
        }
        obj["call_path"]["edges"][0]["citations"] = []  # uncited edge -> forces salvage
        d = parse_and_validate(obj)
        self.assertEqual(d.verdict.decision, Decision.lead)   # verdict survived salvage
        self.assertTrue(d.verdict.mechanism and d.verdict.mechanism.citations)
        self.assertEqual(len(d.call_path.edges), 0)           # proves salvage actually ran

    def test_role_fragment_normalizes_before_validation(self):
        # validate_role_fragment shares the same citation-spelling fix. Use a
        # call-graph-explorer (CallPath) fragment, whose edge citations are strictly
        # typed (list[Citation]) — a "stack-frame" spelling there fails without it.
        sf = _stack_frame()
        sf["kind"] = "stack-frame"
        frag = {"edges": [{"caller_symbol": "A", "callee_symbol": "B",
                           "via": "calls-from", "citations": [sf]}],
                "to_symbol": "B"}
        res = validate_role_fragment("call-graph-explorer", frag)
        self.assertEqual(len(res.edges), 1)
        self.assertEqual(res.edges[0].citations[0].kind, "stack_frame")


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

    def test_skeptic_fail_downgrades_to_lead(self):
        # A skeptic `fail` no longer collapses to abstain when a cited anchor
        # (candidate/hunk/edge) stands: the ladder downgrades to a LEAD in place,
        # capping confidence at medium and keeping the evidence.
        obj = copy.deepcopy(_dossier())
        obj["skeptic"] = [{"claim_ref": "edge0", "status": "fail", "note": "no edge"}]
        d = validate_dossier(obj)
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.verdict.confidence, Confidence.medium)
        self.assertTrue(d.hunks)              # evidence preserved, not dropped
        self.assertTrue(d.call_path.edges)
        self.assertTrue(d.candidate.node)
        # the stale assertive draft is replaced by a soft one naming the candidate
        self.assertNotEqual(d.verdict.needinfo_draft, "please confirm")
        self.assertIn(d.candidate.node, d.verdict.needinfo_draft)

    def test_skeptic_fail_no_anchor_abstains(self):
        # With NO cited anchor (no candidate, no hunks, no cited edge), a skeptic
        # `fail` still collapses to abstain — nothing to hand a human.
        obj = copy.deepcopy(_dossier())
        for k in ("candidate", "hunks", "call_path", "data_flow"):
            obj.pop(k, None)
        obj["skeptic"] = [{"claim_ref": "mechanism", "status": "fail"}]
        d = validate_dossier(obj)
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertIn("skeptic", d.verdict.abstain_reason)

    def test_skeptic_unverifiable_keeps_strong(self):
        # `unverifiable` (a searchfox hole) is advisory, not a downgrade trigger.
        obj = copy.deepcopy(_dossier())
        obj["skeptic"] = [{"claim_ref": "edge0", "status": "unverifiable", "note": "hole"}]
        d = validate_dossier(obj)
        self.assertEqual(d.verdict.decision, Decision.strong_evidence)

    def test_skeptic_fail_with_malformed_citation_still_downgrades(self):
        # A `fail` carrying a malformed citation must NOT be dropped: the ladder keys
        # on status, so the verdict is still downgraded (to a lead — anchor stands).
        obj = copy.deepcopy(_dossier())
        obj["skeptic"] = [{
            "claim_ref": "edge0", "status": "fail",
            "citations": [{"kind": "searchfox", "permalink": "x"}],  # missing symbol_id/repo
        }]
        d = parse_and_validate(obj)
        self.assertEqual(d.verdict.decision, Decision.lead)

    def test_skeptic_fail_survives_salvage_path(self):
        # Force parse_and_validate's salvage path (an uncited call edge) while a
        # failing skeptic result is present: the ladder must still fire on the
        # Dossier(**kwargs) rebuild (candidate/hunk anchor survives -> lead).
        obj = copy.deepcopy(_dossier())
        obj["call_path"]["edges"].append({"caller_symbol": "X", "callee_symbol": "Y"})
        obj["skeptic"] = [{"claim_ref": "edge0", "status": "fail"}]
        d = parse_and_validate(obj)
        self.assertEqual(d.verdict.decision, Decision.lead)

    def test_lead_verdict_validates(self):
        # A directly-emitted lead (plausible changeset, unverified) validates and
        # keeps its soft needinfo_draft.
        obj = copy.deepcopy(_dossier())
        obj["skeptic"] = []
        obj["verdict"] = {
            "decision": "lead", "confidence": "medium",
            "needinfo_draft": "could you take a look at this crash?",
            "mechanism": {"statement": "maybe related", "citations": [_diff_line()]},
        }
        d = validate_dossier(obj)
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.verdict.needinfo_draft, "could you take a look at this crash?")

    def test_lead_confidence_clamped_to_medium(self):
        # A lead must never wear a high-confidence badge — clamp an over-claim.
        obj = copy.deepcopy(_dossier())
        obj["skeptic"] = []
        obj["verdict"] = {
            "decision": "lead", "confidence": "high",
            "mechanism": {"statement": "maybe", "citations": [_diff_line()]},
        }
        d = validate_dossier(obj)
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.verdict.confidence, Confidence.medium)

    def test_lead_confidence_clamp_independent_of_floor(self):
        # The clamp is a fixed 'never high' rule, not tied to the tunable floor: a
        # retuned floor must not let a high-confidence lead through.
        from unittest import mock
        obj = copy.deepcopy(_dossier())
        obj["skeptic"] = []
        obj["verdict"] = {
            "decision": "lead", "confidence": "high",
            "mechanism": {"statement": "m", "citations": [_diff_line()]},
        }
        with mock.patch("crashclouseau.config.get_abstain_below_confidence",
                        return_value=0.95):
            d = validate_dossier(obj)
        self.assertEqual(d.verdict.confidence, Confidence.medium)

    def test_lead_without_anchor_abstains(self):
        # A directly-emitted lead with NO cited candidate/hunk/edge is demoted to
        # abstain — there is nothing to hand a human (guards the anchor invariant on
        # the direct-emit path, not just the skeptic-downgrade path).
        obj = copy.deepcopy(_dossier())
        obj["skeptic"] = []
        for k in ("candidate", "hunks", "call_path"):
            obj.pop(k, None)
        obj["verdict"] = {
            "decision": "lead", "confidence": "medium",
            "needinfo_draft": "please look",
            "mechanism": {"statement": "hunch", "citations": [_stack_frame()]},
        }
        d = validate_dossier(obj)
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertIn("anchor", d.verdict.abstain_reason)

    def test_area_experts_roundtrip(self):
        # #15 phase 2: area_experts is deterministic (not Cited) and survives the
        # DB JSON round-trip on any verdict.
        obj = copy.deepcopy(_dossier())
        obj["area_experts"] = [
            {"name": "Alice", "email": "a@m.org", "nick": "al", "node": "0123456789ab",
             "bug": 123, "reason": "authored candidate 0123456789ab (bug 123)"},
        ]
        d = validate_dossier(obj)
        self.assertEqual(d.area_experts[0].email, "a@m.org")
        d2 = dossier_from_db_json(dossier_to_db_json(d))
        self.assertEqual(d2.area_experts[0].name, "Alice")

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

    def test_skeptic_fragment_is_list(self):
        # The skeptic re-verifies EVERY claim in one pass, so its fragment is a list
        # (matching Dossier.skeptic and the role prompt), not a single object.
        frags = validate_role_fragment(
            "skeptic",
            [{"claim_ref": "edge0", "status": "pass", "citations": [_searchfox()]},
             {"claim_ref": "hunk0", "status": "fail", "note": "no such line"}],
        )
        self.assertEqual(len(frags), 2)
        self.assertEqual(frags[0].status, SkepticStatus.passed)
        self.assertEqual(frags[1].status, SkepticStatus.failed)

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
