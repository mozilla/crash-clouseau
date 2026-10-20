# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Suppression and reopening rules for old engine signatures."""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import config, corroborations, models, sigtrend  # noqa: E402
from crashclouseau.agent import orchestrator as orch  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    AbstainKind,
    Candidate,
    Claim,
    Confidence,
    Decision,
    Dossier,
    SearchfoxCitation,
    Verdict,
)

_SF = SearchfoxCitation(permalink="https://searchfox.org/x#547", symbol_id="_Z1",
                        repo="mozilla-central")
_ON = {"enabled": True, "paths": ("js/src/",)}


def _seed(**over):
    s = {"uuid": "f1d30bd8", "signature": "(anonymous namespace)::BytecodeParser::simulateOp",
         "channel": "nightly", "product": "Firefox", "buildid": "20261019073400",
         "signature_first_seen_ever": "20190619235516", "signature_trend": {},
         "frames": [{"stackpos": 0, "filename": "js/src/vm/BytecodeUtil.cpp", "line": 856},
                    {"stackpos": 1, "filename": "js/src/vm/BytecodeUtil.cpp", "line": 2425}]}
    s.update(over)
    return s


def _actionable():
    return Dossier(
        candidate=Candidate(node="8cde5fbfa29f", bug=1670481),
        verdict=Verdict(decision=Decision.actionable, confidence=Confidence.probable,
                        title="MOZ_RELEASE_ASSERT in BytecodeParser::simulateOp",
                        mechanism=Claim(statement="the release assert fires", citations=[_SF])))


class _Base(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(config, "get_agent_hot_engine", return_value=dict(_ON)),
                  mock.patch.object(sigtrend, "rate_unchanged", return_value=True),
                  mock.patch.object(orch, "_spike_reopens", return_value=False),
                  mock.patch.object(orch, "_incomplete_fix_may_file", return_value=False),
                  mock.patch.object(models.HotEngineAudit, "record")):
            p.start()
            self.addCleanup(p.stop)


class TestHotEngineGate(_Base):
    def _gate(self, seed=None, dossier=None):
        d = dossier or _actionable()
        orch._apply_hot_engine_gate(d, seed or _seed())
        return d

    def test_the_1670481_crash_is_suppressed(self):
        d = self._gate()
        v = d.verdict
        self.assertEqual((v.decision, v.confidence, v.abstain_kind),
                         (Decision.abstain, Confidence.low, AbstainKind.hardware))
        self.assertIn("js/src/vm/BytecodeUtil.cpp", v.abstain_reason)
        self.assertIn("first seen 2678 days before this build", v.abstain_reason)
        self.assertEqual(v.mechanism.statement, "the release assert fires")
        self.assertTrue(d.corroborations["hot_engine_suppressed"])
        sigtrend.rate_unchanged.assert_called_once_with(
            "Firefox", "nightly", "(anonymous namespace)::BytecodeParser::simulateOp")

    def test_a_rate_change_spares_it(self):
        with mock.patch.object(orch, "_frequency_regression_reasons", return_value=["rate"]):
            self.assertEqual(self._gate().verdict.decision, Decision.actionable)
        orch._spike_reopens.return_value = True
        d = self._gate()
        self.assertEqual(d.verdict.decision, Decision.actionable)
        self.assertNotIn("hot_engine_suppressed", d.corroborations)

    def test_an_unmeasured_rate_is_not_a_flat_one(self):
        for answer in (None, False):
            with self.subTest(answer=answer):
                sigtrend.rate_unchanged.return_value = answer
                self.assertEqual(self._gate().verdict.decision, Decision.actionable)
        sigtrend.rate_unchanged.side_effect = RuntimeError("db")
        with mock.patch.object(orch, "_rollback_quietly") as rollback:
            self.assertEqual(self._gate().verdict.decision, Decision.actionable)
        rollback.assert_called_once_with()

    def test_a_new_or_undated_signature_is_not_touched(self):
        for ever in ("20261013073400", None):
            with self.subTest(ever=ever):
                d = self._gate(_seed(signature_first_seen_ever=ever))
                self.assertEqual(d.verdict.decision, Decision.actionable)
        sigtrend.rate_unchanged.assert_not_called()
        orch._spike_reopens.assert_not_called()

    def test_only_the_first_frame_with_a_file_counts(self):
        outside = [{"filename": "dom/media/mediasource/MediaSource.cpp"},
                   {"filename": "js/src/vm/Interpreter.cpp"}]
        self.assertEqual(self._gate(_seed(frames=outside)).verdict.decision, Decision.actionable)
        unnamed = [{"filename": ""}, {"filename": None}, {"filename": "js/src/jit/BaselineIC.cpp"}]
        self.assertEqual(self._gate(_seed(frames=unnamed)).verdict.decision, Decision.abstain)
        self.assertEqual(self._gate(_seed(frames=[])).verdict.decision, Decision.actionable)

    def test_an_oom_is_left_to_its_own_gate(self):
        seed = _seed(signature="OOM | unknown | js::AutoEnterOOMUnsafeRegion::crash_impl")
        self.assertEqual(self._gate(seed).verdict.decision, Decision.actionable)

    def test_abstains_and_the_kill_switch(self):
        abstain = Dossier(verdict=Verdict(decision=Decision.abstain, confidence=Confidence.low,
                                          abstain_reason="nothing in the window"))
        self.assertEqual(self._gate(dossier=abstain).verdict.abstain_reason,
                         "nothing in the window")
        config.get_agent_hot_engine.return_value = dict(_ON, enabled=False)
        self.assertEqual(self._gate().verdict.decision, Decision.actionable)


