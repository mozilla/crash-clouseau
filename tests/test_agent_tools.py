# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# DATABASE_URL=sqlite:// python -m unittest tests.test_agent_tools
import asyncio
import unittest

from crashclouseau.searchfox import (
    CallEdge,
    CallGraph,
    SearchfoxInvocationError,
    SearchfoxNoResult,
    SymbolRef,
)
from crashclouseau.agent.tools import searchfox_cg as sfcg
from crashclouseau.agent.tools.searchfox_cg import (
    SearchfoxCtx,
    calls_from,
    calls_to,
    define,
)
from crashclouseau.vendor.agent_tools.claude_sdk import build_sdk_server
from crashclouseau.vendor.agent_tools.registry import ToolError


def _graph():
    root = SymbolRef(pretty="Foo::Bar", symbol_id="_ZN3Foo3BarEv", repo="mozilla-central")
    callee = SymbolRef(
        pretty="Foo::Baz",
        symbol_id="_ZN3Foo3BazEv",
        file="dom/foo.cpp",
        line=20,
        permalink="https://searchfox.org/mozilla-central/source/dom/foo.cpp#20",
        repo="mozilla-central",
    )
    return CallGraph(
        root=root,
        direction="from",
        depth=1,
        edges=[CallEdge(caller=root, callee=callee, depth=1, permalink=callee.permalink)],
        repo="mozilla-central",
    )


class _StubClient:
    def calls_from(self, symbol, repo=None, depth=1, rev_label=None):
        return _graph()

    def calls_to(self, symbol, repo=None, depth=1, rev_label=None):
        raise SearchfoxNoResult("no callers")

    def define(self, symbol, repo=None, rev_label=None):
        raise SearchfoxInvocationError("binary exploded")


class TestRegistration(unittest.TestCase):
    def test_expected_tools_registered(self):
        names = {t.name for t in sfcg.TOOLS}
        self.assertEqual(
            names,
            {"calls_from", "calls_to", "calls_between", "define", "lookup", "search"},
        )

    def test_schema_excludes_ctx_and_lists_args(self):
        by_name = {t.name: t for t in sfcg.TOOLS}
        schema = by_name["calls_from"].input_schema
        props = schema.get("properties", {})
        self.assertNotIn("ctx", props)
        self.assertIn("symbol", props)
        self.assertIn("depth", props)


class TestHandlers(unittest.TestCase):
    def setUp(self):
        self.ctx = SearchfoxCtx(client=_StubClient())

    def test_calls_from_preserves_citations(self):
        out = asyncio.run(calls_from(self.ctx, "Foo::Bar"))
        self.assertIn("_ZN3Foo3BazEv", out)  # mangled symbol id (citation anchor)
        self.assertIn("searchfox.org", out)  # permalink (citation anchor)
        self.assertIn("Foo::Baz", out)

    def test_no_result_is_abstain_string_not_error(self):
        out = asyncio.run(calls_to(self.ctx, "Nope"))
        self.assertIn("No callers", out)

    def test_hard_error_becomes_toolerror(self):
        with self.assertRaises(ToolError):
            asyncio.run(define(self.ctx, "Foo::Bar"))


class TestSdkWiring(unittest.TestCase):
    def test_build_sdk_server(self):
        ctx = SearchfoxCtx(client=_StubClient())
        server = build_sdk_server("searchfox", ctx, sfcg.TOOLS)
        self.assertIsNotNone(server)


if __name__ == "__main__":
    unittest.main()
