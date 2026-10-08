# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Sibling report queries, seed facts, recording, and rendering in both prompts.

Run: DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
     uv run python -m unittest tests.test_sibling_reports
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import sigfamily  # noqa: E402
from crashclouseau.agent import orchestrator as orch, second_opinion, triage  # noqa: E402
from crashclouseau.agent.schema import Dossier  # noqa: E402

A = "kernelbase.dll | Foo::Bar"
B = "third.dll | Foo::Caller"
C = "Foo::Bar"
REASON = "FACILITY_VISUALCPP / ERROR_PROC_NOT_FOUND"
UNTIL = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)


def _row(term, count, field=None, terms=None):
    out = {"term": term, "count": count}
    if field:
        out["facets"] = {field: [{"term": t, "count": n} for t, n in terms.items()]}
    return out


class FakeRun:
    """Mock report-count queries; ``reason=False`` makes the reason response unusable."""

    def __init__(self, all_rows, reason_rows=(), days=(), fail=False, reason=True):
        self.all_rows, self.reason_rows, self.days = all_rows, reason_rows, days
        self.fail, self.reason = fail, reason
        self.params = []

    def __call__(self, queries):
        if self.fail:
            raise RuntimeError("socorro is down")
        for q in queries:
            self.params.append(q.params)
            if "reason" not in q.params:
                answer = {"facets": {"signature": list(self.all_rows)}}
            elif self.reason:
                answer = {"facets": {"signature": list(self.reason_rows),
                                     "histogram_date": list(self.days)}}
            else:
                answer = {"errors": ["boom"]}
            q.handler(answer, q.handlerdata)


def _fake():
    return FakeRun(
        all_rows=[_row(A, 66, "platform", {"Windows NT": 66}),
                  _row(B, 43, "platform", {"Windows NT": 43})],
        reason_rows=[_row(A, 64, "release_channel", {"release": 59, "beta": 2, "aurora": 3})],
        # Unsorted dates exercise earliest-day selection.
        days=[_row("2026-08-05T00:00:00+00:00", 3, "signature", {A: 3}),
              _row("2026-08-03T00:00:00+00:00", 1, "signature", {A: 1})])


class TestSiblingReports(unittest.TestCase):
    def _call(self, fake, reason=REASON, sigs=(A, B)):
        with mock.patch.object(sigfamily, "_run", fake):
            return sigfamily.sibling_reports(list(sigs), reason, "Firefox", until=UNTIL)

    def test_counts_by_platform_and_same_reason_by_channel(self):
        fake = _fake()
        got = self._call(fake)
        self.assertEqual(got["since"], "2026-04-03")
        self.assertTrue(got["reason_counted"])
        self.assertEqual(got["rows"][A], {
            "reports": 66, "platforms": {"Windows NT": 66}, "same_reason": 64,
            "channels": {"release": 59, "beta": 2, "aurora": 3}, "first_day": "2026-08-03"})
        self.assertEqual(got["rows"][B]["same_reason"], 0)
        self.assertEqual(got["rows"][B]["first_day"], None)
        reason_query = next(p for p in fake.params if "reason" in p)
        self.assertEqual(reason_query["reason"], "=" + REASON)
        self.assertEqual(reason_query["signature"], ["=" + A, "=" + B])

    def test_without_a_reason_only_the_platform_split_is_asked(self):
        fake = _fake()
        got = self._call(fake, reason="")
        self.assertFalse(got["reason_counted"])
        self.assertEqual(len(fake.params), 1)
        self.assertEqual(got["rows"][A]["reports"], 66)

    def test_an_unusable_reason_answer_is_not_counted(self):
        fake = _fake()
        fake.reason = False
        got = self._call(fake)
        self.assertFalse(got["reason_counted"])
        self.assertEqual(got["rows"][A]["same_reason"], 0)

    def test_failures_and_empty_input_return_none(self):
        self.assertIsNone(self._call(FakeRun([], fail=True)))
        self.assertIsNone(self._call(_fake(), sigs=()))


