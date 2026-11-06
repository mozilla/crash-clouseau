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
from datetime import datetime, timedelta, timezone  # noqa: E402
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
        self.assertEqual(seen["_aggs.release_channel"],
                         ["_cardinality.install_time", "moz_crash_reason"])

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


def _reasons(term, count, installs, reasons):
    row = _row(term, count, installs)
    row["facets"]["moz_crash_reason"] = [{"term": t, "count": n} for t, n in reasons]
    return row


_ASSERT = "MOZ_DIAGNOSTIC_ASSERT(entry && *entry == actor->GetLifecycleProxy())"


class TestTheCrashReasonSplit(unittest.TestCase):
    def setUp(self):
        report_bug._RECENT_CACHE.clear()

    def _fetch(self, rows):
        fake, _ = _search(rows)
        with mock.patch.object(report_bug.socorro, "SuperSearch", fake):
            return report_bug.fetch_recent_channel_stats(
                {"signature": "Foo::Bar", "product": "Firefox"}, 30)

    def test_complete_reason_lists_are_kept_and_merged(self):
        got = self._fetch([_reasons("release", 11, 11, []),
                           _reasons("beta", 3, 3, [("A", 2)]),
                           _reasons("aurora", 2, 2, [("A", 1), ("B", 1)])])
        self.assertEqual(got["release"]["reasons"], {})
        self.assertEqual(got["beta"]["reasons"], {"A": 3, "B": 1})

    def test_a_capped_list_is_unknown_for_the_whole_channel(self):
        capped = [("R{}".format(i), 1) for i in range(report_bug._RECENT_FACETS_SIZE)]
        got = self._fetch([_reasons("release", 60, 60, capped),
                           _reasons("beta", 3, 3, [("A", 3)]),
                           _reasons("aurora", 60, 60, capped)])
        self.assertNotIn("reasons", got["release"])
        self.assertNotIn("reasons", got["beta"])

    def test_the_sentence_says_how_many_carry_the_reason(self):
        recent = {"release": {"count": 11, "installs": 11, "reasons": {}},
                  "nightly": {"count": 3, "installs": 3, "reasons": {_ASSERT: 3}},
                  "esr": {"count": 1, "installs": 1, "reasons": {}}}
        self.assertEqual(
            report_bug.build_recent_channels_sentence(recent, "Firefox", 30, reason=_ASSERT),
            "In the last 30 days, Firefox has 11 crashes (from 11 installations; none with this "
            "crash reason) on release, 3 crashes (from 3 installations) on nightly and 1 crash "
            "(without this crash reason) on esr with this signature.")
        recent = {"release": {"count": 4799, "installs": 4500,
                              "reasons": {"MOZ_CRASH(JNI exception)": 145}}}
        self.assertIn("4799 crashes (from 4500 installations; 145 with this crash reason)",
                      report_bug.build_recent_channels_sentence(
                          recent, "Fenix", 30, reason="MOZ_CRASH(JNI exception)"))

    def test_allocation_sizes_in_the_reason_are_ignored(self):
        recent = {"release": {"count": 2, "installs": 2, "reasons": {
            "out of memory: 0x0000000000101000 bytes requested": 1,
            "out of memory: 0x0000000000202000 bytes requested": 1}}}
        self.assertEqual(
            report_bug.build_recent_channels_sentence(
                recent, "Firefox", 30, reason="out of memory: 0x0000000000303000 bytes requested"),
            "In the last 30 days, Firefox has 2 crashes (from 2 installations) on release with "
            "this signature.")

    def test_constants_in_stringified_code_are_kept(self):
        for own, other in (("MOZ_RELEASE_ASSERT(count == 1)", "MOZ_RELEASE_ASSERT(count == 2)"),
                           ("MOZ_CRASH(step 1)", "MOZ_CRASH(step 2)"),
                           ("assertion failed: len == 1", "assertion failed: len == 2")):
            recent = {"release": {"count": 10, "installs": 10, "reasons": {own: 1, other: 9}}}
            self.assertIn("1 with this crash reason", report_bug.build_recent_channels_sentence(
                recent, "Firefox", 30, reason=own), own)

    def test_runtime_numbers_are_ignored_and_identifiers_kept(self):
        key = report_bug._reason_key
        self.assertEqual(key("[Parent 15916, Main Thread] ###!!! ABORT: file a.cpp:420"),
                         key("[Parent 8508, Main Thread] ###!!! ABORT: file a.cpp:420"))
        self.assertEqual(key("ElementAt(aIndex = 2, aLength = 2)"),
                         key("ElementAt(aIndex = 28, aLength = 28)"))
        self.assertEqual(key("zwp_linux_buffer_params_v1#280: error 7"),
                         key("zwp_linux_buffer_params_v1#877: error 7"))
        self.assertEqual(key("index out of bounds: the len is 5017 but the index is 5017"),
                         key("index out of bounds: the len is 271 but the index is 271"))
        self.assertEqual(key("out of memory: 0x00000000000FF958 bytes requested"),
                         key("out of memory: 0x0000000000057A08 bytes requested"))
        self.assertEqual(key("unreachable code: Expression [483] is not cached!"),
                         key("unreachable code: Expression [2320] is not cached!"))
        self.assertNotEqual(key("IPDL error: Msg_Read2 failed"),
                            key("IPDL error: Msg_Read3 failed"))

    def test_error_codes_are_kept(self):
        key = report_bug._reason_key
        self.assertNotEqual(key("zwp_linux_buffer_params_v1#280: error 7"),
                            key("zwp_linux_buffer_params_v1#877: error 8"))
        self.assertNotEqual(key("Failed to open the database: rv=0x80004005"),
                            key("Failed to open the database: rv=0x8000FFFF"))
        recent = {"release": {"count": 10, "installs": 10, "reasons": {
            "params_v1#280: error 7": 1, "params_v1#877: error 8": 9}}}
        self.assertIn("1 with this crash reason", report_bug.build_recent_channels_sentence(
            recent, "Firefox", 30, reason="params_v1#169: error 7"))

    def test_no_reason_or_an_unknown_split_says_nothing_more(self):
        plain = ("In the last 30 days, Firefox has 11 crashes (from 11 installations) on release "
                 "with this signature.")
        known = {"release": {"count": 11, "installs": 11, "reasons": {}}}
        unknown = {"release": {"count": 11, "installs": 11}}
        self.assertEqual(report_bug.build_recent_channels_sentence(known, "Firefox", 30), plain)
        self.assertEqual(report_bug.build_recent_channels_sentence(
            unknown, "Firefox", 30, reason=_ASSERT), plain)

    def test_the_preview_passes_the_crash_reason(self):
        recent = {"release": {"count": 106, "installs": 100, "reasons": {}},
                  "nightly": {"count": 3, "installs": 3, "reasons": {_ASSERT: 3}}}
        c = tav._build_preview(tav._preview_dossier(), recent=recent,
                               details={"moz_crash_reason": _ASSERT})["comment"]
        self.assertIn("106 crashes (from 100 installations; none with this crash reason) on "
                      "release", c)

    def test_the_floor_still_counts_every_report(self):
        recent = {"release": {"count": 60, "installs": 60, "reasons": {}}}
        with mock.patch.object(report_bug.config, "get_spike", return_value=50):
            self.assertEqual(report_bug.channels_over_floor(recent, "Firefox"), ["release"])


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

    def test_the_same_failing_line_files_below_the_floor(self):
        res = self._waiver("same_line")
        self.assertTrue(res["filed"], res)
        self.assertEqual(len(self.created), 1)

    def test_without_agreement_the_floor_holds(self):
        floor = bugzilla_apply.config.get_spike("real_installs", "Firefox", "nightly")
        below = ("1 installation on this signature, below the actionable floor of {}, and no "
                 "channel reached its floor in the last 30 days".format(floor))
        for waiver, skipped in (
                (None, below),
                ("pending", below),
                ("disagreed", below + "; the confirming pass did not reach the same "
                                      "actionable verdict on the same origin or failing line")):
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
        self.assertIn("(bug 2010557) by :bobowen, which landed on 2017-05-30; this signature "
                      "is also reported on builds made before that date.", p["comment"])
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


