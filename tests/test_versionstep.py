# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Release version steps: the scan, its sweep, the brief and the bug text."""
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from tests import REAL_VERSION_SCAN
from tests.test_spike_escalation import _FakeEscalation

from crashclouseau import config, models, spike_report, spikes, versionstep
from crashclouseau.agent import spike_agent, spike_escalation as se

CFG = dict(config.get_version_step())
SIG = "mozilla::gfx::GPUProcessManager::MaybeCrashIfGpuProcessOnceStable"


def _daily(start, counts):
    return {start + timedelta(days=i): n for i, n in enumerate(counts)}


def _hist(days):
    """A ``_histogram.date=major_version`` response from ``{day: {major: count}}``."""
    return {"facets": {"histogram_date": [
        {"term": day + "T00:00:00+00:00", "count": sum(v.values()),
         "facets": {"major_version": [{"term": m, "count": c} for m, c in v.items()]}}
        for day, v in days.items()]}}


class TestScore(unittest.TestCase):
    BEFORE = {"installs": 165000, "signatures": {SIG: [522, 507], "flat": [900, 880],
                                                 "small": [10, 10]}}

    def test_a_step_fires_and_a_flat_signature_does_not(self):
        now = {"installs": 50000, "signatures": {SIG: [812, 241], "flat": [280, 266]}}
        fires = versionstep.score(self.BEFORE, now, 3.62, 1.3, 200)
        self.assertEqual([f["signature"] for f in fires], [SIG])
        self.assertEqual((fires[0]["ratio"], fires[0]["from_installs"]), (1.57, 507))
        self.assertGreater(fires[0]["z"], 3.62)

    def test_the_floor_scales_with_the_period(self):
        # The scaled floor is about 61 installations; 40 is below it.
        now = {"installs": 50000, "signatures": {"small": [40, 40]}}
        self.assertEqual(versionstep.score(self.BEFORE, now, 3.62, 1.3, 200), [])
        self.assertEqual(len(versionstep.score(self.BEFORE, now, 3.62, 1.3, 30)), 1)

    def test_a_new_signature_is_not_a_step(self):
        now = {"installs": 50000, "signatures": {"new": [900, 900]}}
        self.assertEqual(versionstep.score(self.BEFORE, now, 3.62, 1.3, 200), [])

    def test_both_bars_bind(self):
        now = {"installs": 165000, "signatures": {"flat": [1100, 1100]}}
        # x1.25: z clears 3.62 at this size, the ratio bar does not.
        self.assertEqual(versionstep.score(self.BEFORE, now, 3.62, 1.3, 200), [])
        self.assertEqual(len(versionstep.score(self.BEFORE, now, 3.62, 1.2, 200)), 1)


class TestReleaseDays(unittest.TestCase):
    def test_a_trickle_before_the_release_does_not_count(self):
        found = _hist({
            "2026-07-28": {156: 19000},
            "2026-09-01": {156: 19000, 157: 3},
            "2026-09-28": {156: 19000, 157: 15},
            "2026-09-29": {156: 17000, 157: 2753},
        })
        with mock.patch.object(spikes, "_search", return_value=found):
            days = versionstep.release_days("Firefox", "release", date(2026, 10, 6))
        self.assertEqual(days, {157: date(2026, 9, 29)})

    def test_a_major_current_at_the_lookback_start_has_no_release_day(self):
        found = _hist({"2026-07-28": {155: 19000}, "2026-09-15": {155: 9000, 156: 9000}})
        with mock.patch.object(spikes, "_search", return_value=found):
            days = versionstep.release_days("Firefox", "release", date(2026, 10, 6))
        self.assertEqual(days, {156: date(2026, 9, 15)})

    def test_a_failed_read_is_none(self):
        with mock.patch.object(spikes, "_search", side_effect=RuntimeError("boom")):
            self.assertIsNone(versionstep.release_days("Firefox", "release", date(2026, 10, 6)))


