# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Raptor Lake spike detection, filing, deduplication and retries."""
import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest import mock

from crashclouseau import bugzilla_apply, config, models, report_bug, sigage, spike_report
from crashclouseau.agent import spike_escalation as se
from tests import REAL_RAPTOR_LAKE_SPIKE
from tests.test_spike_escalation import _FakeEscalation, _FilerBase, _esc

RAPTOR = {"reports": 50, "installs": 6, "installs_floor": False, "busiest": 17,
          "raptor_lake": 50}
CFG = config.get_agent_spike_escalation()
SPIKE = {"kind": "rate", "window_start": "2026-11-03", "window_end": "2026-11-09"}


class TestDetection(unittest.TestCase):
    def _spike(self, counts, cfg=CFG):
        with mock.patch.object(se, "_period_counts", return_value=counts) as read:
            got = REAL_RAPTOR_LAKE_SPIKE("mozilla::Foo::Bar", ["mozilla::Foo::Bar"], "Firefox",
                                         "beta", date(2026, 11, 3), SPIKE, cfg)
        return got, read

    def test_a_spike_almost_only_on_raptor_lake(self):
        self.assertEqual(self._spike(RAPTOR)[0], RAPTOR)
        self.assertIsNotNone(self._spike(dict(RAPTOR, raptor_lake=45))[0])  # exactly 90%

    def test_below_the_share_or_the_size_it_is_not(self):
        self.assertIsNone(self._spike(dict(RAPTOR, raptor_lake=39))[0])
        self.assertIsNone(self._spike(dict(RAPTOR, reports=9, raptor_lake=9))[0])
        self.assertIsNone(self._spike(None)[0])

    def test_the_share_can_be_switched_off(self):
        got, read = self._spike(RAPTOR, cfg=dict(CFG, raptor_lake_share=None))
        self.assertIsNone(got)
        read.assert_not_called()

    def test_it_reads_the_spike_own_period(self):
        _got, read = self._spike(RAPTOR)
        params = read.call_args.args[0]
        self.assertEqual(params["date"], [">=2026-11-03", "<2026-11-10"])
        self.assertEqual(params["signature"], ["=mozilla::Foo::Bar"])

    def _escalate(self, raptor, noise):
        created = []

        def create(signature, product, channel, build_day, buildid=None, uuid=None,
                   kind="build_day", payload=None, commit=True):
            row = _FakeEscalation(id=1, signature=signature, payload=payload or {})
            created.append(row)
            return row

        with mock.patch.object(se, "_fixed_before_spending", return_value=None), \
                mock.patch.object(se, "_raptor_lake_before_spending", return_value=noise) as gate, \
                mock.patch.object(se, "_raptor_lake_spike", return_value=raptor), \
                mock.patch.object(models.SpikeEscalation, "create", side_effect=create), \
                mock.patch.object(se, "_start", return_value=True):
            started = se._escalate("mozilla::Foo::Bar", ["mozilla::Foo::Bar"], "Firefox", "beta",
                                   date(2026, 11, 3), "20261102090405", SPIKE,
                                   {"spike": SPIKE, "spike_sentence": "s"}, [], CFG, 5,
                                   uuid="u-1")
        return started, created, gate

    def test_a_raptor_lake_spike_passes_the_signature_wide_hardware_decline(self):
        noise = {"reports": 50, "broken_cpu_reports": 50}
        started, created, gate = self._escalate(RAPTOR, noise)
        self.assertTrue(started)
        self.assertEqual(created[0].payload["raptor_lake"], RAPTOR)
        gate.assert_not_called()
        # Nonqualifying spikes still use the signature-wide decline.
        started, created, gate = self._escalate(None, noise)
        self.assertFalse(started)
        self.assertEqual(created[0].statuses, ["done"])
        self.assertNotIn("raptor_lake", created[0].payload)


class TestTheMetaBug(unittest.TestCase):
    def test_signatures_of_the_bugs_blocking_it(self):
        response = mock.Mock(**{"json.return_value": {"bugs": [
            {"id": 2089254, "cf_crash_signature": "[@ mozilla::detail::VectorImpl<T>::new_]"},
            {"id": 1862281, "cf_crash_signature": "[@ A::B]\r\n[@ C | D ]"},
            {"id": 1900001, "cf_crash_signature": "[@ mozilla::Array<T>::operator[]]"}]}})
        with mock.patch.object(se.net, "get", return_value=response) as get:
            self.assertEqual(se._raptor_lake_signatures(), {
                "mozilla::detail::VectorImpl<T>::new_": 2089254, "A::B": 1862281, "C | D": 1862281,
                "mozilla::Array<T>::operator[]": 1900001})
            se._raptor_lake_signatures()
        # Each call must check for newly filed blockers.
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.kwargs["params"]["v1"], str(sigage.RAPTOR_LAKE_META))

    def test_an_unreadable_meta_bug_is_none(self):
        with mock.patch.object(se.net, "get", side_effect=RuntimeError("503")):
            self.assertIsNone(se._raptor_lake_signatures())


