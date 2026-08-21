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
        # `desc_for_node` too: a clean lookup now CHAINS into the is-a-backout resolver, and
        # an unmocked one would reach live hg from a unit test.
        with mock.patch.object(sigage, "backedout_by_for_node", return_value=""), \
             mock.patch.object(sigage, "desc_for_node", return_value=""):
            orch._resolve_candidate_backout(d, _SEED)
        self.assertEqual(d.candidate.backedout_by, "")

    def test_an_unknown_lookup_never_suppresses(self):
        """Tri-state: None means "we could not find out", which must not read as backed out."""
        d = _lead(backedout_by="")
        with mock.patch.object(sigage, "backedout_by_for_node", return_value=None), \
             mock.patch.object(sigage, "desc_for_node", return_value=""):
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
            r, {**_SEED, "raw_crash": {"json_dump": {"crash_info": {"address": "0x8"}}},
                # The gate fails closed, so without searchfox's answer there is no bump for
                # the backout gate to get the last word over and this passes vacuously.
                "struct_layout": {
                    "fault": 8, "status": "verified", "refuted": [], "unresolved": [],
                    "verified": [{"type": "T", "field": "mLength", "offset": 8,
                                  "actual": "mLength"}]}})
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


# The real prod case, verbatim from hg / the GitHub mirror.
_REVERT = "65b7ea25c7db"
_REVERT_FULL = "65b7ea25c7db8fd730e59925c3d2438c45a0c5dc"
_FIX = "507de5c66b0db1f90560276a5122357810f31605"
_FIX_GIT = "20b5fa4084ac135332b025d3a73066f34a275b9d"
_FIX_DESC = (
    "Bug 2046861 - Reject InferenceSession.create() on a native session-create failure "
    "instead of crashing r=ai-platform-reviewers,valentinp"
)
_REVERT_DESC = (
    'Revert "{}" for causing mochitests leaks failures.\n\n'
    "This reverts commit {}.".format(_FIX_DESC, _FIX_GIT)
)


def _backout_lead(decision=Decision.strong_evidence, confidence=Confidence.high,
                  same_push=""):
    """A verdict naming a changeset that IS ITSELF a backout — the prod shape of
    `00b44d2a-4343-4caa-9e12-907550260802`."""
    return Dossier(
        candidate=Candidate(node=_REVERT, bug=2046861, is_backout=True,
                            backout_of_same_push=same_push),
        verdict=Verdict(
            decision=decision, confidence=confidence,
            needinfo_draft="could you reland it?",
            mechanism=Claim(text="restores MOZ_CRASH at line 408", citations=[_SF]),
            consistency=Claim(text="exact line match", citations=[_SF]),
        ),
    )


