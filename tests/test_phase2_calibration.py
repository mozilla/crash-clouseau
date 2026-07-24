# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# DATABASE_URL=sqlite:// python -m unittest tests.test_phase2_calibration
import json
import os
import tempfile
import unittest
from unittest import mock

from crashclouseau import config
from crashclouseau.agent import orchestrator as ORCH
from crashclouseau.agent.schema import Confidence, Decision
from crashclouseau.eval import calibrate as CAL
from crashclouseau.eval import corpus as C
from crashclouseau.eval import metrics as M
from crashclouseau.eval import runner as R
from crashclouseau.eval import study_corpus as SC

# Reuse the fake-result/case builders from the existing eval-metrics test.
from tests.test_eval_metrics import _case, _result


def _write(path, obj):
    with open(path, "w") as handle:
        json.dump(obj, handle)


def _blind(bug, sig, stack_files, window, neg=False, pin=None):
    d = {
        "bug_id": bug,
        "crash": {
            "signature": sig,
            "product": "Core",
            "moz_crash_reason": "",
            "stack_files": stack_files,
            "top_frames": [
                {"stackpos": 0, "module": "xul", "function": "F::g",
                 "file": "path/" + stack_files[0], "line": 10},
            ],
        },
        "candidate_window": window,
        "window_size": len(window),
    }
    if neg or pin:
        d.update({"build_hg_rev": pin or "buildhg000000",
                  "prev_hg_rev": "prevhg000000",
                  "build_git_commit": "g" * 40, "prev_git_commit": "p" * 40})
    return d


def _answer(bug, reg_bug, reg_node, on_stack, revision="bldrev000000"):
    return {
        "crash_bug": {"id": bug, "regressed_by": [reg_bug],
                      "signatures": ["Sig"], "cf_crash_signature": ""},
        "crash_stack": {"uuid": "uuid-%d" % bug, "frames": [], "stack_files": []},
        "regressors": [{
            "bug": reg_bug, "landing_revs": [reg_node], "changesets": [],
            "on_stack": on_stack, "on_stack_files": [],
            "first_build": {"buildid": "20250101000000", "version": "140.0",
                            "revision": revision, "prev_revision": "prev00000000"},
        }],
        "fix": {"landing_revs": [], "changesets": [], "is_backout": False, "in_tree": False},
        "strategy": {"tags": {}},
    }


