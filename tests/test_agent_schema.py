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
    CrashBrief,
    CrashFrame,
    Decision,
    FailureClass,
    RefCitation,
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

    def test_blame_side_normalizes_to_context(self):
        # The live ab3238a5 false-abstain: the model labels a blame-sourced diff line's
        # side "history_blame" (invalid), and one such citation in verdict.mechanism
        # force-abstains an otherwise-correct lead via salvage. It must map to context.
        adapter = TypeAdapter(Citation)
        for spelling in ("history_blame", "history-blame", "blame", "history"):
            raw = _diff_line()
            raw["side"] = spelling
            with self.assertRaises(ValidationError):  # invalid pre-normalize
                adapter.validate_python(raw)
            cit = adapter.validate_python(_normalize_citations(raw))
            self.assertEqual(cit.side, "context")

    def test_lead_with_blame_side_survives_as_lead(self):
        # End-to-end: a lead whose mechanism cites a `history_blame` line must remain a
        # lead through parse_and_validate, not get salvaged to abstain.
        sf = _stack_frame()
        dl = _diff_line()
        dl["side"] = "history_blame"
        obj = copy.deepcopy(_dossier())
        obj["verdict"] = {
            "decision": "lead",
            "confidence": "medium",
            "needinfo_draft": "could you take a look?",
            "mechanism": {"statement": "stale flag -> null deref", "citations": [sf, dl]},
            "consistency": {"statement": "matches the crash path", "citations": [sf]},
        }
        d = parse_and_validate(obj)
        self.assertEqual(d.verdict.decision, Decision.lead)

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

    def test_lead_high_clamped_to_probable(self):
        # Worth-investigating pivot: a lead may self-assert up to `probable`; only `high`
        # (a fully-verified/corroborated badge) is reserved, so a lead's high clamps one
        # notch to probable (not all the way to medium).
        obj = copy.deepcopy(_dossier())
        obj["skeptic"] = []
        obj["verdict"] = {
            "decision": "lead", "confidence": "high",
            "mechanism": {"statement": "maybe", "citations": [_diff_line()]},
        }
        d = validate_dossier(obj)
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.verdict.confidence, Confidence.probable)

    def test_lead_probable_is_allowed(self):
        # A model-asserted lead+probable now STANDS (a strong worth-investigating estimate),
        # rather than being clamped to medium.
        obj = copy.deepcopy(_dossier())
        obj["skeptic"] = []
        obj["verdict"] = {
            "decision": "lead", "confidence": "probable",
            "mechanism": {"statement": "maybe", "citations": [_diff_line()]},
        }
        d = validate_dossier(obj)
        self.assertEqual(d.verdict.confidence, Confidence.probable)

    def test_lead_high_clamp_independent_of_floor(self):
        # The lead 'high -> probable' clamp is fixed, not tied to the tunable strong-evidence
        # floor: a retuned floor must not let a lead wear the reserved `high` badge.
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
        self.assertEqual(d.verdict.confidence, Confidence.probable)

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

    def test_skeptic_fail_on_lead_abstains(self):
        # Worth-investigating pivot: the skeptic is the NOISE guardrail — a `fail` on a
        # model-emitted lead demotes it to abstain, so we never push a lead the skeptic
        # flagged as noise/unrelated (the teeth the reoriented skeptic needs on leads).
        obj = copy.deepcopy(_dossier())
        obj["verdict"] = {
            "decision": "lead", "confidence": "probable",
            "needinfo_draft": "could you take a look?",
            "mechanism": {"statement": "maybe", "citations": [_diff_line()]},
        }
        obj["skeptic"] = [{"claim_ref": "candidate", "status": "fail", "note": "unrelated"}]
        d = validate_dossier(obj)
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertIn("noise", d.verdict.abstain_reason)

    def test_skeptic_unverifiable_keeps_lead(self):
        # `unverifiable` (a searchfox hole / cannot-confirm, NOT a contradiction) must NOT
        # demote a lead — a credible-but-unproven clue is exactly what we want to surface.
        obj = copy.deepcopy(_dossier())
        obj["verdict"] = {
            "decision": "lead", "confidence": "probable",
            "needinfo_draft": "take a look?",
            "mechanism": {"statement": "maybe", "citations": [_diff_line()]},
        }
        obj["skeptic"] = [{"claim_ref": "edge0", "status": "unverifiable", "note": "sf hole"}]
        d = validate_dossier(obj)
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.verdict.confidence, Confidence.probable)

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


