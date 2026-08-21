# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# The exposer classifier's byte set, its rung, and the paragraph a filed bug now carries.
#
# THE PANEL IS A CENSUS, and that is the point of it. tests/poison/poison_fault_panel.json
# holds every distinct fault address in 89 days of Firefox-nightly (2026-05-24..2026-08-20,
# 162,485 reports / 5,234 signatures / 158,285 parseable addresses) that passes
# `_looks_poison`'s own dominance test with the byte-set check removed -- 150 addresses,
# 4,239 reports. Nothing outside that list can fire for ANY choice of `_POISON_BYTES`, so the
# per-byte counts below are exact rather than sampled, and re-running these tests after a
# change to the set says exactly what the change costs. The file also carries the 52 bugs
# filed since 2026-08-05 with their real fault addresses (the pipeline's only outcome panel),
# the in-tree poison constants with file:line, and the five study bugs whose evidence quotes a
# poison literal.
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_exposer_poison
import json
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402

from crashclouseau import report_bug  # noqa: E402
from crashclouseau.agent import orchestrator as orch  # noqa: E402

_PANEL_PATH = os.path.join(os.path.dirname(__file__), "poison", "poison_fault_panel.json")

with open(_PANEL_PATH, encoding="utf-8") as _fh:
    PANEL = json.load(_fh)

CENSUS = PANEL["census"]
BY_BYTE = CENSUS["by_dominant_byte"]


def _dominant_byte(address):
    """The byte `_looks_poison` would test membership on, for an address already known to
    pass its dominance rule."""
    parts, x = [], int(address, 16)
    while x:
        parts.append(x & 0xFF)
        x >>= 8
    return max(set(parts), key=parts.count)


def _fires(byte_set):
    """Reports in the census whose address `_looks_poison` would accept, per dominant byte."""
    return {int(k, 16): v["reports"] for k, v in BY_BYTE.items() if int(k, 16) in byte_set}


class TestCensusShape(unittest.TestCase):
    def test_the_panel_is_the_whole_census(self):
        self.assertEqual(CENSUS["reports"], 162485)
        self.assertEqual(CENSUS["distinct_signatures"], 5234)
        self.assertEqual(CENSUS["parseable_addresses"], 158285)
        self.assertEqual(len(CENSUS["addresses"]), 150)
        self.assertEqual(sum(e["reports"] for e in CENSUS["addresses"]),
                         CENSUS["dominant_reports"])

    def test_the_two_halves_of_the_census_agree(self):
        # `addresses` (per address) and `by_dominant_byte` (per byte) are two reductions of
        # the same 162,485 rows; if they ever disagree the file has been hand-edited.
        rolled = {}
        for entry in CENSUS["addresses"]:
            key = "0x%02X" % _dominant_byte(entry["address"])
            rolled[key] = rolled.get(key, 0) + entry["reports"]
        self.assertEqual(rolled, {k: v["reports"] for k, v in BY_BYTE.items()})
        self.assertEqual(sum(rolled.values()), CENSUS["dominant_reports"])
        for key, row in BY_BYTE.items():
            self.assertEqual(
                row["addresses"],
                sum(1 for e in CENSUS["addresses"]
                    if "0x%02X" % _dominant_byte(e["address"]) == key), key)

    def test_every_panel_address_really_passes_the_dominance_rule(self):
        # The reduction was done with the byte-set check removed; assert that the SHIPPED
        # predicate agrees on the ones whose byte is in the set, so the panel cannot drift
        # away from `_looks_poison` silently.
        for entry in CENSUS["addresses"]:
            fault = int(entry["address"], 16)
            expected = _dominant_byte(entry["address"]) in orch._POISON_BYTES
            self.assertEqual(orch._looks_poison(fault), expected, entry["address"])