class TestScan(unittest.TestCase):
    DAYS = {156: date(2026, 9, 15), 157: date(2026, 9, 29)}
    BEFORE = {"installs": 165000, "signatures": {SIG: [522, 507]}}
    NOW = {"installs": 50000, "signatures": {SIG: [812, 241]}}

    def setUp(self):
        self.releases = self.enterContext(mock.patch.object(
            versionstep, "release_days", return_value=self.DAYS))
        self.installs = self.enterContext(mock.patch.object(
            versionstep, "installs", side_effect=[self.BEFORE, self.NOW]))
        self.daily = self.enterContext(mock.patch.object(
            versionstep, "daily_installs", return_value=None))
        self.versions = self.enterContext(mock.patch.object(
            versionstep, "_versions", return_value=None))

    def _scan(self, today=date(2026, 10, 6)):
        return versionstep.scan("Firefox", "release", today, CFG)

    def test_day_7_compares_the_first_days_with_the_previous_major(self):
        result = self._scan()
        self.assertEqual((result["major"], result["from_major"], result["age"]), (157, 156, 7))
        self.assertEqual([f["signature"] for f in result["fires"]], [SIG])
        self.installs.assert_has_calls([
            mock.call("Firefox", "release", 156, date(2026, 9, 15), date(2026, 9, 29), CFG["facets_size"]),
            mock.call("Firefox", "release", 157, date(2026, 9, 29), date(2026, 10, 6), CFG["facets_size"]),
        ])

    def test_outside_the_days_nothing_is_read(self):
        for today in (date(2026, 10, 1), date(2026, 10, 20)):
            with self.subTest(today=today):
                result = self._scan(today)
                self.assertEqual(result["fires"], [])
                self.assertIn("day", result["skipped"])
                self.installs.assert_not_called()
                self.daily.assert_not_called()
                self.versions.assert_not_called()

    def test_a_failed_read_is_none(self):
        self.installs.side_effect = [self.BEFORE, None]
        self.assertIsNone(self._scan())

    def test_each_fire_carries_the_version_carrying_it(self):
        self.versions.side_effect = [
            {"157.0": 30000, "157.0.1": 20000},
            {"157.0": 90, "157.0.1": 151},
        ]
        self.assertEqual(self._scan()["fires"][0]["version"], "157.0.1")

    def test_unreadable_details_do_not_lose_the_firing(self):
        fire, = self._scan()["fires"]
        self.assertIsNone(fire["version"])
        self.assertEqual(fire["onset"], {"kind": "unresolved"})

    def test_the_onset_counts_installations_not_reports(self):
        # Repeat reports do not affect the onset: the share stays flat for three days.
        population = {156: _daily(self.DAYS[156], [1000] * 14),
                      157: _daily(self.DAYS[157], [1000] * 7)}
        signature = {156: _daily(self.DAYS[156], [10] * 14),
                     157: _daily(self.DAYS[157], [10] * 3 + [33] * 4)}

        def daily(product, channel, major, start, end, signatures=None):
            return (signature if signatures else population)[major]

        self.daily.side_effect = daily
        self.assertEqual(self._scan()["fires"][0]["onset"],
                         {"kind": "later", "day": "2026-10-02"})

    def test_a_missing_previous_major_skips(self):
        self.releases.return_value = {155: date(2026, 9, 1), 157: date(2026, 9, 29)}
        self.assertEqual(self._scan()["fires"], [])
        self.installs.assert_not_called()


class TestCarrierVersion(unittest.TestCase):
    def _carrier(self, sig, all_versions):
        return versionstep.carrier_version(sig, all_versions, 507, 165000, 3.62, 1.3)

    def test_a_dot_release_carrying_the_rise_is_chosen(self):
        self.assertEqual(self._carrier({"157.0": 90, "157.0.1": 151},
                                       {"157.0": 30000, "157.0.1": 20000}), "157.0.1")

    def test_the_earliest_version_clearing_the_bars_wins(self):
        self.assertEqual(self._carrier({"157.0": 150, "157.0.1": 151},
                                       {"157.0": 30000, "157.0.1": 20000}), "157.0")

    def test_without_a_clear_carrier_the_highest_excess(self):
        self.assertEqual(self._carrier({"157.0": 100, "157.0.1": 70},
                                       {"157.0": 30000, "157.0.1": 20000}), "157.0.1")
        self.assertIsNone(self._carrier({}, {"157.0": 30000}))