class TestHunkHeaderSideNormalizes(unittest.TestCase):
    """A hunk-header / structural diff line is labelled ``side:"meta"`` (also
    "header"/"hunk") by the model. That is not a valid added/deleted/context token, so one
    such citation in a verdict's mechanism/consistency would force-abstain an otherwise
    correct lead via ``_salvage`` (a schema false-abstain seen during canary validation).
    It must normalize to ``context`` (a valid, non-behavior-asserting pointer)."""

    def test_meta_side_normalizes_to_context(self):
        adapter = TypeAdapter(Citation)
        for spelling in ("meta", "header", "hunk", "hunk_header", "hunk-header", "META"):
            raw = _diff_line()
            raw["side"] = spelling
            with self.assertRaises(ValidationError):  # invalid pre-normalize
                adapter.validate_python(raw)
            cit = adapter.validate_python(_normalize_citations(raw))
            self.assertEqual(cit.side, "context")

    def test_lead_with_meta_side_survives_as_lead(self):
        # End-to-end: a lead whose mechanism cites a `side:"meta"` hunk-header line must
        # stay a lead through parse_and_validate, not be salvaged to a false abstain.
        sf = _stack_frame()
        dl = _diff_line()
        dl["side"] = "meta"
        obj = copy.deepcopy(_dossier())
        obj["verdict"] = {
            "decision": "lead",
            "confidence": "medium",
            "needinfo_draft": "could you take a look?",
            "mechanism": {"statement": "hunk touches the faulting field",
                          "citations": [sf, dl]},
            "consistency": {"statement": "matches the crash path", "citations": [sf]},
        }
        d = parse_and_validate(obj)
        self.assertEqual(d.verdict.decision, Decision.lead)


class TestOpaqueFrameNoneCoercion(unittest.TestCase):
    """Symbolication can emit a ``null`` function/filename for an opaque frame (macOS
    ``os_unfair_lock``, JIT/stub frames). Those are ``str`` fields, so a literal ``None``
    would fail validation and — because ``CrashBrief.frames`` validates as a whole in
    ``_salvage`` — drop the ENTIRE crash brief and force a false abstain. ``None`` must
    coerce to ``""`` so the frame is kept (empty) and the crash context survives."""

    def test_crashframe_none_fields_coerce_to_empty(self):
        f = CrashFrame(stackpos=3, function=None, filename=None, node=None)
        self.assertEqual((f.function, f.filename, f.node), ("", "", ""))

    def test_crashbrief_with_opaque_null_frame_validates(self):
        brief = CrashBrief.model_validate({
            "uuid": "u-1",
            "frames": [
                {"stackpos": 0, "function": "Foo::Bar", "filename": "foo.cpp", "line": 4},
                {"stackpos": 1, "function": "os_unfair_lock", "filename": None},
            ],
        })
        self.assertEqual(len(brief.frames), 2)
        self.assertEqual(brief.frames[1].filename, "")

    def test_dossier_with_opaque_null_frame_keeps_crash(self):
        # Before the coercion this null-filename frame made CrashBrief.model_validate
        # raise, so _salvage dropped the whole `crash` sub-object. It must survive now.
        obj = copy.deepcopy(_dossier())
        obj["crash"]["frames"] = [
            {"stackpos": 0, "function": "os_unfair_lock", "filename": None},
        ]
        d = validate_dossier(obj)
        self.assertIsNotNone(d.crash)
        self.assertEqual(d.crash.frames[0].filename, "")


