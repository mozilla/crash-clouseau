# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Scoped Bugzilla + Socorro tools for the blind second-opinion agent. libmozdata is mocked
# (no network); the fakes mirror the real call shape (handler(obj, data) + get_data()/wait()).
#   DATABASE_URL=sqlite:// python -m unittest tests.test_second_opinion_tools
import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
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


class _RaisingSuperSearch(_FakeSuperSearch):
    def wait(self):
        raise RuntimeError("HTTP 400")


# The `build_id` rows are here ON PURPOSE, count-ordered and NEW: the tool must never read them
# (first-seen comes from `sigage`), so any regression to the old facet-derived age is visibly
# wrong rather than merely untested.
_FACET_RESULT = {
    "total": 42,
    "facets": {
        "build_id": [{"term": 20260210213212, "count": 37},
                     {"term": 20260201000000, "count": 5}],
        "platform_pretty_version": [{"term": "Windows 10", "count": 40},
                                    {"term": "macOS 14", "count": 2}],
        "process_type": [{"term": "content", "count": 42}],
        "moz_crash_reason": [{"term": "MOZ_RELEASE_ASSERT(x)", "count": 42}],
    },
}
_OLD_BUILD = "20240814213714"


class TestSocorroTool(unittest.TestCase):
    def setUp(self):
        _FakeSuperSearch.RESULT = _FACET_RESULT
        _FakeSuperSearch.last_params = None

    def _run(self, *args, first_seen=_OLD_BUILD, search=_FakeSuperSearch,
             first_seen_channel=None, total=None, other=None, **kwargs):
        """Drive the tool with both Socorro calls faked: the facet query and the sigage
        history lookup (which shares `libmozdata.socorro`, hence the explicit mock).

        ``first_seen_channel`` defaults to ``first_seen`` — i.e. the answer did NOT come from
        the other channels, so the line stays the plain one."""
        hist = {"first_seen": first_seen,
                "first_seen_channel": first_seen if first_seen_channel is None
                else first_seen_channel,
                "total": total, "total_other_channels": other}
        with mock.patch.object(socorro_tool.socorro, "SuperSearch", search), \
                mock.patch.object(socorro_tool.sigage, "signature_history",
                                  return_value=hist) as fs:
            return asyncio.run(crash_stats(*args, **kwargs)), fs

    def test_a_widened_first_seen_names_the_channels_own_too(self):
        """When the answer came from the OTHER channels the model must be told, or it reads a
        two-year-old cross-channel crash as new to this build."""
        out, _ = self._run(SocorroCtx(product="Firefox", channel="nightly"), "Sig",
                           first_seen="20240829075237",
                           first_seen_channel="20260815213849", total=1, other=80)
        self.assertIn("20240829075237", out)
        self.assertIn("ACROSS ALL CHANNELS", out)
        self.assertIn("20260815213849", out)
        self.assertIn("80 of this signature's 81 reports", out)

    def test_an_unwidened_first_seen_says_nothing_about_channels(self):
        out, _ = self._run(SocorroCtx(product="Firefox", channel="nightly"), "Sig")
        self.assertNotIn("ACROSS ALL CHANNELS", out)

    def test_first_seen_comes_from_sigage_not_the_facet(self):
        """THE regression test. The facet holds only NEW builds; first-seen must be the old
        one sigage returns, and the text must state the window so the age reads as a LOWER
        bound."""
        out, fs = self._run(SocorroCtx(product="Firefox", channel="nightly"), "mozilla::Foo::Bar")
        self.assertIn("first-seen buildid: " + _OLD_BUILD, out)
        self.assertNotIn("20260210213212", out)   # what the count-ordered facet would have said
        self.assertNotIn("20260201000000", out)
        self.assertIn("364d", out)                # the window, so age is read as "or older"
        self.assertIn("or older", out)
        fs.assert_called_once_with("mozilla::Foo::Bar", "Firefox", "nightly")
        # the age in days, computed off the buildid rather than hardcoded
        seen = socorro_tool.sigage.to_datetime(_OLD_BUILD)
        expected = (datetime.now(timezone.utc) - seen).days
        self.assertIn("that build is {}d old".format(expected), out)

    def test_build_id_facet_is_never_requested(self):
        """Locks the count-ordered facet out of the query, so it cannot be re-read later."""
        self._run(SocorroCtx(), "Sig")
        self.assertNotIn("build_id", _FakeSuperSearch.last_params["_facets"])

    def test_first_seen_unknown_warns_off_the_counts(self):
        """A failed lookup must not read as 'brand new' — the counts are a 30-day window."""
        out, _ = self._run(SocorroCtx(), "Sig", first_seen=None)
        self.assertIn("first-seen buildid: unknown", out)
        self.assertIn("do NOT infer", out)

    def test_days_is_clamped_at_both_ends(self):
        """`days` is model-supplied and unvalidated: past 365 Socorro answers 400, and a
        negative asks for a FUTURE date, which answers 200 with zero hits — so the agent would
        be told the signature has no recent crashes at all."""
        now = datetime.now(timezone.utc)
        self._run(SocorroCtx(), "Sig", days=3650)
        window = timedelta(days=socorro_tool.sigage.MAX_WINDOW_DAYS)
        self.assertEqual(_FakeSuperSearch.last_params["date"],
                         ">=" + (now - window).strftime("%Y-%m-%d"))
        self._run(SocorroCtx(), "Sig", days=-5)
        self.assertEqual(_FakeSuperSearch.last_params["date"],
                         ">=" + (now - timedelta(days=1)).strftime("%Y-%m-%d"))

    def test_crash_stats_no_data_still_reports_first_seen(self):
        _FakeSuperSearch.RESULT = {}   # handler sets result={} -> falsy -> "no facet data"
        out, _ = self._run(SocorroCtx(), "Sig")
        self.assertIn("no facet data", out)
        self.assertIn("first-seen buildid: " + _OLD_BUILD, out)

    def test_a_failing_lookup_never_raises_into_the_agent_loop(self):
        """The SDK wrapper catches only ToolError, so an unguarded 400 here cost the SO its
        only crash-stats access."""
        out, _ = self._run(SocorroCtx(), "Sig", search=_RaisingSuperSearch)
        self.assertIn("facet lookup failed", out)
        self.assertIn("RuntimeError", out)
        self.assertIn("first-seen buildid: " + _OLD_BUILD, out)

    def test_crash_stats_renders_facets(self):
        out, _ = self._run(
            SocorroCtx(product="Firefox", channel="nightly"), "mozilla::Foo::Bar", days=14)
        self.assertIn("42 crashes", out)
        self.assertIn("OS: Windows 10 (40)", out)
        self.assertIn("process: content (42)", out)
        self.assertIn("MOZ_RELEASE_ASSERT", out)
        # scoped to the signature + product/channel
        self.assertEqual(_FakeSuperSearch.last_params["signature"], "=mozilla::Foo::Bar")
        self.assertEqual(_FakeSuperSearch.last_params["product"], "Firefox")
        self.assertEqual(_FakeSuperSearch.last_params["release_channel"], "nightly")


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

    def test_signature_bugs_name_the_product(self):
        # Every application built on mozilla-central shares Gecko's signatures, and this search
        # spans all of BMO: without the product the model cannot tell a Firefox bug from the
        # Thunderbird one that matched (bug 2057980, `MailNews Core :: Networking: Exchange`).
        _FakeBugzilla.BUGS = {
            2057980: {"id": 2057980, "summary": "opening PDF", "status": "NEW", "resolution": "",
                      "product": "MailNews Core", "component": "Networking: Exchange"},
        }
        with mock.patch.object(bz_tool, "Bugzilla", _FakeBugzilla):
            out = asyncio.run(signature_bugs(BugzillaCtx(), "Foo::Bar"))
        self.assertIn("bug 2057980 [NEW] MailNews Core :: Networking: Exchange — opening PDF",
                      out)
        for field in ("product", "component"):
            self.assertIn(field, _FakeBugzilla.last["params"]["include_fields"])

    def test_signature_bugs_none(self):
        _FakeBugzilla.BUGS = {}
        with mock.patch.object(bz_tool, "Bugzilla", _FakeBugzilla):
            out = asyncio.run(signature_bugs(BugzillaCtx(), "Nope"))
        self.assertIn("no existing bug", out)


if __name__ == "__main__":
    unittest.main()