class TestIsBackoutGate(unittest.TestCase):
    """The MIRROR predicate. `65b7ea25c7db` has an EMPTY hg `backedoutby` — it IS a backout, it
    was not itself backed out — so `_apply_backout_gate` correctly no-ops on it."""

    def test_the_was_backed_out_gate_really_does_not_see_it(self):
        d = _backout_lead()
        orch._apply_backout_gate(d, _SEED)
        self.assertEqual(d.verdict.decision, Decision.strong_evidence)

    def test_a_net_zero_backout_is_suppressed_to_abstain(self):
        """Fix and revert in the SAME push: no build ever contained the fix, so the tree's
        content never differed and the candidate provably changed nothing."""
        d = _backout_lead(same_push=_NODE)
        orch._apply_is_backout_gate(d, _SEED)
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertTrue(d.corroborations["candidate_is_backout"])
        self.assertEqual(d.corroborations["candidate_backout_same_push"], _NODE)
        self.assertTrue(d.corroborations["candidate_backout_suppressed"])

    def test_that_abstain_is_schema_legal_and_explains_itself(self):
        d = _backout_lead(same_push=_NODE)
        orch._apply_is_backout_gate(d, _SEED)
        self.assertIsNone(d.verdict.needinfo_draft)
        self.assertIn(_REVERT, d.verdict.abstain_reason)
        self.assertIn(_NODE[:12], d.verdict.abstain_reason)
        self.assertIn("SAME push", d.verdict.abstain_reason)
        self.assertIsNotNone(d.verdict.mechanism)

    def test_the_worth_investigating_badge_does_not_ride_along(self):
        d = _backout_lead(same_push=_NODE)
        d.verdict = d.verdict.model_copy(update={"p_worth_investigating": 0.9714})
        orch._apply_is_backout_gate(d, _SEED)
        self.assertIsNone(d.verdict.p_worth_investigating)

    def test_a_shipped_backout_is_capped_at_lead_not_suppressed(self):
        """The 2026-07-29 owner decision stands: backing out a fix that DID ship genuinely
        reintroduces a crash and "reland it" is actionable, so it stays reportable."""
        d = _backout_lead()
        orch._apply_is_backout_gate(d, _SEED)
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertTrue(d.corroborations["candidate_backout_capped"])
        self.assertNotIn("candidate_backout_suppressed", d.corroborations)
        # still actionable, but no longer asserting the backout is the cause
        self.assertIn("could you reland it?", d.verdict.needinfo_draft)
        self.assertTrue(d.verdict.needinfo_draft.startswith("(This changeset is a backout"))

    def test_the_cap_produces_a_rung_a_lead_may_legally_hold(self):
        """`model_copy` does not re-run `_consistency_rule`, so a `lead`/`high` would ship as
        an impossible combination the model itself can never emit."""
        d = _backout_lead()
        orch._apply_is_backout_gate(d, _SEED)
        self.assertEqual(d.verdict.confidence, Confidence.probable)
        # and it round-trips through the validator that would have clamped it
        self.assertEqual(
            Verdict(**d.verdict.model_dump()).confidence, Confidence.probable
        )

    def test_a_lead_keeps_its_rung(self):
        """The cap is about the CLAIM (verified cause), not about how much a human should
        care — a backout that reintroduced a shipped crash is exactly as worth investigating."""
        d = _backout_lead(decision=Decision.lead, confidence=Confidence.probable)
        orch._apply_is_backout_gate(d, _SEED)
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.verdict.confidence, Confidence.probable)
        self.assertTrue(d.corroborations["candidate_is_backout"])
        self.assertNotIn("candidate_backout_capped", d.corroborations)

    def test_a_net_zero_lead_is_suppressed_too(self):
        d = _backout_lead(decision=Decision.lead, confidence=Confidence.medium,
                          same_push=_NODE)
        orch._apply_is_backout_gate(d, _SEED)
        self.assertEqual(d.verdict.decision, Decision.abstain)

    def test_an_existing_abstain_keeps_its_reason(self):
        d = Dossier(
            candidate=Candidate(node=_REVERT, is_backout=True, backout_of_same_push=_NODE),
            verdict=Verdict(decision=Decision.abstain, confidence=Confidence.low,
                            abstain_reason="the skeptic vetoed the chain"),
        )
        orch._apply_is_backout_gate(d, _SEED)
        self.assertEqual(d.verdict.abstain_reason, "the skeptic vetoed the chain")
        self.assertTrue(d.corroborations["candidate_is_backout"])
        self.assertNotIn("candidate_backout_suppressed", d.corroborations)

    def test_an_ordinary_candidate_is_untouched(self):
        d = _lead(backedout_by="")
        orch._apply_is_backout_gate(d, _SEED)
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertNotIn("candidate_is_backout", d.corroborations or {})

    def test_never_raises_on_a_missing_verdict_or_candidate(self):
        orch._apply_is_backout_gate(Dossier(verdict=None), _SEED)
        orch._apply_is_backout_gate(Dossier(candidate=None), _SEED)
        orch._apply_is_backout_gate(None, _SEED)