SIBLINGS = [
    {"signature": B, "relation": "pushed-down", "status": "coexisting", "total_all_channels": 40},
    {"signature": A, "relation": "frame-variant", "status": "older", "total_all_channels": 60},
    {"signature": C, "relation": "frame-variant", "status": "unclassified",
     "total_all_channels": 9},
]
RAW = {"reason": REASON, "os_name": "Windows NT"}


class TestTheSeedFact(unittest.TestCase):
    def _facts(self, got):
        with mock.patch.object(sigfamily, "sibling_reports", return_value=got) as call:
            out = orch._sibling_report_facts({"signature_siblings": SIBLINGS}, RAW, "Firefox")
        return out, call

    def test_live_siblings_sorted_by_same_reason_reports(self):
        got = {"since": "2026-04-03", "reason_counted": True, "rows": {
            A: {"reports": 66, "platforms": {"Windows NT": 66}, "same_reason": 64,
                "channels": {"release": 59}, "first_day": "2026-08-03"},
            B: {"reports": 43, "platforms": {"Windows NT": 43}, "same_reason": 0,
                "channels": {}, "first_day": None}}}
        out, call = self._facts(got)
        self.assertEqual(call.call_args[0][0], [B, A], "`unclassified` is not live")
        self.assertEqual([r["signature"] for r in out["rows"]], [A, B])
        self.assertEqual(out["rows"][0]["same_reason"], 64)
        self.assertEqual(out["reason"], REASON)
        self.assertEqual(out["platform"], "Windows NT")
        self.assertEqual(out["since"], "2026-04-03")

    def test_failed_counts_fall_back_to_the_discovery_totals(self):
        out, _call = self._facts(None)
        self.assertIsNone(out["reason"])
        self.assertEqual([(r["signature"], r["reports"], r["same_reason"]) for r in out["rows"]],
                         [(A, 60, None), (B, 40, None)])

    def test_no_live_sibling_asks_nothing(self):
        with mock.patch.object(sigfamily, "sibling_reports") as call:
            self.assertIsNone(orch._sibling_report_facts(
                {"signature_siblings": SIBLINGS[2:]}, RAW, "Firefox"))
            self.assertIsNone(orch._sibling_report_facts({}, RAW, "Firefox"))
        call.assert_not_called()

    def test_the_recorder_keeps_it(self):
        facts = {"reason": REASON, "rows": [{"signature": A}]}
        d = Dossier(crash={"uuid": "u", "signature": "S", "frames": []})
        orch._record_signature_age_facts(d, {"signature": "S", "signature_family_lookup": "ok",
                                             "signature_sibling_reports": facts})
        self.assertEqual(d.corroborations["signature_sibling_reports"], facts)


class TestPushedDownSiblingsAreNotFilingNames(unittest.TestCase):
    def test_the_brief_keeps_them_and_the_filer_does_not(self):
        with mock.patch.object(sigfamily, "sibling_reports", return_value=None):
            brief = orch._sibling_report_facts({"signature_siblings": SIBLINGS}, RAW, "Firefox")
        self.assertIn(B, [r["signature"] for r in brief["rows"]])
        d = Dossier(crash={"uuid": "u", "signature": "S", "frames": []})
        orch._record_signature_age_facts(d, {"signature": "S", "signature_family_lookup": "ok",
                                             "signature_siblings": SIBLINGS})
        self.assertEqual(d.corroborations["signature_siblings_live"], [A])
        family = {"predecessors": [{"signature": "P", "relation": "pushed-down",
                                    "status": "handoff"}],
                  "siblings": SIBLINGS[:2]}
        self.assertEqual(sigfamily.spellings(family), ["P", A],
                         "a pushed-down predecessor is still a name")

    def test_an_undecided_one_stays_a_filing_name(self):
        undecided = dict(SIBLINGS[0], status="undecided")
        d = Dossier(crash={"uuid": "u", "signature": "S", "frames": []})
        orch._record_signature_age_facts(d, {"signature": "S", "signature_family_lookup": "ok",
                                             "signature_siblings": [undecided, SIBLINGS[1]]})
        self.assertEqual(d.corroborations["signature_siblings_live"], [B, A])
        self.assertEqual(sigfamily.spellings({"siblings": [undecided]}), [B])

    def test_name_only_rows_pass(self):
        self.assertTrue(sigfamily.is_filing_sibling(A))
        self.assertTrue(sigfamily.is_filing_sibling({"signature": A}))
        self.assertFalse(sigfamily.is_filing_sibling(SIBLINGS[0]))

    def test_unsymbolicated_names_are_never_filing_names(self):
        wait = "shutdownhang | RtlWaitOnAddress | WaitOnAddress"
        self.assertFalse(sigfamily.is_filing_sibling(wait))
        self.assertFalse(sigfamily.is_filing_sibling({"signature": wait}))
        self.assertFalse(sigfamily.is_filing_sibling(
            {"signature": wait, "relation": "pushed-down", "status": "undecided"}))
        family = {"predecessors": [{"signature": wait, "relation": "pushed-down",
                                    "status": "handoff"}, {"signature": "P"}],
                  "siblings": [{"signature": wait}, {"signature": A}]}
        self.assertEqual(sigfamily.spellings(family), ["P", A])


