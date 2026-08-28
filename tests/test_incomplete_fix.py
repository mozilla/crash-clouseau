# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""A crash whose signature's OWN bug is already fixed, and shipped, and is still crashing.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        uv run python -m unittest tests.test_incomplete_fix

Crash 84794f8d (`libc.so.6 | cuEGLApiInit`, build 20260826091205, `SIGSYS / SYS_SECCOMP` in the
RDD process) is the motivating case. Bug 2063678 was filed for that signature on the day it first
appeared and RESOLVED FIXED; its patch `bbdaf4e3b2c2` landed 2026-08-19, a week before this
build; the crash is still here. There is something to investigate whether or not a changeset can
be named, and our filing path could not say so because it was built to defend a changeset.

The rule is deliberately three conditions, and the third is what makes it usable — see
`bugzilla_apply._incomplete_fix_bug` for the 21-day prod panel behind it and for
`nsAtom::IsStatic`, the counter-example that decides it.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import bugzilla_apply as ba  # noqa: E402
from crashclouseau import corroborations, report_bug as rb  # noqa: E402

SIG = "libc.so.6 | cuEGLApiInit"
BUILD = "20260826091205"
FIRST_SEEN = "20260814090231"                       # the day bug 2063678 was filed
LANDED = datetime(2026, 8, 19, 21, 8, 8, tzinfo=timezone.utc)
LANDING = {"node": "bbdaf4e3b2c2", "pushdate": LANDED}


def _bug(bid=2063678, resolved="2026-08-19T21:27:05Z", created="2026-08-14T17:39:33Z",
         product="Core", component="Audio/Video: Playback", assignee="tboiko@nvidia.com"):
    return {"id": bid, "resolution": "FIXED", "product": product, "component": component,
            "creation_time": created, "cf_last_resolved": resolved,
            "assigned_to_detail": {"email": assignee}}


def _detect(bugs=None, landing=LANDING, first_seen=FIRST_SEEN, build=BUILD, channel="nightly"):
    from crashclouseau import sigage
    rows = [(b, sigage.to_datetime(b.get("cf_last_resolved")))
            for b in (bugs if bugs is not None else [_bug()])]
    with mock.patch.object(ba, "_fixed_bugs_about", return_value=rows), \
         mock.patch.object(ba.models.Node, "landing_for_bug", return_value=landing):
        return ba._incomplete_fix_bug(SIG, build, "Firefox", channel, first_seen=first_seen)


