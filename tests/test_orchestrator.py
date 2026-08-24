# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# DATABASE_URL=sqlite:// python -m unittest tests.test_orchestrator
# (REDIS_URL is set below before importing worker; run_crash_triage is mocked, so
#  no Redis connection, SDK call, or CLI is ever made.)
import os

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau.agent import orchestrator as orch  # noqa: E402
from crashclouseau.agent.errors import MissingHandoffError  # noqa: E402
from crashclouseau.agent.result import CrashTriageResult  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    Candidate,
    Claim,
    Confidence,
    DataFlowHypothesis,
    Decision,
    Dossier,
    SearchfoxCitation,
    StructLayoutCitation,
    Verdict,
)

# This module drives `run_evidence_agent` end to end, and one step of it is ONLINE:
# `_resolve_compiled_out` asks searchfox whether the mechanism's machinery is compiled into the
# build, and fetches the candidate's diff. Against this file's synthetic nodes and symbols those
# are pure cost -- a 404 with retries per run -- so the resolver is stubbed for the module. Its
# behaviour, AND the fact that `run_evidence_agent` really does call it, are covered by
# `tests/test_compiled_out_gate.py`, so stubbing it here cannot hide a wiring regression.
_NO_ONLINE_LOOKUP = None


def setUpModule():
    global _NO_ONLINE_LOOKUP
    _NO_ONLINE_LOOKUP = mock.patch.object(orch, "_resolve_compiled_out")
    _NO_ONLINE_LOOKUP.start()


def tearDownModule():
    _NO_ONLINE_LOOKUP.stop()


_SF = SearchfoxCitation(
    permalink="https://searchfox.org/x#1", symbol_id="_Z1", repo="mozilla-central"
)
_SEED = {"uuid": "u-1", "signature": "S", "channel": "nightly", "stack": "#0 f a:1"}


def _strong_result(cost=0.3):
    return CrashTriageResult(
        num_turns=5,
        total_cost_usd=cost,
        result="ok",
        dossier=Dossier(
            verdict=Verdict(
                decision=Decision.strong_evidence,
                confidence=Confidence.high,
                mechanism=Claim(statement="m", citations=[_SF]),
                consistency=Claim(statement="c", citations=[_SF]),
            )
        ),
    )


def _abstain_result():
    return CrashTriageResult(
        num_turns=2,
        total_cost_usd=0.1,
        result="ok",
        dossier=Dossier(
            verdict=Verdict(decision=Decision.abstain, abstain_reason="not enough")
        ),
    )


def _lead_result(cost=0.2):
    return CrashTriageResult(
        num_turns=4,
        total_cost_usd=cost,
        result="ok",
        dossier=Dossier(
            candidate=Candidate(node="abc123def456", bug=42),  # the lead anchor
            verdict=Verdict(
                decision=Decision.lead,
                confidence=Confidence.medium,
                needinfo_draft="could you take a look at this crash?",
            )
        ),
    )


def _triage_returning(result, record_action=False):
    async def _fake(*, crash, tools_cfg=None, llm_cfg=None, recorder=None, extra=None):
        if record_action and recorder is not None:
            # In production build_result folds the recorder's actions into
            # result.actions; the orchestrator persists result.actions (not the raw
            # recorder), so model that here by reflecting the record on the result.
            act = recorder.record(
                "bugzilla.update_bug", {"bug_id": 1, "changes": {}}, reasoning="x"
            )
            result.actions = [act]
        return result

    return _fake


async def _triage_boom(*, crash, tools_cfg=None, llm_cfg=None, recorder=None, extra=None):
    raise RuntimeError("triage exploded")


async def _triage_transient(*, crash, tools_cfg=None, llm_cfg=None, recorder=None, extra=None):
    raise RuntimeError("API error: Overloaded (529)")


async def _triage_no_handoff(*, crash, tools_cfg=None, llm_cfg=None, recorder=None, extra=None):
    raise MissingHandoffError(
        "crash triage ended after 17 turns with no readable ```json handoff",
        raw_result="Waiting for the background agents; I'll continue once notified.",
        cost_usd=0.81, num_turns=17,
        input_tokens=10, output_tokens=2, cache_read_tokens=5,
    )


async def _triage_must_not_run(**kwargs):
    raise AssertionError("run_crash_triage should not have been called")


_LAYOUT_TYPE = "mozilla::detail::nsTStringRepr"


def _layout(status="verified", type_name=_LAYOUT_TYPE, field="mLength", offset=8,
            actual=None):
    """What `_resolve_struct_layout` leaves on the seed after asking searchfox.

    Every test that expects the fault-offset corroboration to PROMOTE has to carry one of
    these now: the gate fails closed, so an unverified citation is not corroboration."""
    entry = {"type": type_name, "field": field, "offset": offset,
             "actual": actual if actual is not None else field}
    out = {"fault": offset, "status": status,
           "verified": [], "refuted": [], "unresolved": []}
    out[status if status != "unresolved" else "unresolved"].append(entry)
    return out


def _seed_with_fault(addr="0x0000000000000008", layout="verified", **kwargs):
    seed = {"raw_crash": {"json_dump": {"crash_info": {"address": addr}}}}
    if layout:
        seed["struct_layout"] = _layout(layout, **kwargs)
    return seed


def _lead_with_struct(offset=8):
    return Dossier(
        candidate=Candidate(node="abc123def456", bug=42),
        data_flow=DataFlowHypothesis(
            summary="null-deref of mLength",
            operation="null",
            citations=[
                StructLayoutCitation(
                    type_name=_LAYOUT_TYPE,
                    field="mLength",
                    offset=offset,
                )
            ],
        ),
        verdict=Verdict(
            decision=Decision.lead,
            confidence=Confidence.medium,
            needinfo_draft="could you take a look?",
        ),
    )


from crashclouseau.searchfox import FieldEntry, FieldLayout   # noqa: E402


# The REAL `searchfox-cli --field-layout mozilla::detail::nsTStringRepr` output, 2026-08-21
# (size 16, align 8), in the REAL parsed type rather than a hand-rolled stub. That matters:
# `FieldLayout.field_at` falls back to the field whose [offset, offset+size) range CONTAINS
# the address, and `mLength` is offset 8 SIZE 4, so [8,12) contains 0xa. Only a fixture that
# carries the sizes makes `test_refutes_an_offset_that_is_not_a_field_start` a back-test of
# the exact-START rule — against a stub with no `size`/`field_at`, swapping the resolver over
# to `field_at` fails on AttributeError, which proves nothing about the semantics.
_NSTSTRINGREPR = FieldLayout(
    class_name=_LAYOUT_TYPE, size=16, align=8, repo="mozilla-central",
    fields=[
        FieldEntry(offset=0, size=8, type="char *", name="mData"),
        FieldEntry(offset=8, size=4, type="...nsTStringLengthStorage<char>", name="mLength"),
        FieldEntry(offset=12, size=2, type="...StringDataFlags", name="mDataFlags"),
        FieldEntry(offset=14, size=2, type="...StringClassFlags", name="mClassFlags"),
    ],
)


class _FakeClient:
    def __init__(self, layout=_NSTSTRINGREPR, raises=None):
        self.layout, self.raises, self.calls = layout, raises, []

    def field_layout(self, class_name, repo=None, rev_label=None):
        self.calls.append(class_name)
        if self.raises is not None:
            raise self.raises
        return self.layout


