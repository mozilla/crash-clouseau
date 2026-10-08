# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""Actionable population checks and triage-owner routing.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        python -m unittest tests.test_actionable_population
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import bugzilla_apply, report_bug  # noqa: E402
from tests import test_actionable_verdict as tav  # noqa: E402
from tests.test_autofile import _INFO, _Base, _bug, _cfg  # noqa: E402

_RECENT = {"release": {"count": 106, "installs": 100}, "beta": {"count": 1, "installs": 1},
           "nightly": {"count": 1, "installs": 1}}
_OWNER = {"nick": "jimm", "name": "", "email": "owner@moz.example",
          "account": "owner@moz.example", "account_name": "Jim"}


def _search(facets):
    """Stub SuperSearch with ``facets`` and capture its query parameters."""
    seen = {}

    class FakeSearch:
        def __init__(self, params, handler, handlerdata):
            seen.update(params)
            if facets is not None:
                handler({"facets": {"release_channel": facets}}, handlerdata)

        def wait(self):
            return None

    return FakeSearch, seen


def _row(term, count, installs):
    return {"term": term, "count": count,
            "facets": {"cardinality_install_time": {"value": installs}}}


class TestRecentChannelStats(unittest.TestCase):
    def setUp(self):
        report_bug._RECENT_CACHE.clear()

    def test_channels_merge_by_family_and_unknown_values_are_dropped(self):
        fake, seen = _search([_row("release", 40, 30), _row("aurora", 5, 4), _row("beta", 3, 3),
                              _row("esr", 2, 2), _row("esr140", 1, 1), _row("default", 9, 9)])
        info = {"signature": "Foo::Bar", "product": "Firefox"}
        with mock.patch.object(report_bug.socorro, "SuperSearch", fake):
            got = report_bug.fetch_recent_channel_stats(info, 30)
        self.assertEqual(got, {"release": {"count": 40, "installs": 30},
                               "beta": {"count": 8, "installs": 7},
                               "esr": {"count": 3, "installs": 3}})
        self.assertEqual((seen["signature"], seen["product"]), ("=Foo::Bar", "Firefox"))
        self.assertTrue(seen["date"].startswith(">="))
        self.assertEqual(seen["_aggs.release_channel"], "_cardinality.install_time")

    def test_no_answer_is_unknown_and_not_cached(self):
        info = {"signature": "Foo::Bar", "product": "Fenix"}
        fake, _ = _search(None)
        with mock.patch.object(report_bug.socorro, "SuperSearch", fake):
            self.assertIsNone(report_bug.fetch_recent_channel_stats(info, 30))
        fake, _ = _search([])
        with mock.patch.object(report_bug.socorro, "SuperSearch", fake):
            self.assertEqual(report_bug.fetch_recent_channel_stats(info, 30), {})

    def test_no_window_or_no_signature_asks_nothing(self):
        with mock.patch.object(report_bug.socorro, "SuperSearch") as search:
            self.assertIsNone(report_bug.fetch_recent_channel_stats(
                {"signature": "Foo::Bar", "product": "Fenix"}, 0))
            self.assertIsNone(report_bug.fetch_recent_channel_stats(
                {"signature": "Foo::Bar", "product": "Fenix"}, None))
            self.assertIsNone(report_bug.fetch_recent_channel_stats({"product": "Fenix"}, 30))
        search.assert_not_called()

    def test_each_channel_meets_its_own_floor(self):
        floors = {"nightly": 3, "beta": 6, "release": 50}
        with mock.patch.object(report_bug.config, "get_spike",
                               side_effect=lambda typ, product, ch: floors[ch]):
            self.assertEqual(report_bug.channels_over_floor(_RECENT, "Fenix"), ["release"])
            mixed = {"release": {"count": 60, "installs": 49},
                     "nightly": {"count": 3, "installs": 3}}
            self.assertEqual(report_bug.channels_over_floor(mixed, "Fenix"), ["nightly"])
            self.assertEqual(report_bug.channels_over_floor(
                {"release": {"count": 60, "installs": 49}}, "Fenix"), [])
            self.assertEqual(report_bug.channels_over_floor(None, "Fenix"), [])

    def test_the_sentence(self):
        self.assertEqual(
            report_bug.build_recent_channels_sentence(_RECENT, "Fenix", 30),
            "In the last 30 days, Fenix has 106 crashes (from 100 installations) on release, "
            "1 crash on beta and 1 crash on nightly with this signature.")
        self.assertEqual(
            report_bug.build_recent_channels_sentence(
                {"beta": {"count": 4, "installs": 1}}, "Firefox", 30),
            "In the last 30 days, Firefox has 4 crashes (from 1 installation) on "
            "beta/DevEdition with this signature.")
        self.assertIsNone(report_bug.build_recent_channels_sentence({}, "Fenix", 30))

    def test_the_sentence_appears_when_it_adds_to_the_build_count(self):
        adds = report_bug._recent_adds
        own = {"nightly": {"count": 2, "installs": 2}}
        self.assertFalse(adds(own, {"count": 2, "installs": 2}, "nightly"))
        self.assertTrue(adds(own, {"count": 1, "installs": 1}, "nightly"))
        self.assertTrue(adds(own, {"count": 5, "installs": 1}, "nightly"))
        self.assertTrue(adds({"release": {"count": 1, "installs": 1}}, {"count": 2}, "nightly"))
        self.assertTrue(adds({"esr": {"count": 1, "installs": 1}}, {}, "nightly"))
        self.assertFalse(adds({"esr": {"count": 1, "installs": 1}}, {"count": 1, "installs": 1},
                              "esr153"))
        self.assertFalse(adds(None, {"count": 1}, "nightly"))