class TestIsBackoutResolver(unittest.TestCase):
    def _dossier(self):
        return Dossier(candidate=Candidate(node=_REVERT, channel=""),
                       verdict=Verdict(decision=Decision.lead, confidence=Confidence.medium,
                                       needinfo_draft="?"))

    def test_it_reads_the_desc_and_the_same_push_target(self):
        d = self._dossier()
        with mock.patch.object(sigage, "desc_for_node", return_value=_REVERT_DESC) as desc, \
             mock.patch.object(sigage, "same_push_backout_target",
                               return_value=_NODE) as push:
            orch._resolve_candidate_is_backout(d, _SEED)
        desc.assert_called_once_with(_REVERT, "nightly")
        push.assert_called_once_with(_REVERT, "nightly")
        self.assertTrue(d.candidate.is_backout)
        self.assertEqual(d.candidate.backout_of_same_push, _NODE)

    def test_an_ordinary_desc_costs_no_push_lookup(self):
        d = self._dossier()
        with mock.patch.object(sigage, "desc_for_node", return_value=_FIX_DESC), \
             mock.patch.object(sigage, "same_push_backout_target") as push:
            orch._resolve_candidate_is_backout(d, _SEED)
        push.assert_not_called()
        self.assertFalse(d.candidate.is_backout)

    def test_an_unknown_same_push_answer_never_suppresses(self):
        """Tri-state: None means "we could not find out", which must not read as net-zero."""
        d = self._dossier()
        with mock.patch.object(sigage, "desc_for_node", return_value=_REVERT_DESC), \
             mock.patch.object(sigage, "same_push_backout_target", return_value=None):
            orch._resolve_candidate_is_backout(d, _SEED)
        orch._apply_is_backout_gate(d, _SEED)
        self.assertEqual(d.candidate.backout_of_same_push, "")
        self.assertEqual(d.verdict.decision, Decision.lead)

    def test_a_raising_lookup_is_swallowed(self):
        d = self._dossier()
        with mock.patch.object(sigage, "desc_for_node", side_effect=RuntimeError("hg down")):
            orch._resolve_candidate_is_backout(d, _SEED)   # must not raise
        self.assertFalse(d.candidate.is_backout)

    def test_a_candidate_already_known_backed_out_costs_nothing_more(self):
        """It is about to be suppressed outright; don't buy a request for the mirror."""
        d = _lead(backedout_by="")
        with mock.patch.object(sigage, "backedout_by_for_node", return_value=_BACKOUT), \
             mock.patch.object(sigage, "desc_for_node") as desc:
            orch._resolve_candidate_backout(d, _SEED)
        desc.assert_not_called()

    def test_the_resolver_chain_runs_the_mirror_on_a_clean_candidate(self):
        d = _lead(backedout_by="")
        with mock.patch.object(sigage, "backedout_by_for_node", return_value=""), \
             mock.patch.object(sigage, "desc_for_node", return_value=_REVERT_DESC), \
             mock.patch.object(sigage, "same_push_backout_target", return_value=""):
            orch._resolve_candidate_backout(d, _SEED)
        self.assertTrue(d.candidate.is_backout)