class TestNodelessStackFrameCitation(unittest.TestCase):
    """A cited stack frame legitimately has no changeset when the frame is not
    attributable — ``CrashFrame.node`` above is already ``""`` for exactly that case.
    Required, ``StackFrameCitation.node`` was a whole-dossier grenade: on prod dossier 5748
    the model omitted it on two frames and ``_salvage`` binned the verdict, replacing a
    correct analysis with "dossier validation failed (verdict unusable)".

    The NESTING is the point of these tests. A top-level ``StackFrameCitation(...)`` check
    passes while the bug survives, because the damage happens where the citation sits: in
    the verdict's own claims (whole verdict dropped -> FALSE ABSTAIN) and in a call-path
    edge (that edge dropped, losing the lead's anchor)."""

    @staticmethod
    def _nodeless(**over):
        sf = _stack_frame()
        sf.pop("node")
        sf.update(over)
        return sf

    def test_citation_without_node_defaults_to_empty(self):
        c = StackFrameCitation(uuid="u-1", stackpos=3, filename="foo.cpp",
                               function="Foo::Bar", line=42)
        self.assertEqual(c.node, "")

    def test_nodeless_citation_in_consistency_keeps_the_verdict(self):
        # The literal 5748 shape: two nodeless frames on the consistency claim.
        obj = copy.deepcopy(_dossier())
        obj["verdict"]["consistency"]["citations"] = [
            self._nodeless(), self._nodeless(stackpos=1, function="Foo::Baz"),
        ]
        d = parse_and_validate(obj)
        self.assertEqual(d.verdict.decision, Decision.strong_evidence)
        self.assertIsNone(d.verdict.abstain_reason)
        self.assertEqual([c.node for c in d.verdict.consistency.citations], ["", ""])

    def test_nodeless_citation_in_mechanism_keeps_the_verdict(self):
        obj = copy.deepcopy(_dossier())
        obj["verdict"]["mechanism"]["citations"] = [self._nodeless()]
        d = parse_and_validate(obj)
        self.assertEqual(d.verdict.decision, Decision.strong_evidence)
        self.assertIsNone(d.verdict.abstain_reason)

    def test_nodeless_citation_in_call_path_edge_keeps_the_edge(self):
        # The other nesting that blew up. A dropped edge is quieter than a dropped verdict
        # but costs a lead its cited anchor, which `_skeptic_veto` demotes to abstain.
        obj = copy.deepcopy(_dossier())
        obj["call_path"]["edges"][0]["citations"] = [self._nodeless()]
        d = parse_and_validate(obj)
        self.assertEqual(len(d.call_path.edges), 1)
        self.assertEqual(d.verdict.decision, Decision.strong_evidence)

    def test_nodeless_citation_round_trips_through_db_json(self):
        obj = copy.deepcopy(_dossier())
        obj["verdict"]["consistency"]["citations"] = [self._nodeless()]
        back = dossier_from_db_json(dossier_to_db_json(parse_and_validate(obj)))
        self.assertEqual(back.verdict.consistency.citations[0].node, "")

    def test_control_this_nesting_position_really_is_fatal(self):
        # Guard against a vacuous suite: prove a claim-level failure in this exact position
        # DOES bin the whole verdict, so the assertions above are actually load-bearing.
        # Uses the min-citations rule rather than another required field, so a later
        # relaxation of some other field can't quietly turn this control into a no-op.
        obj = copy.deepcopy(_dossier())
        obj["verdict"]["consistency"]["citations"] = []
        d = parse_and_validate(obj)
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertIn("validation failed", d.verdict.abstain_reason)


class TestStackFrameCitationNullTolerance(unittest.TestCase):
    """The other half of the ``node`` fix. A ``str`` field REJECTS an explicit ``null``
    even with a default, and symbolication nulls these very fields — so the defaults alone
    recovered 1 lost verdict of 41 across the prod corpus while the coercion recovered 14.
    Nested in a verdict claim each null was a whole-verdict false abstain."""

    def _cited(self, **over):
        sf = _stack_frame()
        sf.update(over)
        obj = copy.deepcopy(_dossier())
        obj["verdict"]["consistency"]["citations"] = [sf]
        return parse_and_validate(obj)

    def test_every_null_field_keeps_the_verdict(self):
        for field in ("uuid", "filename", "function", "node", "line", "stackpos"):
            with self.subTest(field=field):
                d = self._cited(**{field: None})
                self.assertEqual(d.verdict.decision, Decision.strong_evidence)
                self.assertIsNone(d.verdict.abstain_reason)

    def test_null_coerces_to_the_empty_value(self):
        c = StackFrameCitation(uuid=None, stackpos=None, filename=None,
                               function="os_unfair_lock", line=None, node=None)
        self.assertEqual((c.uuid, c.filename, c.node), ("", "", ""))
        self.assertEqual((c.stackpos, c.line), (0, 0))

    def test_the_real_opaque_frame_survives(self):
        # The case the coercion exists for: a JIT/stub frame whose function is all
        # symbolication could recover.
        d = self._cited(uuid=None, filename=None, line=None, node=None,
                        function="os_unfair_lock")
        self.assertEqual(d.verdict.decision, Decision.strong_evidence)

    def test_a_citation_of_nothing_is_still_refused(self):
        # The cost of defaulting everything: `{"kind": "stack_frame"}` would otherwise be a
        # content-free citation that satisfies the min-citations anti-hallucination rule,
        # and would render as "frame #0" — the CRASHING frame — out of thin air.
        with self.assertRaises(ValidationError):
            StackFrameCitation()
        obj = copy.deepcopy(_dossier())
        obj["verdict"]["consistency"]["citations"] = [{"kind": "stack_frame"}]
        d = parse_and_validate(obj)
        self.assertEqual(d.verdict.decision, Decision.abstain)

    def test_stackpos_alone_does_not_count_as_pointing_somewhere(self):
        # stackpos/line both default to 0, so accepting them alone would re-open the hole.
        with self.assertRaises(ValidationError):
            StackFrameCitation(stackpos=3, line=9)