class TestThePanelIsRegenerable(unittest.TestCase):
    """The fixture's own ``_readme`` names its regenerator. If that path stops resolving, the
    committed artifact becomes hand-maintained data with a dangling provenance pointer -- which
    is the failure mode this whole file exists to avoid."""

    def test_the_regenerator_named_by_the_readme_is_committed_next_to_it(self):
        named = "tests/poison/rebuild_poison_panel.py"
        self.assertIn(named, PANEL["_readme"])
        self.assertTrue(os.path.exists(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), *named.split("/"))))

    def test_the_committed_bytes_are_exactly_what_json_dump_indent_1_prints(self):
        # The regenerator ends with `print(json.dumps(..., indent=1))`, so `rebuild > fixture`
        # has to be a no-op. It cannot run here (its inputs are session scratch), but the
        # SHAPE it produces is checkable, and a hand-edit that reflows the file fails this.
        with open(_PANEL_PATH, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertEqual(raw, json.dumps(PANEL, indent=1) + "\n")


class TestPoisonByteSet(unittest.TestCase):
    def test_the_shipped_set_fires_on_e5_cc_and_4b_and_nothing_else(self):
        fires = _fires(orch._POISON_BYTES)
        self.assertEqual(fires, {0xE5: 2913, 0xCC: 58, 0x4B: 35})
        self.assertEqual(sum(fires.values()), 3006)                 # 2,971 before 0x4B
        self.assertEqual(BY_BYTE["0xE5"]["signatures"], 99)
        self.assertEqual(BY_BYTE["0xCC"]["signatures"], 6)

    def test_ten_of_the_twelve_original_bytes_fire_zero_times(self):
        # The NULL RESULT the docstring records, so nobody re-derives it. It is not a reason
        # to delete them: a byte that never fires costs no precision.
        inert = {0x2B, 0x5A, 0xAB, 0xBE, 0xCD, 0xDD, 0xE4, 0xFB, 0xFD}
        self.assertEqual(_fires(inert), {})
        self.assertTrue(inert <= orch._POISON_BYTES)

    def test_0xa5_is_gone_and_had_never_fired(self):
        self.assertNotIn(0xA5, orch._POISON_BYTES)
        self.assertEqual(_fires({0xA5}), {})

    def test_0x4b_is_in_and_is_why_the_widening_was_worth_it(self):
        self.assertIn(0x4B, orch._POISON_BYTES)
        # 35 reports over 13 signatures and 22 build days -- a standing pattern, not one
        # bad build and not one machine.
        self.assertEqual(BY_BYTE["0x4B"], {"reports": 35, "signatures": 13,
                                           "build_days": 22, "addresses": 6})
        rows = [e for e in CENSUS["addresses"] if _dominant_byte(e["address"]) == 0x4B]
        top = max(rows, key=lambda e: e["reports"])
        self.assertEqual(top["address"], "0x4b4b4b4b4b4b4b4b")
        self.assertEqual(top["reports"], 24)
        self.assertEqual(top["top_signature"], "JS::Value::isGCThing")

    def test_the_two_byte_trap_is_real_but_never_realised(self):
        # `_looks_poison`'s "at least 2 matching bytes" guard exists so a 2-byte fault cannot
        # qualify on one poison byte. Its cost, measured: over 89 days exactly ONE two-byte
        # address in the census has the 0xXYXY shape at all, and its byte is 0xA4.
        short = [e for e in CENSUS["addresses"] if int(e["address"], 16) < 0x10000]
        self.assertEqual([e["address"] for e in short], ["0xa4a4"])
        self.assertNotIn(0xA4, orch._POISON_BYTES)
        self.assertFalse(orch._looks_poison(0xE512))     # one poison byte is not enough
        self.assertTrue(orch._looks_poison(0xE5E5))      # two is

    def test_importing_the_header_wholesale_is_refuted_by_0xff(self):
        # js/src/util/Poison.h:70 defines JS_OOB_PARSE_NODE_PATTERN = 0xFF, so "source the set
        # from the tree" would add the census's second most common dominant byte -- 1,001
        # reports that are all -1 and friends, not poison. Same trap at the other end with
        # 0x00 (219). This is the wrong-direction case the set must never eat.
        names = {c["byte"] for c in PANEL["in_tree_poison_constants"]}
        self.assertIn("0xff", names)
        self.assertNotIn(0xFF, orch._POISON_BYTES)
        self.assertNotIn(0x00, orch._POISON_BYTES)
        self.assertEqual(_fires({0xFF, 0x00}), {0xFF: 1001, 0x00: 219})
        self.assertFalse(orch._looks_poison(0xFFFFFFFFFFFFFFFF))

    def test_every_byte_we_kept_for_provenance_has_a_cited_constant(self):
        cited = {int(c["byte"], 16) for c in PANEL["in_tree_poison_constants"]}
        # The bytes with an in-tree literal are all in the set except the two the census
        # refutes (0xFF) or whose rate is indistinguishable from zero (0x49/0xED/0xDB).
        self.assertTrue({0xE5, 0xE4, 0xCC, 0xCD, 0xAB, 0x2B, 0x4B} <= cited)
        self.assertTrue({0xE5, 0xE4, 0xCC, 0xCD, 0xAB, 0x2B, 0x4B} <= orch._POISON_BYTES)
        self.assertEqual(_fires({0x49, 0xED, 0xDB}), {0x49: 3, 0xED: 3, 0xDB: 1})


class TestTheFilingsPanelIsUntouched(unittest.TestCase):
    """WHAT THE WIDENING MUST NOT EAT. 0 of the 52 bugs filed since 2026-08-05 sat on a
    poison fault under the old set, and 0 do under the new one -- so adding 0x4B costs the
    only outcome panel we have exactly nothing. It also cost nothing to check: the addresses
    are in the fixture."""

    def test_no_filing_fires_under_the_shipped_set(self):
        self.assertEqual(len(PANEL["filings"]), 52)
        fired = [f["bug"] for f in PANEL["filings"]
                 if orch._looks_poison(int(f["address"], 16))]
        self.assertEqual(fired, [])

    def test_none_of_the_bad_outcomes_was_a_poison_crash_either(self):
        # 7 INVALID + 1 WORKSFORME: the clamp has zero measured saves on this panel. That is
        # "no evidence of a save", not "no save" -- a suppression reaches no Feedback row.
        bad = [f for f in PANEL["filings"]
               if f["resolution"] in ("INVALID", "WORKSFORME")]
        self.assertEqual(len(bad), 8)
        self.assertEqual([f["bug"] for f in bad
                          if orch._looks_poison(int(f["address"], 16))], [])


class TestStudyCounterExamples(unittest.TestCase):
    """CE-5: four of the five study bugs whose evidence quotes a poison literal are NOT
    exposers -- their named regressor was accepted as the cause. Those are the filings the
    50-rung clamp emitted nothing about."""

    def test_all_five_ended_with_an_accepted_regressed_by(self):
        bugs = PANEL["study_poison_literal_bugs"]
        self.assertEqual(len(bugs), 5)
        for b in bugs:
            self.assertEqual(b["resolution"], "FIXED", b["bug"])
            self.assertTrue(b["regressed_by"], b["bug"])
        self.assertIn(1980730, [b["bug"] for b in bugs])       # the 0x4b4b case

    def test_four_of_the_five_are_not_exposers_at_all(self):
        # And that is the discriminator's whole problem: a poison literal appears in 1 of the
        # study's 86 exposers and 4 of its 203 non-exposers (Fisher p=1.00). The clamp was
        # built on a signal that does not separate the two populations.
        study = PANEL["study_289"]
        self.assertEqual((study["exposers"], study["non_exposers"]), (86, 203))
        self.assertEqual((study["exposers_with_poison_literal"],
                          study["non_exposers_with_poison_literal"]), (1, 4))
        labelled = {b["bug"]: b["exposer_not_cause"]
                    for b in PANEL["study_poison_literal_bugs"]}
        self.assertEqual(sorted(b for b, e in labelled.items() if not e),
                         sorted(study["poison_literal_bugs"]["non_exposer"]))
        self.assertEqual(sorted(b for b, e in labelled.items() if e),
                         sorted(study["poison_literal_bugs"]["exposer"]))

    def test_nominating_an_exposer_is_the_accepted_answer(self):
        # 84/86 (98%) vs 196/203 (97%) -- the reason the rung moved up rather than the gate
        # being deleted. Under a nominate-`regressed_by` goal the exposer IS the answer.
        study = PANEL["study_289"]
        self.assertEqual(study["exposers_with_accepted_regressed_by"], 84)
        self.assertEqual(study["non_exposers_with_accepted_regressed_by"], 196)

    def test_the_0x4b_study_bug_is_one_the_widening_now_reaches(self):
        self.assertTrue(orch._looks_poison(0x4B4B4B4B4B4B4B4B))


class TestBmoOutcomePanel(unittest.TestCase):
    """The discriminator's validity, or the lack of it: a poison-fault signature ends in an
    accepted `regressed_by` about as often as a volume-matched non-poison one. This is why
    the answer is 'file it as a lead with a caveat' rather than 'trust it' or 'suppress it'."""

    def test_poison_and_control_signatures_have_the_same_outcomes(self):
        p = PANEL["bmo_signature_panel"]["poison"]
        c = PANEL["bmo_signature_panel"]["control"]
        self.assertEqual((p["n"], c["n"]), (104, 104))
        self.assertEqual((p["accepted_regressed_by"], c["accepted_regressed_by"]), (16, 13))
        self.assertEqual((p["fixed"], c["fixed"]), (25, 18))


class TestExposerNote(unittest.TestCase):
    def test_strong_signal_names_the_address_and_asks_the_right_question(self):
        note = report_bug.build_exposer_note({
            "exposer_suspected": True,
            "exposer_strong": True,
            "exposer_signals": ["poison/freed-memory fault address 0xe5e5e5e5e5e5e5ed"],
        })
        self.assertIn("0xe5e5e5e5e5e5e5ed", note)
        self.assertIn("use-after-free", note)
        self.assertIn("INTRODUCE", note)
        self.assertIn("EXPOSE", note)
        self.assertIn("regressed_by", note)          # never "this changeset is innocent"

    def test_weak_signals_alone_print_nothing(self):
        # failure_class=uaf / a PHC free stack / a data-flow free are true of nearly every
        # lifetime crash we file on; a hedge paragraph on all of them is noise, not honesty.
        self.assertEqual(report_bug.build_exposer_note({
            "exposer_suspected": True,
            "exposer_strong": False,
            "exposer_signals": ["failure_class=uaf", "PHC free stack present"],
        }), "")
        self.assertEqual(report_bug.build_exposer_note({}), "")
        self.assertEqual(report_bug.build_exposer_note(None), "")

    def test_a_strong_signal_with_no_address_still_says_the_useful_half(self):
        note = report_bug.build_exposer_note(
            {"exposer_strong": True, "exposer_signals": []})
        self.assertTrue(note.startswith("The fault address is a run of one poison byte"))

    def test_it_reaches_the_filed_comment(self):
        comment = report_bug.build_bug_comment(
            {"uuid": "u", "channel": "nightly"}, None,
            {"corroborations": {"exposer_strong": True,
                                "exposer_signals": [
                                    "poison/freed-memory fault address 0x4b4b4b4b4b4b4b4b"]}},
        )
        self.assertIn("0x4b4b4b4b4b4b4b4b", comment)
        self.assertIn("only EXPOSE", comment)


if __name__ == "__main__":
    unittest.main()