class TestBeforeTheRun(_Base):
    def test_a_flat_hot_signature_is_not_analysed(self):
        result = orch._hot_engine_decided_before_run(_seed())
        self.assertEqual((result.num_turns, result.total_cost_usd), (0, 0.0))
        v = result.dossier.verdict
        self.assertEqual((v.decision, v.abstain_kind), (Decision.abstain, AbstainKind.hardware))
        self.assertIn("js/src/vm/BytecodeUtil.cpp", v.abstain_reason)
        self.assertTrue(result.dossier.corroborations["hot_engine_suppressed"])

    def test_anything_else_runs(self):
        sigtrend.rate_unchanged.return_value = None
        self.assertIsNone(orch._hot_engine_decided_before_run(_seed()))
        sigtrend.rate_unchanged.return_value = True
        orch._incomplete_fix_may_file.return_value = True
        self.assertIsNone(orch._hot_engine_decided_before_run(_seed()))
        models.HotEngineAudit.record.assert_not_called()

    def test_a_suppressed_crash_is_kept_in_the_audit(self):
        orch._hot_engine_decided_before_run(_seed())
        row = models.HotEngineAudit.record.call_args.args[0]
        self.assertEqual({k: row[k] for k in ("uuid", "channel", "path", "age_days", "holdout",
                                              "verdict")},
                         {"uuid": "f1d30bd8", "channel": "nightly",
                          "path": "js/src/vm/BytecodeUtil.cpp", "age_days": 2678,
                          "holdout": False, "verdict": None})

    def test_the_cluster_stays_open_so_a_later_rise_is_seen(self):
        self.assertIn("hot_engine_suppressed", corroborations.suppressions())
        self.assertIn("hot_engine_suppressed", models._INSTANCE_SUPPRESSED)