class TestTheFloor(_Base):
    """Actionable filings below the existing signature-count installation floor."""

    def setUp(self):
        super().setUp()
        for p in (
            mock.patch.object(report_bug, "fetch_signature_stats",
                              return_value=(True, {"count": 1, "installs": 1})),
            mock.patch.object(report_bug, "fetch_recent_channel_stats", return_value=_RECENT),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _actionable(self, dossier=None, **over):
        return self._file(verdict="actionable", confidence=70, dossier=dossier,
                          **dict({"population_days": 30}, **over))

    def test_a_channel_over_its_floor_passes(self):
        res = self._actionable()
        self.assertTrue(res["filed"], res)
        self.assertEqual(len(self.created), 1)
        report_bug.fetch_recent_channel_stats.assert_called_once_with(mock.ANY, 30)
        info = report_bug.fetch_recent_channel_stats.call_args.args[0]
        self.assertEqual((info["signature"], info["product"]), ("Foo::Bar", "Firefox"))

    def test_no_channel_over_its_floor_declines(self):
        report_bug.fetch_recent_channel_stats.return_value = {
            "nightly": {"count": 2, "installs": 2}, "release": {"count": 9, "installs": 9}}
        res = self._actionable()
        self.assertFalse(res["filed"])
        floor = bugzilla_apply.config.get_spike("real_installs", "Firefox", "nightly")
        self.assertEqual(res["skipped"], "1 installation on this signature, below the actionable "
                                         "floor of {}, and no channel reached its floor in the "
                                         "last 30 days".format(floor))
        self.assertEqual(self.created, [])

    def test_unread_counts_decline(self):
        report_bug.fetch_recent_channel_stats.return_value = None
        res = self._actionable()
        self.assertFalse(res["filed"])
        self.assertTrue(res["skipped"].endswith("; the last 30 days of reports could not be read"),
                        res["skipped"])

    def test_the_window_is_a_knob(self):
        for days in (0, None):
            with self.subTest(days=days):
                res = self._actionable(population_days=days)
                self.assertFalse(res["filed"])
                self.assertTrue(res["skipped"].endswith("below the actionable floor of {}".format(
                    bugzilla_apply.config.get_spike("real_installs", "Firefox", "nightly"))))
        report_bug.fetch_recent_channel_stats.assert_not_called()

    def test_a_fresh_origin_is_waived_first(self):
        age = {"landed": "2026-09-12", "days_before_build": 6.5, "predates_signature": True}
        res = self._actionable(dossier={"candidate": {"node": "n"},
                                        "corroborations": {"actionable_origin_age": age}})
        self.assertTrue(res["filed"], res)
        report_bug.fetch_recent_channel_stats.assert_not_called()

    def test_passing_reaches_the_open_bug_gate(self):
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(5)]
        res = self._actionable(wake_stale="off")
        self.assertFalse(res["filed"])
        self.assertEqual(res["skipped"], "open bug 5 exists; an actionable crash is filed only "
                                         "where no bug is")


