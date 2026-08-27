# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""The attribution window widens when the signature's crash RATE is already rising.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        uv run python -m unittest tests.test_trend_window

Plan #19 step 1, spike/trend/REPORT.md section 8. Production bounds an off-stack crash's
candidate window at the PREVIOUS BUILD -- a median 3.21 h of pushlog -- and that window contains
the human-blamed regressor 5 times in 20. A flat 24 h contains it 12 in 20 as-of the filing day
(8-10 at a realistic run-day), and 48/168/504 h contain exactly the same 12 for 2-20x the cost.

What these tests pin is the part a back-test cannot: that the widening happens only on a rise,
that it is never NARROWER than what ships today, that the prompt describes the window the run
actually used, and that both facts reach the dossier so the deployed rate is readable later.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import pushlog, sigtrend  # noqa: E402
from crashclouseau.agent import orchestrator as orch  # noqa: E402
from crashclouseau.agent import triage  # noqa: E402

BUILD = datetime(2026, 7, 22, 10, 4, 59, tzinfo=timezone.utc)


def _ui(**over):
    ui = {"uuid": "u-1", "channel": "nightly", "product": "Firefox", "buildid": BUILD,
          "node": "buildnode", "signature": "nsTStringRepr::Length"}
    ui.update(over)
    return ui


def _rising(ratio=19.36, installs=9):
    """A `trend_facts`-shaped dict that `is_rising` accepts -- the anchor's own numbers."""
    return {"signature_trend_window_days": 7, "signature_trend_installs": installs,
            "signature_trend_reports": installs, "signature_trend_baseline_days": 56,
            "signature_trend_baseline_installs": 3,
            "signature_trend_expected_installs": 0.46, "signature_trend_ratio": ratio}


def _two_builds():
    return mock.patch.object(orch.models.Build, "get_two_last",
                             return_value=[{"revision": "r0"}, {"revision": "r1"}])


def _prev_pushdate(dt):
    return mock.patch.object(orch.models.Build, "get_pushdate_before", return_value=dt)


class TestTheFixtureReallyRises(unittest.TestCase):
    """If `_rising` drifted out of `is_rising`'s acceptance the rest of this file would pass
    while testing the non-rising branch twice."""

    def test_rising(self):
        self.assertTrue(sigtrend.is_rising(_rising()))

    def test_not_rising_below_the_ratio(self):
        self.assertFalse(sigtrend.is_rising(_rising(ratio=2.0)))

    def test_not_rising_below_the_install_floor(self):
        self.assertFalse(sigtrend.is_rising(_rising(installs=1)))

    def test_unmeasured_is_not_rising(self):
        # `{}` means the rollup had too little history. It must never read as "no change".
        self.assertFalse(sigtrend.is_rising({}))