class TestItRunsAfterTheFold(_Base):
    def test_a_second_opinion_boost_cannot_rescue_it(self):
        from crashclouseau.agent.result import CrashTriageResult

        def boost(dossier, so, seed, status=None):
            dossier.verdict = dossier.verdict.model_copy(
                update={"confidence": Confidence.probable})

        with mock.patch.object(orch, "_fold_second_opinion", side_effect=boost), \
                mock.patch.object(orch, "_apply_compiled_out_gate"):
            result = CrashTriageResult(num_turns=1, total_cost_usd=0.1, result="ok",
                                       dossier=_actionable())
            orch.apply_deterministic_gates(result, _seed())
        self.assertEqual(result.dossier.verdict.decision, Decision.abstain)
        self.assertTrue(result.dossier.corroborations["hot_engine_suppressed"])


class TestTheHoldout(_Base):
    def _holdout_seed(self, **over):
        return _seed(hot_engine_holdout={"path": "js/src/vm/BytecodeUtil.cpp", "age_days": 2678},
                     **over)

    def _result(self, dossier=None):
        from crashclouseau.agent.result import CrashTriageResult
        return CrashTriageResult(num_turns=3, total_cost_usd=0.4, result="ok",
                                 dossier=dossier or _actionable())

    def test_the_draw_is_a_stable_share_of_uuids(self):
        config.get_agent_hot_engine.return_value = dict(_ON, holdout_percent=10)
        uuids = ["{:08x}-crash".format(i) for i in range(4000)]
        drawn = [u for u in uuids if orch._in_hot_engine_holdout(u)]
        self.assertTrue(300 < len(drawn) < 500, len(drawn))
        self.assertEqual(drawn, [u for u in uuids if orch._in_hot_engine_holdout(u)])
        self.assertFalse(orch._in_hot_engine_holdout(None))
        for percent, expected in ((0, 0), (100, len(uuids))):
            config.get_agent_hot_engine.return_value = dict(_ON, holdout_percent=percent)
            self.assertEqual(sum(orch._in_hot_engine_holdout(u) for u in uuids), expected)
        # The sampler treats a missing key as zero; config supplies the default.
        config.get_agent_hot_engine.return_value = dict(_ON)
        self.assertFalse(any(orch._in_hot_engine_holdout(u) for u in uuids))

    def test_a_held_out_crash_runs_with_the_gate_facts(self):
        seed = _seed()
        with mock.patch.object(orch, "_in_hot_engine_holdout", return_value=True):
            self.assertIsNone(orch._hot_engine_decided_before_run(seed))
        self.assertEqual(seed["hot_engine_holdout"],
                         {"path": "js/src/vm/BytecodeUtil.cpp", "age_days": 2678})
        models.HotEngineAudit.record.assert_not_called()

    def test_the_gates_leave_its_verdict_for_the_filing_plan(self):
        d = _actionable()
        orch._apply_hot_engine_gate(d, self._holdout_seed())
        self.assertEqual(d.verdict.decision, Decision.actionable)
        self.assertNotIn("hot_engine_suppressed", d.corroborations)

    def _close(self, result, confirmation=None, planned=None, left=None):
        from crashclouseau import bugzilla_apply
        seed = self._holdout_seed()
        dry_run = mock.Mock(return_value=planned or {"filed": False, "dry_run": True})
        with mock.patch.object(bugzilla_apply, "autofile_bug", dry_run), \
                mock.patch.object(models.CrashStack, "get_by_uuid",
                                  return_value=([], {"uuid": "f1d30bd8"})), \
                mock.patch.object(orch, "_job_time_left", return_value=left):
            orch._close_hot_engine_holdout(result, seed, confirmation, started=1.0)
        return seed, dry_run

    def test_closing_records_the_verdict_then_suppresses_it(self):
        result = self._result()
        seed, dry_run = self._close(result, planned={"would_publish": "new_bug"})
        d = result.dossier
        self.assertEqual(d.corroborations["hot_engine_holdout"],
                         {"decision": "actionable", "confidence": "probable",
                          "abstain_kind": None,
                          "title": "MOZ_RELEASE_ASSERT in BytecodeParser::simulateOp",
                          "would_publish": "new_bug", "confirmed": False})
        self.assertEqual(dry_run.call_args.args[4:6], ("actionable", 70))
        self.assertIs(dry_run.call_args.kwargs["dry_run"], True)
        self.assertEqual((d.verdict.decision, d.verdict.abstain_kind),
                         (Decision.abstain, AbstainKind.hardware))
        self.assertIn("js/src/vm/BytecodeUtil.cpp", d.verdict.abstain_reason)
        self.assertTrue(d.corroborations["hot_engine_suppressed"])
        orch._record_hot_engine_audit(seed, d)
        row = models.HotEngineAudit.record.call_args.args[0]
        self.assertTrue(row["holdout"])
        self.assertEqual(row["verdict"]["would_publish"], "new_bug")

    def test_would_publish_describes_the_verdict_it_keeps(self):
        # Reuse declines at the original confidence.
        result = self._result()
        _seed_, dry_run = self._close(result, {"plan": {"skipped": "below the floor"}})
        self.assertIsNone(result.dossier.corroborations["hot_engine_holdout"]["would_publish"])
        dry_run.assert_not_called()
        # Failed confirmation cannot grant the pending floor waiver.
        result = self._result()
        _seed_, dry_run = self._close(
            result, {"plan": {"would_publish": "new_bug"}, "error": "RuntimeError: sdk"},
            planned={"skipped": "below the actionable floor"})
        own = result.dossier.corroborations["hot_engine_holdout"]
        self.assertEqual((own["would_publish"], own["confirmed"]), (None, False))
        self.assertIsNone(dry_run.call_args.kwargs["floor_waiver"])
        # Replan confirmed verdicts with the granted waiver.
        result = self._result()
        _seed_, dry_run = self._close(
            result, {"plan": {"would_publish": "comment"}, "kept": True,
                     "floor_waiver": "agreed"}, planned={"skipped": "below the floor"})
        own = result.dossier.corroborations["hot_engine_holdout"]
        self.assertEqual((own["would_publish"], own["confirmed"]), (None, True))
        self.assertEqual(dry_run.call_args.kwargs["floor_waiver"], "agreed")
        # An abstain overrides the first pass's positive plan.
        abstain = Dossier(verdict=Verdict(decision=Decision.abstain, confidence=Confidence.low,
                                          abstain_reason="failing hardware",
                                          abstain_kind=AbstainKind.hardware))
        result = self._result(abstain)
        _seed_, dry_run = self._close(result, {"plan": {"would_publish": "new_bug"},
                                               "kept": True})
        own = result.dossier.corroborations["hot_engine_holdout"]
        self.assertEqual((own["decision"], own["abstain_kind"], own["would_publish"]),
                         ("abstain", "hardware", None))
        dry_run.assert_not_called()
        self.assertTrue(result.dossier.corroborations["hot_engine_suppressed"])
        # Skip planning when job time is short.
        result = self._result()
        _seed_, dry_run = self._close(result, left=30)
        self.assertIsNone(result.dossier.corroborations["hot_engine_holdout"]["would_publish"])
        dry_run.assert_not_called()

    def test_the_end_of_the_run_cannot_release_it(self):
        for answer in (False, None, RuntimeError("db")):
            with self.subTest(answer=answer):
                if isinstance(answer, Exception):
                    sigtrend.rate_unchanged.side_effect = answer
                else:
                    sigtrend.rate_unchanged.return_value = answer
                orch._spike_reopens.return_value = True
                result = self._result()
                self._close(result)
                self.assertEqual(result.dossier.verdict.decision, Decision.abstain)
                self.assertTrue(result.dossier.corroborations["hot_engine_suppressed"])
        sigtrend.rate_unchanged.assert_not_called()
        orch._spike_reopens.assert_not_called()

    def test_an_ordinary_run_is_not_touched(self):
        result = self._result()
        orch._close_hot_engine_holdout(result, _seed())
        self.assertEqual(result.dossier.verdict.decision, Decision.actionable)
        self.assertNotIn("hot_engine_holdout", result.dossier.corroborations)
        sigtrend.rate_unchanged.assert_not_called()