class TestIsBackoutThroughTheGates(unittest.TestCase):
    def _result(self, dossier):
        return CrashTriageResult(num_turns=1, total_cost_usd=0.1, result="ok", dossier=dossier)

    def test_the_net_zero_suppression_reaches_the_shipped_verdict_row(self):
        r = self._result(_backout_lead(same_push=_NODE))
        orch.apply_deterministic_gates(r, _SEED)
        self.assertEqual(r.dossier.verdict.decision, Decision.abstain)
        self.assertEqual(r.dossier.raw_verdict.decision, Decision.strong_evidence)
        self.assertEqual(orch._verdict_row(r)["verdict"], "abstain")
        self.assertEqual(
            [a for a in (r.actions or []) if a.get("type") == "bugzilla.add_comment"], []
        )

    def test_the_cap_reaches_the_shipped_verdict_row(self):
        r = self._result(_backout_lead())
        orch.apply_deterministic_gates(r, _SEED)
        self.assertEqual(orch._verdict_row(r)["verdict"], "lead")
        self.assertEqual(orch._verdict_row(r)["confidence"], 70)

    def test_a_was_backed_out_abstain_keeps_its_own_reason(self):
        """Both gates can fire on one candidate; the first must win the explanation."""
        d = _backout_lead(same_push=_NODE)
        d.candidate = d.candidate.model_copy(update={"backedout_by": _BACKOUT})
        r = self._result(d)
        orch.apply_deterministic_gates(r, _SEED)
        self.assertEqual(r.dossier.verdict.decision, Decision.abstain)
        self.assertIn("BACKED OUT", r.dossier.verdict.abstain_reason)
        self.assertTrue(r.dossier.corroborations["candidate_is_backout"])

    def test_the_offline_eval_is_unaffected(self):
        """The resolver is online-only, so an eval dossier never carries these fields."""
        r = self._result(_lead(Confidence.probable, backedout_by=""))
        orch.apply_deterministic_gates(r, {**_SEED})
        self.assertEqual(r.dossier.verdict.decision, Decision.lead)
        self.assertNotIn("candidate_is_backout", r.dossier.corroborations or {})

    def test_the_gate_ladder_stays_network_free(self):
        """`apply_deterministic_gates` is shared with the offline eval runner. The previous
        test only proves the GATE is inert on an unset field — it would still pass if the
        RESOLVER were moved into the ladder, which is the mistake that actually costs a
        network call per corpus crash."""
        r = self._result(_lead(Confidence.probable, backedout_by=""))
        with mock.patch.object(sigage, "desc_for_node") as desc, \
             mock.patch.object(sigage, "push_for_node") as push, \
             mock.patch.object(sigage, "backedout_by_for_node") as was:
            orch.apply_deterministic_gates(r, _SEED)
        desc.assert_not_called()
        push.assert_not_called()
        was.assert_not_called()

    def test_the_capped_lead_does_not_keep_asserting_a_cause(self):
        """The cap branch is also where an UNRESOLVED same-push lookup lands, so the draft can
        otherwise claim a regression on a check that never completed."""
        r = self._result(_backout_lead())
        orch.apply_deterministic_gates(r, _SEED)
        self.assertIn("restores earlier behaviour", r.dossier.verdict.needinfo_draft)
        self.assertIn("could you reland it?", r.dossier.verdict.needinfo_draft)
        comments = [a for a in (r.actions or []) if a.get("type") == "bugzilla.add_comment"]
        self.assertTrue(comments)
        self.assertIn("restores earlier behaviour", str(comments[0]))

    def test_a_net_zero_backout_skips_the_paid_second_opinion(self):
        r = self._result(_backout_lead(same_push=_NODE))
        with mock.patch.object(orch.config, "get_agent_second_opinion",
                               return_value={"enabled": True, "min_confidence": 25,
                                             "min_boost_confidence": 50}):
            so, status = orch._maybe_run_second_opinion(r, _SEED)
        self.assertIsNone(so)
        self.assertEqual(status, "skipped_backout_netzero")

    def test_a_shipped_backout_still_buys_one(self):
        """Only the doomed verdict skips: a capped lead is still reported and still wants an
        independent check."""
        r = self._result(_backout_lead())
        with mock.patch.object(orch.config, "get_agent_second_opinion",
                               return_value={"enabled": True, "min_confidence": 25,
                                             "min_boost_confidence": 50}), \
             mock.patch.object(orch, "_will_corroboration_promote", return_value=False), \
             mock.patch("crashclouseau.agent.second_opinion.run_second_opinion") as run:
            run.side_effect = RuntimeError("would have run")
            so, status = orch._maybe_run_second_opinion(r, _SEED)
        self.assertEqual(status, "failed")

    def test_the_model_cannot_inject_either_field(self):
        d = parse_and_validate({
            "candidate": {"node": _NODE, "is_backout": True,
                          "backout_of_same_push": _REVERT},
            "verdict": {"decision": "lead", "confidence": "medium", "needinfo_draft": "?"},
        })
        self.assertFalse(d.candidate.is_backout)
        self.assertEqual(d.candidate.backout_of_same_push, "")
        self.assertEqual(d.verdict.decision, Decision.lead)