class TestStudyCorpusAdapter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.blind = os.path.join(self.tmp, "blind")
        self.answer = os.path.join(self.tmp, "answer")
        self.corpus = os.path.join(self.tmp, "corpus")
        os.makedirs(self.blind)
        os.makedirs(self.answer)

        # ON-STACK positive: a candidate touches the stack file A.cpp.
        onwin = [{"node": "n0on00000000", "bug": 999, "desc": "Bug 999 - fix A",
                  "backedout": False, "files": ["dom/A.cpp"], "n_files": 1},
                 {"node": "n1noise00000", "bug": 555, "desc": "Bug 555 - other",
                  "backedout": False, "files": ["z/Z.cpp"], "n_files": 1}]
        _write(os.path.join(self.blind, "111.json"),
               _blind(111, "F::g", ["A.cpp"], onwin))
        _write(os.path.join(self.answer, "111.json"),
               _answer(111, 999, "n0on00000000", True))

        # OFF-STACK positive: no candidate touches the stack file B.cpp.
        offwin = [{"node": "off0000000aa", "bug": 888,
                   "desc": "Bug 888 - Enable Feature by default", "backedout": False,
                   "files": ["c/C.cpp"], "n_files": 1},
                  {"node": "off0000000bb", "bug": 777, "desc": "Bug 777 - unrelated",
                   "backedout": True, "files": ["d/D.cpp"], "n_files": 1}]
        _write(os.path.join(self.blind, "222.json"),
               _blind(222, "H::k", ["B.cpp"], offwin))
        _write(os.path.join(self.answer, "222.json"),
               _answer(222, 888, "off0000000aa", False))

        # NEGATIVE for bug 111: regressor removed; remaining window is off-stack.
        negwin = [{"node": "n1noise00000", "bug": 555, "desc": "Bug 555 - other",
                   "backedout": False, "files": ["z/Z.cpp"], "n_files": 1}]
        _write(os.path.join(self.blind, "111.neg.json"),
               _blind(111, "F::g", ["A.cpp"], negwin, neg=True))

        # Non-fixture aggregate file that must be skipped.
        _write(os.path.join(self.blind, "_sf_cases.json"), [1, 2, 3])

        # NON-TARGET (Java/Fenix) crash — Clouseau doesn't triage it; must be filtered out.
        jwin = [{"node": "j00000000000", "bug": 333, "desc": "Bug 333",
                 "backedout": False, "files": ["x/X.kt"], "n_files": 1}]
        jblind = _blind(333, "java.lang.IllegalStateException", ["Frag.kt"], jwin)
        jblind["crash"]["product"] = "Firefox for Android"
        _write(os.path.join(self.blind, "333.json"), jblind)
        _write(os.path.join(self.answer, "333.json"), _answer(333, 444, "j0reg0000000", True))

        SC.build_study_corpus(self.blind, self.answer, self.corpus)
        self.cases, _ = C.load_corpus(self.corpus)
        self.by_uuid = {c.uuid: c for c in self.cases}

    def test_counts_and_skip_aggregate(self):
        # 3 target fixtures (111, 222, 111.neg); _sf_cases.json + the Java 333 skipped.
        self.assertEqual(len(self.cases), 3)

    def test_skips_nontarget_java(self):
        self.assertNotIn("uuid-333", self.by_uuid)
        self.assertFalse(SC._is_target_crash(
            {"signature": "java.lang.X", "top_frames": [{"file": "a"}]}))
        self.assertFalse(SC._is_target_crash(
            {"signature": "Foo::bar", "product": "Firefox for Android",
             "top_frames": [{"file": "a"}]}))
        self.assertTrue(SC._is_target_crash(
            {"signature": "mozilla::X::y", "product": "Core",
             "top_frames": [{"file": "dom/A.cpp"}]}))

    def test_onstack_case(self):
        c = self.by_uuid["uuid-111"]
        self.assertFalse(c.is_offstack)
        self.assertFalse(c.is_negative)
        self.assertEqual(c.regressor_bugs, [999])
        self.assertIn("n0on00000000", c.regressor_nodes)
        # On-stack seeds the overlap-scored subset (only the A.cpp candidate scored).
        self.assertEqual(len(c.candidates), 1)
        self.assertEqual(c.candidates[0]["node"], "n0on00000000")
        self.assertEqual(c.candidates[0]["score"], 1)
        self.assertEqual(c.pin_rev, "bldrev000000")

    def test_offstack_case(self):
        c = self.by_uuid["uuid-222"]
        self.assertTrue(c.is_offstack)
        # Off-stack feeds the FULL window, score=None.
        self.assertEqual(len(c.candidates), 2)
        self.assertTrue(all(cand["score"] is None for cand in c.candidates))
        # The "Enable ... by default" candidate is pref-flip tagged + ranks first.
        self.assertTrue(c.candidates[0]["pref_flip"])
        self.assertEqual(c.candidates[0]["node"], "off0000000aa")

    def test_negative_case(self):
        c = self.by_uuid["uuid-111-neg"]
        self.assertTrue(c.is_negative)
        self.assertTrue(c.is_offstack)
        self.assertEqual(c.regressor_bugs, [])
        self.assertEqual(c.regressor_nodes, [])
        # pin_rev comes from the blind build_hg_rev on the .neg fixture.
        self.assertEqual(c.pin_rev, "buildhg000000")

    def test_case_to_crash_sets_offstack_markers(self):
        crash = R._case_to_crash(self.by_uuid["uuid-222"])
        self.assertTrue(crash["is_offstack"])
        self.assertEqual(crash["pin_rev"], "bldrev000000")
        self.assertEqual(crash["prior_hints"], [])