class TestTheHoldoutBrief(unittest.TestCase):
    def test_the_brief_says_why_it_would_be_skipped(self):
        from crashclouseau.agent import triage
        holdout = {"path": "js/src/vm/BytecodeUtil.cpp", "age_days": 2678}
        text = "\n".join(triage._hot_engine_holdout_lines(_seed(hot_engine_holdout=holdout)))
        self.assertIn("OLD SIGNATURE IN HOT ENGINE CODE, STEADY RATE", text)
        self.assertIn("js/src/vm/BytecodeUtil.cpp", text)
        self.assertIn("first seen 2678 days before this build", text)
        self.assertIn("abstain_kind hardware", text)
        # Missing seed data must not fabricate a count after a successful gate lookup.
        self.assertIn("On nightly its installation rate over the last 7 days is not rising "
                      "against the previous 8 weeks, and no spike.", text)
        self.assertNotIn("fewer than", text)
        trend = {"signature_trend_installs": 140, "signature_trend_window_days": 7,
                 "signature_trend_expected_installs": 151.2, "signature_trend_baseline_days": 56,
                 "signature_trend_ratio": 0.93}
        text = "\n".join(triage._hot_engine_holdout_lines(
            _seed(hot_engine_holdout=holdout, signature_trend=trend)))
        self.assertIn("not rising against the previous 8 weeks (140 installations in the last 7 "
                      "days against 151.2 expected from the previous 56 days, 0.93x), and no "
                      "spike.", text)
        # Omit counts from a trend that disagrees with the gate.
        rising = dict(trend, signature_trend_ratio=3.2)
        text = "\n".join(triage._hot_engine_holdout_lines(
            _seed(hot_engine_holdout=holdout, signature_trend=rising)))
        self.assertNotIn("140 installations", text)
        self.assertEqual(triage._hot_engine_holdout_lines(_seed()), [])