class TestTheFiling(_FilerBase):
    def setUp(self):
        super().setUp()
        self.put = self.enterContext(mock.patch.object(bugzilla_apply, "_put_bug", return_value=1))
        for p in (
                mock.patch.object(se, "_raptor_lake_signatures", return_value={}),
                mock.patch.object(models.SpikeEscalation, "raptor_lake_bugs_for", return_value=[]),
        ):
            self.enterContext(p)
        self.brief["raw_crash"] = {"address": "0x0"}

    def _file(self):
        return se.file_raptor_lake_bug(_esc(payload={"raptor_lake": RAPTOR}), dict(self.brief))

    def test_a_missing_processed_crash_retries_instead_of_skipping_the_screen(self):
        for missing in (None, {}):
            with self.subTest(raw_crash=missing):
                self.brief["raw_crash"] = missing
                out = self._file()
                self.assertFalse(out["filed"])
                self.assertTrue(out["retry"])
                # The usual filing fields, like every other result.
                self.assertEqual((out["signature"], out["uuid"], out["buildid"]),
                                 ("mozilla::Foo::Bar", "u-1", "20260903093145"))
                self.assertIn("at", out)
        self.assertEqual(self.created, [])

    def test_a_new_raptor_lake_spike_is_filed_blocking_the_meta_and_closed(self):
        out = self._file()
        self.assertTrue(out["filed"], out)
        self.assertEqual((out["mode"], out["resolution"], out["needinfo"]),
                         ("raptor_lake_invalid", "INVALID", None))
        payload, = self.created
        self.assertEqual(payload["blocks"], ["clouseau", sigage.RAPTOR_LAKE_META])
        self.assertNotIn("flags", payload)
        self.assertFalse(any(k.startswith(("cf_status", "cf_tracking")) for k in payload))
        self.put.assert_called_once_with(
            2070000, {"status": "RESOLVED", "resolution": "INVALID"}, "tok")
        self.assertFalse(out["link_pending"])
        self.assertFalse(out["close_pending"])
        body = payload["description"]
        self.assertIn("50 of the spike's 50 reports (100%)", body)
        self.assertNotIn("Clouseau analysis", body)
        self.assertNotIn("can you have a look", body)
        self.assertIn("from crash-stats counts, without LLM analysis", body)
        self.assertNotIn("Please close it as INVALID if it is wrong", body)

    def test_a_failed_close_keeps_the_filing_and_is_left_pending(self):
        with mock.patch.object(bugzilla_apply, "_put_bug", side_effect=RuntimeError("409")):
            out = self._file()
        self.assertTrue(out["filed"])
        self.assertNotIn("resolution", out)
        self.assertTrue(out["close_pending"])
        self.assertIn("409", out["close_failed"])

    def _assert_declined(self, reason):
        out = self._file()
        self.assertFalse(out["filed"])
        self.assertIn(reason, out["skipped"])
        self.assertEqual(self.created, [])
        self.put.assert_not_called()

    def test_a_known_raptor_lake_signature_is_declined(self):
        with mock.patch.object(se, "_raptor_lake_signatures",
                               return_value={"mozilla::Foo::Bar": 2006289}):
            self._assert_declined("bug 2006289 blocks")

    def test_an_open_bug_is_declined(self):
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature",
                               return_value=[{"id": 568487, "product": "Core"}]):
            self._assert_declined("open bug 568487 carries the signature")

    def test_memory_safety_signals_are_declined(self):
        self.brief["raw_crash"] = {"x": 1}
        with mock.patch.object(se.sensitive, "memory_unsafe_signals", return_value=["poison"]):
            self._assert_declined("memory-safety signals")

    def test_a_failed_meta_link_is_left_pending(self):
        def create(payload, token):
            self.created.append(payload)
            return 2070000, {"blocks"}

        with mock.patch.object(bugzilla_apply, "_create_bug_keeping_the_bug", side_effect=create), \
                mock.patch.object(bugzilla_apply, "_link_blockers", return_value=["clouseau"]):
            out = self._file()
        self.assertTrue(out["filed"])
        self.assertTrue(out["link_pending"])
        self.assertEqual(out["blocks"], ["clouseau"])

    def test_our_own_raptor_lake_bug_declines_while_its_link_is_pending(self):
        with mock.patch.object(models.SpikeEscalation, "raptor_lake_bugs_for", return_value=[2070000]):
            out = self._file()
        self.assertFalse(out["filed"])
        self.assertEqual(out["skipped"], "our Raptor Lake bug 2070000 already covers this signature")
        with mock.patch.object(models.SpikeEscalation, "raptor_lake_bugs_for", return_value=None):
            self.assertTrue(self._file()["retry"])
        self.assertEqual(self.created, [])

    def test_unreadable_bugzilla_retries(self):
        with mock.patch.object(se, "_raptor_lake_signatures", return_value=None):
            self.assertTrue(self._file()["retry"])
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature", return_value=None):
            self.assertTrue(self._file()["retry"])
        self.assertEqual(self.created, [])