class TestTheDetector(unittest.TestCase):
    def test_the_motivating_case(self):
        got = _detect()
        self.assertEqual(got["id"], 2063678)
        self.assertEqual(got["node"], "bbdaf4e3b2c2")
        self.assertEqual(got["component"], "Audio/Video: Playback")
        self.assertEqual(got["assigned_to"], "tboiko@nvidia.com")
        self.assertEqual(got["predates_days"], 0)

    def test_a_fix_that_postdates_the_build_is_the_other_gate_not_this_one(self):
        # Resolved AFTER the build -> `_fixed_after_build_bug`'s question, not ours.
        self.assertIsNone(_detect(bugs=[_bug(resolved="2026-08-28T00:00:00Z")]))

    def test_no_landing_in_our_pushlog_makes_no_claim(self):
        """`cf_last_resolved` is the RESOLUTION clock. Saying "the fix is in this build" needs
        the LANDING, and `Node.clean` keeps only 30 days of it — so an older fix answers None
        rather than guessing from the resolution date."""
        self.assertIsNone(_detect(landing=None))

    def test_a_landing_after_the_build_is_not_in_the_build(self):
        self.assertIsNone(_detect(landing={"node": "n",
                                           "pushdate": datetime(2026, 8, 27, tzinfo=timezone.utc)}))

    def test_a_signature_that_long_predates_its_bug_is_not_its_bug(self):
        """THE CONDITION THAT MAKES IT A RULE. `nsAtom::IsStatic` is still crashing after bug
        2062219's fix and the fix plainly worked (50 reports / 8 installations per build before,
        1-2 / 1-2 after). Its signature predates that bug by 3,073 days."""
        self.assertIsNone(_detect(first_seen="20180101000000"))

    def test_the_grace_is_a_sign_test_not_a_tuned_number(self):
        # Nearest miss in the prod panel is 28 days; the firing case is 0. Both sides of the
        # 7-day contemporaneity unit are pinned so a future edit has to mean it.
        filed = datetime(2026, 8, 14, 17, 39, 33, tzinfo=timezone.utc)
        just_inside = (filed - timedelta(days=6)).strftime("%Y%m%d%H%M%S")
        just_outside = (filed - timedelta(days=8)).strftime("%Y%m%d%H%M%S")
        self.assertIsNotNone(_detect(first_seen=just_inside))
        self.assertIsNone(_detect(first_seen=just_outside))

    def test_a_signature_much_newer_than_its_bug_is_not_its_bug_either(self):
        """The other direction, and it is not hypothetical: a triager adding a new signature to
        an old bug is the same "these crash alike" judgement as signature reuse."""
        self.assertIsNone(_detect(first_seen="20270101000000"))
        # ...but a few days either way is the same event.
        self.assertIsNotNone(_detect(first_seen="20260816000000"))

    def test_an_unknown_first_seen_never_reads_as_new(self):
        """`SignatureFirstDate`'s cron has not minted a row for the newest signatures, which is
        this rule's own target class, so a missing clock must abstain rather than assume."""
        for fs in (None, "", "  "):
            self.assertIsNone(_detect(first_seen=fs), fs)

    def test_no_bugs_no_claim(self):
        self.assertIsNone(_detect(bugs=[]))

    def test_no_build_no_claim(self):
        for b in (None, "", "not-a-build"):
            self.assertIsNone(_detect(build=b), b)

    def test_the_reused_signature_panel_the_sibling_gate_protects(self):
        """`_fixed_after_build_bug`'s docstring lists the filings it must NOT eat — post-fix
        crashes on a REUSED signature, where a FIXED bug predates the build by 22 to 1,310 days.
        This gate must not resurrect any of them, and the ownership condition is what stops it."""
        # `mozilla::ipc::FatalError | IProtocol`, whose signature predates bug 2054485 by 1,054
        # days and which carries three more FIXED bugs at -43.9, -1238 and -1310 days.
        got = _detect(bugs=[_bug(bid=2054485, resolved="2026-07-21T00:00:00Z",
                                 created="2023-08-01T00:00:00Z")],
                      first_seen="20200901000000")
        self.assertIsNone(got)


class TestTheSharedQuery(unittest.TestCase):
    """Both questions the resolution date can answer must see the same bug set, or the pair
    stops being exhaustive."""

    def setUp(self):
        ba._FIXED_BUGS_CACHE.clear()
        self.addCleanup(ba._FIXED_BUGS_CACHE.clear)

    def _rows(self, bugs, **kw):
        resp = mock.Mock()
        resp.json.return_value = {"bugs": bugs}
        with mock.patch.object(ba.net, "get", return_value=resp) as g:
            return ba._fixed_bugs_about(SIG, "Firefox", **kw), g

    def test_only_FIXED_survives(self):
        rows, _ = self._rows([
            dict(_bug(), cf_crash_signature="[@ {}]".format(SIG)),
            dict(_bug(bid=9), resolution="WORKSFORME",
                 cf_crash_signature="[@ {}]".format(SIG)),
        ])
        self.assertEqual([b["id"] for b, _ in rows], [2063678])

    def test_another_application_is_dropped(self):
        rows, _ = self._rows([dict(_bug(bid=7, product="MailNews Core"),
                                   cf_crash_signature="[@ {}]".format(SIG))])
        self.assertEqual(rows, [])

    def test_the_base_field_rides_along_with_the_detail(self):
        # BMO omits `assigned_to_detail` unless `assigned_to` is asked for too, and the omission
        # reads as "unassigned" rather than as an error.
        _, g = self._rows([])
        fields = g.call_args.kwargs["params"]["include_fields"]
        self.assertIn("assigned_to,assigned_to_detail", fields)

    def test_caching_is_opt_in(self):
        bugs = [dict(_bug(), cf_crash_signature="[@ {}]".format(SIG))]
        resp = mock.Mock()
        resp.json.return_value = {"bugs": bugs}
        with mock.patch.object(ba.net, "get", return_value=resp) as g:
            ba._fixed_bugs_about(SIG, "Firefox")
            ba._fixed_bugs_about(SIG, "Firefox")
            self.assertEqual(g.call_count, 2)          # the suppressing gate stays LIVE
            ba._fixed_bugs_about(SIG, "Firefox", use_cache=True)
            ba._fixed_bugs_about(SIG, "Firefox", use_cache=True)
            self.assertEqual(g.call_count, 3)          # ...the high-frequency one does not

    def test_a_lookup_failure_is_not_cached(self):
        with mock.patch.object(ba.net, "get", side_effect=RuntimeError("bmo down")):
            self.assertEqual(ba._fixed_bugs_about(SIG, "Firefox", use_cache=True), [])
        self.assertEqual(ba._FIXED_BUGS_CACHE, {})


