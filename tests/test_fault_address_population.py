# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Test fault-address queries, seed facts, persistence and brief formatting.

Run: DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
     uv run python -m unittest tests.test_fault_address_population
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import config, sigfamily, utils  # noqa: E402
from crashclouseau.agent import orchestrator as orch, second_opinion, triage  # noqa: E402
from crashclouseau.agent.schema import Dossier  # noqa: E402

# Signature fixtures: the variant omits one frame.
SIG = ("arena_t::PurgeInfo::ScanForFirstDirtyPage | arena_t::PurgeInfo::FindDirtyPages | "
       "arena_t::PurgeDirtyPages | arena_t::Purge | arena_t::PurgeLoop | "
       "ArenaCollection::MayPurgeAll")
SIB = ("arena_t::PurgeInfo::ScanForFirstDirtyPage | arena_t::PurgeInfo::FindDirtyPages | "
       "arena_t::Purge | arena_t::PurgeLoop | ArenaCollection::MayPurgeAll")
PUSHED = "ArenaCollection::MayPurgeAll"
UNTIL = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
RAW = {"reason": "SIGSEGV / SEGV_MAPERR", "address": "0x000000000000002a", "cpu_arch": "arm64",
       "json_dump": {"crash_info": {"type": "SIGSEGV /SEGV_MAPERR",
                                    "address": "0x000000000000002a"}}}
INFO = {"signature": SIG, "product": "Fenix"}
FAMILY = {"signature_predecessors": [],
          "signature_siblings": [
              {"signature": SIB, "relation": "frame-variant", "status": "older"},
              {"signature": PUSHED, "relation": "pushed-down", "status": "undecided"}]}


def _terms(pairs):
    return [{"term": t, "count": n} for t, n in pairs]


SIB_ADDRESSES = [
    ("0x000000000000002a", 18), ("0x0000000000010029", 3), ("0x0000006d16cd9010", 1),
    ("0x0000006efd6f5010", 1), ("0x0000007002dba010", 1), ("0x00000070eb75fe9a", 1),
    ("0x000000711c349010", 1), ("0x00000077a2d4c010", 1), ("0x00000079414e3010", 1),
    ("0x0000007b55a8f010", 1), ("0x0000007c83a01752", 1), ("0x000100350101005e", 1),
    ("0x0014ce52e1e21a5b", 1), ("0x0035e3235fed27b2", 1), ("0x009a9fa2a4a6a7d3", 1),
    ("0x00fe3a060204044a", 1)]
ANSWER = {"facets": {"signature": [
    {"term": SIB, "count": 35, "facets": {"address": _terms(SIB_ADDRESSES)}},
    {"term": SIG, "count": 2, "facets": {"address": _terms(
        [("0x000000000000002a", 1), ("0x00000071bd33b010", 1)])}},
]}}


# Filtered-query fixtures: counts at 0x2a and below 0x10000.
AT = {"facets": {"signature": _terms([(SIB, 18), (SIG, 1)])}}
NEAR = {"facets": {"signature": _terms([(SIB, 18), (SIG, 1)])}}


def _kind(params):
    if "_aggs.signature" in params:
        return "addresses"
    return "near_null" if str(params.get("address")).startswith("^") else "at"


class FakeRun:
    """Supply canned responses for each of the three queries."""

    def __init__(self, answer=ANSWER, at=AT, near=NEAR, fail=False, broken=()):
        self.answers = {"addresses": answer, "at": at, "near_null": near}
        self.fail, self.broken, self.params = fail, set(broken), []

    def __call__(self, queries):
        if self.fail:
            raise RuntimeError("socorro is down")
        for q in queries:
            self.params.append(q.params)
            kind = _kind(q.params)
            q.handler({"errors": ["boom"]} if kind in self.broken else self.answers[kind],
                      q.handlerdata)


REASONS = ["^SIGSEGV", "^SIGBUS", "^EXCEPTION_ACCESS_VIOLATION", "^EXC_BAD_ACCESS",
           "^EXCEPTION_IN_PAGE_ERROR"]