class TestFilingFixups(unittest.TestCase):
    PENDING = {"filed": True, "bug": 2090100, "mode": "raptor_lake_invalid",
               "close_pending": True, "close_failed": "503"}

    def setUp(self):
        self.put = self.enterContext(mock.patch.object(bugzilla_apply, "_put_bug", return_value=1))
        for p in (
                mock.patch.object(config, "get_bugzilla_token", return_value="tok"),
                mock.patch.object(config, "autofile_globally_enabled", return_value=True),
                mock.patch.object(config, "autofile_product_declared", return_value=True),
                mock.patch.object(config, "autofile_product_held", return_value=False),
        ):
            self.enterContext(p)

    def _retry(self, status="NEW", filing=None, updated=None):
        esc = _FakeEscalation(id=7, product="Firefox",
                              payload={"filing": dict(filing or self.PENDING)},
                              updated=updated or datetime(2026, 11, 1, tzinfo=timezone.utc))
        rows = None if status is None else [{"id": 2090100, "status": status}]
        with mock.patch.object(bugzilla_apply, "_bugs_by_id", return_value=rows):
            ran = se._retry_raptor_lake_fixups(esc, esc.payload["filing"])
        return ran, esc.payload["filing"]

    def test_a_pending_close_is_retried_on_the_same_bug(self):
        ran, filing = self._retry()
        self.assertEqual(ran, 1)
        self.put.assert_called_once_with(
            2090100, {"status": "RESOLVED", "resolution": "INVALID"}, "tok")
        self.assertEqual((filing["resolution"], filing["close_pending"], filing["fixup_attempts"]),
                         ("INVALID", False, 2))

    def test_a_missing_meta_link_is_retried_on_the_same_bug(self):
        filing = dict(self.PENDING, close_pending=False, link_pending=True, blocks=["clouseau"],
                      resolution="INVALID")
        _ran, out = self._retry(filing=filing)
        self.put.assert_called_once_with(
            2090100, {"blocks": {"add": [sigage.RAPTOR_LAKE_META]}}, "tok")
        self.assertFalse(out["link_pending"])
        self.assertEqual(out["blocks"], ["clouseau", sigage.RAPTOR_LAKE_META])
        with mock.patch.object(bugzilla_apply, "_put_bug", side_effect=RuntimeError("400")):
            _ran, out = self._retry(filing=filing)
        self.assertTrue(out["link_pending"])

    def test_a_bug_someone_moved_is_left_alone(self):
        _ran, filing = self._retry(status="ASSIGNED")
        self.put.assert_not_called()
        self.assertFalse(filing["close_pending"])
        self.assertIn("ASSIGNED", filing["close_skipped"])

    def test_an_unreadable_bug_stays_pending(self):
        _ran, filing = self._retry(status=None)
        self.put.assert_not_called()
        self.assertTrue(filing["close_pending"])

    def test_writes_switched_off_leave_it_pending(self):
        for name, switch, value in (("autofile off", "autofile_globally_enabled", False),
                                    ("product undeclared", "autofile_product_declared", False),
                                    ("product held", "autofile_product_held", True)):
            with self.subTest(name), mock.patch.object(config, switch, return_value=value):
                ran, filing = self._retry()
                self.assertEqual(ran, 0)
                self.assertEqual(filing, self.PENDING)
        self.put.assert_not_called()

    def test_retries_are_spaced_and_bounded(self):
        recent = datetime.now(timezone.utc)
        self.assertEqual(self._retry(updated=recent)[0], 0)
        self.assertEqual(self._retry(filing=dict(self.PENDING, fixup_attempts=3))[0], 0)
        self.put.assert_not_called()

    def test_the_filing_retry_sweep_reaches_it(self):
        for name, filing in (("close", dict(self.PENDING)),
                             ("link", dict(self.PENDING, close_pending=False, link_pending=True))):
            with self.subTest(name):
                esc = _FakeEscalation(id=7, payload={"filing": filing},
                                      updated=datetime(2026, 11, 1, tzinfo=timezone.utc))
                query = mock.MagicMock()
                query.filter.return_value.all.return_value = [esc]
                with mock.patch.object(se.db.session, "query", return_value=query), \
                        mock.patch.object(se, "_retry_raptor_lake_fixups", return_value=1) as fix, \
                        mock.patch.object(se, "file_raptor_lake_bug") as refile:
                    self.assertEqual(se._retry_filings(CFG), 1)
                fix.assert_called_once()
                refile.assert_not_called()


