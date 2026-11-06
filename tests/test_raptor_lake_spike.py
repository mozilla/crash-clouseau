# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Raptor Lake spike detection, filing, deduplication and retries."""
import unittest
from datetime import date, datetime, timezone
from unittest import mock

from crashclouseau import bugzilla_apply, config, models, sigage, spike_report
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

    def _file(self):
        return se.file_raptor_lake_bug(_esc(payload={"raptor_lake": RAPTOR}), dict(self.brief))

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