class TestSampleUuid(unittest.TestCase):
    HIT = {"hits": [{"uuid": "u-1", "build_id": 20261008150215}]}

    def test_the_carrying_version_then_any_but_never_a_preferred_dot_0(self):
        with mock.patch.object(spikes, "_search", side_effect=[{"hits": []}, self.HIT]) as search:
            got = versionstep.sample_uuid("Firefox", "release", [SIG], 157, date(2026, 9, 29),
                                          "157.0.1")
        self.assertEqual(got, ("u-1", "20261008150215"))
        self.assertEqual([c.args[0].get("version") for c in search.call_args_list],
                         ["157.0.1", None])
        with mock.patch.object(spikes, "_search", return_value=self.HIT) as search:
            versionstep.sample_uuid("Firefox", "release", [SIG], 157, date(2026, 9, 29))
        self.assertEqual([c.args[0].get("version") for c in search.call_args_list], [None])
        self.assertEqual(search.call_args.args[0]["_sort"], "-date")


class TestDailyInstalls(unittest.TestCase):
    def test_distinct_installations_per_day(self):
        found = {"facets": {"histogram_date": [
            {"term": "2026-10-02T00:00:00Z", "count": 111,
             "facets": {"cardinality_install_time": {"value": 94}}}]}}
        with mock.patch.object(spikes, "_search", return_value=found) as search:
            got = versionstep.daily_installs("Firefox", "release", 157, date(2026, 9, 29),
                                             date(2026, 10, 6), [SIG])
        self.assertEqual(got, {date(2026, 10, 2): 94})
        params = search.call_args.args[0]
        self.assertEqual((params["_histogram.date"], params["major_version"], params["signature"]),
                         ("_cardinality.install_time", "157", ["=" + SIG]))

    def test_a_failed_or_malformed_read_is_none(self):
        for found in (None, {"facets": {}}, {"facets": {"histogram_date": [{"term": "x"}]}}):
            with mock.patch.object(spikes, "_search", return_value=found):
                self.assertIsNone(versionstep.daily_installs(
                    "Firefox", "release", 157, date(2026, 9, 29), date(2026, 10, 6)))


class TestOnset(unittest.TestCase):
    DAYS = {156: date(2026, 9, 15), 157: date(2026, 9, 29)}

    def _onset(self, prev_share, cur_shares):
        population = {156: _daily(self.DAYS[156], [1000] * 14),
                      157: _daily(self.DAYS[157], [1000] * len(cur_shares))}
        signature = {156: _daily(self.DAYS[156], [prev_share] * 14),
                     157: _daily(self.DAYS[157], cur_shares)}
        return versionstep.onset(signature, population, 156, 157, 1.3)

    def test_a_step_from_the_first_days_is_a_boundary(self):
        self.assertEqual(self._onset(10, [15, 15, 15, 15, 15, 15, 15]), {"kind": "boundary"})

    def test_a_rise_after_flat_first_days_is_later(self):
        self.assertEqual(self._onset(10, [10, 10, 10, 10, 33, 33, 33]),
                         {"kind": "later", "day": "2026-10-03"})

    def test_no_reports_before_is_unresolved(self):
        self.assertEqual(self._onset(0, [15, 15, 15]), {"kind": "unresolved"})


def _result():
    return {"product": "Firefox", "channel": "release", "major": 157, "from_major": 156,
            "release_day": date(2026, 9, 29), "from_release_day": date(2026, 9, 15), "age": 7,
            "installs": 50000, "from_installs": 165000,
            "fires": [{"signature": SIG, "installs": 241, "reports": 812, "from_installs": 507,
                       "from_reports": 522, "ratio": 1.57, "z": 6.1}]}