_RULE = dict(report_bug.config.ONSET_DEFAULT)


def _day(date, **counts):
    return {"term": date + "T00:00:00Z", "count": sum(counts.values()), "facets": {
        "release_channel": [{"term": t, "count": n} for t, n in counts.items()]}}


class TestChannelOnsets(unittest.TestCase):
    def setUp(self):
        report_bug._ONSET_CACHE.clear()

    def _fetch(self, window, days, fail=False):
        seen = []

        def search(params, handler, handlerdata):
            seen.append(params)
            if fail:
                raise RuntimeError("socorro down")
            response = days if "_histogram.date" in params else window
            if response is not None:
                handler(response, handlerdata)
            return mock.Mock()

        with mock.patch.object(report_bug.socorro, "SuperSearch", side_effect=search):
            got = report_bug.fetch_channel_onsets(
                {"signature": "Foo::Bar", "product": "Firefox"}, 24, 28)
        return got, seen

    def test_channels_merge_and_take_their_busiest_earlier_day(self):
        got, seen = self._fetch(
            {"facets": {"release_channel": [
                _row("release", 20, 18), _row("esr", 4, 4), _row("esr153", 3, 3),
                _row("aurora", 3, 3), _row("beta", 1, 1), _row("Bosch", 9, 9)]}},
            {"facets": {"histogram_date": [
                _day("2026-10-12", release=1), _day("2026-10-20", esr140=2, nightly=4),
                _day("2026-10-21", esr=1, esr140=1)]}})
        self.assertEqual(got, {"release": {"reports": 20, "installs": 18, "busiest_day": 1},
                               "esr": {"reports": 7, "installs": 7, "busiest_day": 2},
                               "beta": {"reports": 4, "installs": 4, "busiest_day": 0}})
        window, days = seen
        self.assertEqual((window["signature"], window["product"]), ("=Foo::Bar", "Firefox"))
        start, end = (datetime.fromisoformat(d.lstrip("<>=")) for d in window["date"])
        hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0, tzinfo=None)
        self.assertEqual((start, end), (hour - timedelta(hours=24), hour))
        self.assertEqual(window["_aggs.release_channel"], "_cardinality.install_time")
        self.assertEqual(days["date"], [">=" + (start - timedelta(days=28)).isoformat(),
                                        "<" + start.isoformat()])
        self.assertEqual((days["_histogram.date"], days["_histogram_interval.date"]),
                         ("release_channel", "1d"))

    def test_the_answer_is_cached_for_the_hour(self):
        self._fetch({"facets": {"release_channel": [_row("release", 20, 18)]}},
                    {"facets": {"histogram_date": []}})
        got, seen = self._fetch(None, None)
        self.assertEqual(got, {"release": {"reports": 20, "installs": 18, "busiest_day": 0}})
        self.assertEqual(seen, [])

    def test_an_unread_answer_is_unknown_and_not_cached(self):
        for window, days, fail in ((None, {"facets": {"histogram_date": []}}, False),
                                   ({"facets": {"release_channel": []}}, None, False),
                                   (None, None, True)):
            with self.subTest(window=window, days=days, fail=fail):
                got, _ = self._fetch(window, days, fail=fail)
                self.assertIsNone(got)
        self.assertEqual(report_bug._ONSET_CACHE, {})

    def test_errors_or_a_missing_facet_are_unread_and_an_empty_list_is_zero(self):
        rising = {"errors": [], "facets": {"release_channel": [_row("release", 5, 5)]}}
        empty = {"errors": [], "facets": {"histogram_date": []}}
        for window, days in ((rising, {"errors": ["shard failed"], "facets": {}}),
                             (rising, {"errors": [], "facets": {}}),
                             (rising, {"errors": ["shard failed"], "facets": {"histogram_date": []}}),
                             ({"errors": ["shard failed"], "facets": {}}, empty),
                             ({"errors": [], "facets": {}}, empty)):
            with self.subTest(window=window, days=days):
                got, _ = self._fetch(window, days)
                self.assertIsNone(got)
                self.assertEqual(report_bug._ONSET_CACHE, {})
        got, _ = self._fetch(rising, empty)
        self.assertEqual(got,
                         {"release": {"reports": 5, "installs": 5, "busiest_day": 0}})
        report_bug._ONSET_CACHE.clear()
        got, _ = self._fetch({"facets": {"release_channel": []}}, empty)
        self.assertEqual(got, {})

    def test_missing_inputs_ask_nothing(self):
        with mock.patch.object(report_bug.socorro, "SuperSearch") as search:
            for info, hours, days in (({"product": "Firefox"}, 24, 28),
                                      ({"signature": "Foo::Bar"}, 24, 28),
                                      ({"signature": "Foo::Bar", "product": "Firefox"}, 0, 28),
                                      ({"signature": "Foo::Bar", "product": "Firefox"}, 24, None)):
                self.assertIsNone(report_bug.fetch_channel_onsets(info, hours, days))
        search.assert_not_called()