class TestStructLayoutResolver(unittest.TestCase):
    """The ONLINE half: make "a signal the model cannot fabricate" true."""

    def _resolve(self, dossier, seed, client):
        import crashclouseau.searchfox as SF
        with mock.patch.object(SF, "SearchfoxClient", return_value=client):
            orch._resolve_struct_layout(dossier, seed)
        return seed.get("struct_layout")

    def test_verifies_the_motivating_case(self):
        # bug 2053521: fault 0x8 == nsTStringRepr::mLength (VERIFIED/FIXED, regressed_by
        # 2053211). searchfox agrees, so the flag is earned rather than echoed.
        d = _lead_with_struct(offset=8)
        seed = _seed_with_fault("0x8", layout=None)
        out = self._resolve(d, seed, _FakeClient())
        self.assertEqual(out["status"], "verified")
        self.assertEqual(out["verified"][0]["type"], _LAYOUT_TYPE)
        orch._apply_corroboration_gate(d, seed)
        self.assertEqual(d.verdict.confidence, Confidence.probable)

    def test_refutes_a_field_that_is_not_at_that_offset(self):
        d = _lead_with_struct(offset=8)
        d.data_flow.citations = [StructLayoutCitation(
            type_name=_LAYOUT_TYPE, field="mDataFlags", offset=8)]
        seed = _seed_with_fault("0x8", layout=None)
        out = self._resolve(d, seed, _FakeClient())
        self.assertEqual(out["status"], "refuted")
        self.assertEqual(out["refuted"][0]["actual"], "mLength")
        orch._apply_corroboration_gate(d, seed)
        self.assertEqual(d.verdict.confidence, Confidence.medium)

    def test_refutes_an_offset_that_is_not_a_field_start(self):
        # 4-alignment is NOT the rule (nsINode really places mChildCount at 68 = 0x44, and
        # production shows live 4-aligned small faults 0x1c / 0x2c); what disqualifies 0xa
        # here is that no field of THIS class begins there.
        d = _lead_with_struct(offset=10)
        seed = _seed_with_fault("0xa", layout=None)
        out = self._resolve(d, seed, _FakeClient())
        self.assertEqual(out["status"], "refuted")
        self.assertIsNone(out["refuted"][0]["actual"])

    def test_base_class_offset_is_unresolved_not_refuted(self):
        """`--field-layout` prints inherited members in a separate table the parser drops, so
        a derived type's `fields` start above 0 (measured: nsINode 48, dom::Element 120). A
        small fault there is UNCHECKABLE, not a fabricated offset — putting it in `refuted`
        would poison the count that exists to measure fabrication."""
        derived = FieldLayout(
            class_name="mozilla::dom::Element", size=200, align=8, repo="mozilla-central",
            fields=[FieldEntry(offset=120, size=8, type="void *", name="mState")],
        )
        d = _lead_with_struct(offset=8)
        seed = _seed_with_fault("0x8", layout=None)
        out = self._resolve(d, seed, _FakeClient(layout=derived))
        self.assertEqual(out["status"], "unresolved")
        self.assertEqual(out["refuted"], [])
        orch._apply_corroboration_gate(d, seed)
        self.assertEqual(d.verdict.confidence, Confidence.medium)

    def test_a_citation_that_names_no_field_verifies_nothing(self):
        """`field` defaults to "", so "the fault is an offset into T" is a legal citation —
        and it asserts only the number `_crash_facts` printed. Verifying it against "T has
        SOME field there" would make the whole check opt-out-able by omitting one key, at a
        coincidence rate the audit measured at 33% mean / 93.5% for nsPresContext."""
        d = _lead_with_struct(offset=8)
        d.data_flow.citations = [StructLayoutCitation(type_name=_LAYOUT_TYPE, offset=8)]
        seed = _seed_with_fault("0x8", layout=None)
        client = _FakeClient()
        out = self._resolve(d, seed, client)
        self.assertEqual(client.calls, [])          # and it costs no lookup
        self.assertEqual(out["status"], "unresolved")
        orch._apply_corroboration_gate(d, seed)
        self.assertEqual(d.verdict.confidence, Confidence.medium)
        self.assertEqual(d.corroborations["fault_offset_unverified"], "unresolved")

    def test_searchfox_no_result_fails_closed(self):
        # `--field-layout` exits 0 with "No field layout information found." for a template
        # or an under-qualified name (`nsTStringRepr<char>`); the client turns that into
        # SearchfoxNoResult. Unverifiable is not corroboration.
        from crashclouseau.searchfox import SearchfoxNoResult
        d = _lead_with_struct(offset=8)
        seed = _seed_with_fault("0x8", layout=None)
        out = self._resolve(d, seed, _FakeClient(raises=SearchfoxNoResult("nope")))
        self.assertEqual(out["status"], "unresolved")
        orch._apply_corroboration_gate(d, seed)
        self.assertEqual(d.verdict.confidence, Confidence.medium)

    def test_missing_binary_fails_closed_without_raising(self):
        from crashclouseau.searchfox import SearchfoxNotFound
        import crashclouseau.searchfox as SF
        d = _lead_with_struct(offset=8)
        seed = _seed_with_fault("0x8", layout=None)
        with mock.patch.object(SF, "SearchfoxClient",
                               side_effect=SearchfoxNotFound("no binary")):
            orch._resolve_struct_layout(d, seed)   # must not raise
        self.assertEqual(seed["struct_layout"]["status"], "unresolved")

    def test_costs_nothing_when_there_is_nothing_to_verify(self):
        client = _FakeClient()
        # No fault address at all (this is every corpus fixture today).
        self._resolve(_lead_with_struct(offset=8), {"raw_crash": {}}, client)
        # A fault the gate would never promote on (0x0 is the generic null pointer).
        self._resolve(_lead_with_struct(offset=0),
                      _seed_with_fault("0x0", layout=None), client)
        # An abstain.
        d = _lead_with_struct(offset=8)
        d.verdict = Verdict(decision=Decision.abstain, confidence=Confidence.low,
                            abstain_reason="nothing credible")
        self._resolve(d, _seed_with_fault("0x8", layout=None), client)
        self.assertEqual(client.calls, [])

    def test_lookup_count_is_bounded_by_the_cap(self):
        d = _lead_with_struct(offset=8)
        d.data_flow.citations = [
            StructLayoutCitation(type_name="T%d" % i, field="f", offset=8)
            for i in range(orch._MAX_LAYOUT_LOOKUPS + 3)
        ]
        client = _FakeClient()
        self._resolve(d, _seed_with_fault("0x8", layout=None), client)
        self.assertEqual(len(client.calls), orch._MAX_LAYOUT_LOOKUPS)

    def test_gates_stay_network_free_and_the_callers_resolve(self):
        """`apply_deterministic_gates` is shared with the offline eval runner, so the lookup
        must live OUTSIDE it — and both online callers must actually make it."""
        import inspect
        from crashclouseau.eval import runner as EV
        self.assertNotIn("_resolve_struct_layout",
                         inspect.getsource(orch.apply_deterministic_gates))
        self.assertIn("_resolve_struct_layout",
                      inspect.getsource(orch.run_evidence_agent))
        self.assertIn("_resolve_struct_layout", inspect.getsource(EV.rerun_corpus))