class TestSamePushLookup(unittest.TestCase):
    """`same_push_backout_target` — the precise discriminator, tri-state like its mirror."""

    def _push(self, members):
        return mock.patch.object(
            sigage, "push_for_node", return_value={"changesets": members, "pushid": "44977"}
        )

    def _desc(self, desc):
        return mock.patch.object(sigage, "desc_for_node", return_value=desc)

    def _git2hg(self, mapping):
        from crashclouseau import inspector

        return mock.patch.object(inspector, "git2hg",
                                 side_effect=lambda g: mapping.get(g, ""))

    def test_the_prod_case_the_gate_was_built_for(self):
        """`Revert "<title>"` + `This reverts commit <git sha>`, the Lando shape that current
        mozilla-central actually writes."""
        members = [{"node": _FIX, "desc": _FIX_DESC},
                   {"node": _REVERT_FULL, "desc": _REVERT_DESC},
                   {"node": "f" * 40, "desc": "Bug 1 - something else r=me"}]
        with self._desc(_REVERT_DESC), self._push(members), self._git2hg({_FIX_GIT: _FIX}):
            self.assertEqual(sigage.same_push_backout_target(_REVERT), _FIX)

    def test_an_hg_style_backout_desc_still_works(self):
        desc = "Backed out changeset 507de5c66b0d (bug 2046861) for mochitest failures"
        members = [{"node": _FIX, "desc": _FIX_DESC}, {"node": _REVERT_FULL, "desc": desc}]
        with self._desc(desc), self._push(members):
            self.assertEqual(sigage.same_push_backout_target(_REVERT), _FIX)

    def test_a_same_titled_RELAND_in_the_push_is_not_mistaken_for_the_target(self):
        """THE bug the first cut shipped: sheriffs revert-and-reland in one push, and a reland
        carries the reverted patch's title verbatim — so title matching "proves" net-zero for a
        backout of a days-old changeset. Measured on live m-c at 6.4% of matches."""
        reland = {"node": "53eb67835c2e" + "0" * 28, "desc": _FIX_DESC}
        members = [{"node": _REVERT_FULL, "desc": _REVERT_DESC}, reland]
        with self._desc(_REVERT_DESC), self._push(members), \
             self._git2hg({_FIX_GIT: _FIX}):        # the real target is in an EARLIER push
            self.assertEqual(sigage.same_push_backout_target(_REVERT), "")

    def test_a_reland_landing_after_the_candidate_is_not_a_target_either(self):
        members = [{"node": _REVERT_FULL, "desc": _REVERT_DESC},
                   {"node": "a" * 40, "desc": _FIX_DESC},
                   {"node": "b" * 40, "desc": _FIX_DESC}]
        with self._desc(_REVERT_DESC), self._push(members), self._git2hg({_FIX_GIT: _FIX}):
            self.assertEqual(sigage.same_push_backout_target(_REVERT), "")

    def test_all_targets_must_be_in_the_push_not_just_one(self):
        """Proving one of two reverted patches is same-push says nothing about the other."""
        desc = ("Backed out 2 changesets (bug 2046861) for failures\n\n"
                "Backed out changeset 507de5c66b0d (bug 2046861)\n"
                "Backed out changeset aaaaaaaaaaaa (bug 2046861)")
        members = [{"node": _FIX, "desc": _FIX_DESC}, {"node": _REVERT_FULL, "desc": desc}]
        with self._desc(desc), self._push(members):
            self.assertEqual(sigage.same_push_backout_target(_REVERT), "")
        members.append({"node": "a" * 40, "desc": "Bug 2046861 - the other half r=me"})
        with self._desc(desc), self._push(members):
            self.assertEqual(sigage.same_push_backout_target(_REVERT), _FIX)

    def test_empty_when_the_reverted_patch_is_not_in_this_push(self):
        """The case the 2026-07-29 decision protects: the fix shipped, THEN was backed out."""
        members = [{"node": _REVERT_FULL, "desc": _REVERT_DESC},
                   {"node": "f" * 40, "desc": "Bug 1 - unrelated r=me"}]
        with self._desc(_REVERT_DESC), self._push(members), self._git2hg({_FIX_GIT: _FIX}):
            self.assertEqual(sigage.same_push_backout_target(_REVERT), "")

    def test_none_when_we_could_not_find_out(self):
        # no desc
        with self._desc(""), self._push([{"node": "a" * 40, "desc": "x"}]):
            self.assertIsNone(sigage.same_push_backout_target(_REVERT))
        # desc names nothing it reverts
        with self._desc('Revert "something" because'), self._push([{"node": "a" * 40}]):
            self.assertIsNone(sigage.same_push_backout_target(_REVERT))
        # lando could not map the git sha (a miss and an outage are indistinguishable)
        with self._desc(_REVERT_DESC), self._push([{"node": "a" * 40}]), self._git2hg({}):
            self.assertIsNone(sigage.same_push_backout_target(_REVERT))
        # the push itself did not resolve
        with self._desc(_REVERT_DESC), self._push([]), self._git2hg({_FIX_GIT: _FIX}):
            self.assertIsNone(sigage.same_push_backout_target(_REVERT))

    def test_revert_targets_reads_both_shapes(self):
        with self._git2hg({_FIX_GIT: _FIX}):
            self.assertEqual(sigage.revert_targets(_REVERT_DESC), {_FIX[:12]})
        self.assertEqual(
            sigage.revert_targets("Backed out changeset 507de5c66b0d (bug 1)"),
            {"507de5c66b0d"},
        )
        self.assertIsNone(sigage.revert_targets(_FIX_DESC))
        self.assertIsNone(sigage.revert_targets(""))


