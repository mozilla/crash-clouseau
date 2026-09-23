# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""The spike investigator's view of a signature per day and across channels.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
        uv run python -m unittest tests.test_spike_daily
"""
import asyncio
import os
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau.agent import spike_agent, spike_escalation  # noqa: E402
from crashclouseau.agent.tools import crashstats  # noqa: E402

_HISTOGRAM = {"facets": {"histogram_date": [
    {"term": "2026-09-17T00:00:00+00:00", "count": 38,
     "facets": {"release_channel": [{"term": "esr", "count": 20}, {"term": "release", "count": 16},
                                    {"term": "beta", "count": 2}]}},
    {"term": "2026-09-18T00:00:00+00:00", "count": 801,
     "facets": {"release_channel": [{"term": "release", "count": 713},
                                    {"term": "esr", "count": 80}, {"term": "beta", "count": 8}]}},
]}}


def _facets(ctx=None, **kwargs):
    calls = []

    def search(params):
        calls.append(params)
        return _HISTOGRAM if "_histogram.date" in params else {"total": 3, "facets": {
            kwargs.get("field", "platform"): [{"term": "x", "count": 3}]}}

    with mock.patch.object(crashstats, "_search", side_effect=search):
        out = asyncio.run(crashstats.facets(ctx or crashstats.CrashStatsCtx(channel="beta"),
                                            "sig", **kwargs))
    return out, calls


class TestTheFacetsTool(unittest.TestCase):
    def test_by_day_across_channels(self):
        out, calls = _facets(field="release_channel", days=28, by_day=True, all_channels=True)
        self.assertEqual(calls[0]["_histogram.date"], "release_channel")
        self.assertEqual(calls[0]["_histogram_interval.date"], "1d")
        self.assertNotIn("release_channel", calls[0])
        self.assertIn("reports per day for [@ sig], each day by release_channel (product Firefox, "
                      "all channels, last 28d)", out)
        self.assertIn("  2026-09-18: 801 (release 713, esr 80, beta 8)", out)

    def test_by_day_keeps_the_channel_unless_asked(self):
        _out, calls = _facets(field="version", by_day=True)
        self.assertEqual(calls[0]["release_channel"], ["beta", "aurora"])

    def test_by_day_refuses_a_split_or_a_numeric_field(self):
        out, calls = _facets(field="version", by_day=True, split_at_build="20260918091356")
        self.assertIn("does not combine with split_at_build", out)
        out, calls = _facets(field="uptime", by_day=True)
        self.assertIn("not by 'uptime'", out)
        self.assertEqual(calls, [])

    def test_all_channels_drops_the_scope_of_a_plain_facet(self):
        out, calls = _facets(field="platform", all_channels=True)
        self.assertNotIn("release_channel", calls[0])
        self.assertIn("(product Firefox, all channels, last 14d)", out)
        out, calls = _facets(field="platform")
        self.assertIn("(product Firefox, channel beta, last 14d)", out)


class TestTheDailyCounts(unittest.TestCase):
    def test_signature_filters_and_response_rows_are_preserved(self):
        calls = []
        with mock.patch.object(crashstats, "_search",
                               side_effect=lambda p: calls.append(p) or _HISTOGRAM):
            rows = crashstats.daily_counts("Firefox", ["a", "b"], 28)
        self.assertEqual(calls[0]["signature"], ["=a", "=b"])
        self.assertEqual(rows[0], ("2026-09-17", 38, [("esr", 20), ("release", 16), ("beta", 2)]))
        self.assertEqual(crashstats.daily_lines(rows)[1], "  2026-09-18: 801 (release 713, esr 80, "
                                                          "beta 8)")

    def test_a_failure_is_no_series(self):
        with mock.patch.object(crashstats, "_search", return_value={"errors": ["bad"]}):
            self.assertIsNone(crashstats.daily_counts("Firefox", ["a"], 28))
        with mock.patch.object(crashstats, "_search", side_effect=RuntimeError("down")):
            self.assertIsNone(crashstats.daily_counts("Firefox", ["a"], 28))


class TestTheBrief(unittest.TestCase):
    def test_the_brief_asks_for_every_channel_and_every_spelling(self):
        with mock.patch.object(crashstats, "daily_counts", return_value=[]) as daily:
            spike_escalation._daily_by_channel("Firefox", {"b", "a"})
        daily.assert_called_once_with("Firefox", ["a", "b"], spike_escalation._DAILY_DAYS,
                                      "release_channel", "")

    def test_the_series_reaches_the_prompt(self):
        brief = {"signature": "sig", "channel": "beta",
                 "daily": [("2026-09-17", 38, [("esr", 20), ("release", 16)]),
                           ("2026-09-18", 801, [("release", 713), ("esr", 80)])]}
        prompt = spike_agent._user_prompt(brief)
        self.assertIn("REPORTS PER DAY, all channels queried, 2 date buckets", prompt)
        self.assertIn("  2026-09-18: 801 (release 713, esr 80)", prompt)
        self.assertNotIn("REPORTS PER DAY", spike_agent._user_prompt(dict(brief, daily=None)))

    def test_the_prompt_treats_shared_timing_as_a_lead(self):
        self.assertIn("`by_day`", spike_agent._SYSTEM)
        self.assertIn("`all_channels`", spike_agent._SYSTEM)
        self.assertIn("Timing alone does not establish the cause", spike_agent._SYSTEM)


if __name__ == "__main__":
    unittest.main()
