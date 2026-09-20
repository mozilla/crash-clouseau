# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""Tests for the train fields added when filing a new bug.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
        uv run python -m unittest tests.test_affected_flags

The preview supplies the crash's own train. The filer adds live trains reported by Socorro,
submits the fields in the create, and falls back to individual PUTs after a client rejection.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import bugzilla_apply, report_bug, sigage, spike_report  # noqa: E402
from crashclouseau.agent import spike_escalation as se  # noqa: E402
from tests.test_autofile import _INFO, _PREVIEW, _Base, _bug  # noqa: E402
from tests.test_spike_escalation import _FilerBase, _esc  # noqa: E402

LIVE = {"nightly": 158, "beta": 157, "release": 156, "esr": 153, "esr_previous": 140}
_OWN = {"cf_status_firefox158": "affected"}
_NIGHTLY_PREVIEW = {**_PREVIEW, "status_flags": dict(_OWN)}


class TestTheFlagName(unittest.TestCase):
    def test_the_status_flag_follows_the_versions_major_and_the_channels_family(self):
        self.assertEqual(report_bug._status_flag("158.0a1"), "cf_status_firefox158")
        self.assertEqual(report_bug._status_flag("157.0b4", "beta"), "cf_status_firefox157")
        self.assertEqual(report_bug._status_flag("156.0.1", "release"), "cf_status_firefox156")
        self.assertEqual(report_bug._status_flag("153.2.0esr", "esr153"),
                         "cf_status_firefox_esr153")
        self.assertEqual(report_bug._status_flag("140.15.0esr", "esr140"),
                         "cf_status_firefox_esr140")
        for bad in ("", None, "garbage", "0.1"):
            self.assertIsNone(report_bug._status_flag(bad), bad)
        # Same naming as the tracking flag, a different kind.
        self.assertEqual(report_bug._tracking_flag("153.2.0esr", "esr153"),
                         "cf_tracking_firefox_esr153")
        self.assertEqual(report_bug._tracking_flag("155.0.1"), "cf_tracking_firefox155")

    def test_trains_become_flags(self):
        self.assertEqual(
            report_bug.status_flags_for_trains({("firefox", 157), ("esr", 140)}),
            {"cf_status_firefox157": "affected", "cf_status_firefox_esr140": "affected"})
        self.assertEqual(report_bug.status_flags_for_trains(None), {})