class TestOffstackWindow(unittest.TestCase):
    def test_not_rising_keeps_the_deployed_bound(self):
        with _two_builds():
            w = orch._offstack_window(_ui(), rising=False)
        self.assertEqual(w["mode"], "revs")
        self.assertEqual(w["startrev"], "r0")
        self.assertEqual(w["endrev"], "buildnode")   # the crash's own build node
        self.assertFalse(w["widened"])
        self.assertIsNone(w["hours"])

    def test_rising_widens_to_24h(self):
        with _two_builds(), _prev_pushdate(BUILD - timedelta(hours=3, minutes=13)):
            w = orch._offstack_window(_ui(), rising=True)
        self.assertEqual(w["mode"], "dates")
        self.assertTrue(w["widened"])
        self.assertEqual(w["hours"], 24.0)
        self.assertEqual(w["end"], BUILD)
        self.assertEqual(w["start"], BUILD - timedelta(hours=24))

    def test_the_anchors_regressor_falls_inside_and_the_deployed_bound_misses_it(self):
        """Bug 2063336, the case the whole change exists for. `3bb594db2dda` landed on
        mozilla-central at 2026-07-21T20:23:27Z, 13.69 h before the first crashing build. The
        deployed window starts at the previous build, 3.21 h back."""
        landed = datetime(2026, 7, 21, 20, 23, 27, tzinfo=timezone.utc)
        prev_build = BUILD - timedelta(hours=3.21)
        with _two_builds(), _prev_pushdate(prev_build):
            wide = orch._offstack_window(_ui(), rising=True)
        self.assertLess(wide["start"], landed)
        self.assertGreaterEqual(wide["end"], landed)
        self.assertGreater(prev_build, landed)      # ...and today's bound lands after it

    def test_never_narrower_than_the_deployed_window(self):
        """A gap in the build stream is the one way widening could LOSE a candidate: if the
        previous build is more than 24 h back, 24 h is the narrower window. The bound is the
        earlier of the two, so a widened window is a superset by construction, not by cadence."""
        prev = BUILD - timedelta(hours=40)
        with _two_builds(), _prev_pushdate(prev):
            w = orch._offstack_window(_ui(), rising=True)
        self.assertEqual(w["start"], prev)
        self.assertEqual(w["hours"], 40.0)
        self.assertTrue(w["widened"])

    def test_no_previous_pushdate_still_widens(self):
        with _two_builds(), _prev_pushdate(None):
            w = orch._offstack_window(_ui(), rising=True)
        self.assertEqual(w["start"], BUILD - timedelta(hours=24))

    def test_predecessor_build_missing_falls_back_to_ndays_not_to_24h(self):
        """The degraded path predates this change and must keep its own, wider bound: with no
        `builds` row to anchor on, `update.put_report`'s nightly look-back is what we mirror."""
        ndays = orch.config.get_ndays()
        for rising in (False, True):
            with mock.patch.object(orch.models.Build, "get_two_last",
                                   return_value=[{"revision": "r1"}]):
                w = orch._offstack_window(_ui(), rising=rising)
            self.assertEqual(w["mode"], "dates", rising)
            self.assertEqual(w["start"], BUILD - timedelta(days=ndays), rising)
            # NOT `widened`: this is the pre-existing fallback, and counting it as a widening
            # would inflate the deployed rate this change is going to be judged on.
            self.assertFalse(w["widened"], rising)

    def test_24_is_the_measured_knee(self):
        # Guards the constant itself: 48/168/504 h contain exactly the same 12 of 20 cases for
        # 2-20x the changesets, and the required look-back is bimodal with nothing between
        # 17.4 h and 1,188 h. There is no intermediate width to tune into this number.
        self.assertEqual(orch._RISE_WINDOW_HOURS, 24)


class TestOffstackCandidatesUseTheWindow(unittest.TestCase):
    _WIN = [{"node": "w1", "date": BUILD - timedelta(hours=13), "backedout": False,
             "merge": False, "bug": 1158387,
             "desc": 'Bug 1158387 - Cookie DB: "PRAGMA synchronous = NORMAL"'}]

    def _cfg(self, **over):
        base = {"enabled": True, "max_candidates": 150, "pinned": True,
                "require_callpath_for_strong": True, "exposer_classifier": True,
                "observe_only": True}
        base.update(over)
        return base

    def test_a_dates_window_fetches_by_date_and_keeps_merge_files(self):
        bounds = {"mode": "dates", "start": BUILD - timedelta(hours=24), "end": BUILD,
                  "hours": 24.0, "widened": True}
        with mock.patch.object(pushlog, "pushlog", return_value=self._WIN) as pd, \
             mock.patch.object(pushlog, "pushlog_for_revs") as pfr:
            out = orch._offstack_candidates(_ui(), self._cfg(), window=bounds)
        pfr.assert_not_called()
        self.assertEqual([c["node"] for c in out], ["w1"])
        args, kw = pd.call_args
        self.assertEqual(args[0], bounds["start"])
        self.assertEqual(args[1], bounds["end"])
        # Parity with the rev-bounded branch: this path only READS the window, so it keeps the
        # file lists `_looks_pref_flip` ranks on instead of dropping them like ingestion does.
        self.assertIs(kw["drop_merge_files"], False)
        self.assertTrue(kw["file_filter"]("anything.random"))   # passthrough, as before

    def test_no_window_passed_is_the_deployed_behaviour(self):
        with _two_builds(), \
             mock.patch.object(pushlog, "pushlog_for_revs", return_value=self._WIN) as pfr, \
             mock.patch.object(pushlog, "pushlog") as pd:
            orch._offstack_candidates(_ui(), self._cfg())
        pd.assert_not_called()
        self.assertEqual(pfr.call_args.args[:2], ("r0", "buildnode"))

    def test_a_broken_window_abstains_rather_than_raising(self):
        with mock.patch.object(orch.models.Build, "get_two_last",
                               side_effect=RuntimeError("db down")):
            self.assertEqual(orch._offstack_candidates(_ui(), self._cfg()), [])