class TestTheQuery(unittest.TestCase):
    def _call(self, fake, signature=SIG, address="0x000000000000002a", names=(SIB,), arch="arm64"):
        with mock.patch.object(sigfamily, "_run", fake):
            return sigfamily.fault_addresses(signature, address, list(names), "Fenix", arch,
                                             until=UNTIL)

    def test_three_queries_in_one_batch(self):
        fake = FakeRun()
        got = self._call(fake)
        self.assertEqual([_kind(p) for p in fake.params], ["addresses", "at", "near_null"])
        for p in fake.params:
            self.assertEqual(p["signature"], ["=" + SIG, "=" + SIB])
            self.assertEqual(p["product"], "Fenix")
            self.assertEqual(p["cpu_arch"], "arm64")
            self.assertEqual(p["moz_crash_reason"], "__null__")
            self.assertEqual(p["reason"], REASONS, "only memory-access reports are counted")
            self.assertEqual(p["date"], [">=2026-04-19", "<2026-10-19"])
        self.assertEqual(fake.params[0]["_aggs.signature"], "address")
        self.assertEqual(fake.params[1]["address"], "0x000000000000002a")
        self.assertEqual(fake.params[2]["address"], "^0x000000000000")
        self.assertEqual(got["since"], "2026-04-19")
        self.assertEqual(got["rows"][SIG], {"reports": 2, "at": 1, "near_null": 1,
                                            "addresses": {0x2a: 1, 0x71bd33b010: 1}})
        self.assertEqual(got["rows"][SIB]["addresses"][0x10029], 3)

    def test_the_reason_filter_is_the_seed_predicate(self):
        self.assertEqual(REASONS, ["^" + r for r in utils.ACCESS_FAULT_REASONS])

    def test_a_32_bit_address_gets_a_32_bit_prefix(self):
        fake = FakeRun()
        self._call(fake, address="0x0000001e")
        self.assertEqual(fake.params[1]["address"], "0x0000001e")
        self.assertEqual(fake.params[2]["address"], "^0x0000")

    def test_an_unpadded_or_missing_address_asks_nothing(self):
        for address in ("0x2a", "", None, "garbage"):
            fake = FakeRun()
            self.assertIsNone(self._call(fake, address=address), address)
            self.assertEqual(fake.params, [])

    def test_spellings_of_one_address_are_merged_and_unasked_names_dropped(self):
        answer = {"facets": {"signature": [
            {"term": SIG, "count": 3, "facets": {"address": _terms(
                [("0x0000002a", 1), ("0x000000000000002a", 2), ("junk", 9)])}},
            {"term": "Other", "count": 5, "facets": {"address": []}}]}}
        got = self._call(FakeRun(answer, at=_counts(SIG, 3), near=_counts(SIG, 3)), names=())
        self.assertEqual(got["rows"], {SIG: {"reports": 3, "at": 3, "near_null": 3,
                                             "addresses": {0x2a: 3}}})

    def test_no_architecture_asks_all_of_them(self):
        fake = FakeRun()
        self._call(fake, arch=None)
        self.assertTrue(all("cpu_arch" not in p for p in fake.params))

    def test_failures_return_none(self):
        self.assertIsNone(self._call(FakeRun(fail=True)))
        for kind in ("addresses", "at", "near_null"):
            self.assertIsNone(self._call(FakeRun(broken=[kind])), kind)
        self.assertIsNone(self._call(FakeRun(), signature=""))


def _counts(sig, n):
    return {"facets": {"signature": _terms([(sig, n)])}}


class TestSameCrashNames(unittest.TestCase):
    def test_frame_variants_and_spellings_only(self):
        family = {"predecessors": [{"signature": "P", "relation": "spelling"},
                                   {"signature": "Q", "relation": "pushed-down"}],
                  "siblings": [{"signature": SIB, "relation": "frame-variant"},
                               {"signature": "P", "relation": "frame-variant"},
                               {"signature": PUSHED, "relation": "pushed-down"}, "bare-name"]}
        self.assertEqual(sigfamily.same_crash_names(family), ["P", SIB])
        self.assertEqual(sigfamily.same_crash_names(None), [])


