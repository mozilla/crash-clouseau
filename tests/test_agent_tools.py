# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# DATABASE_URL=sqlite:// python -m unittest tests.test_agent_tools
#
# ...and set here too, so `unittest discover` with a bare environment does not fail at IMPORT and
# silently skip this module (see tests/test_agent.py).
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import asyncio  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau.agent import patch_extract  # noqa: E402
from crashclouseau.agent.tools.patch import PatchCtx, diff  # noqa: E402
from crashclouseau.searchfox import (  # noqa: E402
    CallEdge,
    CallGraph,
    FieldEntry,
    FieldLayout,
    SearchfoxInvocationError,
    SearchfoxNoResult,
    SymbolRef,
)
from crashclouseau.agent.tools import searchfox_cg as sfcg  # noqa: E402
from crashclouseau.agent.tools.searchfox_cg import (  # noqa: E402
    SearchfoxCtx,
    calls_from,
    calls_to,
    define,
    field_layout,
)
from crashclouseau.vendor.agent_tools.claude_sdk import build_sdk_server  # noqa: E402
from crashclouseau.vendor.agent_tools.registry import ToolError  # noqa: E402


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

    def field_layout(self, class_name, repo=None, rev_label=None):
        return FieldLayout(
            class_name="mozilla::detail::nsTStringRepr",
            size=16,
            align=8,
            fields=[
                FieldEntry(offset=0, size=8, type="char16_t *", name="mData"),
                FieldEntry(offset=8, size=4, type="...", name="mLength"),
            ],
            repo="mozilla-central",
        )