class TestTheFilingDoor(unittest.TestCase):
    CFG = {"enabled": True, "min_confidence": 70, "verdicts": ["lead", "culprit"],
           "needinfo": True, "daily_cap": 10, "comment_on_existing": "comment",
           "comment_max_bug_age_days": 30}

    def _autofile(self, dossier, verdict="abstain", confidence=25, fix=None):
        from crashclouseau import report_bug
        with mock.patch.object(ba.config, "get_agent_autofile", return_value=self.CFG), \
             mock.patch.object(ba.config, "autofile_channel_declared", return_value=True), \
             mock.patch.object(ba, "_incomplete_fix_bug", return_value=fix) as det, \
             mock.patch.object(ba.models.Dossier, "already_filed", return_value=None), \
             mock.patch.object(ba.models.Dossier, "filed_bugs_since", return_value=0), \
             mock.patch.object(ba, "_open_bugs_for_signature", return_value=[]), \
             mock.patch.object(ba, "_fixed_after_build_bug", return_value=None), \
             mock.patch.object(report_bug, "build_bug_preview", return_value=None), \
             mock.patch.object(ba.config, "get_bugzilla_token", return_value="tok"):
            res = ba.autofile_bug(
                "u-1", {"uuid": "u-1", "signature": SIG, "channel": "nightly",
                        "product": "Firefox", "buildid": BUILD}, {}, dossier,
                verdict, confidence)
        return res, det

    def test_an_abstain_with_no_signal_is_still_not_filed(self):
        res, det = self._autofile({"corroborations": {}}, fix=None)
        self.assertFalse(res["filed"])
        self.assertIn("not fileable", res["skipped"])
        det.assert_called_once()

    def test_an_abstain_with_the_signal_gets_past_the_verdict_gate(self):
        # It reaches the preview (stubbed empty here), which is every gate PAST the verdict.
        res, _ = self._autofile({"corroborations": {}}, fix={"id": 2063678, "node": "n",
                                                             "pushdate": LANDED})
        self.assertFalse(res["filed"])
        self.assertIn("no candidate regressor", res["skipped"])

    def test_a_fileable_verdict_never_pays_for_the_lookup(self):
        res, det = self._autofile({"corroborations": {}}, verdict="lead", confidence=85)
        det.assert_not_called()
        self.assertIn("no candidate regressor", res["skipped"])

    def test_every_suppression_closes_the_door_before_the_lookup(self):
        for flag in sorted(corroborations.suppressions()):
            res, det = self._autofile({"corroborations": {flag: True}},
                                      fix={"id": 1, "node": "n", "pushdate": LANDED})
            self.assertFalse(res["filed"], flag)
            self.assertIn(flag, res["skipped"], flag)
            det.assert_not_called()

    def test_the_first_seen_comes_from_either_clock(self):
        for key in ("signature_first_seen_ever", "signature_first_seen_windowed"):
            _, det = self._autofile({"corroborations": {key: FIRST_SEEN}})
            self.assertEqual(det.call_args.kwargs["first_seen"], FIRST_SEEN, key)