class TestTheWorker(unittest.TestCase):
    def test_a_raptor_lake_row_files_without_the_investigator(self):
        esc = _FakeEscalation(id=7, signature="mozilla::Foo::Bar", status="pending", attempts=0,
                              payload={"raptor_lake": RAPTOR})
        lock = mock.MagicMock()
        lock.return_value.__enter__.return_value = True
        with mock.patch.object(models.SpikeEscalation, "get", return_value=esc), \
                mock.patch.object(se, "build_spike_brief", return_value={}) as brief, \
                mock.patch.object(se, "file_raptor_lake_bug",
                                  return_value={"filed": True, "bug": 9}) as filer, \
                mock.patch.object(bugzilla_apply, "filing_lock", lock), \
                mock.patch("crashclouseau.agent.spike_agent.run_spike_agent",
                           side_effect=AssertionError("the investigator ran")):
            se.run_spike_escalation(7)
        brief.assert_called_once_with(esc, light=True)
        filer.assert_called_once()
        self.assertEqual(esc.payload["filing"], {"filed": True, "bug": 9})
        self.assertEqual(esc.statuses, ["running", "done"])


class TestTheText(unittest.TestCase):
    def test_the_note_replaces_the_analysis(self):
        note = spike_report.raptor_lake_note({"raptor_lake": RAPTOR})
        self.assertIn("meta bug 1975808", note)
        self.assertIn("Reopen if evidence points to a Firefox defect.", note)
        self.assertIsNone(spike_report.raptor_lake_note({}))


# Bug 2088288: 50 of 64 reports on Raptor Lake, and the grounded run said "external".
ANALYSED = {"reports": 64, "installs": 51, "installs_floor": False, "busiest": 3,
            "raptor_lake": 50, "raptor_lake_excess_share": 50 / 64}


def _external(**over):
    from crashclouseau.agent.spike_agent import SpikeFindings

    kw = dict(summary="Unstable Raptor Lake CPUs mis-execute one instruction in this build.",
              assessment="external", product="Core", component="CSS Parsing and Computation",
              evidence=[{"claim": "78% Raptor Lake", "source": "crash-stats"}])
    kw.update(over)
    return SpikeFindings(**kw)