class TestRegistration(unittest.TestCase):
    def test_expected_tools_registered(self):
        names = {t.name for t in sfcg.TOOLS}
        self.assertEqual(
            names,
            {"calls_from", "calls_to", "calls_between", "define", "lookup", "search",
             "field_layout"},
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
        self.assertTrue(out.startswith(sfcg.NO_GRAPH_RESULT), out[:60])

    def test_no_result_does_not_claim_the_absence(self):
        """It used to answer "No callers found for 'Nope'.", which is false whenever the name
        is under-qualified -- and that is how bug 2067349 was filed. See
        tests/test_tool_provenance.py for the whole argument."""
        out = asyncio.run(calls_to(self.ctx, "Nope")).lower()
        self.assertNotIn("no callers found", out)
        self.assertIn("not evidence", out)

    def test_hard_error_becomes_toolerror(self):
        with self.assertRaises(ToolError):
            asyncio.run(define(self.ctx, "Foo::Bar"))

    def test_field_layout_renders_offsets_and_citation_shape(self):
        out = asyncio.run(field_layout(self.ctx, "mozilla::detail::nsTStringRepr"))
        self.assertIn("offset 8", out)
        self.assertIn("mLength", out)
        self.assertIn("struct_layout", out)  # tells the model the citation shape


class TestSdkWiring(unittest.TestCase):
    def test_build_sdk_server(self):
        ctx = SearchfoxCtx(client=_StubClient())
        server = build_sdk_server("searchfox", ctx, sfcg.TOOLS)
        self.assertIsNotNone(server)


class TestPatchTool(unittest.TestCase):
    def _ext(self):
        fd = patch_extract.FileDiff(
            filename="dom/base/ChildIterator.cpp", status="modified",
            hunks=[patch_extract.Hunk(
                old_start=268, new_start=270, enclosing_function="GetNextChild",
                added_lines=[(270, "  foo();")], deleted_lines=[(268, "  bar();")])],
        )
        return patch_extract.PatchExtraction(
            node="hg123", channel="nightly", raw_diff="x", files=[fd])

    def test_diff_renders_parsed_patch(self):
        # git hash -> git2hg -> extract; output is citation-ready (node/file/line/side/content)
        with mock.patch("crashclouseau.inspector.git2hg", return_value="hg123") as g2h, \
             mock.patch("crashclouseau.agent.patch_extract.extract",
                        return_value=self._ext()) as ex:
            out = asyncio.run(diff(PatchCtx(channel="nightly"), "gitABC"))
        g2h.assert_called_once_with("gitABC")
        ex.assert_called_once_with("hg123", "nightly")
        self.assertIn("dom/base/ChildIterator.cpp", out)
        self.assertIn("+ 270:   foo();", out)
        self.assertIn("- 268:   bar();", out)
        self.assertIn("GetNextChild", out)
        self.assertIn("gitABC", out)   # shows the candidate node the agent knows

    def test_diff_falls_back_when_no_hg_counterpart(self):
        ext = patch_extract.PatchExtraction(node="n", channel="beta", raw_diff=None, files=[])
        with mock.patch("crashclouseau.inspector.git2hg", return_value=""), \
             mock.patch("crashclouseau.agent.patch_extract.extract", return_value=ext) as ex:
            out = asyncio.run(diff(PatchCtx(channel="beta"), "already-hg"))
        ex.assert_called_once_with("already-hg", "beta")  # node used as-is
        self.assertIn("No diff available", out)


if __name__ == "__main__":
    unittest.main()


class TestNetUserAgent(unittest.TestCase):
    """Every direct request we make must carry our identifying User-Agent."""

    def test_injects_user_agent_and_preserves_headers(self):
        from unittest import mock
        from crashclouseau import net
        with mock.patch.object(net.requests, "get") as g:
            net.get("https://lando.moz.tools/x")
            net.get("https://x/y", headers={"X-Token": "t"})
        # UA stamped on the bare call...
        self.assertEqual(g.call_args_list[0].kwargs["headers"]["User-Agent"], net.USER_AGENT)
        # ...and merged alongside a caller-supplied header
        h = g.call_args_list[1].kwargs["headers"]
        self.assertEqual(h["X-Token"], "t")
        self.assertEqual(h["User-Agent"], net.USER_AGENT)

    def test_explicit_user_agent_not_clobbered(self):
        from unittest import mock
        from crashclouseau import net
        with mock.patch.object(net.requests, "post") as p:
            net.post("https://x", headers={"User-Agent": "custom"})
        self.assertEqual(p.call_args.kwargs["headers"]["User-Agent"], "custom")


class TestGit2hg(unittest.TestCase):
    """inspector.git2hg uses libmozdata's Lando client; port semantics: cache hits +
    misses (LandoMissingCommit), but never cache transient errors (retriable)."""

    def setUp(self):
        from crashclouseau import inspector
        inspector._GIT2HG_CACHE.clear()
        inspector._LANDO = None

    def _fake_lando(self, **kw):
        from unittest import mock
        fake = mock.MagicMock()
        fake.git2hg.configure_mock(**kw)
        return mock.patch("crashclouseau.inspector.LandoCommitMapAPI", return_value=fake), fake

    def test_success_caches(self):
        from crashclouseau import inspector
        from libmozdata.lando import CommitMap
        p, fake = self._fake_lando(return_value=CommitMap(git_hash="g", hg_hash="hh"))
        with p:
            self.assertEqual(inspector.git2hg("g"), "hh")
            self.assertEqual(inspector.git2hg("g"), "hh")   # cached
        fake.git2hg.assert_called_once()

    def test_missing_commit_caches_empty(self):
        from crashclouseau import inspector
        from libmozdata.lando import LandoMissingCommit
        p, _ = self._fake_lando(side_effect=LandoMissingCommit("no"))
        with p:
            self.assertEqual(inspector.git2hg("v"), "")
        self.assertIn("v", inspector._GIT2HG_CACHE)

    def test_transient_error_not_cached(self):
        from crashclouseau import inspector
        p, _ = self._fake_lando(side_effect=RuntimeError("network"))
        with p:
            self.assertEqual(inspector.git2hg("t"), "")
        self.assertNotIn("t", inspector._GIT2HG_CACHE)
