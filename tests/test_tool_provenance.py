# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""An empty call graph is not an absence, and an un-recorded tool call is not a measurement.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        uv run python -m unittest tests.test_tool_provenance

Bug 2067349 was filed and closed INVALID on a mechanism asserting that invalidation was
"wired only into InsertChildToChildList/DisconnectChild, never into whole-parent-node
destruction". :jjaschke refuted it by naming three callers. `--calls-to
'nsINode::DisconnectChild'` returns all three; `--calls-to 'DisconnectChild'` -- the spelling
the mechanism itself used -- returns an EMPTY graph, which the tool then reported as
"No callers found for 'DisconnectChild'." So the tool manufactured the absence claim it exists
to break, and nothing persisted enough to notice.

Both halves are tested here because neither is checkable in prod otherwise.
"""
import unittest

from claude_agent_sdk import (
    AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage,
)

from crashclouseau.agent.triage import _RunTrace, build_result
from crashclouseau.agent.tools.searchfox_cg import NO_GRAPH_RESULT, _no_graph_result

_SF = "mcp__searchfox__calls_to"


def _use(tid, name, inp):
    return AssistantMessage(content=[ToolUseBlock(id=tid, name=name, input=inp)],
                            model="claude-opus-4-8")


def _result(tid, content):
    return UserMessage(content=[ToolResultBlock(tool_use_id=tid, content=content)])


def _run(trace, pairs):
    """Feed (tool_use_id, name, input, result_content) tuples through observe()."""
    for tid, name, inp, out in pairs:
        trace.observe(_use(tid, name, inp))
        trace.observe(_result(tid, out))


class TestTheEmptyGraphAnswer(unittest.TestCase):
    def test_it_never_asserts_the_absence(self):
        msg = _no_graph_result("callers of", "DisconnectChild", "calls it")
        self.assertTrue(msg.startswith(NO_GRAPH_RESULT), msg[:60])
        # The three things the old sentence got wrong.
        low = msg.lower()
        self.assertNotIn("no callers found", low)
        self.assertIn("not evidence", low)
        self.assertIn("unanswered", low)
        # ...and it says what to actually do instead.
        self.assertIn("qualified", low)

    def test_it_forbids_the_sentence_we_published(self):
        """We told a module owner the theory "holds up under adversarial search" for a search
        that returned empty because the name was under-qualified."""
        msg = _no_graph_result("callers of", "X", "calls it").lower()
        self.assertIn("do not write that you searched and found none", msg)

    def test_every_call_graph_tool_answers_the_same_way(self):
        import asyncio

        from crashclouseau.searchfox import SearchfoxNoResult
        from crashclouseau.agent.tools.searchfox_cg import (
            SearchfoxCtx, calls_between, calls_from, calls_to,
        )

        class _Empty:
            def calls_from(self, *a, **k):
                raise SearchfoxNoResult("empty")
            calls_to = calls_from

            def calls_between(self, *a, **k):
                raise SearchfoxNoResult("empty")

        ctx = SearchfoxCtx(client=_Empty())
        outs = [
            asyncio.run(calls_to(ctx, "DisconnectChild")),
            asyncio.run(calls_from(ctx, "DisconnectChild")),
            asyncio.run(calls_between(ctx, "A", "B")),
        ]
        for out in outs:
            with self.subTest(out=out[:40]):
                self.assertTrue(out.startswith(NO_GRAPH_RESULT))
                self.assertIn("not evidence", out.lower())


class TestTheProvenance(unittest.TestCase):
    def test_an_empty_answer_is_flagged_from_the_generated_prefix(self):
        t = _RunTrace()
        _run(t, [
            ("a", _SF, {"symbol": "DisconnectChild"},
             _no_graph_result("callers of", "DisconnectChild", "calls it")),
            ("b", _SF, {"symbol": "nsINode::DisconnectChild"},
             "## ContentUnbinder\n- ContentUnbinder::UnbindSubtree (`_ZN15...`)"),
        ])
        calls = t.provenance()["calls"]
        self.assertEqual([c["arg"] for c in calls],
                         ["symbol=DisconnectChild", "symbol=nsINode::DisconnectChild"])
        self.assertEqual([c["empty"] for c in calls], [True, False])
        self.assertEqual({c["tool"] for c in calls}, {"calls_to"})

    def test_only_the_searchfox_family_gets_a_per_call_record(self):
        t = _RunTrace()
        _run(t, [
            ("a", _SF, {"symbol": "ns::Foo::bar"}, "## X\n- Y"),
            ("b", "mcp__patch__diff", {"node": "abc123"}, "diff --git ..."),
            ("c", "Read", {"file_path": "/tmp/x"}, "contents"),
        ])
        prov = t.provenance()
        self.assertEqual(len(prov["calls"]), 1)
        # ...but the aggregate still sees all three, so nothing became invisible.
        self.assertEqual(set(prov["totals"]), {_SF, "mcp__patch__diff", "Read"})
        self.assertEqual(prov["totals"][_SF]["n"], 1)

    def test_the_cap_is_counted_not_silent(self):
        t = _RunTrace()
        n = _RunTrace._PROVENANCE_MAX + 7
        _run(t, [(str(i), _SF, {"symbol": "S%d" % i}, "## X\n- Y") for i in range(n)])
        prov = t.provenance()
        self.assertEqual(len(prov["calls"]), _RunTrace._PROVENANCE_MAX)
        self.assertEqual(prov["dropped"], 7)
        self.assertEqual(prov["totals"][_SF]["n"], n)   # the total is NOT capped

    def test_no_dropped_key_when_nothing_was_dropped(self):
        t = _RunTrace()
        _run(t, [("a", _SF, {"symbol": "ns::Foo::bar"}, "## X\n- Y")])
        self.assertNotIn("dropped", t.provenance())

    def test_it_records_which_role_asked(self):
        """The mechanism is written by patch-scout / data-flow-tracer, so "who enumerated"
        is the half that matters; `None` is the principal."""
        t = _RunTrace()
        t.observe(_use("task1", "Agent", {"subagent_type": "skeptic", "description": "check"}))
        t.observe(AssistantMessage(
            content=[ToolUseBlock(id="s1", name=_SF, input={"symbol": "ns::A::b"})],
            model="m", parent_tool_use_id="task1"))
        t.observe(_result("s1", "## X\n- Y"))
        self.assertEqual(t.provenance()["calls"][0]["by"], "skeptic")

    def test_calls_between_args_are_readable(self):
        """`source`/`target` used to fall through to a repr of the whole input dict."""
        t = _RunTrace()
        _run(t, [("a", "mcp__searchfox__calls_between", {"source": "A", "target": "B"},
                  "## X\n- Y")])
        self.assertEqual(t.provenance()["calls"][0]["arg"], "source=A")

    def test_result_text_survives_every_content_shape(self):
        empty = _no_graph_result("callers of", "X", "calls it")
        for content in (empty,
                        [{"type": "text", "text": empty}],
                        [empty]):
            with self.subTest(shape=type(content).__name__):
                t = _RunTrace()
                _run(t, [("a", _SF, {"symbol": "X"}, content)])
                self.assertTrue(t.provenance()["calls"][0]["empty"], content)
        # None / an unknown shape must cost the flag, never the run.
        t = _RunTrace()
        _run(t, [("a", _SF, {"symbol": "X"}, None)])
        self.assertFalse(t.provenance()["calls"][0]["empty"])


class TestItReachesThePayload(unittest.TestCase):
    """`orchestrator` persists `result.model_dump(mode="json")` wholesale, so the field only
    has to exist on the result -- but if it silently did not, everything above is decoration."""

    class _Msg:
        is_error = False
        num_turns = 3
        total_cost_usd = 1.23
        result = '```json\n{"verdict": {"decision": "abstain", "abstain_reason": "x"}}\n```'
        usage = None
        model_usage = None

    def test_build_result_carries_it_into_model_dump(self):
        prov = {"calls": [{"tool": "calls_to", "arg": "symbol=ns::A::b", "empty": True,
                           "secs": 0.1, "by": None}],
                "totals": {"mcp__searchfox__calls_to": {"n": 1, "secs": 0.1}}}
        res = build_result(self._Msg(), tool_calls=prov)
        self.assertEqual(res.tool_calls, prov)
        self.assertEqual(res.model_dump(mode="json")["tool_calls"], prov)

    def test_it_defaults_to_empty_rather_than_none(self):
        res = build_result(self._Msg())
        self.assertEqual(res.tool_calls, {})
        self.assertEqual(res.model_dump(mode="json")["tool_calls"], {})


if __name__ == "__main__":
    unittest.main()