class TestCorroborationGate(unittest.TestCase):
    def test_fault_address_parsing(self):
        self.assertEqual(orch._fault_address(_seed_with_fault("0x8")["raw_crash"]), 8)
        self.assertEqual(
            orch._fault_address({"json_dump": {"crash_info": {"address": "0x10"}}}), 16
        )
        self.assertIsNone(orch._fault_address(None))
        self.assertIsNone(orch._fault_address({"json_dump": {"crash_info": {}}}))

    def test_gate_promotes_lead_to_probable_on_offset_match(self):
        d = _lead_with_struct(offset=8)
        flags = orch._apply_corroboration_gate(d, _seed_with_fault("0x8"))
        self.assertTrue(flags["fault_address_offset_match"])
        self.assertEqual(flags["fault_field"], "mLength")
        self.assertEqual(d.verdict.confidence, Confidence.probable)
        self.assertEqual(d.corroborations["fault_offset"], 8)

    def test_verdict_row_maps_probable_to_70(self):
        d = _lead_with_struct(offset=8)
        orch._apply_corroboration_gate(d, _seed_with_fault("0x8"))
        row = orch._verdict_row(
            CrashTriageResult(num_turns=1, total_cost_usd=0.1, result="ok", dossier=d)
        )
        self.assertEqual(row["verdict"], "lead")
        self.assertEqual(row["confidence"], 70)

    def test_no_promotion_when_offset_mismatches_fault(self):
        d = _lead_with_struct(offset=12)  # cited field is at 12, fault is 0x8
        orch._apply_corroboration_gate(d, _seed_with_fault("0x8"))
        self.assertNotIn("fault_address_offset_match", d.corroborations)
        self.assertEqual(d.verdict.confidence, Confidence.medium)

    def test_no_promotion_on_zero_fault_address(self):
        # 0x0 is the generic null pointer (ambiguous), not a pinpoint field offset.
        d = _lead_with_struct(offset=0)
        orch._apply_corroboration_gate(d, _seed_with_fault("0x0"))
        self.assertEqual(d.verdict.confidence, Confidence.medium)

    def test_no_promotion_below_the_second_opinion_boost_floor(self):
        """`_fold_second_opinion` refuses to boost a lead/low ("a boost would jump two rungs,
        p_worth 0.50 -> 0.72 ... the corroborate side was never part of the calibration
        fit"). The corroboration gate is the OTHER promoter and lands on exactly
        `autofile.min_confidence`, so it takes the same floor. Ship-corpus cost: 2 of 90."""
        d = _lead_with_struct(offset=8)
        d.verdict = Verdict(decision=Decision.lead, confidence=Confidence.low,
                            needinfo_draft="?")
        flags = orch._apply_corroboration_gate(d, _seed_with_fault("0x8"))
        # The FLAG is still recorded — the floor withholds the jump, not the evidence.
        self.assertTrue(flags["fault_address_offset_match"])
        self.assertTrue(d.corroborations["fault_address_offset_match"])
        self.assertEqual(d.verdict.confidence, Confidence.low)

    def test_medium_is_at_the_floor_and_still_promotes(self):
        # 50 >= min_boost_confidence(50): the floor must not eat the 7-of-90 medium arm.
        self.assertEqual(
            orch.config.get_agent_second_opinion()["min_boost_confidence"], 50)
        d = _lead_with_struct(offset=8)
        orch._apply_corroboration_gate(d, _seed_with_fault("0x8"))
        self.assertEqual(d.verdict.confidence, Confidence.probable)

    def test_will_corroboration_promote_mirrors_the_floor(self):
        """The SO peek and the gate must agree, or a lead buys a ~$1 review for a promotion
        that can no longer happen."""
        low = _lead_with_struct(offset=8)
        low.verdict = Verdict(decision=Decision.lead, confidence=Confidence.low,
                              needinfo_draft="?")
        self.assertFalse(
            orch._will_corroboration_promote(low, _seed_with_fault("0x8")))
        self.assertTrue(
            orch._will_corroboration_promote(
                _lead_with_struct(offset=8), _seed_with_fault("0x8")))

    def test_no_promotion_when_the_citation_was_never_verified(self):
        """FAIL CLOSED. `_crash_facts` hands the model the fault address and `roles.py` tells
        it to answer with a matching `struct_layout` citation, so agreement alone is
        agreement-by-construction. No searchfox answer on the seed -> no flag."""
        d = _lead_with_struct(offset=8)
        flags = orch._apply_corroboration_gate(d, _seed_with_fault("0x8", layout=None))
        self.assertNotIn("fault_address_offset_match", flags)
        self.assertEqual(d.verdict.confidence, Confidence.medium)

    def test_refuted_citation_is_recorded_and_does_not_promote(self):
        d = _lead_with_struct(offset=8)
        seed = _seed_with_fault("0x8", layout="refuted", actual="mData")
        orch._apply_corroboration_gate(d, seed)
        self.assertEqual(d.verdict.confidence, Confidence.medium)
        # Countable: an absent flag cannot be told apart from "no citation at all".
        self.assertEqual(d.corroborations["fault_offset_unverified"], "refuted")

    def test_searchfox_outage_does_not_promote(self):
        d = _lead_with_struct(offset=8)
        orch._apply_corroboration_gate(d, _seed_with_fault("0x8", layout="unresolved"))
        self.assertEqual(d.verdict.confidence, Confidence.medium)
        self.assertEqual(d.corroborations["fault_offset_unverified"], "unresolved")

    def test_no_promotion_without_struct_citation(self):
        d = Dossier(
            candidate=Candidate(node="abc123def456", bug=42),
            verdict=Verdict(
                decision=Decision.lead,
                confidence=Confidence.medium,
                needinfo_draft="take a look?",
            ),
        )
        orch._apply_corroboration_gate(d, _seed_with_fault("0x8"))
        self.assertEqual(d.verdict.confidence, Confidence.medium)
        self.assertFalse(d.corroborations.get("fault_address_offset_match"))

    def test_strong_evidence_not_touched_by_gate(self):
        d = _strong_result().dossier
        d.data_flow = DataFlowHypothesis(
            summary="x", operation="null",
            citations=[StructLayoutCitation(
                type_name="T", field="f", offset=8)],
        )
        orch._apply_corroboration_gate(
            d, _seed_with_fault("0x8", type_name="T", field="f"))
        # gate only promotes leads; a strong-evidence verdict is left alone
        self.assertEqual(d.verdict.decision, Decision.strong_evidence)
        self.assertEqual(d.verdict.confidence, Confidence.high)

    def test_lead_may_self_assert_up_to_probable(self):
        # Worth-investigating pivot: a lead MAY self-assert up to probable (a strong
        # worth-investigating estimate); only `high` is reserved (clamped to probable).
        self.assertEqual(
            Verdict(decision=Decision.lead, confidence=Confidence.probable,
                    needinfo_draft="?").confidence, Confidence.probable)
        self.assertEqual(
            Verdict(decision=Decision.lead, confidence=Confidence.high,
                    needinfo_draft="?").confidence, Confidence.probable)
        self.assertEqual(
            Verdict(decision=Decision.lead, confidence=Confidence.medium,
                    needinfo_draft="?").confidence, Confidence.medium)


class TestEnqueueGating(unittest.TestCase):
    def test_disabled_is_noop(self):
        with mock.patch.object(orch.config, "get_agent_enabled", return_value=False), \
             mock.patch.object(orch.worker, "get_queue") as gq:
            orch.enqueue_agent("u-1")
        gq.assert_not_called()

    def test_enabled_enqueues(self):
        q = mock.MagicMock()
        with mock.patch.object(orch.config, "get_agent_enabled", return_value=True), \
             mock.patch.object(orch, "_proto_already_triaged", return_value=False), \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            orch.enqueue_agent("u-1")
        q.enqueue_call.assert_called_once()
        kwargs = q.enqueue_call.call_args.kwargs
        self.assertIs(kwargs["func"], orch.run_evidence_agent)
        self.assertEqual(kwargs["args"], ("u-1",))
        # RQ's enqueue_call takes `timeout`, not `job_timeout` — the wrong kwarg raised
        # TypeError and silently dropped every agent job. Lock the correct name + that
        # the value is passed (RQ's 180s default would kill a ~20-min triage).
        self.assertIn("timeout", kwargs)
        self.assertNotIn("job_timeout", kwargs)
        self.assertEqual(kwargs["timeout"], orch.config.get_agent_job_timeout())
        # And it must actually match rq.Queue.enqueue_call's real signature.
        import inspect
        import rq
        sig = inspect.signature(rq.Queue.enqueue_call)
        self.assertLessEqual(set(kwargs), set(sig.parameters))

    def test_force_bypasses_channel_and_proto(self):
        # A retrigger forces past both the channel gate and proto dedup, and tells
        # run_evidence_agent to re-run past its own guards (kwargs force=True).
        q = mock.MagicMock()
        with mock.patch.object(orch.config, "get_agent_enabled", return_value=True), \
             mock.patch.object(orch.config, "get_agent_channels", return_value=["nightly"]), \
             mock.patch.object(orch, "_proto_already_triaged", return_value=True), \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            orch.enqueue_agent("u-1", "beta", force=True)  # wrong channel + proto dup
        q.enqueue_call.assert_called_once()
        self.assertEqual(q.enqueue_call.call_args.kwargs["kwargs"], {"force": True})

    def test_non_nightly_channel_skipped(self):
        q = mock.MagicMock()
        with mock.patch.object(orch.config, "get_agent_enabled", return_value=True), \
             mock.patch.object(orch.config, "get_agent_channels", return_value=["nightly"]), \
             mock.patch.object(orch, "_proto_already_triaged", return_value=False), \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            orch.enqueue_agent("u-1", "beta")
            orch.enqueue_agent("u-1", "release")
        q.enqueue_call.assert_not_called()

    def test_nightly_channel_enqueues(self):
        q = mock.MagicMock()
        with mock.patch.object(orch.config, "get_agent_enabled", return_value=True), \
             mock.patch.object(orch.config, "get_agent_channels", return_value=["nightly"]), \
             mock.patch.object(orch, "_proto_already_triaged", return_value=False), \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            orch.enqueue_agent("u-1", "nightly")
        q.enqueue_call.assert_called_once()

    def test_proto_already_triaged_not_enqueued(self):
        q = mock.MagicMock()
        with mock.patch.object(orch.config, "get_agent_enabled", return_value=True), \
             mock.patch.object(orch.config, "get_agent_skip_if_existing", return_value=True), \
             mock.patch.object(orch, "_proto_already_triaged", return_value=True), \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            orch.enqueue_agent("u-1", "nightly")
        q.enqueue_call.assert_not_called()

    def test_proto_dedup_fails_open(self):
        # A DB error in the dedup check must NOT skip the crash (fail-open to enqueue).
        q = mock.MagicMock()
        with mock.patch.object(orch.config, "get_agent_enabled", return_value=True), \
             mock.patch.object(orch.models.UUID, "proto_already_analyzed",
                               side_effect=RuntimeError("db down")), \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            orch.enqueue_agent("u-1", "nightly")  # must not raise
        q.enqueue_call.assert_called_once()