class TestWhichTrainsHaveTheSignature(unittest.TestCase):
    """`sigage.trains_from_versions`, the pure half of `affected_trains`."""

    def setUp(self):
        sigage._read_live_trains.cache_clear()
        self.addCleanup(sigage._read_live_trains.cache_clear)

    def test_live_trains_reads_product_details_with_a_bound_and_caches_success(self):
        response = mock.Mock()
        response.json.return_value = {
            "FIREFOX_NIGHTLY": "158.0a1",
            "LATEST_FIREFOX_RELEASED_DEVEL_VERSION": "157.0b4",
            "LATEST_FIREFOX_VERSION": "156.0.1",
            "FIREFOX_ESR_NEXT": "153.2.0esr",
            "FIREFOX_ESR": "140.15.0esr",
        }
        with mock.patch.object(sigage.net, "get", return_value=response) as get:
            self.assertEqual(sigage.live_trains(), LIVE)
            self.assertEqual(sigage.live_trains(), LIVE)
        get.assert_called_once_with(sigage._FIREFOX_VERSIONS_URL,
                                    timeout=sigage.net.SERVICE_TIMEOUT)
        response.raise_for_status.assert_called_once_with()

    def test_a_failed_product_details_read_is_not_cached(self):
        response = mock.Mock()
        response.json.return_value = {
            "FIREFOX_NIGHTLY": "158.0a1",
            "LATEST_FIREFOX_RELEASED_DEVEL_VERSION": "157.0b4",
            "LATEST_FIREFOX_VERSION": "156.0.1",
            "FIREFOX_ESR_NEXT": "",
            "FIREFOX_ESR": "153.2.0esr",
        }
        with mock.patch.object(sigage.net, "get",
                               side_effect=[RuntimeError("timeout"), response]) as get:
            self.assertEqual(sigage.live_trains(), {})
            self.assertEqual(sigage.live_trains(),
                             {"nightly": 158, "beta": 157, "release": 156, "esr": 153})
        self.assertEqual(get.call_count, 2)

    def test_only_live_majors_count_and_the_suffix_picks_the_family(self):
        observed = {"158.0a1": 3, "157.0b4": 1, "157.0a1": 2, "155.0.3": 9, "150.0": 1,
                    "140.3.0esr": 2, "115.20.0esr": 1, "garbage": 4, "156.0": 0}
        self.assertEqual(sigage.trains_from_versions(observed, LIVE),
                         {("firefox", 158), ("firefox", 157), ("esr", 140)})

    def test_an_esr_version_never_matches_a_desktop_major(self):
        # 153 is the ESR line and, in LIVE, nobody's desktop train; 156.0esr is nonsense.
        self.assertEqual(sigage.trains_from_versions({"153.2.0esr": 1, "156.0esr": 1}, LIVE),
                         {("esr", 153)})

    def test_no_live_trains_means_no_trains(self):
        self.assertEqual(sigage.trains_from_versions({"158.0a1": 3}, {}), set())
        self.assertEqual(sigage.trains_from_versions({}, LIVE), set())
        self.assertEqual(sigage.trains_from_versions(None, None), set())

    def test_affected_trains_answers_none_when_it_could_not_ask(self):
        with mock.patch.object(sigage, "live_trains", return_value={}):
            self.assertIsNone(sigage.affected_trains("Foo::Bar"))
        with mock.patch.object(sigage, "live_trains", return_value=LIVE), \
                mock.patch.object(sigage, "observed_versions", return_value=None):
            self.assertIsNone(sigage.affected_trains("Foo::Bar"))
        with mock.patch.object(sigage, "live_trains", return_value=LIVE), \
                mock.patch.object(sigage, "observed_versions", return_value={"157.0b2": 1}) as ov:
            self.assertEqual(sigage.affected_trains("Foo::Bar", "Fenix"), {("firefox", 157)})
        ov.assert_called_once_with("Foo::Bar", "Fenix", sigage.VERSION_RATES_DAYS)

    def test_observed_versions_reads_the_facet(self):
        facet = {"total": 9, "facets": {"version": [
            {"term": "158.0a1", "count": 6}, {"term": "157.0b4", "count": 2},
            {"term": " ", "count": 1}]}}

        def fake(params=None, handler=None, handlerdata=None, **kw):
            self.assertEqual(params["_facets"], "version")
            self.assertEqual(params["_results_number"], 0)
            self.assertEqual(params["signature"], "=Foo::Bar")
            self.assertEqual(params["product"], "Fenix")
            self.assertNotIn("release_channel", params, "every channel, on purpose")
            handler(facet, handlerdata)
            return mock.Mock(wait=lambda: None)

        with mock.patch.object(sigage.socorro, "SuperSearch", side_effect=fake):
            self.assertEqual(sigage.observed_versions("Foo::Bar", "Fenix"),
                             {"158.0a1": 6, "157.0b4": 2})
        self.assertIsNone(sigage.observed_versions(""))