BLOCK = {"reason": REASON, "platform": "Windows NT", "since": "2026-04-03", "days": 182,
         "rows": [
             {"signature": A, "relation": "frame-variant", "reports": 66,
              "platforms": {"Windows NT": 66}, "same_reason": 64,
              "channels": {"release": 59, "aurora": 3, "beta": 2}, "first_day": "2026-08-03"},
             {"signature": B, "relation": "pushed-down", "reports": 1,
              "platforms": {"Mac OS X": 1}, "same_reason": 0, "channels": {},
              "first_day": None}]}


class TestTheBrief(unittest.TestCase):
    def test_the_block(self):
        lines = triage._sibling_lines({"signature_sibling_reports": BLOCK})
        self.assertEqual(lines[0], "")
        self.assertIn("all channels over the last 182 days (from 2026-04-03)", lines[1])
        self.assertIn("`{}` (this crash is on Windows NT)".format(REASON), lines[1])
        self.assertIn("do not show that it is the same crash", lines[1])
        self.assertEqual(lines[2], "  `{}` (shares a frame, at most two frames differ): 66 reports "
                                   "(Windows NT 66); 64 with the same reason (release 59, aurora "
                                   "3, beta 2), first on 2026-08-03".format(A))
        self.assertEqual(lines[3], "  `{}` (its frames are deeper in this crash's stack): 1 report "
                                   "(Mac OS X 1); none with the same reason".format(B))

    def test_without_reason_counts(self):
        block = dict(BLOCK, reason=None, since=None,
                     rows=[dict(r, same_reason=None, platforms={}) for r in BLOCK["rows"]])
        lines = triage._sibling_lines({"signature_sibling_reports": block})
        self.assertIn("same-reason counts are unavailable", lines[1])
        self.assertEqual(lines[2], "  `{}` (shares a frame, at most two frames differ): 66 "
                                   "reports".format(A))

    def test_the_list_is_capped(self):
        rows = [dict(BLOCK["rows"][1], signature="S{}".format(i)) for i in range(7)]
        lines = triage._sibling_lines({"signature_sibling_reports": dict(BLOCK, rows=rows)})
        self.assertEqual(len(lines), 2 + triage._MAX_SIBLING_LINES + 1)
        self.assertEqual(lines[-1], "  ... 2 more")

    def test_absent_is_silent_and_present_reaches_both_prompts(self):
        self.assertEqual(triage._sibling_lines({}), [])
        crash = {"uuid": "u-1", "signature": "S", "channel": "nightly",
                 "signature_sibling_reports": BLOCK}
        self.assertIn("SIBLING SIGNATURES", "\n".join(triage._crash_facts(crash)))
        self.assertIn("SIBLING SIGNATURES", second_opinion._user_prompt(crash, None))


if __name__ == "__main__":
    unittest.main()