class TestTheText(unittest.TestCase):
    def test_the_sentence_and_the_heading(self):
        spike = versionstep.spike_facts(_result(), _result()["fires"][0], CFG)
        self.assertEqual((spike["window_start"], spike["window_end"]),
                         ("2026-09-29", "2026-10-05"))
        sentence = spikes.describe(spike, "release")
        self.assertEqual(sentence, (
            "Firefox 157, in its first 7 days on release (2026-09-29 to 2026-10-05): this "
            "signature crashed 241 of the 50,000 installations with a crash report (812 reports). "
            "Firefox 156, from its release on 2026-09-15 to 157's: 507 of 165,000 (522 reports). "
            "That is 1.57x the share of installations (binomial excess z = 6.1, bar 3.62). "
            "The daily counts do not show whether the rise began with the release."))
        spike["onset"] = {"kind": "boundary"}
        sentence = spikes.describe(spike, "release")
        self.assertTrue(sentence.endswith("The higher share shows from its first days on release."))
        paragraph = spike_report.spike_paragraph(
            {"spike": spike, "spike_sentence": sentence, "channel": "release",
             "product": "Firefox",
             "version_step": {"version": "157.0", "ratio": 3.1, "from_version": "156.0.1"}})
        self.assertTrue(paragraph.startswith(
            "**Filed because this signature's crash rate rose with Firefox 157.** Firefox 157,"))
        self.assertIn("The higher share shows from its first days", paragraph)
        # Avoid repeating the separate per-version summary.
        self.assertNotIn("By version", paragraph)

    def test_a_dot_release_carrier_is_named(self):
        fire = dict(_result()["fires"][0], onset={"kind": "later", "day": "2026-10-08"},
                    version="157.0.1")
        sentence = spikes.describe(versionstep.spike_facts(_result(), fire, CFG), "release")
        self.assertTrue(sentence.endswith("not established. Selected version: 157.0.1."))
        fire["version"] = "157.0"
        sentence = spikes.describe(versionstep.spike_facts(_result(), fire, CFG), "release")
        self.assertNotIn("Selected version:", sentence)

    def test_a_later_onset_does_not_claim_the_release(self):
        fire = dict(_result()["fires"][0], onset={"kind": "later", "day": "2026-10-03"})
        spike = versionstep.spike_facts(_result(), fire, CFG)
        sentence = spikes.describe(spike, "release")
        self.assertTrue(sentence.endswith(
            "The higher daily share appears from 2026-10-03; a release-related cause is "
            "not established."))
        paragraph = spike_report.spike_paragraph({"spike": spike, "spike_sentence": sentence})
        self.assertTrue(paragraph.startswith(
            "**Filed because this signature's crash rate rose on Firefox 157.**"))
        text = spike_agent._user_prompt({"signature": SIG, "channel": "release", "spike": spike,
                                         "spike_sentence": sentence})
        self.assertIn("The higher daily share appears from 2026-10-03; its cause is unresolved.", text)
        self.assertIn("Compare dates across versions and the Nightly/Beta series.", text)
        self.assertNotIn("so the change is in 157's train", text)

    def test_the_prompt_shows_where_the_step_began(self):
        spike = versionstep.spike_facts(_result(), _result()["fires"][0], CFG)
        series = {"nightly": {"156": {"installs": 3, "all_installs": 900, "per_10k": 33.3},
                              "157": {"installs": 9, "all_installs": 950, "per_10k": 94.7}},
                  "beta": {"156": {"installs": 20, "all_installs": 5000, "per_10k": 40.0},
                           "157": {"installs": 35, "all_installs": 5100, "per_10k": 68.6}},
                  "builds": [{"buildid": "20260825094512", "installs": 1, "all_installs": 60,
                              "per_10k": 166.67}]}
        text = spike_agent._user_prompt({"signature": SIG, "channel": "release", "spike": spike,
                                         "spike_sentence": spikes.describe(spike, "release"),
                                         "version_series": series})
        self.assertIn("a RELEASE VERSION STEP compares major versions", text)
        self.assertIn("- nightly 157: 9 of 950 (94.7 per 10k)", text)
        self.assertIn("- beta 156: 20 of 5000 (40.0 per 10k)", text)
        self.assertIn("  20260825094512: 1 / 60 (166.67)", text)
        self.assertIn("No pushlog window is given", text)
        self.assertNotIn("No candidate changesets could be enumerated", text)