class TestCalibrationMetrics(unittest.TestCase):
    def test_per_case_rows_and_false_investigate(self):
        cases = [
            _case("u-hit", reg="regnode00001"),                     # positive, will hit
            _case("u-miss", reg="regnode00002"),                    # positive, abstain
            _case("u-neg", reg="", is_negative=True),               # negative, reported=FP
        ]
        results = {
            "u-hit": _result(node="regnode00001", lead=True),       # lead, cites regressor
            "u-miss": _result(),                                    # abstain
            "u-neg": _result(node="somethingelse", lead=True),      # lead on absent culprit
        }
        rows = M.per_case_rows(cases, results)
        by = {r["uuid"]: r for r in rows}
        self.assertTrue(by["u-hit"]["hit"])
        self.assertTrue(by["u-hit"]["reported"])
        self.assertEqual(by["u-hit"]["score"], 50)                  # medium
        self.assertEqual(by["u-miss"]["decision"], "abstain")
        # An abstain is never "reported", so its rung is excluded from calibration even
        # though the raw confidence still maps to a score.
        self.assertFalse(by["u-miss"]["reported"])
        self.assertTrue(by["u-neg"]["is_negative"])
        self.assertTrue(by["u-neg"]["reported"])
        self.assertFalse(by["u-neg"]["hit"])

        fi = M.false_investigate(cases, results)
        self.assertEqual(fi["n_negative"], 1)
        self.assertEqual(fi["n_false_investigate"], 1)
        self.assertEqual(fi["false_investigate_rate"], 1.0)

    def test_reliability_bins_and_ece(self):
        rows = [
            {"reported": True, "score": 50, "hit": True, "is_negative": False},
            {"reported": True, "score": 50, "hit": False, "is_negative": False},
            {"reported": True, "score": 70, "hit": True, "is_negative": False},
            {"reported": True, "score": 70, "hit": True, "is_negative": False},
            {"reported": False, "score": None, "hit": False, "is_negative": False},
        ]
        bins = M.reliability_bins(rows)
        b50 = next(b for b in bins if b["score"] == 50)
        b70 = next(b for b in bins if b["score"] == 70)
        self.assertEqual(b50["p_hit"], 0.5)
        self.assertEqual(b70["p_hit"], 1.0)
        # ECE = (|.5-.5|*2 + |.7-1|*2)/4 = 0.15
        self.assertAlmostEqual(M.ece(bins), 0.15, places=6)

    def test_reliability_bins_count_person_worth(self):
        # The calibration is PERSON-level: a silver-nugget row (exact miss, right person =>
        # worth=True) counts toward the rung's P, decoupling worth-investigating from proof.
        rows = [
            {"reported": True, "score": 70, "hit": False, "worth": True, "is_negative": False},
            {"reported": True, "score": 70, "hit": False, "worth": False, "is_negative": False},
            {"reported": True, "score": 70, "hit": False, "is_negative": True},   # negative miss
        ]
        b70 = next(b for b in M.reliability_bins(rows) if b["score"] == 70)
        self.assertEqual(b70["n_hit"], 1)          # only the silver nugget; true miss + neg don't
        self.assertEqual(b70["n_negative"], 1)
        self.assertAlmostEqual(b70["p_hit"], 1 / 3, places=6)
        # threshold_sweep TP is person-level too: 1 worth-hit of 3 reported => precision 1/3.
        sweep = {r["tau"]: r for r in M.threshold_sweep(rows)}
        self.assertAlmostEqual(sweep[70]["precision"], 1 / 3, places=6)

    def test_threshold_sweep_precision_and_fp(self):
        rows = [
            {"reported": True, "score": 70, "hit": True, "is_negative": False},
            {"reported": True, "score": 50, "hit": False, "is_negative": False},
            {"reported": True, "score": 50, "hit": False, "is_negative": True},   # FP neg
        ]
        sweep = {r["tau"]: r for r in M.threshold_sweep(rows)}
        self.assertEqual(sweep[70]["precision"], 1.0)
        self.assertEqual(sweep[70]["n_false_investigate"], 0)
        # At tau=50 all three reported: 1 TP of 3 => precision 1/3; 1 of 1 negative reported.
        self.assertAlmostEqual(sweep[50]["precision"], 1 / 3, places=6)
        self.assertEqual(sweep[50]["n_false_investigate"], 1)
        self.assertEqual(sweep[50]["false_investigate_rate"], 1.0)

    def test_wilson_ci(self):
        low, high = M.wilson_ci(0, 30)
        self.assertEqual(low, 0.0)
        self.assertLess(high, 0.12)          # ~0.11 upper bound for 0/30
        self.assertGreater(high, 0.10)