class TestTheExternalVerdictGate(unittest.TestCase):
    RATE_SPIKE = dict(SPIKE, rate_exposure={
        "baseline_start": "2026-10-27", "window_installs": 1000, "baseline_installs": 1000,
    })

    def _gate(self, counts, findings=None, grounded=True, cfg=CFG, *,
              baseline=None, spike=RATE_SPIKE):
        esc = _esc(payload={"spike": spike, "siblings": ["mozilla::Foo::Bar"]})
        with mock.patch.object(se, "_period_counts", side_effect=[counts, baseline]) as read:
            got = se._raptor_lake_external(esc, findings or _external(), grounded, cfg)
        return got, read

    def test_a_grounded_external_verdict_on_a_mostly_raptor_spike(self):
        empty = {"reports": 0, "raptor_lake": 0}
        self.assertEqual(self._gate(ANALYSED, baseline=empty)[0], ANALYSED)
        self.assertIsNotNone(self._gate(ANALYSED, _external(assessment="environment"),
                                        baseline=empty)[0])
        boundary, _read = self._gate(dict(ANALYSED, raptor_lake=32), baseline=empty)
        self.assertEqual(boundary["raptor_lake_excess_share"], 0.5)

    def test_a_raptor_majority_does_not_classify_growth_on_other_cpus(self):
        counts = {"reports": 100, "raptor_lake": 60}
        driver = _external(summary="The new reports came from a driver update.")
        for baseline in ({"reports": 60, "raptor_lake": 60},
                         {"reports": 70, "raptor_lake": 58}):
            with self.subTest(baseline=baseline):
                self.assertIsNone(self._gate(counts, driver, baseline=baseline)[0])

    def test_rate_spikes_scale_the_baseline_by_reporting_installations(self):
        exposure = dict(self.RATE_SPIKE["rate_exposure"], baseline_installs=2000)
        spike = dict(self.RATE_SPIKE, rate_exposure=exposure)
        prior = {"reports": 100, "raptor_lake": 20}
        result, _read = self._gate(ANALYSED, baseline=prior, spike=spike)
        self.assertEqual(result["raptor_lake_excess_share"], 1.0)

    def test_rate_spikes_do_not_credit_raptor_growth_explained_by_exposure(self):
        exposure = dict(self.RATE_SPIKE["rate_exposure"], window_installs=4000)
        spike = dict(self.RATE_SPIKE, rate_exposure=exposure)
        prior = {"reports": 80, "raptor_lake": 80}
        during = {"reports": 520, "raptor_lake": 320}
        self.assertIsNone(self._gate(during, baseline=prior, spike=spike)[0])

    def test_rate_spikes_without_saved_exposure_keep_the_ordinary_path(self):
        result, read = self._gate(ANALYSED, spike=SPIKE)
        self.assertIsNone(result)
        read.assert_not_called()

    def test_build_day_uses_the_loudest_day_to_bound_baseline_raptor_reports(self):
        spike = {"kind": "build_day", "baseline_max": 60, "history_days": 21}
        prior = {"reports": 100, "raptor_lake": 60}
        self.assertIsNone(self._gate({"reports": 100, "raptor_lake": 60},
                                     baseline=prior, spike=spike)[0])
        spike["baseline_max"] = 16
        prior["raptor_lake"] = 11
        result, _read = self._gate(ANALYSED, baseline=prior, spike=spike)
        self.assertAlmostEqual(result["raptor_lake_excess_share"], 39 / 48)

    def test_version_steps_scale_the_baseline_by_reporting_installations(self):
        spike = dict(SPIKE, kind="version_step", major=150, from_major=149,
                     from_release_day="2026-10-03", all_installs=1000, from_all_installs=2000)
        prior = {"reports": 128, "raptor_lake": 100}
        self.assertIsNone(self._gate(ANALYSED, baseline=prior, spike=spike)[0])

    def test_an_unavailable_baseline_keeps_the_ordinary_path(self):
        self.assertIsNone(self._gate(ANALYSED)[0])

    def test_other_verdicts_low_shares_and_small_spikes_file_as_before(self):
        for name, counts, findings, grounded in (
                ("regression", ANALYSED, _external(assessment="regression"), True),
                ("unknown", ANALYSED, _external(assessment="unknown"), True),
                ("ungrounded", ANALYSED, None, False),
                ("12% like FIXED 2071557", dict(ANALYSED, raptor_lake=8), None, True),
                ("6 reports", dict(ANALYSED, reports=6, raptor_lake=6), None, True),
                ("unreadable", None, None, True)):
            with self.subTest(name):
                self.assertIsNone(self._gate(counts, findings, grounded)[0])

    def test_the_gate_can_be_switched_off(self):
        got, read = self._gate(ANALYSED, cfg=dict(CFG, raptor_lake_external_share=None))
        self.assertIsNone(got)
        read.assert_not_called()

    def test_the_default_share_is_half(self):
        self.assertEqual(CFG["raptor_lake_external_share"], 0.5)


class TestTheAnalysedFiling(_FilerBase):
    def setUp(self):
        super().setUp()
        self.put = self.enterContext(mock.patch.object(bugzilla_apply, "_put_bug", return_value=1))
        for p in (
                mock.patch.object(se, "_raptor_lake_signatures", return_value={}),
                mock.patch.object(models.SpikeEscalation, "raptor_lake_bugs_for", return_value=[]),
        ):
            self.enterContext(p)
        self.esc = _esc(payload={"raptor_lake": ANALYSED, "raptor_lake_from": "analysis",
                                 "raptor_lake_candidate_bugs": {}})
        self.brief["raw_crash"] = {"address": "0x0"}

    def test_it_is_filed_invalid_with_the_analysis_and_no_needinfo(self):
        findings = _external(culprit={"node": "2222222bbbbcccc", "bug": 22,
                                      "confidence": "medium", "why": "w"})
        out = se._file_raptor_lake(self.esc, dict(self.brief), findings, True)
        self.assertTrue(out["filed"], out)
        self.assertEqual((out["mode"], out["resolution"], out["needinfo"]),
                         ("raptor_lake_invalid", "INVALID", None))
        payload, = self.created
        self.assertEqual(payload["blocks"], ["clouseau", sigage.RAPTOR_LAKE_META])
        self.assertEqual(payload["keywords"], ["crash"])
        self.assertNotIn("regressed_by", payload)
        self.assertNotIn("flags", payload)
        body = payload["description"]
        self.assertIn("50 of the spike's 64 reports (78%)", body)
        self.assertIn("finds an external cause rather than a Firefox defect", body)
        self.assertIn("mis-execute one instruction", body)
        self.assertNotIn("without LLM analysis", body)

    def test_an_open_bug_is_declined_like_any_raptor_lake_spike(self):
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature",
                               return_value=[{"id": 1837205, "product": "Core"}]):
            out = se._file_raptor_lake(self.esc, dict(self.brief), _external(), True)
        self.assertFalse(out["filed"])
        self.assertIn("open bug 1837205", out["skipped"])
        self.assertEqual(self.created, [])

    def test_memory_safety_signals_go_to_the_ordinary_filer(self):
        with mock.patch.object(se.sensitive, "memory_unsafe_signals", return_value=["poison"]), \
                mock.patch.object(se, "file_spike_bug",
                                  return_value={"filed": True, "bug": 9}) as ordinary:
            out = se._file_raptor_lake(self.esc, self.brief, _external(), True)
        ordinary.assert_called_once()
        self.assertNotIn("raptor_lake", ordinary.call_args.args[1])
        self.assertEqual(out["bug"], 9)
        self.assertIn("memory-safety", out["raptor_lake_declined"])

    def test_a_counts_only_row_ignores_findings(self):
        esc = _esc(payload={"raptor_lake": RAPTOR})
        with mock.patch.object(se, "file_raptor_lake_bug", return_value={"filed": True}) as f:
            se._file_raptor_lake(esc, {}, _external(), True)
        f.assert_called_once_with(esc, {})