class TestTheSweep(unittest.TestCase):
    def setUp(self):
        self.created = []

        def create(signature, product, channel, build_day, buildid=None, uuid=None,
                   kind="build_day", payload=None, commit=True):
            row = _FakeEscalation(id=len(self.created) + 1, signature=signature,
                                  product=product, channel=channel, build_day=build_day,
                                  buildid=buildid, uuid=uuid, kind=kind, payload=payload or {})
            self.created.append(row)
            return row

        se._VERSION_SCANS.clear()
        self.addCleanup(se._VERSION_SCANS.clear)
        patches = [
            mock.patch.object(se, "_version_scan", REAL_VERSION_SCAN),
            mock.patch.object(versionstep, "scan", return_value=_result()),
            mock.patch.object(versionstep, "sample_uuid",
                              return_value=("u-socorro", "20260928155810")),
            mock.patch.object(models.SpikeEscalation, "for_pair", return_value=None),
            mock.patch.object(models.SpikeEscalation, "latest_for_signature", return_value=None),
            mock.patch.object(models.SpikeEscalation, "count_since", return_value=0),
            mock.patch.object(models.SpikeEscalation, "create", side_effect=create),
            mock.patch.object(se, "classic_runs", return_value=[]),
            mock.patch.object(se, "representative_uuid", return_value=None),
        ]
        for patch in patches:
            self.enterContext(patch)
        self.escalate = self.enterContext(mock.patch.object(se, "_escalate", return_value=True))
        self.cfg = config.get_agent_spike_escalation()

    def test_a_step_is_escalated_on_the_major_release_day(self):
        self.assertEqual(se._sweep_version_steps("Firefox", "release", self.cfg, 2), 1)
        args, kw = self.escalate.call_args
        signature, siblings, product, channel, day, picked, spike = args[:7]
        self.assertEqual((signature, product, channel, day, picked),
                         (SIG, "Firefox", "release", date(2026, 9, 29), "20260928155810"))
        self.assertEqual((spike["kind"], spike["major"], spike["ratio"]),
                         ("version_step", 157, 1.57))
        self.assertEqual(kw["uuid"], "u-socorro")
        self.assertIn("Firefox 157, in its first 7 days", args[7]["spike_sentence"])

    def test_the_carrying_version_is_sampled(self):
        result = _result()
        result["fires"][0]["version"] = "157.0.1"
        with mock.patch.object(versionstep, "scan", return_value=result):
            se._sweep_version_steps("Firefox", "release", self.cfg, 2)
        self.assertEqual(versionstep.sample_uuid.call_args.args[5], "157.0.1")
        self.assertEqual(self.escalate.call_args.args[6]["version"], "157.0.1")

    def test_an_ingested_report_is_preferred(self):
        with mock.patch.object(se, "representative_uuid", return_value="u-db"):
            se._sweep_version_steps("Firefox", "release", self.cfg, 2)
        self.assertEqual(self.escalate.call_args.kwargs["uuid"], "u-db")

    def test_the_scan_is_read_once_a_day(self):
        se._sweep_version_steps("Firefox", "release", self.cfg, 2)
        se._sweep_version_steps("Firefox", "release", self.cfg, 2)
        versionstep.scan.assert_called_once()

    def test_a_failed_scan_is_read_again(self):
        with mock.patch.object(versionstep, "scan", return_value=None) as scan:
            self.assertEqual(se._sweep_version_steps("Firefox", "release", self.cfg, 2), 0)
            self.assertEqual(se._sweep_version_steps("Firefox", "release", self.cfg, 2), 0)
        self.assertEqual(scan.call_count, 2)

    def test_an_oom_step_is_recorded_not_investigated(self):
        result = _result()
        result["fires"][0]["signature"] = "OOM | small"
        with mock.patch.object(versionstep, "scan", return_value=result):
            self.assertEqual(se._sweep_version_steps("Firefox", "release", self.cfg, 2), 0)
        self.escalate.assert_not_called()
        row, = self.created
        self.assertEqual((row.kind, row.build_day, row.statuses), ("version_step",
                                                                   date(2026, 9, 29), ["done"]))
        self.assertIn("recorded only", row.payload["skipped"])

    def test_an_escalated_pair_or_a_recent_escalation_is_left_alone(self):
        with mock.patch.object(models.SpikeEscalation, "for_pair", return_value=object()):
            self.assertEqual(se._sweep_version_steps("Firefox", "release", self.cfg, 2), 0)
        with mock.patch.object(models.SpikeEscalation, "latest_for_signature",
                               return_value=object()):
            self.assertEqual(se._sweep_version_steps("Firefox", "release", self.cfg, 2), 0)
        self.escalate.assert_not_called()
        self.assertEqual(self.created, [])

    def test_other_channels_and_no_room_read_nothing(self):
        self.assertEqual(se._sweep_version_steps("Firefox", "beta", self.cfg, 2), 0)
        self.assertEqual(se._sweep_version_steps("Fenix", "release", self.cfg, 2), 0)
        self.assertEqual(se._sweep_version_steps("Firefox", "release", self.cfg, 0), 0)
        versionstep.scan.assert_not_called()

    def test_a_running_ordinary_run_waits(self):
        with mock.patch.object(se, "classic_runs", return_value=[{"status": "running"}]):
            self.assertEqual(se._sweep_version_steps("Firefox", "release", self.cfg, 2), 0)
        self.escalate.assert_not_called()

    def test_a_bug_the_ordinary_triage_filed_declines_the_step(self):
        runs = [{"uuid": "u-1", "status": "done", "filed_bug": {"filed": True, "bug": 2090001}}]
        with mock.patch.object(se, "classic_runs", return_value=runs):
            self.assertEqual(se._sweep_version_steps("Firefox", "release", self.cfg, 2), 0)
        self.escalate.assert_not_called()
        row, = self.created
        self.assertEqual((row.kind, row.uuid, row.statuses), ("version_step", "u-1", ["done"]))
        self.assertEqual(row.payload["skipped"], "the ordinary triage filed bug 2090001 for this spike")