class TestPushLookup(unittest.TestCase):
    """`push_for_node` is the one NEW network call. Untested, a dropped `full=1` turns
    `changesets` into bare strings, the resolver raises, the orchestrator swallows it, and
    BOTH halves of the gate silently vanish — which is exactly the 97% false positive back."""

    def setUp(self):
        sigage._PUSH_CACHE.clear()

    def _response(self, payload):
        r = mock.Mock()
        r.json.return_value = payload
        r.raise_for_status.return_value = None
        return r

    def test_it_asks_json_pushes_for_the_full_changeset_list(self):
        payload = {"pushes": {"44977": {"date": 1785232342,
                                        "changesets": [{"node": _FIX, "desc": _FIX_DESC}]}}}
        with mock.patch("crashclouseau.net.get",
                        return_value=self._response(payload)) as get:
            push = sigage.push_for_node(_REVERT, "nightly")
        url, kwargs = get.call_args.args[0], get.call_args.kwargs
        self.assertTrue(url.endswith("/json-pushes"), url)
        self.assertEqual(kwargs["params"],
                         {"changeset": _REVERT, "version": "2", "full": "1"})
        self.assertEqual(push["pushid"], "44977")
        # `full=1` is load-bearing: without it hg returns bare 40-char strings here.
        self.assertEqual(push["changesets"], [{"node": _FIX, "desc": _FIX_DESC}])

    def test_a_second_ask_is_served_from_the_cache(self):
        payload = {"pushes": {"1": {"changesets": [{"node": _FIX}]}}}
        with mock.patch("crashclouseau.net.get",
                        return_value=self._response(payload)) as get:
            sigage.push_for_node(_REVERT, "nightly")
            sigage.push_for_node(_REVERT, "nightly")
        self.assertEqual(get.call_count, 1)

    def test_a_failed_request_is_empty_not_an_exception(self):
        with mock.patch("crashclouseau.net.get", side_effect=RuntimeError("hg down")):
            self.assertEqual(sigage.push_for_node(_REVERT, "nightly"), {})

    def test_an_empty_channel_makes_no_request_at_all(self):
        with mock.patch("crashclouseau.net.get") as get:
            self.assertEqual(sigage.push_for_node(_REVERT, ""), {})
        get.assert_not_called()

    def test_the_desc_lookup_rides_the_shared_json_rev_cache(self):
        payload = {"node": _REVERT, "desc": _REVERT_DESC, "pushdate": [1785232342, 0]}
        with mock.patch.object(sigage, "json_rev", return_value=payload) as f:
            self.assertEqual(sigage.desc_for_node(_REVERT, "nightly"), _REVERT_DESC)
            self.assertEqual(sigage.pushdate_for_node(_REVERT, "nightly"), [1785232342, 0])
        self.assertEqual([c.args for c in f.call_args_list],
                         [(_REVERT, "nightly"), (_REVERT, "nightly")])

    def test_the_real_prod_desc_is_recognised_as_a_backout(self):
        """`pushlog.BACKOUT_PAT` is `^`-anchored; `Revert "Bug ...` must still match, or the
        whole gate never fires on the case that motivated it."""
        from crashclouseau import pushlog

        self.assertTrue(pushlog.is_backed_out(_REVERT_DESC))
        self.assertTrue(pushlog.is_backed_out(
            "Backed out changeset 507de5c66b0d (bug 2046861) for failures"))
        self.assertFalse(pushlog.is_backed_out(_FIX_DESC))


if __name__ == "__main__":
    unittest.main()
