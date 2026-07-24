# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Scoped Bugzilla + Socorro tools for the blind second-opinion agent. libmozdata is mocked
# (no network); the fakes mirror the real call shape (handler(obj, data) + get_data()/wait()).
#   DATABASE_URL=sqlite:// python -m unittest tests.test_second_opinion_tools
import asyncio
import os
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite://")

from crashclouseau.agent.tools import bugzilla as bz_tool  # noqa: E402
from crashclouseau.agent.tools import socorro as socorro_tool  # noqa: E402
from crashclouseau.agent.tools.bugzilla import BugzillaCtx, bug, signature_bugs  # noqa: E402
from crashclouseau.agent.tools.socorro import SocorroCtx, crash_stats  # noqa: E402


class _FakeSuperSearch:
    RESULT = {}
    last_params = None

    def __init__(self, params=None, handler=None, handlerdata=None, **kw):
        self._h, self._d = handler, handlerdata
        _FakeSuperSearch.last_params = params

    def wait(self):
        self._h(_FakeSuperSearch.RESULT, self._d)  # SuperSearch: handler(json, data)
        return self


class TestSocorroTool(unittest.TestCase):
    def test_crash_stats_renders_facets(self):
        _FakeSuperSearch.RESULT = {
            "total": 42,
            "facets": {
                "build_id": [{"term": 20260102000000, "count": 37},
                             {"term": 20260101000000, "count": 5}],   # unsorted on purpose
                "platform_pretty_version": [{"term": "Windows 10", "count": 40},
                                            {"term": "macOS 14", "count": 2}],
                "process_type": [{"term": "content", "count": 42}],
                "moz_crash_reason": [{"term": "MOZ_RELEASE_ASSERT(x)", "count": 42}],
            },
        }
        with mock.patch.object(socorro_tool.socorro, "SuperSearch", _FakeSuperSearch):
            out = asyncio.run(crash_stats(
                SocorroCtx(product="Firefox", channel="nightly"), "mozilla::Foo::Bar", days=14))
        self.assertIn("42 crashes", out)
        self.assertIn("first-seen buildid: 20260101000000", out)   # min across builds
        self.assertIn("OS: Windows 10 (40)", out)
        self.assertIn("process: content (42)", out)
        self.assertIn("MOZ_RELEASE_ASSERT", out)
        # scoped to the signature + product/channel
        self.assertEqual(_FakeSuperSearch.last_params["signature"], "=mozilla::Foo::Bar")
        self.assertEqual(_FakeSuperSearch.last_params["product"], "Firefox")
        self.assertEqual(_FakeSuperSearch.last_params["release_channel"], "nightly")

    def test_crash_stats_no_data(self):
        _FakeSuperSearch.RESULT = {}   # handler sets result={} -> falsy -> "no data"
        with mock.patch.object(socorro_tool.socorro, "SuperSearch", _FakeSuperSearch):
            out = asyncio.run(crash_stats(SocorroCtx(), "Sig"))
        self.assertIn("no data", out)


class _FakeBugzilla:
    BUGS: dict = {}
    last: dict = {}

    def __init__(self, params=None, bugids=None, include_fields=None,
                 bughandler=None, bugdata=None, **kw):
        self._h, self._d = bughandler, bugdata
        _FakeBugzilla.last = {"params": params, "bugids": bugids}

    def get_data(self):
        return self

    def wait(self):
        for b in _FakeBugzilla.BUGS.values():
            self._h(b, self._d)   # Bugzilla: bughandler(bug, data)
        return self


class TestBugzillaTool(unittest.TestCase):
    def test_bug_info(self):
        _FakeBugzilla.BUGS = {123: {
            "id": 123, "product": "Core", "component": "DOM: Core & HTML",
            "summary": "boom on release", "status": "RESOLVED", "resolution": "FIXED",
            "keywords": ["regression", "crash"], "regressed_by": [100],
            "regressions": [200, 201], "dupe_of": None}}
        with mock.patch.object(bz_tool, "Bugzilla", _FakeBugzilla):
            out = asyncio.run(bug(BugzillaCtx(), 123))
        self.assertIn("bug 123 — Core :: DOM: Core & HTML", out)
        self.assertIn("boom on release", out)
        self.assertIn("RESOLVED FIXED", out)
        self.assertIn("regressed_by: 100", out)
        self.assertIn("regressions: 200, 201", out)

    def test_bug_not_found(self):
        _FakeBugzilla.BUGS = {}
        with mock.patch.object(bz_tool, "Bugzilla", _FakeBugzilla):
            out = asyncio.run(bug(BugzillaCtx(), 999))
        self.assertIn("not accessible", out)

    def test_signature_bugs(self):
        _FakeBugzilla.BUGS = {
            10: {"id": 10, "summary": "old dupe", "status": "RESOLVED", "resolution": "DUPLICATE"},
            20: {"id": 20, "summary": "open one", "status": "NEW", "resolution": ""},
        }
        with mock.patch.object(bz_tool, "Bugzilla", _FakeBugzilla):
            out = asyncio.run(signature_bugs(BugzillaCtx(), "Foo::Bar"))
        self.assertIn("bug 20 [NEW]", out)
        self.assertIn("bug 10 [RESOLVED DUPLICATE]", out)
        self.assertEqual(_FakeBugzilla.last["params"]["v1"], "Foo::Bar")   # scoped to the signature

    def test_signature_bugs_none(self):
        _FakeBugzilla.BUGS = {}
        with mock.patch.object(bz_tool, "Bugzilla", _FakeBugzilla):
            out = asyncio.run(signature_bugs(BugzillaCtx(), "Nope"))
        self.assertIn("no existing bug", out)


if __name__ == "__main__":
    unittest.main()
