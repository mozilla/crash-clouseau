# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Backed-out-candidate suppression: a changeset that is no longer in the tree cannot be acted
# on, so naming one costs a triager's attention and returns nothing.
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_backout_gate
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import sigage  # noqa: E402
from crashclouseau.agent import orchestrator as orch  # noqa: E402
from crashclouseau.agent.result import CrashTriageResult  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    Candidate,
    Claim,
    Confidence,
    Decision,
    Dossier,
    SearchfoxCitation,
    Verdict,
    parse_and_validate,
)

_SF = SearchfoxCitation(
    permalink="https://searchfox.org/x#1", symbol_id="_Z1", repo="mozilla-central"
)
_SEED = {"uuid": "u-1", "signature": "S", "channel": "nightly", "stack": "#0 f a:1",
         "is_offstack": False}
_NODE = "507de5c66b0d"
_BACKOUT = "65b7ea25c7db8fd730e59925c3d2438c45a0c5dc"


def _lead(confidence=Confidence.medium, backedout_by=_BACKOUT):
    return Dossier(
        candidate=Candidate(node=_NODE, bug=2046861, backedout_by=backedout_by),
        verdict=Verdict(decision=Decision.lead, confidence=confidence,
                        needinfo_draft="could you take a look?",
                        mechanism=Claim(text="native abort in Run", citations=[_SF])),
    )


class TestBackoutGate(unittest.TestCase):
    def test_a_backed_out_candidate_is_suppressed_to_abstain(self):
        d = _lead()
        orch._apply_backout_gate(d, _SEED)
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertTrue(d.corroborations["candidate_backedout"])
        self.assertTrue(d.corroborations["candidate_backedout_suppressed"])
        self.assertEqual(d.corroborations["candidate_backedout_by"], _BACKOUT)

    def test_the_abstain_is_schema_legal_and_explains_itself(self):
        """An abstain MUST carry a reason and MUST NOT carry a needinfo_draft
        (`Verdict._consistency_rule`), so this cannot be a `model_copy` of the lead."""
        d = _lead()
        orch._apply_backout_gate(d, _SEED)
        self.assertIsNone(d.verdict.needinfo_draft)
        self.assertIn(_NODE, d.verdict.abstain_reason)
        self.assertIn(_BACKOUT[:12], d.verdict.abstain_reason)
        self.assertIn("nothing to act on", d.verdict.abstain_reason)
        # kept, so the page can still show WHAT was found before it was dropped
        self.assertIsNotNone(d.verdict.mechanism)

    def test_the_worth_investigating_badge_does_not_ride_along(self):
        d = _lead(Confidence.probable)
        d.verdict = d.verdict.model_copy(update={"p_worth_investigating": 0.9714})
        orch._apply_backout_gate(d, _SEED)
        self.assertIsNone(d.verdict.p_worth_investigating)

    def test_every_rung_is_suppressed_not_clamped(self):
        """Unlike the stale-signature downweight this is not a confidence question: rung 25 has
        nowhere lower to go, and 14 of the 17 measured cases sat at 25 or 50."""
        for conf in (Confidence.low, Confidence.medium, Confidence.probable):
            d = _lead(conf)
            orch._apply_backout_gate(d, _SEED)
            self.assertEqual(d.verdict.decision, Decision.abstain, conf.value)

    def test_strong_evidence_is_suppressed_too(self):
        d = Dossier(
            candidate=Candidate(node=_NODE, backedout_by=_BACKOUT),
            verdict=Verdict(
                decision=Decision.strong_evidence, confidence=Confidence.high,
                needinfo_draft="this is the regressor",
                mechanism=Claim(text="m", citations=[_SF]),
                consistency=Claim(text="c", citations=[_SF]),
            ),
        )
        orch._apply_backout_gate(d, _SEED)
        self.assertEqual(d.verdict.decision, Decision.abstain)

    def test_a_clean_candidate_is_untouched(self):
        d = _lead(backedout_by="")
        orch._apply_backout_gate(d, _SEED)
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertNotIn("candidate_backedout", d.corroborations or {})

    def test_the_models_own_backedout_flag_is_never_a_trigger(self):
        """`candidate.backedout` is LLM-supplied and means "is itself a backout commit" in the
        seed. Only our own hg lookup may suppress a verdict."""
        d = Dossier(
            candidate=Candidate(node=_NODE, backedout=True, backedout_by=""),
            verdict=Verdict(decision=Decision.lead, confidence=Confidence.medium,
                            needinfo_draft="?"),
        )
        orch._apply_backout_gate(d, _SEED)
        self.assertEqual(d.verdict.decision, Decision.lead)

    def test_an_existing_abstain_is_flagged_but_its_reason_is_kept(self):
        d = Dossier(
            candidate=Candidate(node=_NODE, backedout_by=_BACKOUT),
            verdict=Verdict(decision=Decision.abstain, confidence=Confidence.low,
                            abstain_reason="the skeptic vetoed the chain"),
        )
        orch._apply_backout_gate(d, _SEED)
        self.assertEqual(d.verdict.abstain_reason, "the skeptic vetoed the chain")
        self.assertTrue(d.corroborations["candidate_backedout"])
        self.assertNotIn("candidate_backedout_suppressed", d.corroborations)

    def test_never_raises_on_a_missing_verdict_or_candidate(self):
        orch._apply_backout_gate(Dossier(verdict=None), _SEED)
        orch._apply_backout_gate(Dossier(candidate=None), _SEED)
        orch._apply_backout_gate(None, _SEED)