class TestTheAnalysedWorker(unittest.TestCase):
    def test_an_external_verdict_routes_to_the_raptor_lake_filer(self):
        esc = _FakeEscalation(id=7, signature="mozilla::Foo::Bar", status="pending", attempts=0,
                              payload={}, add_usage=lambda **k: None)
        run = SimpleNamespace(total_cost_usd=0.5, input_tokens=1, output_tokens=1,
                              cache_read_tokens=0, findings=None, provenance=[], result="r",
                              error=None, grounded=True, num_turns=1, tool_calls=0,
                              usage=lambda: {})

        async def agent(brief):
            return run

        lock = mock.MagicMock()
        lock.return_value.__enter__.return_value = True
        findings = _external()
        node = "3333333ddddeeee"
        brief = {"candidate_nodes": {node: {"bug": 4242}, "4444444eeeeffff": {"bug": None}}}
        with mock.patch.object(models.SpikeEscalation, "get", return_value=esc), \
                mock.patch.object(se, "build_spike_brief", return_value=brief), \
                mock.patch("crashclouseau.agent.spike_agent.run_spike_agent", agent), \
                mock.patch.object(se, "validate_findings", return_value=(findings, [])), \
                mock.patch.object(se, "_public_brief", return_value={}), \
                mock.patch.object(se.samesite, "record_for_spike"), \
                mock.patch.object(se, "_raptor_lake_external", return_value=ANALYSED), \
                mock.patch.object(bugzilla_apply, "filing_lock", lock), \
                mock.patch.object(se, "_file_raptor_lake",
                                  return_value={"filed": True, "bug": 9}) as raptor, \
                mock.patch.object(se, "file_spike_bug",
                                  side_effect=AssertionError("ordinary filer")):
            se.run_spike_escalation(7)
        raptor.assert_called_once_with(esc, brief, findings, True)
        self.assertEqual((esc.payload["raptor_lake"], esc.payload["raptor_lake_from"]),
                         (ANALYSED, "analysis"))
        self.assertEqual(esc.payload["raptor_lake_candidate_bugs"], {node: 4242})
        self.assertEqual(esc.statuses, ["running", "done"])