class TestAddressCounts(unittest.TestCase):
    def test_the_split(self):
        rows = [{"reports": 10, "at": 4, "near_null": 7,
                 "addresses": {0x2a: 4, 0x0: 2, 0x18: 1, 0x7f0010: 3}},
                {"reports": 3, "at": 1, "near_null": 1, "addresses": {0x2a: 1, 0x10000: 2}}]
        counts, merged = sigfamily.address_counts(rows, 0x2a)
        self.assertEqual(counts, {"reports": 13, "at_address": 5, "near_null": 3,
                                  "elsewhere": 5, "distinct": 5, "complete": True})
        self.assertEqual(merged[0x2a], 5)

    def test_exact_counts_survive_a_capped_facet(self):
        # The analysed address is omitted from the capped facet.
        listed = {0x7f0000000000 + 0x1000 * i: 1 for i in range(100)}
        rows = [{"reports": 125, "at": 1, "near_null": 0, "addresses": listed}]
        counts, _merged = sigfamily.address_counts(rows, 0x7fffffff0000)
        self.assertEqual(counts, {"reports": 125, "at_address": 1, "near_null": 0,
                                  "elsewhere": 124, "distinct": 100, "complete": False})

    def test_a_wild_address_keeps_every_near_null_report(self):
        rows = [{"reports": 10, "at": 3, "near_null": 7, "addresses": {0x7f0010: 3, 0x0: 7}}]
        counts, _merged = sigfamily.address_counts(rows, 0x7f0010)
        self.assertEqual((counts["at_address"], counts["near_null"], counts["elsewhere"]),
                         (3, 7, 0))

    def test_nothing(self):
        counts, merged = sigfamily.address_counts([], 0x2a)
        self.assertEqual(counts["reports"], 0)
        self.assertTrue(counts["complete"])
        self.assertEqual(merged, {})


class TestIsAccessFault(unittest.TestCase):
    def test_reasons(self):
        for reason in ("SIGSEGV / SEGV_MAPERR", "SIGBUS / BUS_ADRERR", "SIGSEGV / SI_KERNEL",
                       "EXCEPTION_ACCESS_VIOLATION_READ", "EXC_BAD_ACCESS / KERN_INVALID_ADDRESS",
                       "EXCEPTION_IN_PAGE_ERROR_READ / STATUS_DEVICE_HARDWARE_ERROR"):
            self.assertTrue(utils.is_access_fault({"reason": reason}), reason)
        for reason in ("SIGABRT", "EXCEPTION_BREAKPOINT", "SIGILL / ILL_ILLOPN",
                       "EXCEPTION_STACK_OVERFLOW", ""):
            self.assertFalse(utils.is_access_fault({"reason": reason}), reason)
        self.assertFalse(utils.is_access_fault(None))

    def test_the_dump_type_stands_in_for_a_missing_reason(self):
        raw = {"json_dump": {"crash_info": {"type": "EXC_BAD_ACCESS / KERN_INVALID_ADDRESS"}}}
        self.assertTrue(utils.is_access_fault(raw))

    def test_a_moz_crash_is_not_one(self):
        self.assertFalse(utils.is_access_fault(
            {"reason": "SIGSEGV / SEGV_MAPERR", "moz_crash_reason": "MOZ_RELEASE_ASSERT(x)"}))
        self.assertFalse(utils.is_access_fault(
            {"reason": "SIGSEGV / SEGV_MAPERR", "json_dump": {"moz_crash_reason": "panic"}}))


EXPECTED = {
    "address": "0x2a", "fault_address": None, "product": "Fenix", "cpu_arch": "arm64", "since": "2026-04-19",
    "days": 182,
    "signature": {"reports": 2, "at_address": 1, "near_null": 0, "elsewhere": 1, "distinct": 2,
                  "complete": True},
    "family": {"reports": 37, "at_address": 19, "near_null": 0, "elsewhere": 18, "distinct": 17,
               "complete": True, "names": 1},
    "addresses": [["0x2a", 19], ["0x10029", 3], ["0x6d16cd9010", 1], ["0x6efd6f5010", 1],
                  ["0x7002dba010", 1], ["0x70eb75fe9a", 1], ["0x711c349010", 1],
                  ["0x71bd33b010", 1], ["0x77a2d4c010", 1], ["0x79414e3010", 1],
                  ["0x7b55a8f010", 1], ["0x7c83a01752", 1], ["0x100350101005e", 1],
                  ["0x14ce52e1e21a5b", 1], ["0x35e3235fed27b2", 1], ["0x9a9fa2a4a6a7d3", 1]],
}