class TestCalibrateModule(unittest.TestCase):
    def test_isotonic_monotone(self):
        bins = [
            {"score": 50, "n": 4, "p_hit": 0.8},   # violates monotonicity vs 70
            {"score": 70, "n": 4, "p_hit": 0.4},
            {"score": 85, "n": 2, "p_hit": 0.9},
        ]
        fitted = CAL.isotonic(bins)
        self.assertEqual(len(fitted), 3)
        self.assertLessEqual(fitted[0], fitted[1] + 1e-9)
        self.assertLessEqual(fitted[1], fitted[2] + 1e-9)
        # 50 & 70 pool to the weighted mean (0.6); 85 stays 0.9.
        self.assertAlmostEqual(fitted[0], 0.6, places=6)
        self.assertAlmostEqual(fitted[2], 0.9, places=6)

    def test_pick_threshold_precision_first(self):
        rows = [
            {"reported": True, "score": 85, "hit": True, "is_negative": False, "uuid": "a"},
            {"reported": True, "score": 70, "hit": True, "is_negative": False, "uuid": "b"},
            {"reported": True, "score": 50, "hit": False, "is_negative": True, "uuid": "c"},
        ]
        thr = CAL.pick_threshold(rows)
        # Max reported-negative score = 50 => precision-first tau is the next rung up (70).
        self.assertEqual(thr["max_negative_score"], 50)
        self.assertEqual(thr["tau_precision_first"], 70)

    def test_calibrate_end_to_end(self):
        tmp = tempfile.mkdtemp()
        rows = []
        # 20 medium (half hit), 20 probable (mostly hit), 6 negatives reported at medium.
        for i in range(20):
            rows.append({"uuid": "m%d" % i, "reported": True, "score": 50,
                         "hit": i % 2 == 0, "is_negative": False})
        for i in range(20):
            rows.append({"uuid": "p%d" % i, "reported": True, "score": 70,
                         "hit": i % 5 != 0, "is_negative": False})
        for i in range(6):
            rows.append({"uuid": "n%d" % i, "reported": (i < 1), "score": 50,
                         "hit": False, "is_negative": True})
        with open(os.path.join(tmp, "results.jsonl"), "w") as handle:
            for r in rows:
                handle.write(json.dumps(r) + "\n")
        res = CAL.calibrate(tmp, target_precision=0.8)
        self.assertTrue(os.path.exists(os.path.join(tmp, "calibration_table.json")))
        self.assertEqual(res["n_negative"], 6)
        # Calibrated table is monotone non-decreasing across rungs.
        table = res["calibration_table"]
        vals = [table[k] for k in sorted(table, key=int)]
        self.assertEqual(vals, sorted(vals))