class TestTheAnalysedFilingIsScreened(TestTheAnalysedFiling):
    """The analysis is public text: screen it like the ordinary filer does."""
    NODE = "3333333ddddeeee"

    def _restricted(self, public):
        self.brief["candidate_nodes"] = {self.NODE: {"node": self.NODE, "bug": 4242}}
        findings = _external(summary="Unstable CPUs; {} only moved code.".format(self.NODE))
        with mock.patch("crashclouseau.disclosure.public_bugs", side_effect=public):
            return se._file_raptor_lake(self.esc, dict(self.brief), findings, True)

    def test_a_changeset_of_a_restricted_bug_keeps_the_analysis_out(self):
        out = self._restricted(lambda ids: {int(i) for i in ids if int(i) != 4242})
        self.assertTrue(out["filed"], out)
        self.assertTrue(out["analysis_withheld"])
        body = self.created[0]["description"]
        self.assertNotIn(self.NODE, body)
        self.assertNotIn("4242", body)
        self.assertIn("Clouseau's analysis (not attached) finds an external cause", body)
        self.assertNotIn("consulted no source", body)
        self.assertNotIn("without LLM analysis", body)

    def test_a_public_culprit_bug_does_not_override_the_saved_restricted_bug(self):
        restricted_bug, public_bug = 2046734, 2046735
        self.esc.payload["raptor_lake_candidate_bugs"] = {self.NODE: restricted_bug}
        findings = _external(culprit={"node": self.NODE, "bug": public_bug,
                                      "confidence": "medium"})
        with mock.patch("crashclouseau.disclosure.public_bugs",
                        side_effect=lambda ids: set(ids) - {restricted_bug}) as lookup:
            out = se._file_raptor_lake(self.esc, self.brief, findings, True)
        self.assertTrue(out["filed"], out)
        self.assertTrue(out["analysis_withheld"])
        self.assertTrue(any({public_bug, restricted_bug} <= call.args[0]
                            for call in lookup.call_args_list))
        payload, = self.created
        self.assertNotIn(self.NODE, payload["description"])

    def test_a_restricted_changeset_in_the_final_crash_reason_files_withheld(self):
        restricted_bug = 2046734
        self.esc.payload["raptor_lake_candidate_bugs"] = {self.NODE: restricted_bug}
        with mock.patch.object(report_bug, "fetch_crash_reason",
                               return_value={"moz_crash_reason": "revision " + self.NODE}), \
                mock.patch("crashclouseau.disclosure.public_bugs",
                           side_effect=lambda ids: set(ids) - {restricted_bug}):
            out = se._file_raptor_lake(self.esc, self.brief, _external(), True)
        # The ordinary filer takes it and withholds it instead of dropping the filing.
        self.assertIn("final bug description", out["raptor_lake_declined"])
        self.assertTrue(out["filed"], out)
        self.assertNotEqual(out.get("mode"), "raptor_lake_invalid")
        payload, = self.created
        self.assertEqual(payload["groups"], ["core-security"])
        self.put.assert_not_called()

    def test_final_fields_are_screened_when_the_analysis_is_clean(self):
        restricted_bug = 2046734
        self.esc.payload["raptor_lake_candidate_bugs"] = {self.NODE: restricted_bug}
        for signature in ("mozilla::Foo<{}>".format(self.NODE),
                          "Foo" * 79 + "<{}>".format(self.NODE)):
            with self.subTest(signature=signature), \
                    mock.patch("crashclouseau.disclosure.public_bugs",
                               side_effect=lambda ids: set(ids) - {restricted_bug}):
                self.esc.signature = signature
                brief = dict(self.brief, signature=signature)
                out = se._file_raptor_lake(self.esc, brief, _external(), True)
                self.assertFalse(out["filed"])
                self.assertNotIn("analysis_withheld", out)
                self.assertIn("title or signature names a nonpublic bug", out["skipped"])
        self.assertEqual(self.created, [])
        self.put.assert_not_called()

    def test_pending_classifications_are_rechecked_for_raptor_growth(self):
        counts = dict(ANALYSED)
        del counts["raptor_lake_excess_share"]
        self.esc.payload["raptor_lake"] = counts
        with mock.patch.object(se, "_raptor_lake_external", return_value=None) as gate, \
                mock.patch.object(se, "file_spike_bug", return_value={"filed": False}) as ordinary:
            out = se._file_raptor_lake(self.esc, self.brief, _external(), True)
        gate.assert_called_once()
        ordinary.assert_called_once()
        self.assertIn("contribution to the increase", out["raptor_lake_declined"])
        self.assertEqual(self.created, [])

    def test_pending_rate_classifications_without_exposure_use_the_ordinary_path(self):
        self.esc.payload["spike"] = SPIKE
        with mock.patch.object(se, "file_spike_bug", return_value={"filed": False}) as ordinary:
            out = se._file_raptor_lake(self.esc, self.brief, _external(), True)
        ordinary.assert_called_once()
        self.assertIn("contribution to the increase", out["raptor_lake_declined"])
        self.assertEqual(self.created, [])

    def test_an_unreadable_visibility_lookup_retries(self):
        out = self._restricted(lambda ids: None)
        self.assertFalse(out["filed"])
        self.assertTrue(out["retry"])
        self.assertEqual(self.created, [])

    def test_retry_withholds_a_changeset_missing_from_the_rebuilt_brief(self):
        findings = _external(summary="Unstable CPUs; {} only moved code.".format(self.NODE))
        original = dict(self.brief, candidate_nodes={self.NODE: {"bug": 4242}})
        esc = _FakeEscalation(**vars(self.esc))
        esc.updated = datetime(2026, 11, 1, tzinfo=timezone.utc)
        esc.merge_payload({"raptor_lake_candidate_bugs": {self.NODE: 4242},
                           "findings": findings.model_dump(), "grounded": True})
        with mock.patch("crashclouseau.disclosure.public_bugs", return_value=None):
            first = se._file_raptor_lake(esc, original, findings, True)
        self.assertTrue(first["retry"])
        self.assertEqual(self.created, [])
        esc.merge_payload({"filing": first})
        query = mock.MagicMock()
        query.filter.return_value.all.return_value = [esc]
        lock = mock.MagicMock()
        lock.return_value.__enter__.return_value = True
        with mock.patch.object(se.db.session, "query", return_value=query), \
                mock.patch.object(bugzilla_apply, "filing_lock", lock), \
                mock.patch.object(se, "build_spike_brief", return_value=self.brief) as build, \
                mock.patch("crashclouseau.disclosure.public_bugs",
                           side_effect=lambda ids: set(ids) - {4242}):
            self.assertEqual(se._retry_filings(CFG), 1)
        build.assert_called_once_with(esc, light=True)
        self.assertTrue(esc.payload["filing"]["analysis_withheld"])
        payload, = self.created
        self.assertNotIn(self.NODE, payload["description"])

    def test_old_rows_without_a_saved_candidate_map_withhold_the_analysis(self):
        del self.esc.payload["raptor_lake_candidate_bugs"]
        findings = _external(summary="Unstable CPUs; {} only moved code.".format(self.NODE))
        out = se._file_raptor_lake(self.esc, self.brief, findings, True)
        self.assertTrue(out["filed"], out)
        self.assertTrue(out["analysis_withheld"])
        payload, = self.created
        self.assertNotIn(self.NODE, payload["description"])

    def test_missing_processed_crash_defers_filing_until_it_is_available(self):
        findings = _external()
        for missing in (None, {}):
            with self.subTest(raw_crash=missing):
                brief = dict(self.brief, raw_crash=missing)
                out = se._file_raptor_lake(self.esc, brief, findings, True)
                self.assertFalse(out["filed"])
                self.assertTrue(out["retry"])
                self.assertEqual(out["channel"], "nightly")
                self.assertEqual(self.created, [])
        out = se._file_raptor_lake(self.esc, self.brief, findings, True)
        self.assertEqual(out["resolution"], "INVALID")

    def test_an_unreadable_crash_is_not_handed_to_the_ordinary_filer(self):
        self.brief["raw_crash"] = {"x": 1}
        with mock.patch.object(se.sensitive, "memory_unsafe_signals",
                               side_effect=ValueError("malformed")), \
                mock.patch.object(se, "file_spike_bug",
                                  side_effect=AssertionError("ordinary filer")):
            out = se._file_raptor_lake(self.esc, self.brief, _external(), True)
        self.assertFalse(out["filed"])
        self.assertIn("unreadable", out["memory_unsafe_signals"])
        self.assertEqual(self.created, [])

    def test_lost_findings_file_as_before_the_route(self):
        with mock.patch.object(se, "file_spike_bug", return_value={"filed": True}) as ordinary:
            se._file_raptor_lake(self.esc, self.brief, None, True)
        ordinary.assert_called_once_with(self.esc, self.brief, None, grounded=True)