class TestTheBugItWouldFile(unittest.TestCase):
    FIX = {"id": 2063678, "node": "bbdaf4e3b2c2", "pushdate": LANDED,
           "product": "Core", "component": "Audio/Video: Playback",
           "assigned_to": "tboiko@nvidia.com", "resolved": "2026-08-19T21:27:05Z"}
    UUID_INFO = {"uuid": "84794f8d", "signature": SIG, "channel": "nightly",
                 "product": "Firefox", "buildid": BUILD, "version": "156.0a1"}
    STACK = {"frames": [{"stackpos": 0, "function": "cuEGLApiInit",
                         "filename": "", "line": 0}]}

    def setUp(self):
        rb._USER_CACHE.clear()
        self.addCleanup(rb._USER_CACHE.clear)

    def _preview(self, dossier, fix=None):
        with mock.patch.object(rb, "fetch_signature_stats", return_value=(True, "")), \
             mock.patch.object(rb, "fetch_crash_reason", return_value={}), \
             mock.patch.object(rb, "_bugzilla_user", return_value={
                 "exists": True, "nick": "tboiko", "real": "Tymur Boiko [:tboiko]",
                 "askable": True}), \
             mock.patch.object(rb.models.UUID, "get_info", return_value={"version": "156.0a1"}):
            return rb.build_bug_preview(self.UUID_INFO, self.STACK, dossier, incomplete_fix=fix)

    def test_no_candidate_and_no_signal_still_files_nothing(self):
        self.assertIsNone(self._preview({"verdict": {"decision": "abstain"}}))

    def test_the_signal_alone_is_enough_to_build_a_bug(self):
        p = self._preview({"verdict": {"decision": "abstain"}, "candidate": None}, fix=self.FIX)
        self.assertIsNotNone(p)
        self.assertEqual(p["title"], "Crash in [@ {}]".format(SIG))
        # product/component come from the bug whose fix did not hold: it is the same defect.
        self.assertEqual((p["product"], p["component"]), ("Core", "Audio/Video: Playback"))

    def test_it_asks_the_person_who_wrote_the_fix(self):
        p = self._preview({"verdict": {"decision": "abstain"}}, fix=self.FIX)
        self.assertEqual(p["needinfo_email"], "tboiko@nvidia.com")
        self.assertEqual(p["needinfo"], ":tboiko, can you have a look please?")

    def test_it_claims_no_regression(self):
        # There is no changeset being accused, so the `regression` keyword and `regressed_by`
        # would both be asserting something nobody established.
        p = self._preview({"verdict": {"decision": "abstain"}}, fix=self.FIX)
        self.assertEqual(p["keywords"], ["crash"])
        self.assertEqual(p["regressed_by"], [])

    def test_an_unaskable_assignee_leaves_the_flag_unset(self):
        with mock.patch.object(rb, "_bugzilla_user", return_value={
                "exists": True, "nick": "gone", "real": "", "askable": False}):
            self.assertEqual(rb._person_for_account("gone@x.com"), {})
        self.assertEqual(rb._person_for_account("nobody@mozilla.org"), {})
        self.assertEqual(rb._person_for_account(""), {})

    def test_the_note_states_the_three_checkable_things(self):
        note = rb.build_incomplete_fix_note(self.FIX, "nightly")
        self.assertIn("bug 2063678", note)
        self.assertIn("bbdaf4e3b2c2", note)
        self.assertIn("2026-08-19", note)
        self.assertIn("still crashing", note)
        # ...and invites the correction rather than asserting a cause.
        self.assertIn("duplicate", note)
        for guess in ("regressor", "caused by", "introduced"):
            self.assertNotIn(guess, note)

    def test_the_note_needs_both_the_bug_and_the_landing(self):
        self.assertIsNone(rb.build_incomplete_fix_note(None, "nightly"))
        self.assertIsNone(rb.build_incomplete_fix_note({"id": 1}, "nightly"))
        self.assertIsNone(rb.build_incomplete_fix_note({"node": "n"}, "nightly"))

    def test_the_note_is_in_the_comment_above_the_analysis(self):
        p = self._preview({"verdict": {"decision": "abstain"}}, fix=self.FIX)
        self.assertIn("already fixed once", p["comment"])
        self.assertLess(p["comment"].index("already fixed once"),
                        p["comment"].index("can you have a look"))