class TestBackoutResolver(unittest.TestCase):
    def test_it_asks_hg_with_the_seeds_channel_not_the_candidates(self):
        """`candidate.channel` is a model-supplied field and is empty on every dossier; an empty
        channel makes json_rev skip the request and cache {}, poisoning the run."""
        d = Dossier(candidate=Candidate(node=_NODE, channel=""),
                    verdict=Verdict(decision=Decision.lead, confidence=Confidence.medium,
                                    needinfo_draft="?"))
        with mock.patch.object(sigage, "backedout_by_for_node",
                               return_value=_BACKOUT) as f:
            orch._resolve_candidate_backout(d, _SEED)
        f.assert_called_once_with(_NODE, "nightly")
        self.assertEqual(d.candidate.backedout_by, _BACKOUT)

    def test_a_clean_lookup_leaves_the_field_empty(self):
        d = _lead(backedout_by="")
        with mock.patch.object(sigage, "backedout_by_for_node", return_value=""):
            orch._resolve_candidate_backout(d, _SEED)
        self.assertEqual(d.candidate.backedout_by, "")

    def test_an_unknown_lookup_never_suppresses(self):
        """Tri-state: None means "we could not find out", which must not read as backed out."""
        d = _lead(backedout_by="")
        with mock.patch.object(sigage, "backedout_by_for_node", return_value=None):
            orch._resolve_candidate_backout(d, _SEED)
        orch._apply_backout_gate(d, _SEED)
        self.assertEqual(d.candidate.backedout_by, "")
        self.assertEqual(d.verdict.decision, Decision.lead)

    def test_a_raising_lookup_is_swallowed(self):
        d = _lead(backedout_by="")
        with mock.patch.object(sigage, "backedout_by_for_node",
                               side_effect=RuntimeError("hg down")):
            orch._resolve_candidate_backout(d, _SEED)   # must not raise
        self.assertEqual(d.candidate.backedout_by, "")

    def test_an_already_resolved_candidate_is_not_looked_up_again(self):
        d = _lead()
        with mock.patch.object(sigage, "backedout_by_for_node") as f:
            orch._resolve_candidate_backout(d, _SEED)
        f.assert_not_called()