class TestTheBrief(unittest.TestCase):
    def test_a_step_reads_the_train_not_the_release_window(self):
        from crashclouseau import sigfamily
        from crashclouseau.agent import orchestrator

        spike = versionstep.spike_facts(_result(), _result()["fires"][0], CFG)
        esc = _FakeEscalation(id=9, signature=SIG, product="Firefox", channel="release",
                              build_day=date(2026, 9, 29), buildid="20260928155810",
                              uuid="u-1", kind="version_step",
                              payload={"spike": spike, "siblings": [SIG]})
        seed = {"frames": [], "candidates": [{"node": "abc", "score": 3}], "stack": ""}
        series = {"nightly": {}, "beta": {}, "builds": []}
        with (
            mock.patch.object(orchestrator, "build_seed", return_value=seed),
            mock.patch.object(models.CrashStack, "get_by_uuid", return_value=({}, {})),
            mock.patch.object(sigfamily, "family_from_seed", return_value={}),
            mock.patch.object(se, "_window_for") as window,
            mock.patch.object(se, "classic_runs", return_value=[]),
            mock.patch.object(se, "_stacks", return_value=[]),
            mock.patch.object(se, "_daily_by_channel", return_value=[]),
            mock.patch.object(se, "_coloaded", return_value=None),
            mock.patch.object(versionstep, "cycle_series", return_value=series) as cycle,
        ):
            brief = se.build_spike_brief(esc)
        window.assert_not_called()
        cycle.assert_called_once_with("Firefox", [SIG], 157, date(2026, 9, 29))
        self.assertEqual(brief["version_series"], series)
        self.assertEqual([c["node"] for c in brief["candidates"]["onstack"]], ["abc"])
        self.assertEqual(brief["candidates"]["window"], [])


