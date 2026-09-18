# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""A bug we FILE says which trains have the crash: `cf_status_firefox<major> = affected` for the
crash's own version and for every live train Socorro shows the signature on, each in its own PUT
after the create (`bugzilla_apply._set_status_flags`). New bugs only: a comment on somebody
else's bug touches no flag. Relman feedback of 2026-09-18, relayed by Calixte.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
        uv run python -m unittest tests.test_affected_flags

The preview's half is pure (the flag for the crash's own version); the trains are
`sigage.trains_from_versions` on a version facet; the filer's half runs against `_Base`'s
stubbed BMO (tests/test_autofile.py) and the spike filer against `_FilerBase`'s.
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
    The other trains need a SuperSearch, which is the filer's business (`_set_status_flags`)."""

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

    def test_the_flags_never_ride_the_create(self):
        # Their own PUTs after the create, like the tracking flag: a retired flag would reject the
        # create whole. A test elsewhere asserts the tuple's members reach the body; this one
        # asserts these two never do.
        payload = bugzilla_apply._create_payload(
            {**_NIGHTLY_PREVIEW, "tracking_flag": "cf_tracking_firefox158"}, "")
        self.assertNotIn("status_flags", payload)
        self.assertNotIn("tracking_flag", payload)
        self.assertFalse(any(k.startswith("cf_status_") for k in payload))


class TestTheFilerSetsTheFlags(_Base):
    """`_Base` stubs every BMO write (PUTs land in `self.puts`) and has `sigage.affected_trains`
    answer `None` -- could not ask -- unless a test says otherwise."""

    def _file(self, preview=None, trains=None):
        report_bug.build_bug_preview.return_value = preview or _NIGHTLY_PREVIEW
        sigage.affected_trains.return_value = trains
        return bugzilla_apply.autofile_bug(
            "u-1", _INFO, {}, {"candidate": {"node": "n"}}, "lead", 70)

    def _status_puts(self):
        return [(b, c) for b, c in self.puts if any(k.startswith("cf_status_") for k in c)]

    def test_the_own_train_and_the_observed_trains_each_in_their_own_put(self):
        res = self._file(trains={("firefox", 158), ("firefox", 157)})
        self.assertTrue(res["filed"])
        self.assertEqual(res["mode"], "new_bug")
        self.assertEqual(res["status_flags"],
                         {"cf_status_firefox157": "affected", "cf_status_firefox158": "affected"})
        # Each flag ALONE in its PUT: a PUT is atomic across fields, and a retired flag must cost
        # that flag only...
        self.assertEqual(sorted(self._status_puts(), key=lambda x: sorted(x[1])),
                         [(999, {"cf_status_firefox157": "affected"}),
                          (999, {"cf_status_firefox158": "affected"})])
        # ...and never inside the create, which an unknown field rejects whole.
        self.assertFalse(any(k.startswith("cf_status_") for k in self.created[0]))
        self.assertNotIn("status_flags_failed", res)
        # The crash's signature and PRODUCT (Socorro's, not Bugzilla's): a Fenix signature is
        # asked about on Fenix.
        sigage.affected_trains.assert_called_once_with("Foo::Bar", "Firefox")
        # The other PUTs are where they were.
        self.assertEqual(res["regressed_by"], [42])
        self.assertEqual(res["blocks"], ["clouseau"])
        self.assertEqual(len(self.filed), 1)

    def test_a_refused_flag_costs_that_flag_alone(self):
        def put(bug, changes, token):
            if "cf_status_firefox157" in changes:
                raise RuntimeError("There is no field named 'cf_status_firefox157'")
            self.puts.append((bug, changes))
            return bug
        bugzilla_apply._put_bug.side_effect = put
        res = self._file(trains={("firefox", 157)})
        self.assertTrue(res["filed"])
        self.assertEqual(res["status_flags"], _OWN)
        self.assertEqual(res["status_flags_failed"], ["cf_status_firefox157"])
        self.assertEqual(res["regressed_by"], [42])        # the other PUTs were untouched
        self.assertEqual(len(self.filed), 1)               # and the filing is on record

    def test_an_unreachable_socorro_leaves_the_own_train(self):
        # `None` from `affected_trains` is "could not ask", not "on no train": the crash's own
        # train is still a fact.
        res = self._file(trains=None)
        self.assertEqual(res["status_flags"], _OWN)
        self.assertEqual(self._status_puts(), [(999, _OWN)])
        self.assertNotIn("status_flags_failed", res)

    def test_an_unexpected_train_discovery_error_cannot_lose_the_created_bug(self):
        sigage.affected_trains.side_effect = ValueError("unexpected facet")
        res = self._file()
        self.assertTrue(res["filed"])
        self.assertEqual(res["status_flags"], _OWN)
        self.assertEqual(self._status_puts(), [(999, _OWN)])
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
        self.assertEqual(self._status_puts(), [(999, _OWN)])
        sigage.affected_trains.assert_not_called()

    def test_nothing_to_state_means_no_put_and_no_key(self):
        res = self._file(preview={**_PREVIEW, "status_flags": {}}, trains=set())
        self.assertTrue(res["filed"])
        self.assertEqual(self._status_puts(), [])
        self.assertNotIn("status_flags", res)
        self.assertNotIn("status_flags_failed", res)

    def test_a_comment_on_an_existing_bug_touches_no_flag(self):
        # Somebody else's bug: its flags are curated by hand, and the venue comment is not the
        # place to override them. New bugs only (Calixte, 2026-09-18).
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(12345)]
        res = self._file(trains={("firefox", 158), ("firefox", 157)})
        self.assertTrue(res["filed"])
        self.assertEqual(res["mode"], "comment_on_existing")
        self.assertEqual(self._status_puts(), [])
        self.assertNotIn("status_flags", res)
        sigage.affected_trains.assert_not_called()

    def test_a_preview_from_before_this_existed_is_byte_identical_on_no_other_train(self):
        res = self._file(preview=_PREVIEW, trains=None)
        self.assertTrue(res["filed"])
        self.assertEqual(self._status_puts(), [])
        self.assertNotIn("status_flags", res)
        self.assertNotIn("status_flags_failed", res)


class TestTheSpikeFilerSetsThem(_FilerBase):
    """The spike filer shares `_set_status_flags`; `_FilerBase` stubs it to answer the preview's
    own train. Here a recorder stands in, to see what the spike filer hands it and keeps."""

    def test_a_spike_bug_states_its_trains(self):
        calls = []

        def set_flags(bug_id, preview, signature, product, token):
            calls.append((bug_id, preview.get("status_flags"), signature, product))
            return ({"cf_status_firefox157": "affected", "cf_status_firefox156": "affected"},
                    ["cf_status_firefox150"])

        with mock.patch.object(bugzilla_apply, "_set_status_flags", side_effect=set_flags):
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual(res["mode"], "spike_new_bug")
        self.assertEqual(calls, [(2070000, {"cf_status_firefox157": "affected"},
                                  "mozilla::Foo::Bar", "Firefox")])
        self.assertEqual(res["status_flags"],
                         {"cf_status_firefox157": "affected", "cf_status_firefox156": "affected"})
        self.assertEqual(res["status_flags_failed"], ["cf_status_firefox150"])

    def test_the_default_stub_keeps_the_own_train(self):
        res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual(res["status_flags"], {"cf_status_firefox157": "affected"})
        self.assertNotIn("status_flags_failed", res)


if __name__ == "__main__":
    unittest.main()