class TestThePreviewStatesTheCrashsOwnTrain(unittest.TestCase):
    """The preview's half is pure: the flag for the crash's own version, whatever the channel.
    The filer obtains other trains with a SuperSearch (`_train_flags`)."""

    def _preview(self, channel, version, **dossier_over):
        uuid_info = {"uuid": "u-1", "signature": "Foo::Bar", "channel": channel,
                     "product": "Firefox", "buildid": "20260903215306", "version": version}
        dossier = {"verdict": {"decision": "lead"},
                   "candidate": {"node": "n", "bug": 1, "author": "A"}, **dossier_over}
        with mock.patch.object(report_bug, "fetch_signature_stats", return_value=(True, "")), \
                mock.patch.object(report_bug, "fetch_crash_reason", return_value={}), \
                mock.patch.object(report_bug, "resolve_product_component",
                                  return_value=("Core", "Networking: Cookies")), \
                mock.patch.object(report_bug, "_needinfo_person", return_value={}), \
                mock.patch.object(report_bug.models.UUID, "get_info", return_value={}):
            return report_bug.build_bug_preview(uuid_info, {"frames": []}, dossier)

    def test_every_channel_states_its_own_train(self):
        self.assertEqual(self._preview("nightly", "158.0a1")["status_flags"], _OWN)
        self.assertEqual(self._preview("beta", "157.0b4")["status_flags"],
                         {"cf_status_firefox157": "affected"})
        self.assertEqual(self._preview("release", "156.0.1")["status_flags"],
                         {"cf_status_firefox156": "affected"})
        self.assertEqual(self._preview("esr153", "153.2.0esr")["status_flags"],
                         {"cf_status_firefox_esr153": "affected"})

    def test_an_actionable_verdict_states_it_too(self):
        # `affected` is a fact about a version, not a regression claim: the actionable verdict
        # drops the regression marks and keeps this one.
        p = self._preview("release", "156.0.1", verdict={"decision": "actionable"})
        self.assertEqual(p["status_flags"], {"cf_status_firefox156": "affected"})
        self.assertIsNone(p["tracking_flag"])
        self.assertEqual(p["keywords"], ["crash"])

    def test_no_version_means_no_flag(self):
        self.assertEqual(self._preview("nightly", "")["status_flags"], {})

    def test_the_spike_preview_states_it_as_well(self):
        brief = {"signature": "Foo::Bar", "channel": "beta", "product": "Firefox",
                 "version": "157.0b4", "buildid": "20260903215306",
                 "spike": {"kind": "build_day", "count": 60, "installs": 55,
                           "baseline": [5, 4, 6]}}
        with mock.patch.object(spike_report, "build_spike_comment", return_value="c"):
            p = spike_report.build_spike_preview(brief, None, product="Core", component="General")
        self.assertEqual(p["status_flags"], {"cf_status_firefox157": "affected"})
        with mock.patch.object(spike_report, "build_spike_comment", return_value="c"):
            p = spike_report.build_spike_preview(dict(brief, version=None), None,
                                                 product="Core", component="General")
        self.assertEqual(p["status_flags"], {})

    def test_the_flags_ride_the_create(self):
        # `_train_flags` combines the preview fields with trains reported by Socorro;
        # `_create_payload` posts them under their BMO field names.
        preview = {**_NIGHTLY_PREVIEW, "tracking_flag": "cf_tracking_firefox158"}
        with mock.patch("crashclouseau.sigage.affected_trains",
                        return_value={("firefox", 157)}) as trains:
            flags = bugzilla_apply._train_flags(preview, "Foo::Bar", "Firefox")
        trains.assert_called_once_with("Foo::Bar", "Firefox")
        self.assertEqual(flags, {"cf_tracking_firefox158": "?", "cf_status_firefox158": "affected",
                                 "cf_status_firefox157": "affected"})
        payload = bugzilla_apply._create_payload(preview, "dev@moz.example", flags)
        for flag, value in flags.items():
            self.assertEqual(payload[flag], value)
        self.assertEqual(payload["flags"][0]["name"], "needinfo")
        self.assertNotIn("status_flags", payload)
        self.assertNotIn("tracking_flag", payload)
        # Omitting train fields and passing an empty mapping produce the same body.
        self.assertEqual(bugzilla_apply._create_payload(preview, ""),
                         bugzilla_apply._create_payload(preview, "", {}))
        self.assertFalse(any(k.startswith("cf_") and k != "cf_crash_signature"
                             for k in bugzilla_apply._create_payload(preview, "")))