class TestTheRiseRule(unittest.TestCase):
    def test_each_bar(self):
        onsets = {"release": {"reports": 20, "installs": 18, "busiest_day": 1},
                  "esr": {"reports": 7, "installs": 7, "busiest_day": 0},
                  "beta": {"reports": 4, "installs": 4, "busiest_day": 0},
                  "nightly": {"reports": 9, "installs": 2, "busiest_day": 0}}
        self.assertEqual(report_bug.channels_with_onset(onsets, _RULE), ["esr", "release"])

    def test_the_ratio_is_against_the_busiest_day(self):
        steady = {"release": {"reports": 34, "installs": 30, "busiest_day": 7}}
        self.assertEqual(report_bug.channels_with_onset(steady, _RULE), [])
        steady["release"]["reports"] = 35
        self.assertEqual(report_bug.channels_with_onset(steady, _RULE), ["release"])

    def test_nothing_known_or_no_rule_is_no_rise(self):
        row = {"release": {"reports": 20, "installs": 18, "busiest_day": 0}}
        self.assertEqual(report_bug.channels_with_onset(None, _RULE), [])
        self.assertEqual(report_bug.channels_with_onset(row, None), [])

    def test_socorro_is_asked_only_when_the_month_can_pass(self):
        info = {"signature": "Foo::Bar", "product": "Firefox"}
        rising = {"release": {"reports": 20, "installs": 18, "busiest_day": 1}}
        with mock.patch.object(report_bug, "fetch_channel_onsets", return_value=rising) as fetch:
            self.assertEqual(report_bug.recent_onsets(
                info, {"nightly": {"count": 9, "installs": 4}}, _RULE), (None, []))
            self.assertEqual(report_bug.recent_onsets(info, None, _RULE), (None, []))
            self.assertEqual(report_bug.recent_onsets(info, _RECENT, None), (None, []))
            fetch.assert_not_called()
            self.assertEqual(report_bug.recent_onsets(info, _RECENT, _RULE),
                             (rising, ["release"]))
            fetch.assert_called_once_with(info, 24, 28)

    def test_the_sentence(self):
        onsets = {"release": {"reports": 20, "installs": 18, "busiest_day": 1},
                  "beta": {"reports": 5, "installs": 1, "busiest_day": 0}}
        self.assertEqual(
            report_bug.build_onset_sentence(onsets, ["beta", "release"], "Firefox", _RULE),
            "On release, this signature has 20 crashes (from 18 installations) in the last 24 "
            "hours, against at most 1 a day over the previous 28 days. On beta/DevEdition, this "
            "signature has 5 crashes (from 1 installation) in the last 24 hours, against none "
            "over the previous 28 days.")
        self.assertIsNone(report_bug.build_onset_sentence(onsets, [], "Firefox", _RULE))