class TestTheSeedFact(unittest.TestCase):
    def _facts(self, raw=RAW, family=FAMILY, run=None, enabled=True):
        cfg = dict(config.get_agent_signature_family(), enabled=enabled)
        real = sigfamily.fault_addresses
        calls = []

        def spy(*args, **kwargs):
            calls.append(args)
            return real(*args, **kwargs, until=UNTIL)

        with mock.patch.object(config, "get_agent_signature_family", return_value=cfg), \
                mock.patch.object(sigfamily, "_run", run or FakeRun()), \
                mock.patch.object(sigfamily, "fault_addresses", side_effect=spy):
            return orch._fault_address_population(INFO, {}, raw, family), calls

    def test_the_family_split(self):
        got, calls = self._facts()
        self.assertEqual(got, EXPECTED)
        self.assertEqual(calls, [(SIG, "0x000000000000002a", [SIB], "Fenix", "arm64")],
                         "the pushed-down name is not asked")

    def test_the_indexed_address_is_the_one_compared(self):
        # Compare indexed 0x0 even when the stackwalker reports 0x180.
        raw = {"reason": "EXCEPTION_ACCESS_VIOLATION_READ", "address": "0x0000000000000000",
               "cpu_arch": "amd64", "json_dump": {"crash_info": {
                   "type": "EXCEPTION_ACCESS_VIOLATION_READ", "address": "0x0000000000000180"}}}
        answer = {"facets": {"signature": [{"term": SIG, "count": 3, "facets": {"address": _terms(
            [("0x0000000000000000", 2), ("0x0000000000000160", 1)])}}]}}
        got, calls = self._facts(raw=raw, family={},
                                 run=FakeRun(answer, at=_counts(SIG, 2), near=_counts(SIG, 3)))
        self.assertEqual(calls[0][1], "0x0000000000000000")
        self.assertEqual((got["address"], got["fault_address"]), ("0x0", "0x180"))
        self.assertEqual(got["signature"]["at_address"], 2)
        lines = triage._fault_address_lines({"fault_address_population": got})
        self.assertIn("This report faults at 0x180, which crash-stats records as 0x0.", lines[1])

    def test_a_capped_facet_still_counts_this_report(self):
        answer = {"facets": {"signature": [{"term": SIG, "count": 125, "facets": {
            "address": _terms([("0x00007f0000001000", 1), ("0x00007f0000002000", 1)])}}]}}
        got, _calls = self._facts(family={}, run=FakeRun(answer, at=_counts(SIG, 1),
                                                         near=_counts(SIG, 1)))
        self.assertEqual(got["signature"], {"reports": 125, "at_address": 1, "near_null": 0,
                                            "elsewhere": 124, "distinct": 2, "complete": False})
        lines = triage._fault_address_lines({"fault_address_population": got})
        self.assertEqual(lines[2], "  this signature: 125 reports: 1 at 0x2a, 124 elsewhere; "
                                   "at least 2 distinct addresses")
        self.assertTrue(lines[3].startswith("  most frequent (counts are lower bounds): "))

    def test_without_another_name_there_is_no_family(self):
        answer = {"facets": {"signature": [ANSWER["facets"]["signature"][1]]}}
        got, _calls = self._facts(family={}, run=FakeRun(answer, at=_counts(SIG, 1),
                                                         near=_counts(SIG, 1)))
        self.assertIsNone(got["family"])
        self.assertEqual(got["signature"]["reports"], 2)
        self.assertEqual(got["addresses"], [["0x2a", 1], ["0x71bd33b010", 1]])

    def test_only_this_report_says_nothing(self):
        answer = {"facets": {"signature": [
            {"term": SIG, "count": 1, "facets": {"address": _terms([("0x2a", 1)])}}]}}
        got, _calls = self._facts(family={}, run=FakeRun(answer, at=_counts(SIG, 1),
                                                         near=_counts(SIG, 1)))
        self.assertIsNone(got)

    def test_not_asked_when_the_address_means_nothing(self):
        no_indexed = {k: v for k, v in RAW.items() if k != "address"}
        for raw in (dict(RAW, moz_crash_reason="MOZ_CRASH()"), dict(RAW, reason="SIGABRT"),
                    {"reason": "SIGSEGV / SEGV_MAPERR"}, no_indexed):
            got, calls = self._facts(raw=raw)
            self.assertIsNone(got)
            self.assertEqual(calls, [])

    def test_the_kill_switch_stops_it(self):
        got, calls = self._facts(enabled=False)
        self.assertIsNone(got)
        self.assertEqual(calls, [])

    def test_a_failed_lookup_is_none(self):
        got, _calls = self._facts(run=FakeRun(fail=True))
        self.assertIsNone(got)

    def test_the_recorder_keeps_it(self):
        d = Dossier(crash={"uuid": "u", "signature": SIG, "frames": []})
        orch._record_fault_address_population(d, {"fault_address_population": EXPECTED})
        self.assertEqual(d.corroborations["fault_address_population"], EXPECTED)
        d = Dossier(crash={"uuid": "u", "signature": SIG, "frames": []})
        orch._record_fault_address_population(d, {"fault_address_population": None})
        self.assertNotIn("fault_address_population", d.corroborations or {})