class TestTheFilerSetsTheFlags(_Base):
    """`_Base` stubs every BMO write (the create's body lands in `self.created`, PUTs in
    `self.puts`) and has `sigage.affected_trains` answer `None` -- could not ask -- unless a test
    says otherwise."""

    def _file(self, preview=None, trains=None):
        report_bug.build_bug_preview.return_value = preview or _NIGHTLY_PREVIEW
        sigage.affected_trains.return_value = trains
        return bugzilla_apply.autofile_bug(
            "u-1", _INFO, {}, {"candidate": {"node": "n"}}, "lead", 70)

    @staticmethod
    def _status_in(body):
        return {k: v for k, v in body.items() if k.startswith("cf_status_")}

    def _status_puts(self):
        return [(b, c) for b, c in self.puts if any(k.startswith("cf_status_") for k in c)]

    def test_the_own_train_and_the_observed_trains_ride_the_create(self):
        res = self._file(trains={("firefox", 158), ("firefox", 157)})
        self.assertTrue(res["filed"])
        self.assertEqual(res["mode"], "new_bug")
        both = {"cf_status_firefox157": "affected", "cf_status_firefox158": "affected"}
        # The train fields are in the create body, with no separate status PUT.
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self._status_in(self.created[0]), both)
        self.assertEqual(self._status_puts(), [])
        self.assertEqual(res["status_flags"], both)
        self.assertNotIn("status_flags_failed", res)
        # The crash's signature and PRODUCT (Socorro's, not Bugzilla's): a Fenix signature is
        # asked about on Fenix.
        sigage.affected_trains.assert_called_once_with("Foo::Bar", "Firefox")
        # Existing blocker and regression-link updates are unchanged.
        self.assertEqual(res["regressed_by"], [42])
        self.assertEqual(res["blocks"], ["clouseau"])
        self.assertEqual(len(self.filed), 1)

    def test_a_flag_bmo_has_never_created_costs_that_flag_alone(self):
        # Simulate BMO rejecting an unknown field before creation. The retry keeps needinfo and
        # fallback PUTs isolate the unsupported field.
        def create(payload, token):
            self.created.append(payload)
            if "cf_status_firefox157" in payload:
                raise bugzilla_apply.BugzillaRejected(
                    "bugzilla create failed (400): code 53, Can't use cf_status_firefox157 as "
                    "a field name", status=400)
            return 999

        def put(bug, changes, token):
            if "cf_status_firefox157" in changes:
                raise RuntimeError("Can't use cf_status_firefox157 as a field name")
            self.puts.append((bug, changes))
            return bug
        bugzilla_apply._create_bug.side_effect = create
        bugzilla_apply._put_bug.side_effect = put
        res = self._file(trains={("firefox", 157)})
        self.assertTrue(res["filed"])
        self.assertEqual(len(self.created), 2)                  # with the flags, then without
        self.assertEqual(self._status_in(self.created[1]), {})
        self.assertIn("flags", self.created[1])                 # the needinfo stayed aboard
        self.assertEqual(res["needinfo"], "dev@moz.example")
        self.assertEqual(self._status_puts(), [(999, _OWN)])    # the supported PUT succeeded
        self.assertEqual(res["status_flags"], _OWN)
        self.assertEqual(res["status_flags_failed"], ["cf_status_firefox157"])
        self.assertEqual(res["regressed_by"], [42])             # the other PUTs were untouched
        self.assertEqual(len(self.filed), 1)                    # and the filing is on record

    def test_a_server_error_with_flags_aboard_is_never_retried(self):
        # A 5xx is not a verdict on the flags, and the POST may have landed: never re-post.
        def create(payload, token):
            self.created.append(payload)
            raise bugzilla_apply.BugzillaRejected("bugzilla create failed (503): gateway",
                                                  status=503)
        bugzilla_apply._create_bug.side_effect = create
        res = self._file(trains={("firefox", 157)})
        self.assertFalse(res["filed"])
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.filed, [])

    def test_the_ladder_drops_the_flags_before_the_needinfo_and_surfaces_the_first_refusal(self):
        # If every smaller body receives a 4xx, surface the first response.
        calls = []

        def create(payload, token):
            calls.append(payload)
            raise bugzilla_apply.BugzillaRejected(
                "the component is closed" if len(calls) == 1 else "less useful", status=400)
        bugzilla_apply._create_bug.side_effect = create
        res = self._file(trains={("firefox", 157)})
        self.assertFalse(res["filed"])
        self.assertEqual([("cf_status_firefox157" in c, "flags" in c) for c in calls],
                         [(True, True), (False, True), (False, False)])
        self.assertIn("the component is closed", res["skipped"])
        self.assertNotIn("less useful", res["skipped"])
        self.assertEqual(self.filed, [])

    def test_a_server_error_on_the_retry_is_what_the_record_says(self):
        # A 503 on the retry is ambiguous, so report it and do not attempt another POST.
        calls = []

        def create(payload, token):
            calls.append(payload)
            if "cf_status_firefox157" in payload:
                raise bugzilla_apply.BugzillaRejected(
                    "bugzilla create failed (400): code 53, Can't use cf_status_firefox157 as "
                    "a field name", status=400)
            raise bugzilla_apply.BugzillaRejected("bugzilla create failed (503): gateway",
                                                  status=503)
        bugzilla_apply._create_bug.side_effect = create
        with self.assertLogs(level="ERROR") as logs:
            res = self._file(trains={("firefox", 157)})
        self.assertFalse(res["filed"])
        self.assertEqual(len(calls), 2)
        self.assertIn("503", res["skipped"])
        self.assertNotIn("code 53", res["skipped"])
        self.assertTrue(any("code 53" in m for m in logs.output))   # the first refusal is logged
        self.assertEqual(self.filed, [])

    def test_an_unreachable_socorro_leaves_the_own_train(self):
        # `None` means discovery failed; retain the preview's own-train field.
        res = self._file(trains=None)
        self.assertEqual(res["status_flags"], _OWN)
        self.assertEqual(self._status_in(self.created[0]), _OWN)
        self.assertNotIn("status_flags_failed", res)

    def test_an_unexpected_train_discovery_error_cannot_cost_the_filing(self):
        sigage.affected_trains.side_effect = ValueError("unexpected facet")
        res = self._file()
        self.assertTrue(res["filed"])
        self.assertEqual(res["status_flags"], _OWN)
        self.assertEqual(self._status_in(self.created[0]), _OWN)
        self.assertEqual(len(self.created), 1)
        self.assertEqual(len(self.filed), 1)

    def test_a_bucket_bug_states_its_own_train_only(self):
        # The signature is the [meta] tracker's catch-all: it says nothing about which trains
        # have THIS cause, so Socorro is not even asked.
        preview = {**_NIGHTLY_PREVIEW, "title": "Suggest blocks shutdown inside viaduct",
                   "cf_crash_signature": "", "blocked": ["clouseau", 1866944],
                   "bucket": {"meta_bugs": [1866944], "key": "k"}}
        res = self._file(preview=preview, trains={("firefox", 158), ("firefox", 157)})
        self.assertTrue(res["filed"])
        self.assertEqual(res["status_flags"], _OWN)
        self.assertEqual(self._status_in(self.created[0]), _OWN)
        sigage.affected_trains.assert_not_called()

    def test_nothing_to_state_means_no_flag_and_no_key(self):
        res = self._file(preview={**_PREVIEW, "status_flags": {}}, trains=set())
        self.assertTrue(res["filed"])
        self.assertEqual(self._status_in(self.created[0]), {})
        self.assertEqual(self._status_puts(), [])
        self.assertNotIn("status_flags", res)
        self.assertNotIn("status_flags_failed", res)

    def test_a_comment_on_an_existing_bug_touches_no_flag(self):
        # Train fields apply only to newly filed bugs.
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(12345)]
        res = self._file(trains={("firefox", 158), ("firefox", 157)})
        self.assertTrue(res["filed"])
        self.assertEqual(res["mode"], "comment_on_existing")
        self.assertEqual(self.created, [])
        self.assertEqual(self._status_puts(), [])
        self.assertNotIn("status_flags", res)
        sigage.affected_trains.assert_not_called()

    def test_a_preview_from_before_this_existed_is_byte_identical_on_no_other_train(self):
        res = self._file(preview=_PREVIEW, trains=None)
        self.assertTrue(res["filed"])
        self.assertFalse(any(k.startswith("cf_") and k != "cf_crash_signature"
                             for k in self.created[0]))
        self.assertEqual(self._status_puts(), [])
        self.assertNotIn("status_flags", res)
        self.assertNotIn("status_flags_failed", res)