class TestPersonLevel(unittest.TestCase):
    """The person-level ('silver nugget') metric — offline, no hg lookups."""

    def test_author_email_normalises(self):
        from crashclouseau.eval import authors
        self.assertEqual(authors.author_email("Sotaro Ikeda <SOTARO@x.com>"), "sotaro@x.com")
        self.assertEqual(authors.author_email("plainhandle"), "plainhandle")

    def test_same_person(self):
        from crashclouseau.eval import authors
        regs = ["Sotaro Ikeda <sotaro@x.com>", "Other Dev <other@y.com>"]
        self.assertTrue(authors.same_person("Sotaro Ikeda <sotaro@x.com>", regs))   # exact
        self.assertTrue(authors.same_person("S. Ikeda <SOTARO@x.com>", regs))       # diff name, same email
        self.assertFalse(authors.same_person("Nope <nope@z.com>", regs))
        self.assertFalse(authors.same_person("", regs))

    def test_person_hit_uses_candidate_author(self):
        # A lead blaming the WRONG changeset but the SAME author counts as a person-hit.
        case = _case("u", reg="somenode0000")
        case.regressor_authors = ["Real Regressor <rr@moz.com>"]
        r = _result(node="wrongnode0001", lead=True)  # cites a different changeset
        author_of = {"wrongnode0001": "Real Regressor <rr@moz.com>"}.get
        self.assertTrue(M.person_hit(case, r.dossier, author_of))
        author_of2 = {"wrongnode0001": "Someone Else <se@moz.com>"}.get
        self.assertFalse(M.person_hit(case, r.dossier, author_of2))

    def test_per_case_rows_person_columns(self):
        # With an author resolver, each row carries person_hit + the resolved cited_author.
        case = _case("u", reg="somenode0000")
        case.regressor_authors = ["Real Regressor <rr@moz.com>"]
        r = _result(node="wrongnode0001", lead=True)
        author_of = {"wrongnode0001": "Real Regressor <rr@moz.com>"}.get
        row = M.per_case_rows([case], {"u": r}, author_of=author_of)[0]
        self.assertTrue(row["person_hit"])
        self.assertEqual(row["cited_author"], "Real Regressor <rr@moz.com>")
        # Offline (no resolver) the person columns are simply absent — no network lookup.
        row_offline = M.per_case_rows([case], {"u": r})[0]
        self.assertNotIn("person_hit", row_offline)

    def test_person_metrics_and_compute(self):
        # One wrong-changeset-right-author positive (person-hit) + one reported negative (person-FP).
        pos = _case("u", reg="somenode0000")
        pos.regressor_authors = ["Real Regressor <rr@moz.com>"]
        neg = _case("n", reg="", is_negative=True)                      # empty regressor set
        rpos = _result(node="wrongnode0001", lead=True)
        rneg = _result(node="otherx000000", lead=True)                  # reported on absent culprit
        author_of = {"wrongnode0001": "Real Regressor <rr@moz.com>",
                     "otherx000000": "Somebody Else <se@moz.com>"}.get
        pm = M.person_metrics([pos, neg], {"u": rpos, "n": rneg}, author_of)
        self.assertEqual(pm["n_reported"], 2)
        self.assertEqual(pm["n_person_hit"], 1)          # the negative reaches no true author
        self.assertEqual(pm["person_precision"], 0.5)
        # compute_metrics threads it through only when an author resolver is supplied.
        m = M.compute_metrics([pos, neg], {"u": rpos, "n": rneg}, author_of=author_of)
        self.assertEqual(m.person_precision, 0.5)
        self.assertEqual(m.n_person_hit, 1)
        self.assertEqual(m.n_reported, 2)
        # The precision-first false-investigate trio must survive the mapping into Metrics
        # (a reported negative exists here) — guards a key-swap in compute_metrics.
        self.assertEqual(m.n_negative, 1)
        self.assertEqual(m.n_false_investigate, 1)
        self.assertEqual(m.false_investigate_rate, 1.0)
        m0 = M.compute_metrics([pos, neg], {"u": rpos, "n": rneg})
        self.assertEqual(m0.person_precision, 0.0)
        self.assertEqual(m0.n_reported, 0)


class TestCalibrationConfig(unittest.TestCase):
    """``config.get_agent_calibration`` — the fitted rung->P table the shipped pipeline reads."""

    def test_inline_table_normalises_keys(self):
        with mock.patch.object(config, "get_agent",
                               return_value={"calibration": {"table": {"50": 0.88, "70": "0.95"}}}):
            table = config.get_agent_calibration()
        self.assertEqual(table, {50: 0.88, 70: 0.95})

    def test_reads_calibrate_wrapper_from_path(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "calibration_table.json")
        _write(path, {"calibration_table": {"50": 0.6, "85": 0.91}, "n_rows": 3})
        with mock.patch.object(config, "get_agent",
                               return_value={"calibration": {"path": path}}):
            table = config.get_agent_calibration()
        self.assertEqual(table, {50: 0.6, 85: 0.91})

    def test_empty_when_unconfigured(self):
        with mock.patch.object(config, "get_agent", return_value={}):
            self.assertEqual(config.get_agent_calibration(), {})
        with mock.patch.object(config, "get_agent",
                               return_value={"calibration": {"path": "/no/such/file.json"}}):
            self.assertEqual(config.get_agent_calibration(), {})