def _is_postgres():
    try:
        return models.db.engine.dialect.name == "postgresql"
    except Exception:
        return False


@unittest.skipUnless(_is_postgres(), "the JSONB audit table needs a disposable Postgres")
class TestTheAuditTable(unittest.TestCase):
    def setUp(self):
        models.create()
        models.db.session.query(models.HotEngineAudit).delete()
        models.db.session.commit()

    def _row(self, **over):
        row = {"uuid": "u-1", "signature": "js::Interpret", "product": "Firefox",
               "channel": "nightly", "buildid": "20261019073400", "path": "js/src/vm/x.cpp",
               "age_days": 900, "holdout": False, "verdict": None}
        row.update(over)
        return row

    def test_a_forced_rerun_keeps_the_holdout_result(self):
        own = {"decision": "lead", "confidence": "medium", "would_publish": None}
        self.assertTrue(models.HotEngineAudit.record(self._row(holdout=True, verdict=own)))
        self.assertTrue(models.HotEngineAudit.record(self._row(buildid="20261020095929")))
        [row] = models.HotEngineAudit.rows()
        self.assertEqual((row["holdout"], row["verdict"], row["buildid"]),
                         (True, own, "20261020095929"))
        self.assertTrue(models.HotEngineAudit.record(self._row(uuid="u-2")))
        self.assertEqual([r["holdout"] for r in models.HotEngineAudit.rows()], [True, False])