class TestTheBrief(unittest.TestCase):
    def test_the_block(self):
        lines = triage._fault_address_lines({"fault_address_population": EXPECTED})
        self.assertEqual(lines[0], "")
        self.assertEqual(lines[1], "FAULT ADDRESSES ACROSS REPORTS: Fenix arm64 memory-access "
                                   "reports without a MOZ_CRASH_REASON, all channels, last 182 "
                                   "days (from 2026-04-19). This report faults at 0x2a.")
        self.assertEqual(lines[2], "  this signature: 2 reports: 1 at 0x2a, 1 elsewhere; "
                                   "2 distinct addresses")
        self.assertEqual(lines[3], "  with its 1 other name for the same crash: 37 reports: "
                                   "19 at 0x2a, 18 elsewhere; 17 distinct addresses")
        self.assertTrue(lines[4].startswith("  most frequent: 0x2a (19), 0x10029 (3), "
                                            "0x6d16cd9010 (1)"))
        self.assertEqual(lines[5], triage._FAULT_ADDRESS_GUIDANCE)
        self.assertIn("not a reason to lower confidence or to abstain", lines[5])

    def test_near_null_reports_are_named(self):
        facts = dict(EXPECTED, family=None, addresses=[], cpu_arch=None, since=None, signature={
            "reports": 6, "at_address": 1, "near_null": 5, "elsewhere": 0, "distinct": 1,
            "complete": True})
        lines = triage._fault_address_lines({"fault_address_population": facts})
        self.assertIn("Fenix memory-access reports without", lines[1])
        self.assertIn("last 182 days. This report", lines[1])
        self.assertEqual(lines[2], "  this signature: 6 reports: 1 at 0x2a, 5 more below 0x10000 "
                                   "(a null or near-null pointer); 1 distinct address")
        self.assertEqual(lines[3], triage._FAULT_ADDRESS_GUIDANCE)

    def test_absent_is_silent_and_present_reaches_both_prompts(self):
        self.assertEqual(triage._fault_address_lines({}), [])
        self.assertEqual(triage._fault_address_lines({"fault_address_population": None}), [])
        crash = {"uuid": "u-1", "signature": SIG, "channel": "nightly",
                 "fault_address_population": EXPECTED}
        self.assertIn("FAULT ADDRESSES ACROSS REPORTS", "\n".join(triage._crash_facts(crash)))
        self.assertIn("FAULT ADDRESSES ACROSS REPORTS", second_opinion._user_prompt(crash, None))


if __name__ == "__main__":
    unittest.main()