class TestReaper(unittest.TestCase):
    def test_reap_reenqueues_stale_running(self):
        q = mock.MagicMock()
        with mock.patch.object(orch.models.Dossier, "get_stale_running",
                               return_value=["u1", "u2"]), \
             mock.patch.object(orch.models.Dossier, "get_stale_pending", return_value=[]), \
             mock.patch.object(orch.models.Dossier, "bump_reap_attempts", return_value=1), \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            n = orch.reap_stale_agent_jobs()
        self.assertEqual(n, 2)
        self.assertEqual(q.enqueue_call.call_count, 2)
        kwargs = q.enqueue_call.call_args.kwargs
        self.assertIs(kwargs["func"], orch.run_evidence_agent)
        self.assertEqual(kwargs["kwargs"], {"force": False})  # running orphans aren't forced
        self.assertIn("timeout", kwargs)      # not job_timeout (RQ signature)

    def test_reap_reenqueues_stale_pending_forced(self):
        # A retrigger that got orphaned (pending, job lost to a restart) is recovered,
        # forced so proto-dedup can't skip the explicit re-run.
        q = mock.MagicMock()
        with mock.patch.object(orch.models.Dossier, "get_stale_running", return_value=[]), \
             mock.patch.object(orch.models.Dossier, "get_stale_pending",
                               return_value=["p1"]), \
             mock.patch.object(orch.models.Dossier, "bump_reap_attempts", return_value=1), \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            n = orch.reap_stale_agent_jobs()
        self.assertEqual(n, 1)
        q.enqueue_call.assert_called_once()
        self.assertEqual(q.enqueue_call.call_args.kwargs["kwargs"], {"force": True})

    def test_reap_leaves_a_pending_run_that_is_merely_queued(self):
        """The one that cost 68 runs. `pending` past the stale window is only a LOST job
        if nothing is going to pick it up; behind a backlog it is a perfectly healthy run
        waiting its turn. Three workers drain ~11 jobs/hour, so any batch bigger than that
        guarantees a tail older than the 35-min window, and re-enqueueing it just moves it
        to the back of the same queue until the cap fails it."""
        q = mock.MagicMock()
        with mock.patch.object(orch.models.Dossier, "get_stale_running", return_value=[]), \
             mock.patch.object(orch.models.Dossier, "get_stale_pending",
                               return_value=["queued", "lost"]), \
             mock.patch.object(orch, "_live_job_uuids", return_value={"queued"}), \
             mock.patch.object(orch.models.Dossier, "bump_reap_attempts", return_value=1), \
             mock.patch.object(orch.models.Dossier, "set_status") as set_status, \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            n = orch.reap_stale_agent_jobs()
        self.assertEqual(n, 1)                      # only the genuinely lost one
        q.enqueue_call.assert_called_once()
        self.assertEqual(q.enqueue_call.call_args.kwargs["args"], ("lost",))
        set_status.assert_not_called()              # and nothing was failed

    def test_reap_skips_the_pending_sweep_when_the_queue_is_unreadable(self):
        # Fails SAFE. Not knowing whether a job is alive must not be read as "it is dead":
        # a wrong give-up is terminal, while skipping one pass costs a few minutes. The
        # running sweep is unaffected -- it never consults the queue.
        q = mock.MagicMock()
        with mock.patch.object(orch.models.Dossier, "get_stale_running",
                               return_value=["orphan"]), \
             mock.patch.object(orch.models.Dossier, "get_stale_pending",
                               return_value=["p1", "p2"]), \
             mock.patch.object(orch, "_live_job_uuids", side_effect=RuntimeError("redis")), \
             mock.patch.object(orch.models.Dossier, "bump_reap_attempts", return_value=1), \
             mock.patch.object(orch.models.Dossier, "set_status") as set_status, \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            n = orch.reap_stale_agent_jobs()
        self.assertEqual(n, 1)
        self.assertEqual(q.enqueue_call.call_args.kwargs["args"], ("orphan",))
        set_status.assert_not_called()

    def test_reap_does_not_filter_running_by_queue_membership(self):
        """A `running` orphan must still be reaped even though its job is in
        StartedJobRegistry -- which is where a job sits whether its worker is alive or was
        SIGKILLed, so membership proves nothing. The heartbeat is the signal there."""
        # A pending candidate has to be present or the filter block never runs and this
        # asserts nothing -- the first version of this test passed with the running sweep
        # filtered too.
        q = mock.MagicMock()
        with mock.patch.object(orch.models.Dossier, "get_stale_running",
                               return_value=["oom"]), \
             mock.patch.object(orch.models.Dossier, "get_stale_pending",
                               return_value=["queued"]), \
             mock.patch.object(orch, "_live_job_uuids", return_value={"oom", "queued"}), \
             mock.patch.object(orch.models.Dossier, "bump_reap_attempts", return_value=1), \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            n = orch.reap_stale_agent_jobs()
        self.assertEqual(n, 1)                      # the running orphan, not the queued one
        q.enqueue_call.assert_called_once()
        self.assertEqual(q.enqueue_call.call_args.kwargs["args"], ("oom",))


class TestOwnJobIdWiring(unittest.TestCase):
    """The model-level arms are useless if the call site never passes the id — and
    deleting both kwargs left the whole suite green before these existed."""

    def _patches(self):
        return TestRunEvidenceAgent._patches(self)

    def test_both_liveness_checks_get_our_own_job_id(self):
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        MDoss.skip_triage.return_value = False
        MDoss.claim_running.return_value = True
        with pD, pV, pC, pS, pSc, \
             mock.patch.object(orch, "_current_job", return_value=mock.Mock(id="job-abc")), \
             mock.patch.object(orch.config, "get_agent_skip_if_existing", return_value=True), \
             mock.patch.object(orch, "_proto_already_triaged", return_value=False), \
             mock.patch("crashclouseau.agent.triage.run_crash_triage",
                        _triage_returning(_abstain_result())):
            orch.run_evidence_agent("u-1")
        self.assertEqual(MDoss.skip_triage.call_args.kwargs.get("own_job_id"), "job-abc")
        self.assertEqual(MDoss.claim_running.call_args.kwargs.get("own_job_id"), "job-abc")

    def test_outside_a_worker_it_degrades_to_the_old_behaviour(self):
        # `_current_job()` is None in unit tests and direct calls; the id must then be
        # None, i.e. neither arm can fire.
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        MDoss.skip_triage.return_value = False
        MDoss.claim_running.return_value = True
        with pD, pV, pC, pS, pSc, \
             mock.patch.object(orch, "_current_job", return_value=None), \
             mock.patch.object(orch.config, "get_agent_skip_if_existing", return_value=True), \
             mock.patch.object(orch, "_proto_already_triaged", return_value=False), \
             mock.patch("crashclouseau.agent.triage.run_crash_triage",
                        _triage_returning(_abstain_result())):
            orch.run_evidence_agent("u-1")
        self.assertIsNone(MDoss.claim_running.call_args.kwargs.get("own_job_id"))