class TestTheSpikeFilerSetsThem(_FilerBase):
    """The spike filer shares `_train_flags`, `_create_payload` and `_record_train_flags`.
    `_FilerBase` stubs the create (its body lands in `self.created`) and has Socorro answer
    "could not ask", so a spike bug states the crash's own train unless a test says otherwise."""

    @staticmethod
    def _status_in(body):
        return {k: v for k, v in body.items() if k.startswith("cf_status_")}

    def test_a_spike_bug_states_its_trains_in_the_create(self):
        with mock.patch("crashclouseau.sigage.affected_trains",
                        return_value={("firefox", 157), ("firefox", 156)}) as trains:
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual(res["mode"], "spike_new_bug")
        trains.assert_called_once_with("mozilla::Foo::Bar", "Firefox")
        both = {"cf_status_firefox157": "affected", "cf_status_firefox156": "affected"}
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self._status_in(self.created[0]), both)
        self.assertEqual(res["status_flags"], both)
        self.assertNotIn("status_flags_failed", res)

    def test_flags_that_came_off_a_refused_create_are_set_one_put_each(self):
        puts = []

        def put(bug, changes, token):
            if "cf_status_firefox150" in changes:
                raise RuntimeError("Can't use cf_status_firefox150 as a field name")
            puts.append((bug, changes))
            return bug
        bugzilla_apply._create_bug_keeping_the_bug.side_effect = (
            lambda p, tok: self.created.append(p) or (2070000, {"train_flags"}))
        with mock.patch("crashclouseau.sigage.affected_trains",
                        return_value={("firefox", 157), ("firefox", 150)}), \
             mock.patch.object(bugzilla_apply, "_put_bug", side_effect=put):
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertTrue(res["filed"])
        self.assertEqual(puts, [(2070000, {"cf_status_firefox157": "affected"})])
        self.assertEqual(res["status_flags"], {"cf_status_firefox157": "affected"})
        self.assertEqual(res["status_flags_failed"], ["cf_status_firefox150"])

    def test_the_default_stub_keeps_the_own_train(self):
        res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual(res["status_flags"], {"cf_status_firefox157": "affected"})
        self.assertEqual(self._status_in(self.created[0]), {"cf_status_firefox157": "affected"})
        self.assertNotIn("status_flags_failed", res)


if __name__ == "__main__":
    unittest.main()