class TestRefCitation(unittest.TestCase):
    """The union had no member for the source/history tools: the agent reads a changeset,
    cites it honestly, and no legal ``kind`` existed. Largest single loss in the prod
    corpus — 22 of 41 destroyed verdicts, still firing 2026-08-04."""

    def _with_kind(self, kind, **over):
        cit = {"kind": kind, "node": "0123456789ab", "filename": "foo.cpp", "line": 7}
        cit.update(over)
        obj = copy.deepcopy(_dossier())
        obj["verdict"]["consistency"]["citations"] = [cit]
        return parse_and_validate(obj)

    def test_invented_kinds_normalize_to_ref_and_keep_the_verdict(self):
        for kind in ("changeset", "source", "source_raw_file", "history",
                     "history_changeset", "source_line", "pinned_source", "ref"):
            with self.subTest(kind=kind):
                d = self._with_kind(kind)
                self.assertEqual(d.verdict.decision, Decision.strong_evidence)
                self.assertEqual(d.verdict.consistency.citations[0].kind, "ref")

    def test_stack_kind_goes_to_stack_frame_not_the_catch_all(self):
        # 5 of the 6 prod `kind:"stack"` citations carry the full StackFrameCitation field
        # set. Routing them to the catch-all validates but throws away function/stackpos/
        # uuid and renders an hg link where the page should name the frame.
        sf = _stack_frame()
        sf["kind"] = "stack"
        obj = copy.deepcopy(_dossier())
        obj["verdict"]["consistency"]["citations"] = [sf]
        d = parse_and_validate(obj)
        cit = d.verdict.consistency.citations[0]
        self.assertIsInstance(cit, StackFrameCitation)
        self.assertEqual((cit.function, cit.stackpos, cit.uuid),
                         ("Foo::Bar", 0, "uuid-1"))

    def test_rev_and_path_are_accepted_as_aliases(self):
        # What the source/history tools' own output calls these things, and the model copies
        # that vocabulary: 21 prod citations name the changeset `rev` and the file `path`
        # and carry no node/filename key at all. This is that exact shape.
        obj = copy.deepcopy(_dossier())
        obj["verdict"]["consistency"]["citations"] = [{
            "kind": "source_raw_file", "rev": "0123456789ab",
            "path": "dom/Foo.cpp", "line": 12, "content": "delete mFoo;",
        }]
        d = parse_and_validate(obj)
        cit = d.verdict.consistency.citations[0]
        self.assertIsInstance(cit, RefCitation)
        self.assertEqual((cit.node, cit.filename), ("0123456789ab", "dom/Foo.cpp"))

    def test_canonical_names_still_win_for_persisted_dossiers(self):
        # The aliases must not REPLACE the field names, or every dossier already persisted
        # with node/filename keys stops validating.
        d = self._with_kind("ref")
        cit = d.verdict.consistency.citations[0]
        self.assertEqual((cit.node, cit.filename), ("0123456789ab", "foo.cpp"))
        back = dossier_from_db_json(dossier_to_db_json(d))
        self.assertEqual(back.verdict.consistency.citations[0].node, "0123456789ab")

    def test_ref_with_only_content_is_accepted(self):
        d = self._with_kind("changeset", node="", filename="", line=0,
                            content="-  delete mFoo;")
        self.assertEqual(d.verdict.decision, Decision.strong_evidence)

    def test_ref_pointing_at_nothing_is_refused(self):
        with self.assertRaises(ValidationError):
            RefCitation()
        obj = copy.deepcopy(_dossier())
        obj["verdict"]["consistency"]["citations"] = [{"kind": "changeset"}]
        d = parse_and_validate(obj)
        self.assertEqual(d.verdict.decision, Decision.abstain)

    def test_an_unknown_kind_still_fails_rather_than_becoming_a_ref(self):
        # The alias map stays PARTIAL on purpose: mapping every unrecognized kind to `ref`
        # would make it total, but that is unmeasured. Pin the current contract so the
        # choice is deliberate the next time someone looks.
        obj = copy.deepcopy(_dossier())
        obj["verdict"]["consistency"]["citations"] = [
            {"kind": "wat", "node": "0123456789ab"},
        ]
        d = parse_and_validate(obj)
        self.assertEqual(d.verdict.decision, Decision.abstain)

    def test_ref_edge_is_kept_but_is_not_a_searchfox_citation(self):
        # A `ref` is deliberately the WEAKEST kind. The SF-3 consequence — that it cannot
        # manufacture the searchfox-verified call path off-stack strong evidence requires —
        # is asserted against the real gate in tests.test_offstack.
        obj = copy.deepcopy(_dossier())
        obj["call_path"]["edges"][0]["citations"] = [
            {"kind": "changeset", "node": "0123456789ab"},
        ]
        d = parse_and_validate(obj)
        self.assertEqual(len(d.call_path.edges), 1)
        cit = d.call_path.edges[0].citations[0]
        self.assertIsInstance(cit, RefCitation)
        self.assertNotIsInstance(cit, SearchfoxCitation)