class TestWorthInvestigating(unittest.TestCase):
    """``orchestrator._apply_worth_investigating`` — populate p_worth from the FINAL rung."""

    def test_populates_from_table(self):
        r = _result(node="regnode00001", lead=True)          # lead @ medium => score 50
        with mock.patch.object(config, "get_agent_calibration", return_value={50: 0.9}):
            ORCH._apply_worth_investigating(r.dossier)
        self.assertEqual(r.dossier.verdict.p_worth_investigating, 0.9)

    def test_none_without_table(self):
        r = _result(node="regnode00001", lead=True)
        with mock.patch.object(config, "get_agent_calibration", return_value={}):
            ORCH._apply_worth_investigating(r.dossier)
        self.assertIsNone(r.dossier.verdict.p_worth_investigating)

    def test_abstain_stays_none(self):
        r = _result()                                         # abstain
        with mock.patch.object(config, "get_agent_calibration", return_value={25: 0.4}):
            ORCH._apply_worth_investigating(r.dossier)
        self.assertIsNone(r.dossier.verdict.p_worth_investigating)

    def test_rung_absent_from_table_leaves_none(self):
        r = _result(node="regnode00001", lead=True)          # score 50, table only has 70
        with mock.patch.object(config, "get_agent_calibration", return_value={70: 0.95}):
            ORCH._apply_worth_investigating(r.dossier)
        self.assertIsNone(r.dossier.verdict.p_worth_investigating)

    def test_gates_populate_worth_end_to_end(self):
        # Driven through the SHARED apply_deterministic_gates (prod + eval path), not the helper
        # directly — guards that the call is actually wired into the pipeline.
        r = _result(node="regnode00001", lead=True)          # lead @ medium => 50
        seed = {"is_offstack": False, "experts": [], "raw_crash": {}, "candidates": []}
        with mock.patch.object(config, "get_agent_calibration", return_value={50: 0.6}):
            ORCH.apply_deterministic_gates(r, seed)
        self.assertEqual(r.dossier.verdict.p_worth_investigating, 0.6)

    def test_gates_populate_final_post_gate_rung(self):
        # A bare lead @ 50 whose candidate bug is corroborated by a single in-window prior
        # signature: the corroboration gate bumps it to probable (70). p_worth must map the FINAL
        # (post-gate) rung 70, NOT the pre-gate 50 — proving _apply_worth_investigating runs LAST.
        r = _result(node="regnode00001", lead=True, bug=12345)
        seed = {"is_offstack": False, "experts": [], "raw_crash": {},
                "candidates": [], "prior_regressor_bugs": [12345]}
        with mock.patch.object(config, "get_agent_calibration", return_value={50: 0.5, 70: 0.9}):
            ORCH.apply_deterministic_gates(r, seed)
        self.assertEqual(r.dossier.verdict.confidence, Confidence.probable)   # gate bumped it
        self.assertEqual(r.dossier.verdict.p_worth_investigating, 0.9)        # mapped FINAL rung


class TestReportThresholds(unittest.TestCase):
    """``runner._apply_report_thresholds`` — the SweepConfig.confidence_thresholds report gate."""

    def test_downgrades_below_per_decision_threshold(self):
        r = _result(node="regnode00001", lead=True)          # lead @ 50
        R._apply_report_thresholds(r, {"lead": 70})
        self.assertEqual(r.dossier.verdict.decision, Decision.abstain)
        self.assertIn("report threshold", r.dossier.verdict.abstain_reason)

    def test_keeps_at_or_above_threshold(self):
        r = _result(node="regnode00001", lead=True)          # lead @ 50
        R._apply_report_thresholds(r, {"lead": 50})
        self.assertEqual(r.dossier.verdict.decision, Decision.lead)

    def test_report_fallback_key(self):
        r = _result(node="regnode00001", strong=True)        # strong @ high => 85
        R._apply_report_thresholds(r, {"report": 90})
        self.assertEqual(r.dossier.verdict.decision, Decision.abstain)

    def test_empty_is_noop(self):
        r = _result(node="regnode00001", lead=True)
        R._apply_report_thresholds(r, {})
        self.assertEqual(r.dossier.verdict.decision, Decision.lead)


if __name__ == "__main__":
    unittest.main()