class TestTheCandidateTheVerdictRejected(unittest.TestCase):
    """The second reason files on a run the VERDICT could not file on -- and most of those runs
    still carry the changeset the verdict declined to file on: 354 of the 455 rule-1b abstains
    in the 30 days to 2026-08-28 (77.8%) have a candidate node, 61 of them in this build's
    pushlog window. Nothing below CHANGES that behaviour; it pins it, because both halves were
    untested. No test in this file had ever passed a candidate with a node, and replacing the
    strip in ``autofile_bug``'s ``if incomplete_fix:`` block with ``pass`` left the whole suite
    green -- a guard that cannot fail is not a guard.

    The routing is pinned rather than "fixed" on purpose. ``build_bug_preview`` derives the
    needinfo AND the comment's "by X" attribution from the same ``person`` so the ask and the
    credit can never name two different people, so taking product/component from the FIXED bug
    when a changeset IS named would also credit that bug's assignee with a changeset they did
    not write. Measured against it: on the three prod runs where a candidate-bearing abstain
    sits on a signature owned by a FIXED bug, the candidate's bug is in the SAME component as
    the fix's, so the reroute buys nothing and costs an attribution.
    """

    FIX = {"id": 2063678, "node": "bbdaf4e3b2c2", "pushdate": LANDED,
           "product": "Core", "component": "Audio/Video: Playback",
           "assigned_to": "tboiko@nvidia.com", "resolved": "2026-08-19T21:27:05Z",
           "predates_days": 0}
    CAND = {"node": "221f70b5648a", "bug": 2043188, "author": "Hg Name <hg@example.com>"}
    CAND_PERSON = {"nick": "candauthor", "account": "cand@example.com"}
    UUID_INFO = {"uuid": "u-1", "signature": SIG, "channel": "nightly",
                 "product": "Firefox", "buildid": BUILD, "version": "156.0a1"}
    STACK = {"frames": [{"stackpos": 0, "function": "cuEGLApiInit", "filename": "", "line": 0}]}
    CFG = {"enabled": True, "min_confidence": 70, "verdicts": ["lead", "culprit"],
           "needinfo": True, "daily_cap": 10, "comment_on_existing": "comment",
           "comment_max_bug_age_days": 30}

    def _dossier(self, in_window):
        return {"verdict": {"decision": "abstain", "confidence": "low"},
                "candidate": dict(self.CAND),
                "skeptic": [{"claim_ref": "candidate:221f70b5648a", "status": "fail",
                             "note": "confirmed noise, not a defensible lead"}],
                "corroborations": {"candidate_in_pushlog_window": in_window}}

    def _preview(self, in_window=True):
        with mock.patch.object(rb, "fetch_signature_stats", return_value=(True, "")), \
             mock.patch.object(rb, "fetch_crash_reason", return_value={}), \
             mock.patch.object(rb, "resolve_product_component",
                               return_value=("Core", "Networking: HTTP")), \
             mock.patch.object(rb, "_needinfo_person", return_value=dict(self.CAND_PERSON)), \
             mock.patch.object(rb.models.UUID, "get_info", return_value={"version": "156.0a1"}):
            return rb.build_bug_preview(self.UUID_INFO, self.STACK, self._dossier(in_window),
                                        incomplete_fix=self.FIX)

    def _autofile(self, preview, in_window=True):
        from crashclouseau import report_bug
        self.created, self.regressed = {}, None

        def _create(payload, token):
            self.created = payload
            return 2099999, False

        def _regress(bug, bugs, token):
            self.regressed = list(bugs)
            return list(bugs)

        with mock.patch.object(ba.config, "get_agent_autofile", return_value=self.CFG), \
             mock.patch.object(ba.config, "autofile_channel_declared", return_value=True), \
             mock.patch.object(ba, "_incomplete_fix_bug", return_value=self.FIX), \
             mock.patch.object(ba.models.Dossier, "already_filed", return_value=None), \
             mock.patch.object(ba.models.Dossier, "already_commented", return_value=None), \
             mock.patch.object(ba.models.Dossier, "filed_bugs_since", return_value=0), \
             mock.patch.object(ba.models.Dossier, "record_filed_bug"), \
             mock.patch.object(ba, "_open_bugs_for_signature", return_value=[]), \
             mock.patch.object(ba, "_fixed_after_build_bug", return_value=None), \
             mock.patch.object(ba, "_create_bug_keeping_the_bug", side_effect=_create), \
             mock.patch.object(ba, "_link_blockers", return_value=[]), \
             mock.patch.object(ba, "_link_regressed_by", side_effect=_regress), \
             mock.patch.object(report_bug, "build_bug_preview", return_value=preview), \
             mock.patch.object(ba.config, "get_bugzilla_token", return_value="tok"):
            return ba.autofile_bug("u-1", self.UUID_INFO, self.STACK,
                                   self._dossier(in_window), "abstain", 25)

    def test_the_candidate_keeps_the_routing_and_the_ask_names_the_same_person(self):
        p = self._preview()
        self.assertEqual((p["product"], p["component"]), ("Core", "Networking: HTTP"))
        self.assertEqual(p["needinfo_email"], "cand@example.com")
        self.assertIn("221f70b5648a", p["comment"])
        self.assertIn(":candauthor, can you have a look please?", p["comment"])
        # The changeset is credited to the person the ask names, and to nobody else.
        self.assertIn("by :candauthor", p["comment"])
        self.assertNotIn("tboiko", p["comment"])
        # ...and the reason the bug exists is still stated, above the analysis.
        self.assertIn("already fixed once", p["comment"])
        self.assertLess(p["comment"].index("already fixed once"),
                        p["comment"].index("221f70b5648a"))

    def test_the_skeptics_refusal_travels_with_the_changeset_it_refuses(self):
        # The one thing that keeps this comment honest is in the comment: the named changeset
        # and the note saying it was ruled out are in the same text. Suppressing the analysis
        # on this path would delete the second and keep the first.
        p = self._preview()
        self.assertIn("confirmed noise, not a defensible lead", p["comment"])

    def test_the_structured_regression_claim_never_reaches_bugzilla(self):
        p = self._preview(in_window=True)
        # `build_bug_preview` does not know why it is filing, so it asserts both...
        self.assertEqual(p["keywords"], ["crash", "regression"])
        self.assertEqual(p["regressed_by"], [self.CAND["bug"]])
        res = self._autofile(p)
        # ...and the filer takes both back before the create and before the regressed_by PUT.
        self.assertTrue(res["filed"], res)
        self.assertEqual(self.created["keywords"], ["crash"])
        self.assertEqual(self.regressed, [])
        self.assertEqual(res["regressed_by"], [])

    def test_the_filing_row_records_which_reason_filed_it(self):
        res = self._autofile(self._preview(in_window=False))
        self.assertTrue(res["filed"], res)
        self.assertEqual(res["incomplete_fix"],
                         {"bug": 2063678, "node": "bbdaf4e3b2c2",
                          "pushdate": LANDED.isoformat(), "predates_days": 0})

    def test_a_verdict_path_filing_records_no_such_key(self):
        # The key IS the discriminator, so it must be absent when the verdict filed the bug.
        from crashclouseau import report_bug
        with mock.patch.object(ba.config, "get_agent_autofile", return_value=self.CFG), \
             mock.patch.object(ba.config, "autofile_channel_declared", return_value=True), \
             mock.patch.object(ba, "_incomplete_fix_bug", return_value=self.FIX) as det, \
             mock.patch.object(ba.models.Dossier, "already_filed", return_value=None), \
             mock.patch.object(ba.models.Dossier, "already_commented", return_value=None), \
             mock.patch.object(ba.models.Dossier, "filed_bugs_since", return_value=0), \
             mock.patch.object(ba.models.Dossier, "record_filed_bug"), \
             mock.patch.object(ba, "_open_bugs_for_signature", return_value=[]), \
             mock.patch.object(ba, "_fixed_after_build_bug", return_value=None), \
             mock.patch.object(ba, "_create_bug_keeping_the_bug",
                               return_value=(2099998, False)), \
             mock.patch.object(ba, "_link_blockers", return_value=[]), \
             mock.patch.object(ba, "_link_regressed_by", side_effect=lambda b, x, t: list(x)), \
             mock.patch.object(report_bug, "build_bug_preview",
                               return_value=self._preview(in_window=True)), \
             mock.patch.object(ba.config, "get_bugzilla_token", return_value="tok"):
            res = ba.autofile_bug("u-2", self.UUID_INFO, self.STACK,
                                  self._dossier(True), "lead", 70)
        det.assert_not_called()
        self.assertTrue(res["filed"], res)
        self.assertNotIn("incomplete_fix", res)
        # ...and the strip is scoped to the other reason: this bug keeps its regression claim.
        self.assertEqual(res["regressed_by"], [self.CAND["bug"]])


if __name__ == "__main__":
    unittest.main()