class TestTheReview(unittest.TestCase):
    def _rows(self):
        from datetime import datetime, timezone
        day = lambda d: datetime(2026, 10, d, tzinfo=timezone.utc)  # noqa: E731
        return [
            {"uuid": "u-1", "signature": "js::A", "created": day(21), "holdout": False,
             "verdict": None},
            {"uuid": "u-2", "signature": "js::A", "created": day(20), "holdout": True,
             "verdict": {"decision": "abstain", "confidence": "low"}},
            {"uuid": "u-3", "signature": "js::B", "created": day(22), "holdout": True,
             "verdict": {"decision": "lead", "confidence": "medium", "would_publish": "new_bug",
                         "title": "a real one"}},
        ]

    def test_the_holdout_summary_lists_what_it_would_have_reported(self):
        from crashclouseau import hot_engine_audit as audit
        h = audit.holdout(self._rows())
        self.assertEqual((h["runs"], h["by_decision"]), (2, {"abstain": 1, "lead": 1}))
        self.assertEqual([r["uuid"] for r in h["reported"]], ["u-3"])

    def test_outcomes_count_only_what_happened_after_the_first_skip(self):
        from crashclouseau import hot_engine_audit as audit
        bugs = {
            "js::A": [
                {"id": 1, "resolution": "FIXED", "cf_last_resolved": "2026-10-25T08:00:00Z",
                 "creation_time": "2019-06-20T00:00:00Z"},
                {"id": 2, "resolution": "FIXED", "cf_last_resolved": "2026-10-19T08:00:00Z",
                 "creation_time": "2019-06-20T00:00:00Z"},
                {"id": 3, "resolution": "", "cf_last_resolved": None,
                 "creation_time": "2026-10-21T00:00:00Z"},
            ],
            "js::B": [{"id": 4, "resolution": "INVALID", "cf_last_resolved": "2026-10-23 08:00:00",
                       "creation_time": "2020-01-01T00:00:00Z"}],
        }
        seen = []

        def lookup(signatures):
            seen.append(signatures)
            return bugs

        found = audit.outcomes(self._rows(), lookup)
        self.assertEqual(seen, [["js::A", "js::B"]])
        self.assertEqual([(o["kind"], o["bug"]["id"]) for o in found], [("fixed", 1), ("filed", 3)])
        self.assertEqual(found[0]["since"].day, 20)
        # Filter bug event dates while retaining the historical first suppression.
        from datetime import datetime, timezone
        rows = [dict(self._rows()[0], created=datetime(2026, 9, 1, tzinfo=timezone.utc)),
                dict(self._rows()[0], uuid="u-9",
                     created=datetime(2026, 10, 19, tzinfo=timezone.utc))]
        fix = {"js::A": [{"id": 5, "resolution": "FIXED", "cf_last_resolved": "2026-10-10T00:00:00Z",
                          "creation_time": "2026-09-05T00:00:00Z"},
                         {"id": 6, "resolution": "", "creation_time": "2026-09-05T00:00:00Z"}]}
        october = audit.outcomes(rows, lambda sigs: fix,
                                 since=datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertEqual([(o["kind"], o["bug"]["id"], o["since"].day) for o in october],
                         [("fixed", 5, 1)])
        text = audit.format_report({"skipped": 3, "signatures": 2,
                                    "holdout": audit.holdout(self._rows()), "outcomes": found})
        self.assertIn("hold-out runs: 2 (1 abstain, 1 lead)", text)
        self.assertIn("u-3 lead/medium would publish: new_bug -- js::B -- a real one", text)
        self.assertIn("fixed bug 1", text)


class TestTheConfig(unittest.TestCase):
    def test_defaults_and_the_kill_switch(self):
        with mock.patch.object(config, "get_agent", return_value={}):
            self.assertEqual(config.get_agent_hot_engine(), dict(_ON, holdout_percent=10))
            with mock.patch.dict(os.environ, {"HOT_ENGINE_GATE_ENABLED": "0"}):
                self.assertFalse(config.get_agent_hot_engine()["enabled"])
            for value, expected in (("0", 0), ("25", 25), ("250", 100), ("x", 10)):
                with mock.patch.dict(os.environ, {"HOT_ENGINE_HOLDOUT_PERCENT": value}):
                    self.assertEqual(config.get_agent_hot_engine()["holdout_percent"], expected)


if __name__ == "__main__":
    unittest.main()