class TestThroughTheGates(unittest.TestCase):
    def _result(self, dossier):
        return CrashTriageResult(num_turns=1, total_cost_usd=0.1, result="ok", dossier=dossier)

    def test_suppression_reaches_the_shipped_verdict_row(self):
        r = self._result(_lead(Confidence.medium))
        orch.apply_deterministic_gates(r, _SEED)
        self.assertEqual(r.dossier.verdict.decision, Decision.abstain)
        # the raw verdict still records what the agent actually concluded
        self.assertEqual(r.dossier.raw_verdict.decision, Decision.lead)
        self.assertEqual(orch._verdict_row(r)["verdict"], "abstain")

    def test_no_needinfo_action_survives_the_suppression(self):
        r = self._result(_lead(Confidence.probable))
        orch.apply_deterministic_gates(r, _SEED)
        self.assertEqual(
            [a for a in (r.actions or []) if a.get("type") == "bugzilla.add_comment"], []
        )

    def test_it_gets_the_last_word_over_a_corroboration_bump(self):
        from crashclouseau.agent.schema import DataFlowHypothesis, StructLayoutCitation
        d = Dossier(
            candidate=Candidate(node=_NODE, backedout_by=_BACKOUT),
            data_flow=DataFlowHypothesis(
                summary="null-deref of mLength", operation="null",
                citations=[StructLayoutCitation(type_name="T", field="mLength", offset=8)],
            ),
            verdict=Verdict(decision=Decision.lead, confidence=Confidence.medium,
                            needinfo_draft="?"),
        )
        r = self._result(d)
        orch.apply_deterministic_gates(
            r, {**_SEED, "raw_crash": {"json_dump": {"crash_info": {"address": "0x8"}}}})
        self.assertTrue(r.dossier.corroborations["fault_address_offset_match"])
        self.assertEqual(r.dossier.verdict.decision, Decision.abstain)

    def test_the_offline_eval_is_unaffected(self):
        """The resolver is online-only, so an eval dossier never carries `backedout_by` and the
        gate cannot fire — which is what keeps `apply_deterministic_gates` network-free."""
        r = self._result(_lead(Confidence.probable, backedout_by=""))
        orch.apply_deterministic_gates(r, {**_SEED})
        self.assertEqual(r.dossier.verdict.decision, Decision.lead)
        self.assertNotIn("candidate_backedout", r.dossier.corroborations or {})


class TestSecondOpinionIsNotBought(unittest.TestCase):
    def test_a_backed_out_candidate_skips_the_paid_pass(self):
        r = CrashTriageResult(num_turns=1, total_cost_usd=0.1, result="ok", dossier=_lead())
        with mock.patch.object(orch.config, "get_agent_second_opinion",
                               return_value={"enabled": True, "min_confidence": 25,
                                             "min_boost_confidence": 50}):
            so, status = orch._maybe_run_second_opinion(r, _SEED)
        self.assertIsNone(so)
        self.assertEqual(status, "skipped_backedout")


class TestModelCannotInjectIt(unittest.TestCase):
    def test_backedout_by_is_stripped_from_the_handoff(self):
        """The field suppresses a verdict outright, so a model that could set one could silence
        its own report."""
        d = parse_and_validate({
            "candidate": {"node": _NODE, "backedout_by": _BACKOUT},
            "verdict": {"decision": "lead", "confidence": "medium", "needinfo_draft": "?"},
        })
        self.assertEqual(d.candidate.backedout_by, "")
        self.assertEqual(d.verdict.decision, Decision.lead)


class TestSigageLookup(unittest.TestCase):
    def _rev(self, payload):
        return mock.patch.object(sigage, "json_rev", return_value=payload)

    def test_the_backout_sha_when_hg_reports_one(self):
        with self._rev({"node": _NODE, "backedoutby": _BACKOUT}):
            self.assertEqual(sigage.backedout_by_for_node(_NODE), _BACKOUT)

    def test_empty_string_when_hg_says_it_is_clean(self):
        """A clean changeset simply has NO `backedoutby` key — which must be distinguishable
        from a failed lookup, or every unresolvable node would be treated as clean."""
        with self._rev({"node": _NODE, "pushdate": [1785232342, 0]}):
            self.assertEqual(sigage.backedout_by_for_node(_NODE), "")
        with self._rev({"node": _NODE, "backedoutby": ""}):
            self.assertEqual(sigage.backedout_by_for_node(_NODE), "")

    def test_none_when_we_could_not_find_out(self):
        with self._rev({}):                       # 404 / timeout / empty channel
            self.assertIsNone(sigage.backedout_by_for_node(_NODE))

    def test_it_rides_the_shared_json_rev_cache(self):
        """Free by construction: the same cached request already serves pushdate + git sha."""
        payload = {"node": _NODE, "backedoutby": _BACKOUT,
                   "pushdate": [1785232342, 0], "git_commit": "20b5fa4084ac"}
        with mock.patch.object(sigage, "json_rev", return_value=payload) as f:
            self.assertEqual(sigage.backedout_by_for_node(_NODE, "nightly"), _BACKOUT)
            self.assertEqual(sigage.git_commit_for_node(_NODE, "nightly"), "20b5fa4084ac")
        self.assertEqual([c.args for c in f.call_args_list],
                         [(_NODE, "nightly"), (_NODE, "nightly")])


if __name__ == "__main__":
    unittest.main()