class TestBuildSeedWidensOnARise(unittest.TestCase):
    _RES = {"frames": [{"stackpos": 0, "function": "F", "filename": "a.cpp",
                        "line": 1, "changesets": {}}]}       # off-stack: nothing scored
    _UI = {"uuid": "u-1", "id": 1, "signature": "nsTStringRepr::Length", "buildid": BUILD,
           "channel": "nightly", "product": "Firefox", "java": False, "node": "buildnode"}
    _INFO = {"signature": "nsTStringRepr::Length", "channel": "nightly", "product": "Firefox",
             "buildid": "20260722100459", "version": "1"}

    def _seed(self, trend):
        window = [{"node": "w1", "date": BUILD - timedelta(hours=13), "backedout": False,
                   "merge": False, "bug": 555, "desc": "Bug 555 - touch nsTStringRepr"}]
        cfg = {"enabled": True, "max_candidates": 150, "pinned": True,
               "require_callpath_for_strong": True, "exposer_classifier": True,
               "observe_only": True, "prior_signature": False}
        with mock.patch.object(orch.config, "get_agent_offstack", return_value=cfg), \
             mock.patch.object(orch.models.CrashStack, "get_by_uuid",
                               return_value=(self._RES, self._UI)), \
             mock.patch.object(orch.models.UUID, "get_info", return_value=self._INFO), \
             mock.patch.object(orch, "_signature_trend", return_value=trend), \
             _two_builds(), _prev_pushdate(BUILD - timedelta(hours=3)), \
             mock.patch.object(pushlog, "pushlog_for_revs", return_value=window) as pfr, \
             mock.patch.object(pushlog, "pushlog", return_value=window) as pd, \
             mock.patch.object(orch.models.Node, "authors_for", return_value={}), \
             mock.patch.object(orch, "_crashing_area_experts", return_value=[]), \
             mock.patch("crashclouseau.inspector.get_crash_data", return_value={}):
            return orch.build_seed("u-1"), pfr, pd

    def test_a_rising_signature_gets_the_wide_window(self):
        seed, pfr, pd = self._seed(_rising())
        pfr.assert_not_called()
        pd.assert_called_once()
        self.assertEqual(seed["candidate_window"], {"hours": 24.0, "widened": True})
        # The rate the window decision was made on is the SAME dict the seed carries, so the
        # sentence a reader sees and the window the run used cannot disagree.
        self.assertEqual(seed["signature_trend"], _rising())

    def test_a_quiet_signature_keeps_the_deployed_window(self):
        seed, pfr, pd = self._seed({})
        pd.assert_not_called()
        pfr.assert_called_once()
        self.assertEqual(seed["candidate_window"], {"hours": None, "widened": False})
        self.assertEqual(seed["signature_trend"], {})

    def test_an_on_stack_crash_records_no_window(self):
        """`candidate_window` is the OFF-STACK pushlog slice. On-stack candidates come from
        `Changeset.find`'s stack-file-filtered `get_ndays()`-day window -- a different quantity,
        so this key stays None rather than describing that one."""
        res = {"frames": [{"stackpos": 0, "function": "F", "filename": "a.cpp", "line": 1,
                           "changesets": {"n1": {"score": 9, "bugid": 1, "backedout": False,
                                                 "pushdate": BUILD}}}]}
        with mock.patch.object(orch.models.CrashStack, "get_by_uuid",
                               return_value=(res, self._UI)), \
             mock.patch.object(orch.models.UUID, "get_info", return_value=self._INFO), \
             mock.patch.object(orch, "_signature_trend", return_value=_rising()), \
             mock.patch.object(orch.models.Node, "authors_for", return_value={}), \
             mock.patch("crashclouseau.inspector.get_crash_data", return_value={}):
            seed = orch.build_seed("u-1")
        self.assertFalse(seed["is_offstack"])
        self.assertIsNone(seed["candidate_window"])