class TestLiveJobUuids(unittest.TestCase):
    """`_live_job_uuids` matches on the job's first ARG, not on a recorded job_id: a job
    that is still queued has never had its id written to the dossier (`set_job_id` runs
    when the run STARTS), and that is precisely the state we need to recognise."""

    def _queue(self, queued_ids, jobs):
        q = mock.MagicMock()
        q.get_job_ids.return_value = list(queued_ids)
        q.job_class.fetch_many.return_value = jobs
        return q

    def _run(self, q, registry_ids=()):
        reg = mock.MagicMock()
        reg.return_value.get_job_ids.return_value = list(registry_ids)
        with mock.patch("rq.registry.StartedJobRegistry", reg), \
             mock.patch("rq.registry.ScheduledJobRegistry", reg), \
             mock.patch("rq.registry.DeferredJobRegistry", reg):
            return orch._live_job_uuids(q)

    def test_collects_the_uuid_from_each_job(self):
        jobs = [mock.Mock(args=("u1",)), mock.Mock(args=("u2",))]
        self.assertEqual(self._run(self._queue(["j1", "j2"], jobs)), {"u1", "u2"})

    def test_includes_the_registries_not_just_the_queue(self):
        # A job parked for an RQ retry lives in ScheduledJobRegistry, not on the queue --
        # `Retry(...)` is how a transient failure is requeued, and that run is alive.
        jobs = [mock.Mock(args=("scheduled",))]
        q = self._queue([], jobs)
        self.assertEqual(self._run(q, registry_ids=["s1"]), {"scheduled"})

    def test_tolerates_an_expired_job_and_an_argless_one(self):
        # fetch_many yields None for an id that expired between listing and fetching.
        jobs = [None, mock.Mock(args=()), mock.Mock(args=("real",))]
        self.assertEqual(self._run(self._queue(["a", "b", "c"], jobs)), {"real"})

    def test_empty_queue_short_circuits_without_fetching(self):
        q = self._queue([], [])
        self.assertEqual(self._run(q), set())
        q.job_class.fetch_many.assert_not_called()

    def test_reap_gives_up_past_cap(self):
        # A crash that keeps orphaning must be failed VISIBLY, not re-enqueued forever.
        q = mock.MagicMock()
        with mock.patch.object(orch.models.Dossier, "get_stale_running",
                               return_value=["oomer"]), \
             mock.patch.object(orch.models.Dossier, "get_stale_pending", return_value=[]), \
             mock.patch.object(orch.models.Dossier, "bump_reap_attempts", return_value=3), \
             mock.patch.object(orch.config, "get_agent_reap_max_attempts", return_value=2), \
             mock.patch.object(orch.models.Dossier, "set_status") as set_status, \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            n = orch.reap_stale_agent_jobs()
        self.assertEqual(n, 0)                 # nothing re-enqueued
        q.enqueue_call.assert_not_called()     # gave up instead of re-running
        set_status.assert_called_once()
        self.assertEqual(set_status.call_args.args[0], "oomer")
        self.assertEqual(set_status.call_args.args[1], "error")

    def test_reap_still_reenqueues_within_cap(self):
        # A one-off transient orphan (attempt within cap) is still retried.
        q = mock.MagicMock()
        with mock.patch.object(orch.models.Dossier, "get_stale_running",
                               return_value=["blip"]), \
             mock.patch.object(orch.models.Dossier, "get_stale_pending", return_value=[]), \
             mock.patch.object(orch.models.Dossier, "bump_reap_attempts", return_value=2), \
             mock.patch.object(orch.config, "get_agent_reap_max_attempts", return_value=2), \
             mock.patch.object(orch.models.Dossier, "set_status") as set_status, \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            n = orch.reap_stale_agent_jobs()
        self.assertEqual(n, 1)
        q.enqueue_call.assert_called_once()
        set_status.assert_not_called()

    def test_reap_noop_when_none(self):
        with mock.patch.object(orch.models.Dossier, "get_stale_running", return_value=[]), \
             mock.patch.object(orch.models.Dossier, "get_stale_pending", return_value=[]), \
             mock.patch.object(orch.worker, "get_queue") as gq:
            self.assertEqual(orch.reap_stale_agent_jobs(), 0)
        gq.assert_not_called()

    def test_reap_never_raises(self):
        with mock.patch.object(orch.models.Dossier, "get_stale_running",
                               side_effect=RuntimeError("db down")):
            self.assertEqual(orch.reap_stale_agent_jobs(), 0)  # swallowed

    def test_reap_pushes_app_context_off_main_thread(self):
        # The clock runs the reaper on an APScheduler pool thread with no Flask app
        # context; the reaper must push one or its DB query raises. Run it on a fresh
        # thread and assert the DB call sees an app context.
        import threading
        import flask
        seen = {}

        def _fake_get_stale(stale):
            seen["ctx"] = flask.has_app_context()
            return []

        def _run():
            with mock.patch.object(orch.models.Dossier, "get_stale_running",
                                   side_effect=_fake_get_stale), \
                 mock.patch.object(orch.models.Dossier, "get_stale_pending",
                                   return_value=[]):
                orch.reap_stale_agent_jobs()

        t = threading.Thread(target=_run)
        t.start()
        t.join()
        self.assertTrue(seen.get("ctx"))  # reaper established an app context off-thread