class TestTheConfirmedWaiver(_Base):
    """Confirmation waiver below the installation floor."""

    def setUp(self):
        super().setUp()
        bugzilla_apply.config.get_agent_autofile.return_value = _cfg(population_days=30)
        for p in (
            mock.patch.object(report_bug, "fetch_signature_stats",
                              return_value=(True, {"count": 4, "installs": 1})),
            mock.patch.object(report_bug, "fetch_recent_channel_stats",
                              return_value={"nightly": {"count": 4, "installs": 1}}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _waiver(self, floor_waiver):
        return bugzilla_apply.autofile_bug("u-1", _INFO, {}, {"candidate": {"node": "n"}},
                                           "actionable", 70, floor_waiver=floor_waiver)

    def test_agreement_files_below_the_floor(self):
        res = self._waiver("agreed")
        self.assertTrue(res["filed"], res)
        self.assertEqual(len(self.created), 1)
        report_bug.fetch_recent_channel_stats.assert_not_called()

    def test_without_agreement_the_floor_holds(self):
        floor = bugzilla_apply.config.get_spike("real_installs", "Firefox", "nightly")
        below = ("1 installation on this signature, below the actionable floor of {}, and no "
                 "channel reached its floor in the last 30 days".format(floor))
        for waiver, skipped in (
                (None, below),
                ("pending", below),
                ("disagreed", below + "; the confirming pass did not reach the same "
                                      "actionable verdict and origin")):
            with self.subTest(waiver=waiver):
                res = self._waiver(waiver)
                self.assertFalse(res["filed"])
                self.assertEqual(res["skipped"], skipped)
        self.assertEqual(self.created, [])

    def test_a_disagreement_still_passes_on_another_channel(self):
        report_bug.fetch_recent_channel_stats.return_value = _RECENT
        res = self._waiver("disagreed")
        self.assertTrue(res["filed"], res)

    def test_agreement_does_not_pass_the_open_bug_gate(self):
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(5)]
        bugzilla_apply.config.get_agent_autofile.return_value = _cfg(population_days=30,
                                                                     wake_stale="off")
        res = self._waiver("agreed")
        self.assertFalse(res["filed"])
        self.assertEqual(res["skipped"], "open bug 5 exists; an actionable crash is filed only "
                                         "where no bug is")


class TestThePreview(unittest.TestCase):
    def test_recent_counts_follow_the_build_count(self):
        c = tav._build_preview(tav._preview_dossier(), recent=_RECENT)["comment"]
        line = ("In the last 30 days, Firefox has 106 crashes (from 100 installations) on "
                "release, 1 crash on beta/DevEdition and 1 crash on nightly with this signature.")
        self.assertIn(line, c)
        self.assertLess(c.find("There are 85 crashes"), c.find(line))
        self.assertLess(c.find(line), c.find("has been reported since"))

    def test_counts_that_add_nothing_are_left_out(self):
        own = {"release": {"count": 85, "installs": 77}}
        c = tav._build_preview(tav._preview_dossier(), recent=own)["comment"]
        self.assertNotIn("In the last", c)
        c = tav._build_preview(tav._preview_dossier(), recent=None)["comment"]
        self.assertNotIn("In the last", c)

    def test_a_regressor_filing_does_not_ask(self):
        d = tav._preview_dossier()
        d["verdict"]["decision"] = "lead"
        with mock.patch.object(report_bug, "fetch_recent_channel_stats") as recent:
            tav._build_preview(d)
        recent.assert_not_called()


def _old(days=3413.0):
    return tav._preview_dossier(actionable_origin_age={
        "landed": "2017-05-30", "days_before_build": days, "predates_signature": False})


class TestTheAsk(unittest.TestCase):
    def test_an_old_origin_by_an_inactive_author_asks_the_triage_owner(self):
        with mock.patch.object(report_bug, "_person_for_account", return_value=dict(_OWNER)):
            p = tav._build_preview(_old(), active=False, owner="owner@moz.example")
        self.assertEqual(p["needinfo_email"], "owner@moz.example")
        self.assertEqual(p["needinfo"], ":jimm, as triage owner, can you have a look please?")
        self.assertIn(":jimm, as triage owner, can you have a look please?", p["comment"])
        self.assertIn("(bug 2010557) by :bobowen.", p["comment"])
        self.assertNotIn(":bobowen, can you have a look", p["comment"])

    def test_a_restricted_bug_ccs_the_triage_owner_it_asks(self):
        with mock.patch.object(report_bug, "_person_for_account", return_value=dict(_OWNER)), \
                mock.patch.object(report_bug.sensitive, "is_withheld", return_value=True), \
                mock.patch.object(report_bug, "security_group", return_value="core-security"):
            p = tav._build_preview(_old(), active=False, owner="owner@moz.example")
        self.assertEqual(p["groups"], ["core-security"])
        self.assertEqual(p["needinfo_email"], "owner@moz.example")
        self.assertEqual(p["cc"], ["owner@moz.example"])

    def test_an_active_author_or_a_recent_origin_keeps_the_author(self):
        for name, dossier, active in (("active", _old(), True),
                                      ("unknown activity", _old(), None),
                                      ("recent origin", _old(days=200.0), False)):
            with self.subTest(name), \
                    mock.patch.object(report_bug, "_person_for_account",
                                      return_value=dict(_OWNER)):
                p = tav._build_preview(dossier, active=active, owner="owner@moz.example")
                self.assertEqual(p["needinfo_email"], "bob@x.com")
                self.assertEqual(p["needinfo"], ":bobowen, can you have a look please?")


class TestOwnerForOldOrigin(unittest.TestCase):
    _AUTHOR = {"nick": "jya", "account": "jya@example.org"}

    def _ask(self, person=None, age=3413.0, days=365, active=False, owner="owner@moz.example",
             user=None, found=None):
        corro = {} if age is None else {"actionable_origin_age": {"days_before_build": age}}
        with mock.patch.object(report_bug, "_component_activity", return_value=active) as act, \
                mock.patch.object(report_bug, "_component_triage_owner",
                                  return_value=owner) as own, \
                mock.patch.object(report_bug, "_bugzilla_user",
                                  return_value=user or {"exists": True, "nick": "jimm"}), \
                mock.patch.object(report_bug, "_person_for_account",
                                  return_value=dict(_OWNER) if found is None else found):
            got = report_bug._owner_for_old_origin(
                self._AUTHOR if person is None else person, {"corroborations": corro},
                "Core", "Audio/Video: Playback", days)
        return got, act, own

    def test_an_inactive_author_gives_the_owner(self):
        got, act, _ = self._ask()
        self.assertEqual(got["account"], "owner@moz.example")
        act.assert_called_once_with("jya@example.org", "Core", "Audio/Video: Playback", 365)

    def test_no_account_gives_the_owner_without_an_activity_read(self):
        got, act, _ = self._ask(person={})
        self.assertEqual(got["account"], "owner@moz.example")
        act.assert_not_called()

    def test_an_unknown_age_is_checked(self):
        got, act, _ = self._ask(age=None)
        self.assertEqual(got["account"], "owner@moz.example")
        act.assert_called_once()

    def test_what_keeps_the_author(self):
        cases = {
            "knob off": {"days": 0},
            "recent origin": {"age": 365.0},
            "active author": {"active": True},
            "activity unknown": {"active": None},
            "owner unreadable": {"owner": None},
            "no owner": {"owner": ""},
            "owner is the author": {"owner": "JYA@example.org"},
            "owner unverified": {"user": {"exists": True, "unverified": True}},
            "owner not askable": {"found": {}},
        }
        for name, over in cases.items():
            with self.subTest(name):
                got, _, _ = self._ask(**over)
                self.assertIsNone(got)


class _Response:
    def __init__(self, payload, fail=False):
        self.payload, self.fail = payload, fail

    def raise_for_status(self):
        if self.fail:
            raise RuntimeError("503")

    def json(self):
        return self.payload


class TestBugzillaReads(unittest.TestCase):
    def setUp(self):
        report_bug._ACTIVITY_CACHE.clear()
        report_bug._COMPONENT_OWNER_CACHE.clear()

    def test_activity_is_one_comment_by_the_account_in_the_window(self):
        with mock.patch.object(report_bug.net, "get",
                               return_value=_Response({"bugs": [{"id": 1}]})) as get:
            self.assertTrue(report_bug._component_activity("a@x", "Core", "DOM", 365))
            self.assertTrue(report_bug._component_activity("A@x", "Core", "DOM", 365))
        get.assert_called_once()
        params = get.call_args.kwargs["params"]
        self.assertEqual((params["product"], params["component"], params["j_top"]),
                         ("Core", "DOM", "AND_G"))
        self.assertEqual((params["f1"], params["o1"], params["v1"]),
                         ("longdesc", "changedby", "a@x"))
        self.assertEqual((params["f2"], params["o2"], params["v2"]),
                         ("longdesc", "changedafter", "-365d"))

    def test_activity_answers(self):
        with mock.patch.object(report_bug.net, "get", return_value=_Response({"bugs": []})):
            self.assertFalse(report_bug._component_activity("b@x", "Core", "DOM", 365))
        for resp in (_Response({}, fail=True), _Response({"error": True})):
            with mock.patch.object(report_bug.net, "get", return_value=resp):
                self.assertIsNone(report_bug._component_activity("c@x", "Core", "DOM", 365))

    def test_triage_owner(self):
        with mock.patch.object(report_bug.net, "get", return_value=_Response(
                {"name": "DOM", "triage_owner": "owner@moz.example"})) as get:
            self.assertEqual(report_bug._component_triage_owner("Core", "DOM"),
                             "owner@moz.example")
        self.assertTrue(get.call_args.args[0].endswith("/rest/component"))
        self.assertEqual(get.call_args.kwargs["params"], {"product": "Core", "component": "DOM"})
        for resp in (_Response({}, fail=True), _Response({"error": 1, "code": 51})):
            with mock.patch.object(report_bug.net, "get", return_value=resp):
                self.assertIsNone(report_bug._component_triage_owner("Core", "Nope"))


if __name__ == "__main__":
    unittest.main()