class TestThePromptDescribesTheWindowItUsed(unittest.TestCase):
    def _prompt(self, window):
        crash = {"uuid": "u-1", "signature": "nsTStringRepr::Length", "channel": "nightly",
                 "product": "Firefox", "buildid": "20260722100459", "is_offstack": True,
                 "candidate_window": window, "stack": "frame 0",
                 "candidates": [{"node": "w1", "score": None, "bug": 1, "backedout": False,
                                 "desc": "Bug 1 - x", "noise": False}]}
        return triage._user_prompt(crash)

    def test_widened_says_so_and_says_how_wide(self):
        text = self._prompt({"hours": 24.0, "widened": True})
        self.assertIn("24 hours before this crash's build", text)
        self.assertIn("WIDER than the usual last-good-build bound", text)
        self.assertNotIn("window between the last-good build and this crash's build", text)

    def test_the_deployed_window_keeps_the_old_wording(self):
        text = self._prompt({"hours": None, "widened": False})
        self.assertIn("window between the last-good build and this crash's build", text)
        self.assertNotIn("WIDER than", text)

    def test_a_seed_with_no_window_key_keeps_the_old_wording(self):
        self.assertIn("window between the last-good build and this crash's build",
                      self._prompt(None))


class TestTheWideningIsRecorded(unittest.TestCase):
    """The log line lives ~2 h (no drain) and the seed is not persisted, so without these the
    deployed widening rate would be unreadable -- the `bdd848c` arrangement."""

    class _Dossier:
        corroborations = None

    def test_records_both_facts(self):
        d = self._Dossier()
        orch._record_candidate_window_facts(
            d, {"candidate_window": {"hours": 24.0, "widened": True}})
        self.assertEqual(d.corroborations["candidate_window_hours"], 24.0)
        self.assertIs(d.corroborations["candidate_window_widened"], True)

    def test_records_the_deployed_window_too(self):
        # Both arms have to be recorded or the widened ones cannot be compared to anything.
        d = self._Dossier()
        orch._record_candidate_window_facts(
            d, {"candidate_window": {"hours": 3.21, "widened": False}})
        self.assertEqual(d.corroborations["candidate_window_hours"], 3.21)
        self.assertIs(d.corroborations["candidate_window_widened"], False)

    def test_on_stack_records_nothing(self):
        d = self._Dossier()
        orch._record_candidate_window_facts(d, {"candidate_window": None})
        self.assertIsNone(d.corroborations)

    def test_it_preserves_the_flags_already_there(self):
        d = self._Dossier()
        d.corroborations = {"candidate_in_pushlog_window": True}
        orch._record_candidate_window_facts(
            d, {"candidate_window": {"hours": 24.0, "widened": True}})
        self.assertTrue(d.corroborations["candidate_in_pushlog_window"])
        self.assertEqual(len(d.corroborations), 3)

    def test_no_dossier_no_seed_no_crash(self):
        orch._record_candidate_window_facts(None, {"candidate_window": {"hours": 1.0}})
        orch._record_candidate_window_facts(self._Dossier(), None)


class TestPushlogMergeFileOverride(unittest.TestCase):
    """`pushlog()` used to hard-wire `suppresses_merge_extraction(channel)`. The off-stack
    reader needs the file lists on a channel that suppresses, and passes False."""

    _RESP = {"pushes": {"1": {"date": 1753000000, "changesets": [
        {"node": "abcdef1234567890", "desc": "Bug 1 - x", "author": "a <a@b.c>",
         "files": ["netwerk/cookie/CookiePersistentStorage.cpp"], "parents": ["p1", "p2"]}]}}}

    def _collect(self, drop):
        start = BUILD - timedelta(hours=24)
        with mock.patch.object(pushlog.net, "get") as g, \
             mock.patch.object(pushlog, "suppresses_merge_extraction", return_value=True):
            g.return_value.json.return_value = self._RESP
            kw = {} if drop is None else {"drop_merge_files": drop}
            return pushlog.pushlog(start, BUILD, channel="beta",
                                   file_filter=lambda f: True, **kw)

    def test_default_still_suppresses(self):
        self.assertEqual(self._collect(None)[0]["files"], [])

    def test_false_keeps_the_files(self):
        self.assertEqual(self._collect(False)[0]["files"],
                         ["netwerk/cookie/CookiePersistentStorage.cpp"])


if __name__ == "__main__":
    unittest.main()