class TestBuildSeed(unittest.TestCase):
    def test_none_for_unknown_uuid(self):
        with mock.patch.object(orch.models.CrashStack, "get_by_uuid", return_value=({}, {})):
            self.assertIsNone(orch.build_seed("u-x"))

    def test_none_when_no_changesets(self):
        res = {"frames": [{"stackpos": 0, "function": "f", "filename": "a.cpp",
                           "line": 1, "changesets": {}}]}
        with mock.patch.object(orch.models.CrashStack, "get_by_uuid", return_value=(res, {})):
            self.assertIsNone(orch.build_seed("u-x"))

    def test_seed_built(self):
        res = {"frames": [{"stackpos": 0, "function": "Foo::Bar", "filename": "a.cpp",
                           "line": 42, "changesets": {"abc": {"score": 3}}}]}
        with mock.patch.object(orch.models.CrashStack, "get_by_uuid", return_value=(res, {})), \
             mock.patch.object(orch.models.UUID, "get_info",
                               return_value={"signature": "Foo::Bar", "channel": "nightly",
                                             "product": "Firefox", "buildid": "x", "version": "1"}), \
             mock.patch("crashclouseau.inspector.get_crash_data", return_value={}):
            seed = orch.build_seed("u-1")
        self.assertEqual(seed["uuid"], "u-1")
        self.assertEqual(seed["signature"], "Foo::Bar")
        self.assertIn("Foo::Bar", seed["stack"])

    def test_seed_candidates_ranked_and_deduped(self):
        res = {"frames": [
            {"stackpos": 0, "function": "F", "filename": "a.cpp", "line": 1,
             "changesets": {"n1": {"score": 3, "bugid": 111, "backedout": False},
                            "n2": {"score": 9, "bugid": 222, "backedout": True}}},
            {"stackpos": 1, "function": "G", "filename": "b.cpp", "line": 2,
             "changesets": {"n1": {"score": 5, "bugid": 111, "backedout": False}}},
        ]}
        with mock.patch.object(orch.models.CrashStack, "get_by_uuid", return_value=(res, {})), \
             mock.patch.object(orch.models.UUID, "get_info",
                               return_value={"signature": "F", "channel": "nightly",
                                             "product": "Firefox", "buildid": "x", "version": "1"}), \
             mock.patch("crashclouseau.inspector.get_crash_data", return_value={}):
            seed = orch.build_seed("u-1")
        cands = seed["candidates"]
        self.assertEqual([c["node"] for c in cands], ["n2", "n1"])  # by score desc
        self.assertEqual(
            cands[0],
            {"node": "n2", "score": 9, "bug": 222, "backedout": True,
             "pushdate": None, "noise": False},
        )
        self.assertEqual(cands[1]["score"], 5)  # n1 deduped to its max score across frames

    def test_seed_surfaces_pushdate_and_inlines(self):
        res = {"frames": [
            {"stackpos": 4, "function": "nsGenericHTMLElement::AfterSetAttr",
             "filename": "nsGenericHTMLElement.cpp", "line": 960,
             "changesets": {"d86be929745b": {"score": 7, "bugid": 2053211,
                                             "backedout": False,
                                             "pushdate": "2026-07-10T12:00:00"}}},
        ]}
        raw = {"json_dump": {"crashing_thread": {"frames": [
            {"frame": 4, "function": "nsGenericHTMLElement::AfterSetAttr",
             "inlines": [{"function": "HashString"},
                         {"function": "nsTStringRepr::Length"}]},
        ]}}}
        with mock.patch.object(orch.models.CrashStack, "get_by_uuid", return_value=(res, {})), \
             mock.patch.object(orch.models.UUID, "get_info",
                               return_value={"signature": "S", "channel": "nightly",
                                             "product": "Firefox", "buildid": "x", "version": "1"}), \
             mock.patch("crashclouseau.inspector.get_crash_data", return_value=raw):
            seed = orch.build_seed("u-1")
        # pushdate is no longer dropped
        self.assertEqual(seed["candidates"][0]["pushdate"], "2026-07-10T12:00:00")
        # the inlined leaf functions (invisible before) reach the agent's stack text
        self.assertIn("inlined: HashString, nsTStringRepr::Length", seed["stack"])

    def test_inlines_by_stackpos_shapes_and_degradation(self):
        # crashing_thread as a dict
        raw = {"json_dump": {"crashing_thread": {"frames": [
            {"frame": 0, "inlines": [{"function": "A"}]},
            {"frame": 1},  # no inlines
        ]}}}
        self.assertEqual(orch._inlines_by_stackpos(raw), {0: ["A"]})
        # threads[idx] shape
        raw2 = {"json_dump": {"crashing_thread": 1, "threads": [
            {"frames": []},
            {"frames": [{"frame": 3, "inlines": [{"function": "B"}]}]},
        ]}}
        self.assertEqual(orch._inlines_by_stackpos(raw2), {3: ["B"]})
        # garbage degrades to {}
        self.assertEqual(orch._inlines_by_stackpos(None), {})
        self.assertEqual(orch._inlines_by_stackpos({"json_dump": {}}), {})

    def test_seed_downranks_anchor_frame_only_candidate(self):
        # A candidate supported ONLY by a universal anchor frame is down-ranked below a
        # lower-raw-score candidate on a real frame (#15 phase 3), never dropped.
        res = {"frames": [
            {"stackpos": 0, "function": "MessageLoop::Run", "filename": "ipc/x.cpp",
             "line": 1, "changesets": {"anchor": {"score": 100, "bugid": 1, "backedout": False}}},
            {"stackpos": 1, "function": "RealCode::doThing", "filename": "dom/y.cpp",
             "line": 2, "changesets": {"real": {"score": 5, "bugid": 2, "backedout": False}}},
        ]}
        with mock.patch.object(orch.models.CrashStack, "get_by_uuid", return_value=(res, {})), \
             mock.patch.object(orch.models.UUID, "get_info",
                               return_value={"channel": "nightly"}), \
             mock.patch.object(orch.models.Node, "authors_for", return_value={}), \
             mock.patch("crashclouseau.inspector.get_crash_data", return_value={}):
            seed = orch.build_seed("u-1")
        cands = {c["node"]: c for c in seed["candidates"]}
        self.assertTrue(cands["anchor"]["noise"])
        self.assertFalse(cands["real"]["noise"])
        # 100*0.1=10 > 5, so anchor still ranks first here — but it's tagged noise so
        # the agent/prompt down-ranks it; the raw score is preserved for fidelity.
        self.assertEqual(cands["anchor"]["score"], 100)
        self.assertEqual([c["node"] for c in seed["candidates"]], ["anchor", "real"])

    def test_candidate_on_real_and_anchor_frame_not_noise(self):
        # A node supported by BOTH an anchor frame and a real code frame is NOT tagged
        # noise (regression: the per-node all-noise fix), and keeps its max raw score.
        res = {"frames": [
            {"stackpos": 0, "function": "MessageLoop::Run", "filename": "ipc/x.cpp",
             "line": 1, "changesets": {"both": {"score": 100, "bugid": 1, "backedout": False}}},
            {"stackpos": 1, "function": "RealCode::doThing", "filename": "dom/y.cpp",
             "line": 2, "changesets": {"both": {"score": 5, "bugid": 1, "backedout": False}}},
        ]}
        with mock.patch.object(orch.models.CrashStack, "get_by_uuid", return_value=(res, {})), \
             mock.patch.object(orch.models.UUID, "get_info", return_value={"channel": "nightly"}), \
             mock.patch.object(orch.models.Node, "authors_for", return_value={}), \
             mock.patch("crashclouseau.inspector.get_crash_data", return_value={}):
            seed = orch.build_seed("u-1")
        both = {c["node"]: c for c in seed["candidates"]}["both"]
        self.assertFalse(both["noise"])
        self.assertEqual(both["score"], 100)

    def test_ubiquitous_symbol_frame_is_noise(self):
        # A frame whose FUNCTION is a ubiquitous primitive (not just its path) is noise.
        res = {"frames": [
            {"stackpos": 0, "function": "mozilla::HashMap<int>::lookup", "filename": "dom/z.cpp",
             "line": 1, "changesets": {"n": {"score": 9, "bugid": 1, "backedout": False}}},
        ]}
        with mock.patch.object(orch.models.CrashStack, "get_by_uuid", return_value=(res, {})), \
             mock.patch.object(orch.models.UUID, "get_info", return_value={"channel": "nightly"}), \
             mock.patch.object(orch.models.Node, "authors_for", return_value={}), \
             mock.patch("crashclouseau.inspector.get_crash_data", return_value={}):
            seed = orch.build_seed("u-1")
        self.assertTrue(seed["candidates"][0]["noise"])

    def test_seed_attaches_area_experts(self):
        res = {"frames": [
            {"stackpos": 0, "function": "F", "filename": "dom/a.cpp", "line": 1,
             "changesets": {"n1": {"score": 9, "bugid": 111, "backedout": False}}},
        ]}
        authors = {"n1": {"email": "dev@m.org", "real": "Dev", "nick": "d",
                          "bug": 111, "backedout": False}}
        with mock.patch.object(orch.models.CrashStack, "get_by_uuid", return_value=(res, {})), \
             mock.patch.object(orch.models.UUID, "get_info",
                               return_value={"channel": "nightly"}), \
             mock.patch.object(orch.models.Node, "authors_for", return_value=authors), \
             mock.patch("crashclouseau.inspector.get_crash_data", return_value={}):
            seed = orch.build_seed("u-1")
        self.assertEqual(len(seed["experts"]), 1)
        self.assertEqual(seed["experts"][0]["email"], "dev@m.org")
        self.assertIn("n1", seed["experts"][0]["reason"])