class TestFailureClassVocabulary(unittest.TestCase):
    """``oom`` is a real Firefox crash family the enum could not say. The model wrote it
    honestly, the enum rejected it, and because ``CrashBrief`` validates whole in
    ``_salvage`` that dropped every frame: 21 prod dossiers in a month."""

    def _brief(self, value):
        obj = copy.deepcopy(_dossier())
        obj["crash"]["failure_class"] = value
        return parse_and_validate(obj)

    def test_oom_is_kept(self):
        self.assertEqual(self._brief("oom").crash.failure_class, FailureClass.oom)

    def test_case_is_folded(self):
        self.assertEqual(self._brief("OOM").crash.failure_class, FailureClass.oom)

    def test_unknown_degrades_to_other_instead_of_dropping_the_brief(self):
        for value in ("other:rust_panic", "jit_or_corruption", "", None):
            with self.subTest(value=value):
                d = self._brief(value)
                self.assertIsNotNone(d.crash)
                self.assertEqual(d.crash.failure_class, FailureClass.other)

    def test_degrading_never_asserts_a_mechanism(self):
        # `other` is the non-behaviour-asserting member — an unrecognized class must not
        # be able to masquerade as a real one (the orchestrator's exposer classifier reads
        # this field).
        self.assertNotEqual(FailureClass("jit_or_corruption"), FailureClass.uaf)
        self.assertEqual(FailureClass("jit_or_corruption"), FailureClass.other)


class TestCrashFrameIntCoercion(unittest.TestCase):
    """``_none_to_empty`` stopped one field short of ``line`` — exactly the field
    symbolication nulls (``_stack_text`` shows the model ``#7 None :None``). 177 null lines
    across 89 prod dossiers, the biggest single cause of the 127 dropped crash briefs."""

    def _frames(self, frame):
        obj = copy.deepcopy(_dossier())
        obj["crash"]["frames"] = [
            {"stackpos": 0, "function": "Foo::Bar", "filename": "foo.cpp", "line": 1},
            frame,
        ]
        return parse_and_validate(obj)

    def test_null_line_keeps_the_whole_brief(self):
        d = self._frames({"stackpos": 1, "function": "F", "filename": "f.cpp", "line": None})
        self.assertIsNotNone(d.crash)
        self.assertEqual(len(d.crash.frames), 2)
        self.assertEqual(d.crash.frames[1].line, 0)

    def test_missing_and_null_stackpos_keep_the_brief(self):
        # An INLINED frame has no position of its own; 52 prod frames omitted it.
        for frame in ({"function": "F", "filename": "f.cpp", "line": 2},
                      {"stackpos": None, "function": "F", "filename": "f.cpp"}):
            with self.subTest(frame=frame):
                d = self._frames(frame)
                self.assertIsNotNone(d.crash)
                self.assertEqual(len(d.crash.frames), 2)

    def test_placeholder_strings_are_folded(self):
        for placeholder in ("", "None", "unknown", "?"):
            with self.subTest(placeholder=placeholder):
                d = self._frames({"stackpos": 1, "function": "F", "line": placeholder})
                self.assertIsNotNone(d.crash)
                self.assertEqual(d.crash.frames[1].line, 0)

    def test_real_drift_is_still_rejected(self):
        # The coercion must not become "swallow anything" — a genuinely unparseable value
        # should still surface rather than being silently zeroed.
        d = self._frames({"stackpos": 1, "function": "F", "line": "line forty-two"})
        self.assertIsNone(d.crash)


if __name__ == "__main__":
    unittest.main()