class TestCycleSeries(unittest.TestCase):
    def _facets(self, params):
        field = params["_facets"]
        signature, population = {
            "version": ({"156.0a1": 3, "157.0a1": 9}, {"156.0a1": 900, "157.0a1": 950}),
            "major_version": ({"156": 20, "157": 35}, {"156": 5000, "157": 5100}),
            # Keep a signature build even when its denominator is missing.
            "build_id": ({"20260825094512": 1, "20260826094000": 2}, {"20260825094512": 60}),
        }[field]
        terms = signature if "signature" in params else population
        return {"facets": {field: [
            {"term": k, "count": v, "facets": {"cardinality_install_time": {"value": v}}}
            for k, v in terms.items()]}}

    def test_every_build_of_either_side_is_listed(self):
        with mock.patch.object(spikes, "_search", side_effect=self._facets) as search:
            series = versionstep.cycle_series("Firefox", [SIG], 157, date(2026, 9, 29))
        self.assertEqual([(b["buildid"], b["installs"], b["all_installs"]) for b in series["builds"]],
                         [("20260825094512", 1, 60), ("20260826094000", 2, 0)])
        self.assertIsNone(series["builds"][1]["per_10k"])
        self.assertTrue(all(c.args[0]["_facets_size"] >= 1000 for c in search.call_args_list))

    def test_a_failed_or_malformed_response_gives_no_series_not_zeros(self):
        for response in (None, {"facets": {}}):
            def failing(params):
                if "signature" in params and params["_facets"] == "version":
                    return response
                return self._facets(params)

            with self.subTest(response=response), mock.patch.object(spikes, "_search", side_effect=failing):
                self.assertIsNone(
                    versionstep.cycle_series("Firefox", [SIG], 157, date(2026, 9, 29)))

    def test_a_measured_zero_is_kept(self):
        def zero(params):
            if "signature" in params and params["_facets"] == "version":
                return {"facets": {"version": []}}
            return self._facets(params)

        with mock.patch.object(spikes, "_search", side_effect=zero):
            series = versionstep.cycle_series("Firefox", [SIG], 157, date(2026, 9, 29))
        self.assertEqual(series["nightly"]["157"], {"installs": 0, "all_installs": 950,
                                                    "per_10k": 0.0})

    def test_a_truncated_facet_gives_no_series(self):
        with (
            mock.patch.object(spikes, "_search", side_effect=self._facets),
            mock.patch.object(versionstep, "_BUILD_FACETS", 2),
        ):
            self.assertIsNone(versionstep.cycle_series("Firefox", [SIG], 157, date(2026, 9, 29)))


class TestFallbackSeed(unittest.TestCase):
    def _seed(self, uuid_info):
        from crashclouseau import inspector, sigage, sigfamily, tools
        from crashclouseau.agent import orchestrator

        esc = _FakeEscalation(id=9, signature=SIG, product="Firefox", channel="release",
                              buildid="20260924084938", build_day=date(2026, 9, 29))
        with (
            mock.patch.object(inspector, "get_crash_data", return_value={"version": "157.0"}),
            mock.patch.object(models.CrashStack, "get_by_uuid", return_value=({}, None)),
            mock.patch.object(orchestrator, "_signature_trend", return_value={}),
            mock.patch.object(orchestrator, "_version_rates", return_value={}),
            mock.patch.object(orchestrator, "_hardware_noise", return_value={}),
            mock.patch.object(orchestrator, "_poison_population", return_value=None),
            mock.patch.object(sigage, "signature_history", return_value={}),
            mock.patch.object(sigage, "first_seen_ever", return_value={}),
            mock.patch.object(sigfamily, "lookup", return_value={}),
            mock.patch.object(tools, "get_changeset", return_value="fcc3166468d2") as rev,
        ):
            return se._minimal_seed("u-socorro", uuid_info, esc), rev

    def test_a_report_outside_the_database_is_pinned_to_its_build(self):
        seed, rev = self._seed(None)
        self.assertEqual((seed["pin_rev"], seed["build_node"]), ("fcc3166468d2", "fcc3166468d2"))
        rev.assert_called_once_with(datetime(2026, 9, 24, 8, 49, 38, tzinfo=timezone.utc),
                                    "release", "Firefox")
        seed, rev = self._seed({"node": "aaaaaaaaaaaa"})
        self.assertEqual(seed["pin_rev"], "aaaaaaaaaaaa")
        rev.assert_not_called()