class TestRunEvidenceAgent(unittest.TestCase):
    def setUp(self):
        # Default: proto-signature not seen (so runs proceed). The dedicated dedup test
        # overrides this locally. Avoids these tests hitting the real DB-less query.
        p = mock.patch.object(orch, "_proto_already_triaged", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def _patches(self):
        MDoss = mock.MagicMock()
        MDoss.get_by_uuid.return_value = None
        MDoss.skip_triage.return_value = False  # not skipped (no dossier / fresh run)
        MDoss.claim_running.return_value = True  # this worker wins the atomic claim
        MVerd = mock.MagicMock()
        return (
            mock.patch.object(orch.models, "Dossier", MDoss),
            mock.patch.object(orch.models, "Verdict", MVerd),
            mock.patch.object(orch.models, "commit"),
            mock.patch.object(orch, "build_seed", return_value=dict(_SEED)),
            mock.patch.object(orch, "_seed_score", return_value=5),
            MDoss,
            MVerd,
        )

    def _done_upsert(self, MDoss):
        for c in MDoss.upsert.call_args_list:
            if c.kwargs.get("status") == "done":
                return c
        return None

    def test_happy_strong_persists_culprit(self):
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        with pD, pV, pC, pS, pSc, \
             mock.patch("crashclouseau.agent.triage.run_crash_triage",
                        _triage_returning(_strong_result())):
            orch.run_evidence_agent("u-1")
        self.assertIsNotNone(self._done_upsert(MDoss))
        MVerd.set.assert_called_once()
        self.assertEqual(MVerd.set.call_args.kwargs["verdict"], "culprit")
        self.assertEqual(MVerd.set.call_args.kwargs["confidence"], 85)

    def test_abstain_persists_abstain(self):
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        with pD, pV, pC, pS, pSc, \
             mock.patch("crashclouseau.agent.triage.run_crash_triage",
                        _triage_returning(_abstain_result())):
            orch.run_evidence_agent("u-1")
        self.assertEqual(MVerd.set.call_args.kwargs["verdict"], "abstain")

    def test_lead_persists_lead(self):
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        with pD, pV, pC, pS, pSc, \
             mock.patch("crashclouseau.agent.triage.run_crash_triage",
                        _triage_returning(_lead_result())):
            orch.run_evidence_agent("u-1")
        self.assertEqual(MVerd.set.call_args.kwargs["verdict"], "lead")
        self.assertEqual(MVerd.set.call_args.kwargs["confidence"], 50)

    def test_exception_isolation(self):
        # A non-transient error: settle on `error`, record the reason, do NOT raise.
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        with pD, pV, pC, pS, pSc, \
             mock.patch("crashclouseau.agent.triage.run_crash_triage", _triage_boom):
            orch.run_evidence_agent("u-1")  # must not raise
        call = MDoss.set_status.call_args
        self.assertEqual(call.args[:2], ("u-1", "error"))
        self.assertIn("triage exploded", call.kwargs.get("error", ""))
        MVerd.set.assert_not_called()

    def test_transient_failure_requeues_when_retries_left(self):
        # A transient blip with RQ retries remaining: reset to `pending` (so the retry
        # can re-claim it), record the reason, and RE-RAISE so RQ requeues the job.
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        with pD, pV, pC, pS, pSc, \
             mock.patch.object(orch, "_current_job", return_value=mock.Mock(retries_left=2)), \
             mock.patch("crashclouseau.agent.triage.run_crash_triage", _triage_transient):
            with self.assertRaises(RuntimeError):
                orch.run_evidence_agent("u-1")
        call = MDoss.set_status.call_args
        self.assertEqual(call.args[:2], ("u-1", "pending"))
        self.assertIn("Overloaded", call.kwargs.get("error", ""))

    def test_transient_failure_errors_when_no_retries_left(self):
        # Same transient blip, but retries are exhausted -> settle on `error`, no raise.
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        with pD, pV, pC, pS, pSc, \
             mock.patch.object(orch, "_current_job", return_value=mock.Mock(retries_left=0)), \
             mock.patch("crashclouseau.agent.triage.run_crash_triage", _triage_transient):
            orch.run_evidence_agent("u-1")  # must not raise
        call = MDoss.set_status.call_args
        self.assertEqual(call.args[:2], ("u-1", "error"))
        self.assertIn("Overloaded", call.kwargs.get("error", ""))

    def test_missing_handoff_errors_and_keeps_the_forensics(self):
        """A run that never emitted a ```json handoff must settle on `error` — never on
        a status=done abstain that reads like a considered verdict — and must keep the
        agent's final text and the money it cost: `set_status` records neither, and this
        is exactly the spend that went unnoticed for three days."""
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        with pD, pV, pC, pS, pSc, \
             mock.patch.object(orch, "_current_job", return_value=mock.Mock(retries_left=2)), \
             mock.patch("crashclouseau.agent.triage.run_crash_triage", _triage_no_handoff):
            orch.run_evidence_agent("u-1")  # must not raise: NOT retryable
        call = MDoss.set_status.call_args
        self.assertEqual(call.args[:2], ("u-1", "error"))
        self.assertIn("no readable", call.kwargs.get("error", ""))
        self.assertEqual(MDoss.upsert.call_args.kwargs["cost_usd"], 0.81)
        merged = MDoss.merge_payload.call_args.args[1]
        self.assertIn("Waiting for the background agents", merged["result"])
        self.assertEqual(merged["num_turns"], 17)
        MVerd.set.assert_not_called()          # no phantom abstain in the verdict table
        self.assertIsNone(self._done_upsert(MDoss))

    def test_missing_handoff_is_not_retryable(self):
        """Classified by TYPE, not by what the message happens to say — an RQ retry
        re-runs the whole ~20-min triage at full price, while an `error` row leaves the
        proto cluster recoverable for free.

        The marker word goes in the MESSAGE, which is the only thing `_should_retry`
        looks at: put it in ``raw_result`` instead and the assertion passes with the
        isinstance branch DELETED, which is how this test previously failed to test
        anything. `build_result` builds this message from the agent's own run, so
        quoting its final text is a plausible future edit — and a crash-triage run
        talks about timeouts and streams for a living."""
        marker_in_the_message = MissingHandoffError(
            "crash triage: the shutdownhang timed out on the stream, and the run "
            "ended with no readable ```json handoff",
        )
        self.assertTrue(  # the message really would match, absent the type check
            any(m in str(marker_in_the_message) for m in orch._TRANSIENT_MARKERS))
        self.assertFalse(orch._should_retry(marker_in_the_message))
        self.assertTrue(orch._should_retry(RuntimeError("connection reset")))

    def test_failed_row_keeps_the_tail_where_the_broken_handoff_is(self):
        # A plain `[:8000]` would drop the malformed block entirely: it is the LAST
        # thing the model writes, and that family's results run 8.5k-15.5k chars.
        text = "HEAD" + ("x" * 20000) + "```json\n{,}\n```"
        elided = orch._elide(text)
        self.assertTrue(elided.startswith("HEAD"))
        self.assertTrue(elided.endswith("```json\n{,}\n```"))
        self.assertIn("chars elided", elided)
        self.assertLess(len(elided), 8200)
        short = "no fence here"
        self.assertEqual(orch._elide(short), short)  # kept whole, no marker

    def test_skip_if_existing(self):
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        MDoss.skip_triage.return_value = True  # already done / a fresh run in progress
        with pD, pV, pC, pS, pSc, \
             mock.patch("crashclouseau.agent.triage.run_crash_triage", _triage_must_not_run):
            orch.run_evidence_agent("u-1")  # must not raise (triage not called)
        MDoss.upsert.assert_not_called()

    def test_lost_atomic_claim_skips(self):
        # skip_triage passed (looked stale/absent) but another worker won the atomic
        # claim first -> this worker must NOT run (no double-pay).
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        MDoss.skip_triage.return_value = False
        MDoss.claim_running.return_value = False  # lost the race
        with pD, pV, pC, pS, pSc, \
             mock.patch.object(orch, "_proto_already_triaged", return_value=False), \
             mock.patch("crashclouseau.agent.triage.run_crash_triage", _triage_must_not_run):
            orch.run_evidence_agent("u-1")  # must not raise (triage not called)
        MVerd.set.assert_not_called()
        self.assertIsNone(self._done_upsert(MDoss))  # no "done" persisted

    def test_skip_if_proto_already_triaged(self):
        # No dossier for THIS uuid, but a proto-sibling was already triaged -> skip
        # (one paid run per proto-signature cluster, across builds).
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        MDoss.skip_triage.return_value = False  # this uuid not itself done/running
        with pD, pV, pC, pS, pSc, \
             mock.patch.object(orch, "_proto_already_triaged", return_value=True), \
             mock.patch("crashclouseau.agent.triage.run_crash_triage", _triage_must_not_run):
            orch.run_evidence_agent("u-1")  # must not raise (triage not called)
        MDoss.upsert.assert_not_called()

    def test_build_seed_none_skips(self):
        pD, pV, pC, _, pSc, MDoss, MVerd = self._patches()
        with pD, pV, pC, pSc, \
             mock.patch.object(orch, "build_seed", return_value=None), \
             mock.patch("crashclouseau.agent.triage.run_crash_triage", _triage_must_not_run):
            orch.run_evidence_agent("u-1")
        MDoss.upsert.assert_not_called()

    def test_over_budget_flagged_but_persists(self):
        # real cap is 2.0 (config); a $5 run is over budget.
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        with pD, pV, pC, pS, pSc, \
             mock.patch("crashclouseau.agent.triage.run_crash_triage",
                        _triage_returning(_strong_result(cost=5.0))):
            orch.run_evidence_agent("u-1")
        done = self._done_upsert(MDoss)
        self.assertIsNotNone(done)
        self.assertTrue(done.kwargs["payload"].get("over_budget"))

    def test_recorded_actions_persisted(self):
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        with pD, pV, pC, pS, pSc, \
             mock.patch("crashclouseau.agent.triage.run_crash_triage",
                        _triage_returning(_strong_result(), record_action=True)):
            orch.run_evidence_agent("u-1")
        done = self._done_upsert(MDoss)
        self.assertEqual(len(done.kwargs["payload"]["actions"]), 1)
        self.assertEqual(done.kwargs["payload"]["actions"][0]["type"], "bugzilla.update_bug")

    def test_tokens_persisted(self):
        # The aggregate token usage on the result is written to the done dossier
        # (previously never passed -> the tasks view always showed 0/0/0).
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        res = _strong_result()
        res.input_tokens, res.output_tokens, res.cache_read_tokens = 1234, 56, 7890
        with pD, pV, pC, pS, pSc, \
             mock.patch("crashclouseau.agent.triage.run_crash_triage", _triage_returning(res)):
            orch.run_evidence_agent("u-1")
        done = self._done_upsert(MDoss)
        self.assertEqual(done.kwargs["input_tokens"], 1234)
        self.assertEqual(done.kwargs["output_tokens"], 56)
        self.assertEqual(done.kwargs["cache_read_tokens"], 7890)

    def test_force_reruns_via_claim(self):
        # A retrigger (force=True) bypasses the skip_triage/proto EARLY-OUT but STILL goes
        # through the atomic claim (the concurrency guard); it does not unconditionally
        # upsert running. retrigger_agent resets the dossier to pending so the claim wins.
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        MDoss.skip_triage.return_value = True  # would normally skip
        MDoss.claim_running.return_value = True  # claimable (reset to pending)
        with pD, pV, pC, pS, pSc, \
             mock.patch.object(orch, "_proto_already_triaged", return_value=True), \
             mock.patch("crashclouseau.agent.triage.run_crash_triage",
                        _triage_returning(_strong_result())):
            orch.run_evidence_agent("u-1", force=True)
        MDoss.claim_running.assert_called_once()  # went through the guard
        self.assertIsNotNone(self._done_upsert(MDoss))
        MVerd.set.assert_called_once()

    def test_force_loser_of_claim_does_not_double_pay(self):
        # Two concurrent retriggers of one uuid: the job that loses claim_running must NOT
        # run a triage or persist -- this is what prevents the double-pay.
        pD, pV, pC, pS, pSc, MDoss, MVerd = self._patches()
        MDoss.claim_running.return_value = False  # lost the atomic claim
        with pD, pV, pC, pS, pSc, \
             mock.patch.object(orch, "_proto_already_triaged", return_value=True), \
             mock.patch("crashclouseau.agent.triage.run_crash_triage", _triage_must_not_run):
            orch.run_evidence_agent("u-1", force=True)  # must not run / not raise
        self.assertIsNone(self._done_upsert(MDoss))
        MVerd.set.assert_not_called()


class TestRetrigger(unittest.TestCase):
    def test_cancel_running_job_sends_stop(self):
        d = mock.MagicMock(status="running", payload={"job_id": "job-1"})
        with mock.patch.object(orch.models.Dossier, "get_by_uuid", return_value=d), \
             mock.patch("rq.command.send_stop_job_command") as stop:
            self.assertTrue(orch.cancel_running_job("u-1"))
        stop.assert_called_once()
        self.assertEqual(stop.call_args.args[1], "job-1")  # (connection, job_id)

    def test_cancel_noop_when_not_running(self):
        d = mock.MagicMock(status="done", payload={"job_id": "job-1"})
        with mock.patch.object(orch.models.Dossier, "get_by_uuid", return_value=d), \
             mock.patch("rq.command.send_stop_job_command") as stop:
            self.assertFalse(orch.cancel_running_job("u-1"))
        stop.assert_not_called()

    def test_cancel_noop_without_job_id(self):
        d = mock.MagicMock(status="running", payload={})
        with mock.patch.object(orch.models.Dossier, "get_by_uuid", return_value=d), \
             mock.patch("rq.command.send_stop_job_command") as stop:
            self.assertFalse(orch.cancel_running_job("u-1"))
        stop.assert_not_called()

    def test_retrigger_cancels_resets_then_force_enqueues(self):
        with mock.patch.object(orch, "cancel_running_job", return_value=True) as cxl, \
             mock.patch.object(orch.models.Dossier, "reset_for_retrigger") as rst, \
             mock.patch.object(orch, "enqueue_agent") as enq:
            out = orch.retrigger_agent("u-1")
        cxl.assert_called_once_with("u-1")
        rst.assert_called_once_with("u-1")  # reset so claim_running can re-take it
        enq.assert_called_once()
        self.assertTrue(enq.call_args.kwargs.get("force"))
        self.assertEqual(out, {"uuid": "u-1", "cancelled": True, "already_filed": None})

    def test_retriggering_a_run_that_already_filed_warns_and_says_which_bug(self):
        """A retrigger of a filed run is a request to re-analyse a crash whose analysis is
        already public. On 2026-08-24 a 20-uuid retrigger experiment put a second copy of one
        analysis on bug 2065072 (the component owner replied "No need for it to post again")
        and filed a new bug 2066051, and neither was visible in advance."""
        with mock.patch.object(orch, "cancel_running_job", return_value=False), \
             mock.patch.object(orch.models.Dossier, "already_filed",
                               return_value={"filed": True, "bug": 2065072,
                                             "mode": "new_bug"}), \
             mock.patch.object(orch.models.Dossier, "reset_for_retrigger"), \
             mock.patch.object(orch, "enqueue_agent"), \
             self.assertLogs(level="WARNING") as cm:
            out = orch.retrigger_agent("u-1")
        self.assertEqual(out["already_filed"], 2065072)
        joined = "\n".join(cm.output)
        self.assertIn("ALREADY went to bugzilla", joined)
        self.assertIn("2065072", joined)
        self.assertIn("new_bug", joined)

    def test_a_retrigger_of_a_never_filed_run_is_silent_about_bugzilla(self):
        with mock.patch.object(orch, "cancel_running_job", return_value=False), \
             mock.patch.object(orch.models.Dossier, "already_filed", return_value=None), \
             mock.patch.object(orch.models.Dossier, "reset_for_retrigger"), \
             mock.patch.object(orch, "enqueue_agent"), \
             self.assertLogs(level="INFO") as cm:
            out = orch.retrigger_agent("u-1")
        self.assertIsNone(out["already_filed"])
        self.assertNotIn("ALREADY went to bugzilla", "\n".join(cm.output))


class TestStackText(unittest.TestCase):
    """_stack_text: presentation-only panic-prologue stripping for the agent prompt."""

    @staticmethod
    def _f(pos, fn, filename="", line=-1):
        return {"stackpos": pos, "function": fn, "filename": filename, "line": line}

    def test_strips_leading_prologue_keeps_stackpos(self):
        frames = [
            self._f(0, "rust_begin_unwind"),
            self._f(1, "core::panicking::panic_fmt"),
            self._f(2, "core::option::unwrap_failed"),
            self._f(3, "mozilla::dom::Foo::Process", "Foo.cpp", 42),
            self._f(4, "nsThread::ProcessNextEvent", "nsThread.cpp", 1),
        ]
        before = [dict(f) for f in frames]
        text = orch._stack_text(frames)
        self.assertNotIn("rust_begin_unwind", text)          # prologue elided
        self.assertIn("#3 mozilla::dom::Foo::Process", text)  # real frame, original stackpos
        self.assertIn("elided", text)                         # note present
        self.assertEqual(frames, before)                      # input NOT mutated (presentation-only)

    def test_normal_stack_unchanged(self):
        frames = [self._f(0, "mozilla::dom::Bar::Do", "Bar.cpp", 10),
                  self._f(1, "Caller::run", "Caller.cpp", 5)]
        text = orch._stack_text(frames)
        self.assertTrue(text.startswith("#0 "))
        self.assertNotIn("elided", text)

    def test_all_prologue_not_emptied(self):
        frames = [self._f(0, "rust_begin_unwind"), self._f(1, "MOZ_Crash")]
        self.assertIn("rust_begin_unwind", orch._stack_text(frames))  # kept (would be empty)

    def test_abort_adjacent_real_frame_not_stripped(self):
        # a real crash in abort-adjacent code (AbortController) must survive — it's not a
        # runtime machinery symbol.
        frames = [self._f(0, "mozilla::dom::AbortController::Abort", "AbortController.cpp", 9),
                  self._f(1, "x::y", "a.cpp", 1)]
        self.assertTrue(orch._stack_text(frames).startswith("#0 "))


if __name__ == "__main__":
    unittest.main()