class TestTheSuddenRise(_Base):
    _BELOW = {"nightly": {"count": 1, "installs": 1}, "release": {"count": 21, "installs": 19}}
    _RISING = {"release": {"reports": 20, "installs": 18, "busiest_day": 1},
               "nightly": {"reports": 1, "installs": 1, "busiest_day": 0}}

    def setUp(self):
        super().setUp()
        for p in (
            mock.patch.object(report_bug, "fetch_signature_stats",
                              return_value=(True, {"count": 1, "installs": 1})),
            mock.patch.object(report_bug, "fetch_recent_channel_stats",
                              return_value=self._BELOW),
            mock.patch.object(report_bug, "fetch_channel_onsets", return_value=self._RISING),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _actionable(self, **over):
        return self._file(verdict="actionable", confidence=70,
                          **dict({"population_days": 30, "onset": dict(_RULE)}, **over))

    def _declined(self, res):
        floor = bugzilla_apply.config.get_spike("real_installs", "Firefox", "nightly")
        self.assertFalse(res["filed"])
        self.assertEqual(res["skipped"], "1 installation on this signature, below the actionable "
                                         "floor of {}, and no channel reached its floor in the "
                                         "last 30 days".format(floor))
        self.assertEqual(self.created, [])

    def test_a_rise_on_another_channel_files(self):
        res = self._actionable()
        self.assertTrue(res["filed"], res)
        self.assertEqual(len(self.created), 1)
        report_bug.fetch_channel_onsets.assert_called_with(mock.ANY, 24, 28)

    def test_a_steady_channel_declines(self):
        report_bug.fetch_channel_onsets.return_value = {
            "release": {"reports": 20, "installs": 18, "busiest_day": 7}}
        self._declined(self._actionable())

    def test_an_unread_rise_declines(self):
        report_bug.fetch_channel_onsets.return_value = None
        self._declined(self._actionable())

    def test_the_rule_is_a_knob(self):
        self._declined(self._actionable(onset=None))
        report_bug.fetch_channel_onsets.assert_not_called()

    def test_a_channel_over_its_floor_does_not_ask(self):
        report_bug.fetch_recent_channel_stats.return_value = _RECENT
        self.assertTrue(self._actionable()["filed"])
        report_bug.fetch_channel_onsets.assert_not_called()


class TestTheRiseInTheBug(unittest.TestCase):
    _LINE = ("On release, this signature has 20 crashes (from 18 installations) in the last 24 "
             "hours, against at most 1 a day over the previous 28 days.")

    def test_the_comment_says_why_a_rise_was_filed(self):
        rising = {"release": {"reports": 20, "installs": 18, "busiest_day": 1}}
        c = tav._build_preview(tav._preview_dossier(), recent=_RECENT, onsets=rising)["comment"]
        recent = "1 crash on nightly with this signature. "
        self.assertIn(recent + self._LINE, c)

    def test_no_rise_says_nothing(self):
        steady = {"release": {"reports": 20, "installs": 18, "busiest_day": 7}}
        for onsets in (steady, None):
            with self.subTest(onsets=onsets):
                c = tav._build_preview(tav._preview_dossier(), recent=_RECENT,
                                       onsets=onsets)["comment"]
                self.assertNotIn("in the last 24 hours", c)
                self.assertIn("1 crash on nightly with this signature.", c)


class TestTheRiseConfig(unittest.TestCase):
    def test_the_default_the_shipped_value_and_the_off_switch(self):
        config = report_bug.config
        with mock.patch.object(config, "get_agent", return_value={"autofile": {}}):
            self.assertEqual(config.get_agent_autofile("nightly")["onset"], _RULE)
        with mock.patch.object(config, "get_agent",
                               return_value={"autofile": {"onset": {"ratio": 3}}}):
            self.assertEqual(config.get_agent_autofile("nightly")["onset"],
                             dict(_RULE, ratio=3))
        for off in (None, {}, False):
            with mock.patch.object(config, "get_agent", return_value={"autofile": {"onset": off}}):
                self.assertIsNone(config.get_agent_autofile("nightly")["onset"])
        self.assertEqual(config.get_agent_autofile("nightly", product="Firefox")["onset"], _RULE)


if __name__ == "__main__":
    unittest.main()