class TestTheAnalysedRestart(unittest.TestCase):
    def test_a_restart_reuses_the_saved_findings_without_rerunning(self):
        findings = _external()
        esc = _FakeEscalation(id=7, signature="mozilla::Foo::Bar", status="error", attempts=1,
                              payload={"raptor_lake": ANALYSED, "raptor_lake_from": "analysis",
                                       "findings": findings.model_dump(), "grounded": True})
        lock = mock.MagicMock()
        lock.return_value.__enter__.return_value = True
        with mock.patch.object(models.SpikeEscalation, "get", return_value=esc), \
                mock.patch.object(se, "build_spike_brief", return_value={}) as brief, \
                mock.patch("crashclouseau.agent.spike_agent.run_spike_agent",
                           side_effect=AssertionError("the investigator ran")), \
                mock.patch.object(se.samesite, "record_for_spike"), \
                mock.patch.object(bugzilla_apply, "filing_lock", lock), \
                mock.patch.object(se, "_file_raptor_lake",
                                  return_value={"filed": True, "bug": 9}) as raptor:
            se.run_spike_escalation(7)
        brief.assert_called_once_with(esc, light=True)
        (_esc_arg, _brief, restored, grounded), _ = raptor.call_args
        self.assertEqual((restored.summary, restored.assessment, grounded),
                         (findings.summary, "external", True))
        self.assertEqual(esc.statuses, ["running", "done"])
