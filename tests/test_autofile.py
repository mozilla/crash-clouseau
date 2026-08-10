# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Automatic Bugzilla filing — the ONE unattended write in the product.
#   DATABASE_URL=sqlite:// python -m unittest tests.test_autofile
import os

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import inspect  # noqa: E402
import requests  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import bugzilla_apply, report_bug  # noqa: E402
from crashclouseau import config as cconfig  # noqa: E402


_PREVIEW = {
    "title": "Crash in [@ Foo::Bar]",
    "comment": "the whole bug opener",
    "product": "Core", "component": "DOM: Core & HTML",
    "version": "Trunk", "type": "defect",
    "keywords": ["crash", "regression"],
    "cf_crash_signature": "[@ Foo::Bar]",
    "blocked": ["clouseau", 42],
    "needinfo": ":dev, can you have a look please?",
    "needinfo_email": "dev@moz.example",
}
_INFO = {"uuid": "u-1", "signature": "Foo::Bar", "channel": "nightly"}


def _cfg(**over):
    base = {"enabled": True, "min_confidence": 70, "verdicts": ["lead", "culprit"],
            "needinfo": True, "daily_cap": 10, "comment_on_existing": True,
            "comment_max_bug_age_days": 30}
    base.update(over)
    return base


def _bug(bid, created=None):
    """One row of what `_open_bugs_for_signature` returns."""
    return {"id": bid, "creation_time": created}


class _Base(unittest.TestCase):
    def setUp(self):
        self.created = []
        self.comments = []
        self.puts = []
        self.filed = []
        p = [
            mock.patch.object(bugzilla_apply.config, "get_agent_autofile",
                              return_value=_cfg()),
            mock.patch.object(bugzilla_apply.config, "get_bugzilla_token",
                              return_value="tok"),
            mock.patch.object(bugzilla_apply.models.Dossier, "already_filed",
                              return_value=None),
            mock.patch.object(bugzilla_apply.models.Dossier, "filed_bugs_since",
                              return_value=0),
            mock.patch.object(bugzilla_apply.models.Dossier, "record_filed_bug",
                              side_effect=lambda u, i: self.filed.append((u, i)) or True),
            mock.patch.object(bugzilla_apply, "_open_bugs_for_signature", return_value=[]),
            # Unresolvable by default (it would otherwise ask hg over the network). That is
            # also the honest default for these tests: with no landing date the filer keeps
            # its pre-existing "comment on the oldest open bug" behaviour, so every test below
            # that does not care about timing exercises exactly what it used to.
            mock.patch.object(bugzilla_apply, "_candidate_landed", return_value=None),
            # Never reopened, unless a test says otherwise (it is a Bugzilla request).
            mock.patch.object(bugzilla_apply, "_last_reopened", return_value=None),
            mock.patch.object(bugzilla_apply, "_create_bug",
                              side_effect=lambda p, t: self.created.append(p) or 999),
            mock.patch.object(bugzilla_apply, "_post_comment",
                              side_effect=lambda b, t, pv, tok: self.comments.append((b, t)) or 7),
            mock.patch.object(bugzilla_apply, "_put_bug",
                              side_effect=lambda b, c, t: self.puts.append((b, c)) or b),
        ]
        for x in p:
            x.start()
            self.addCleanup(x.stop)
        pv = mock.patch("crashclouseau.report_bug.build_bug_preview", return_value=_PREVIEW)
        pv.start()
        self.addCleanup(pv.stop)

    def _file(self, verdict="lead", confidence=70, **cfg_over):
        if cfg_over:
            bugzilla_apply.config.get_agent_autofile.return_value = _cfg(**cfg_over)
        return bugzilla_apply.autofile_bug("u-1", _INFO, {}, {"candidate": {"node": "n"}},
                                           verdict, confidence)


class TestGates(_Base):
    def test_files_a_rung70_lead(self):
        res = self._file()
        self.assertTrue(res["filed"])
        self.assertEqual((res["bug"], res["mode"]), (999, "new_bug"))
        self.assertEqual(len(self.created), 1)

    def test_disabled_is_the_kill_switch(self):
        res = self._file(enabled=False)
        self.assertFalse(res["filed"])
        self.assertEqual(self.created, [])

    def test_below_the_rung_is_not_filed(self):
        # The whole point of choosing rung 70: the medium rung (50) is ~2.5x the volume
        # and the population the blind reviewer refutes most often.
        for conf in (None, 25, 50, 69):
            with self.subTest(conf=conf):
                self.assertFalse(self._file(confidence=conf)["filed"])
        self.assertEqual(self.created, [])

    def test_abstain_is_never_filed(self):
        self.assertFalse(self._file(verdict="abstain", confidence=90)["filed"])
        self.assertEqual(self.created, [])

    def test_never_files_twice_for_one_crash(self):
        # The orphan reaper re-runs a crashed run; without this it would re-file.
        bugzilla_apply.models.Dossier.already_filed.return_value = {"bug": 5}
        res = self._file()
        self.assertFalse(res["filed"])
        self.assertEqual(res["prior"], {"bug": 5})
        self.assertEqual(self.created, [])

    def test_daily_cap_stops_a_runaway(self):
        bugzilla_apply.models.Dossier.filed_bugs_since.return_value = 10
        self.assertFalse(self._file()["filed"])
        self.assertEqual(self.created, [])

    def test_no_token_does_not_file(self):
        bugzilla_apply.config.get_bugzilla_token.return_value = ""
        self.assertFalse(self._file()["filed"])
        self.assertEqual(self.created, [])

    def test_unresolved_component_does_not_file(self):
        # `resolve_product_component` is best-effort and empties on a Bugzilla read
        # failure. Filing then gets rejected outright ("Bad argument param sent to
        # Bugzilla::Product::new"), but the reason to check is that a HALF-resolved pair
        # would land the bug on a team with no idea why they got it.
        for pc in ({"product": "", "component": "DOM"}, {"product": "Core", "component": ""}):
            with self.subTest(**pc):
                with mock.patch("crashclouseau.report_bug.build_bug_preview",
                                return_value={**_PREVIEW, **pc}):
                    res = self._file()
                self.assertFalse(res["filed"])
                self.assertIn("wrong component", res["skipped"])
        self.assertEqual(self.created, [])

    def test_offstack_observe_only_is_not_filed(self):
        # `_apply_offstack_observe_only` empties `result.actions` to "SUPPRESS any outward
        # action" while the off-stack canary's calibration is watched. This filer builds its
        # own payload and never reads `result.actions`, so it walked straight through that
        # suppression — 14 of 66 rung-70 verdicts in 30 days carry the flag.
        res = bugzilla_apply.autofile_bug(
            "u-1", _INFO, {}, {"candidate": {"node": "n"},
                               "corroborations": {"offstack_observe_only": True}},
            "lead", 70)
        self.assertFalse(res["filed"])
        self.assertIn("observe-only", res["skipped"])
        self.assertEqual(self.created, [])

    def test_an_unsymbolicated_signature_is_not_filed(self):
        # "Crash in [@ @0xe2ba40f948]" matches nothing and dedupes against nothing — the
        # address differs per crash — and if no frame resolves to code there is nothing
        # tying the crash to the candidate.
        info = {**_INFO, "signature": "@0xe2ba40f948"}
        res = bugzilla_apply.autofile_bug("u-1", info, {}, {"candidate": {"node": "n"}},
                                          "lead", 70)
        self.assertFalse(res["filed"])
        self.assertIn("unsymbolicated", res["skipped"])
        self.assertEqual(self.created, [])

    def test_a_partly_symbolicated_signature_still_files(self):
        # The guard must require EVERY component to be a bare address: this real signature
        # carries "unknown" and a raw frame yet is perfectly actionable.
        info = {**_INFO,
                "signature": "OOM | unknown | memcpy_repmovs_Intel | RTCEncodedFrameBase"}
        self.assertTrue(self._file_with(info)["filed"])

    def _file_with(self, info):
        return bugzilla_apply.autofile_bug("u-1", info, {}, {"candidate": {"node": "n"}},
                                           "lead", 70)

    def test_unsymbolicated_predicate(self):
        for sig in ("@0xe2ba40f948", "0xdeadbeef", "@0x0 | @0x1"):
            self.assertTrue(bugzilla_apply._is_unsymbolicated(sig), sig)
        for sig in ("@0x0 | js::gc::TraceEdgeInternal", "mozilla::Foo::Bar", "", None):
            self.assertFalse(bugzilla_apply._is_unsymbolicated(sig), sig)

    def test_no_candidate_does_not_file(self):
        with mock.patch("crashclouseau.report_bug.build_bug_preview", return_value=None):
            self.assertFalse(self._file()["filed"])
        self.assertEqual(self.created, [])


class TestDuplicates(_Base):
    def test_existing_open_bug_gets_a_comment_not_a_duplicate(self):
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(12345)]
        res = self._file()
        self.assertTrue(res["filed"])
        self.assertEqual((res["bug"], res["mode"]), (12345, "comment_on_existing"))
        self.assertEqual(self.created, [])
        self.assertEqual(self.comments, [(12345, "the whole bug opener")])
        self.assertEqual(self.puts[0][0], 12345)

    def test_the_oldest_open_bug_is_preferred(self):
        # With several open bugs for one signature the earliest is the canonical one.
        # Newest-first would prefer a recent duplicate — possibly one we filed ourselves.
        bugzilla_apply._open_bugs_for_signature.return_value = [
            _bug(1990812), _bug(2060922)]
        res = self._file()
        self.assertEqual(res["bug"], 1990812)

    def test_a_failed_lookup_skips_rather_than_risking_a_duplicate(self):
        # `_open_bugs_for_signature` returns None on a network failure. A missed filing is
        # recoverable; a duplicate on BMO is not.
        bugzilla_apply._open_bugs_for_signature.return_value = None
        res = self._file()
        self.assertFalse(res["filed"])
        self.assertIn("not risking a duplicate", res["skipped"])
        self.assertEqual(self.created, [])
        self.assertEqual(self.comments, [])

    def test_comment_on_existing_off_skips_it_does_not_file_anyway(self):
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(12345)]
        res = self._file(comment_on_existing=False)
        self.assertFalse(res["filed"])
        self.assertEqual(self.created, [])
        self.assertEqual(self.comments, [])

    def test_comment_on_existing_off_still_skips_a_bug_that_predates_the_cause(self):
        # The kill-switch keeps its plain meaning — "an open bug exists, do not write" — even
        # once the age rule would have filed a new bug. Someone who turns it off is asking for
        # no writes, not for a different kind of write.
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(1798397, _OLD)]
        bugzilla_apply._candidate_landed.return_value = _LANDED
        res = self._file(comment_on_existing=False)
        self.assertFalse(res["filed"])
        self.assertEqual((self.created, self.comments), ([], []))


# Bug 1798397 (`Crash in [@ nsAtom::IsStatic]`, open since 2022, its own comments proposing
# nsAtom for the irrelevant-signature list) versus the changeset named for crash
# ddeac1a4-64d1-4413-b03b-f79540260809, which landed 1375 days later. The numbers below are
# that crash and the one filing this rule must NOT change (bug 2060924, which we had filed
# ourselves 9 days after its own regressor landed).
_OLD = "2022-10-31T19:44:56Z"
_LANDED = datetime(2026, 8, 7, tzinfo=timezone.utc)


class TestBugPredatesTheCause(_Base):
    def test_an_open_bug_older_than_the_regressor_gets_a_new_bug(self):
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(1798397, _OLD)]
        bugzilla_apply._candidate_landed.return_value = _LANDED
        res = self._file()
        self.assertTrue(res["filed"])
        self.assertEqual(res["mode"], "new_bug")
        self.assertEqual(res["predating_bugs"], [1798397])
        self.assertEqual(self.comments, [])
        self.assertEqual(len(self.created), 1)

    def test_the_new_bug_says_why_it_is_not_a_comment(self):
        # An unexplained second bug on a live signature reads as a broken deduplicator; the
        # triager who might duplicate it needs the reasoning in front of them.
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(1798397, _OLD)]
        bugzilla_apply._candidate_landed.return_value = _LANDED
        with mock.patch("crashclouseau.report_bug.build_bug_preview",
                        side_effect=lambda *a, **kw: dict(_PREVIEW, related=kw["related_bugs"])
                        ) as preview:
            self._file()
        self.assertEqual(preview.call_args.kwargs["related_bugs"], [1798397])

    def test_a_bug_filed_after_the_regressor_still_gets_the_comment(self):
        # The real counter-case: bug 2060924 was filed 2026-08-05 for a changeset that landed
        # 2026-07-27, and commenting there was right. A rule that flipped this one too would
        # be filing a duplicate every time a crash recurs.
        bugzilla_apply._open_bugs_for_signature.return_value = [
            _bug(2060924, "2026-08-05T16:09:09Z")]
        bugzilla_apply._candidate_landed.return_value = datetime(
            2026, 7, 27, 8, 17, 24, tzinfo=timezone.utc)
        res = self._file()
        self.assertEqual((res["bug"], res["mode"]), (2060924, "comment_on_existing"))
        self.assertEqual(self.created, [])

    def test_a_bug_filed_just_before_the_regressor_is_within_the_slack(self):
        # 30 days of slack: a bug filed around the time the regressor landed is plausibly
        # about it, and a hair-trigger here trades a buried report for a duplicate.
        bugzilla_apply._open_bugs_for_signature.return_value = [
            _bug(2060924, "2026-07-20T00:00:00Z")]
        bugzilla_apply._candidate_landed.return_value = _LANDED     # 18 days later
        self.assertEqual(self._file()["mode"], "comment_on_existing")

    def test_a_usable_recent_bug_beats_an_ancient_one_and_no_third_bug_is_filed(self):
        # Oldest-first is only a tie-break among bugs that could be about this crash. With a
        # 2022 bug and one we filed last week both open, the answer is last week's.
        bugzilla_apply._open_bugs_for_signature.return_value = [
            _bug(1798397, _OLD), _bug(2061999, "2026-08-08T00:00:00Z")]
        bugzilla_apply._candidate_landed.return_value = _LANDED
        res = self._file()
        self.assertEqual((res["bug"], res["mode"]), (2061999, "comment_on_existing"))
        self.assertNotIn("predating_bugs", res)
        self.assertEqual(self.created, [])

    def test_an_unresolved_landing_date_changes_nothing(self):
        # hg unreachable, or a candidate with no node. Unknown timing must leave the filer on
        # its previous behaviour rather than invent a duplicate.
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(1798397, _OLD)]
        bugzilla_apply._candidate_landed.return_value = None
        self.assertEqual(self._file()["mode"], "comment_on_existing")

    def test_an_unusable_creation_time_changes_nothing(self):
        # BMO did not return the field, or returned something unparseable. Same rule: fail
        # toward commenting.
        for created in (None, "", "not a date"):
            with self.subTest(created=created):
                self.setUp()
                bugzilla_apply._open_bugs_for_signature.return_value = [_bug(1798397, created)]
                bugzilla_apply._candidate_landed.return_value = _LANDED
                self.assertEqual(self._file()["mode"], "comment_on_existing")

    def test_the_regressors_own_bug_is_the_venue_whatever_the_dates_say(self):
        # Crash b66819b5: the candidate `e6335c6fffd3` is literally "Bug 1990812 - handle the
        # case where switching the decoder state machine fails due to shutdown", and 1990812
        # was open for this signature. On creation time alone (filed 2025-09-25, candidate
        # landed 2025-10-26) it missed the window by ONE day and we would have filed the
        # near-duplicate that `_open_bugs_for_signature` was fixed to prevent.
        bugzilla_apply._open_bugs_for_signature.return_value = [
            _bug(1990812, "2025-09-25T13:46:32Z")]
        bugzilla_apply._candidate_landed.return_value = datetime(
            2025, 10, 26, 20, 42, 57, tzinfo=timezone.utc)
        res = bugzilla_apply.autofile_bug(
            "u-1", _INFO, {}, {"candidate": {"node": "e6335c6fffd3", "bug": 1990812}},
            "lead", 70)
        self.assertEqual((res["bug"], res["mode"]), (1990812, "comment_on_existing"))
        self.assertEqual(self.created, [])

    def test_a_reopen_after_the_cause_landed_rescues_the_bug(self):
        # Same bug, the other way round: 1990812 was reopened 2025-11-14 because the crash came
        # back. A bug reopened AFTER the candidate landed is live for a crash the candidate
        # could have caused, whatever year it was originally filed in.
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(1798397, _OLD)]
        bugzilla_apply._candidate_landed.return_value = _LANDED
        bugzilla_apply._last_reopened.return_value = datetime(
            2026, 8, 1, tzinfo=timezone.utc)
        res = self._file()
        self.assertEqual((res["bug"], res["mode"]), (1798397, "comment_on_existing"))
        self.assertEqual(self.created, [])

    def test_a_reopen_that_also_predates_the_cause_rescues_nothing(self):
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(1798397, _OLD)]
        bugzilla_apply._candidate_landed.return_value = _LANDED
        bugzilla_apply._last_reopened.return_value = datetime(
            2024, 5, 22, tzinfo=timezone.utc)
        self.assertEqual(self._file()["mode"], "new_bug")

    def test_an_unreachable_history_leaves_the_age_verdict_standing(self):
        # The reopen check is a RESCUE, not a gate: it can only ever save a bug the age test
        # already rejected, so a BMO blip must not resurrect the burying behaviour.
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(1798397, _OLD)]
        bugzilla_apply._candidate_landed.return_value = _LANDED
        bugzilla_apply._last_reopened.return_value = None
        self.assertEqual(self._file()["mode"], "new_bug")

    def test_every_open_bug_predating_the_cause_is_recorded(self):
        bugzilla_apply._open_bugs_for_signature.return_value = [
            _bug(1798397, _OLD), _bug(1900000, "2023-01-01T00:00:00Z")]
        bugzilla_apply._candidate_landed.return_value = _LANDED
        self.assertEqual(self._file()["predating_bugs"], [1798397, 1900000])


class TestBugForThisRegression(unittest.TestCase):
    """The predicate on its own."""

    def setUp(self):
        p = mock.patch.object(bugzilla_apply, "_last_reopened", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def test_the_regressors_own_bug_wins_even_when_a_plausible_older_one_exists(self):
        # Oldest-first would otherwise hand the crash to 1500000 and never reach the bug the
        # changeset was actually written for.
        got = bugzilla_apply._bug_for_this_regression(
            [_bug(1500000, "2026-08-01T00:00:00Z"), _bug(1990812, "2022-01-01T00:00:00Z")],
            _LANDED, 30, candidate_bug=1990812)
        self.assertEqual(got, (1990812, []))

    def test_a_regressor_bug_that_is_not_open_for_this_signature_decides_nothing(self):
        got = bugzilla_apply._bug_for_this_regression(
            [_bug(1798397, _OLD)], _LANDED, 30, candidate_bug=2043000)
        self.assertEqual(got, (None, [1798397]))

    def test_no_open_bugs_means_no_venue_and_nothing_skipped(self):
        self.assertEqual(
            bugzilla_apply._bug_for_this_regression([], _LANDED, 30), (None, []))
        self.assertEqual(
            bugzilla_apply._bug_for_this_regression(None, _LANDED, 30), (None, []))

    def test_the_boundary_is_inclusive(self):
        created = "2026-07-08T00:00:00Z"                      # exactly 30 days before
        self.assertEqual(
            bugzilla_apply._bug_for_this_regression([_bug(1, created)], _LANDED, 30)[0], 1)
        self.assertIsNone(
            bugzilla_apply._bug_for_this_regression([_bug(1, created)], _LANDED, 29)[0])

    def test_a_naive_creation_time_is_read_as_utc(self):
        # BMO returns "2022-10-31T19:44:56Z", but nothing guarantees the Z survives every
        # round-trip. A tz-naive value must not blow up on the subtraction.
        got = bugzilla_apply._bug_for_this_regression(
            [_bug(1, "2022-10-31T19:44:56")], _LANDED, 30)
        self.assertEqual(got, (None, [1]))


class TestPayload(_Base):
    def test_needinfo_flag_is_on_the_created_bug(self):
        self._file()
        self.assertEqual(self.created[0]["flags"],
                         [{"name": "needinfo", "status": "?", "requestee": "dev@moz.example"}])

    def test_needinfo_can_be_turned_off_without_stopping_filing(self):
        res = self._file(needinfo=False)
        self.assertTrue(res["filed"])
        self.assertNotIn("flags", self.created[0])
        self.assertIsNone(res["needinfo"])

    def test_regressed_by_is_never_set(self):
        # The field that would assert causation as structured data. The suspected regressor
        # is named in the comment prose instead, where a human can weigh it.
        self._file()
        self.assertNotIn("regressed_by", self.created[0])


class TestBlockerLinking(_Base):
    """BMO's create endpoint accepts `blocks`/`blocked` and silently DISCARDS both —
    allizom filings 1852344/1852345/1852346 all came back 200 with blocks=[]. Only a
    follow-up PUT works, and that PUT is atomic: one unknown id rejects the whole list."""

    def test_blockers_are_never_sent_on_create(self):
        self._file()
        for key in ("blocked", "blocks"):
            self.assertNotIn(key, self.created[0])

    def test_blockers_are_linked_by_a_follow_up_put(self):
        res = self._file()
        self.assertEqual(self.puts, [(999, {"blocks": {"add": ["clouseau", 42]}})])
        self.assertEqual(res["blocks"], ["clouseau", 42])

    def test_an_unknown_regressor_bug_does_not_cost_the_meta_link(self):
        # The PUT rejects the WHOLE list on one bad id (code 101), so a restricted or wrong
        # regressor bug would otherwise also drop the `clouseau` link.
        calls = []

        def put(bug, changes, token):
            calls.append(changes)
            if 42 in changes["blocks"]["add"]:
                raise RuntimeError("Bug 42 does not exist.")
            return bug

        bugzilla_apply._put_bug.side_effect = put
        res = self._file()
        self.assertTrue(res["filed"])
        self.assertEqual(res["blocks"], ["clouseau"])
        self.assertEqual([c["blocks"]["add"] for c in calls], [["clouseau", 42], ["clouseau"]])

    def test_a_failed_link_never_unfiles_the_bug(self):
        bugzilla_apply._put_bug.side_effect = RuntimeError("bugzilla 500")
        res = self._file()
        self.assertTrue(res["filed"])
        self.assertEqual(res["bug"], 999)
        self.assertEqual(res["blocks"], [])

    def test_what_failed_to_link_is_recorded(self):
        # A restricted regressor bug (BMO answers 102) silently cost 2 of the first 3 real
        # filings their regressor link. The gap has to be auditable.
        def put(bug, changes, token):
            if 42 in changes["blocks"]["add"]:
                raise RuntimeError("Bug 42 does not exist.")
            return bug
        bugzilla_apply._put_bug.side_effect = put
        res = self._file()
        self.assertEqual(res["blocks"], ["clouseau"])
        self.assertEqual(res["blocks_unlinked"], [42])

    def test_nothing_unlinked_records_nothing(self):
        res = self._file()
        self.assertNotIn("blocks_unlinked", res)


class TestSignatureMatching(_Base):
    def test_specific_signatures_also_search_summaries(self):
        for sig in ("mozilla::MediaDecoder::SetCDMProxy",
                    "OOM | unknown | memcpy_repmovs_Intel | RTCEncodedFrameBase"):
            self.assertTrue(bugzilla_apply._is_specific_signature(sig), sig)

    def test_short_or_bare_tokens_do_not(self):
        # Searching summaries for "memcpy" returns 32 open bugs; commenting on the wrong
        # one is worse than filing a duplicate.
        for sig in ("memcpy", "OOM", "", None, "shortish"):
            self.assertFalse(bugzilla_apply._is_specific_signature(sig), repr(sig))

    def test_payload_carries_what_bmo_requires(self):
        self._file()
        p = self.created[0]
        self.assertEqual(p["summary"], "Crash in [@ Foo::Bar]")
        self.assertEqual(p["description"], "the whole bug opener")
        for required in ("product", "component", "version", "type"):
            self.assertTrue(p.get(required), "BMO rejects a create without " + required)
        # `title`/`comment`/`needinfo*` are preview-only keys and must not be posted.
        for leaked in ("title", "comment", "needinfo", "needinfo_email"):
            self.assertNotIn(leaked, p)

    def test_the_outcome_is_recorded_for_audit_and_idempotence(self):
        self._file()
        uuid, info = self.filed[0]
        self.assertEqual(uuid, "u-1")
        self.assertEqual((info["bug"], info["mode"], info["filed"]), (999, "new_bug", True))
        self.assertTrue(info["at"])


class TestFailuresAreContained(_Base):
    def test_a_bugzilla_error_never_raises(self):
        bugzilla_apply._create_bug.side_effect = RuntimeError("bugzilla 500")
        res = self._file()
        self.assertFalse(res["filed"])
        self.assertIn("bugzilla write failed", res["skipped"])
        self.assertEqual(self.filed, [])          # nothing recorded, so a retry may re-file

    def test_a_preview_error_never_raises(self):
        with mock.patch("crashclouseau.report_bug.build_bug_preview",
                        side_effect=ValueError("boom")):
            res = self._file()
        self.assertFalse(res["filed"])
        self.assertIn("preview failed", res["skipped"])


class TestTheNeedinfoNeverCostsTheBug(_Base):
    """BMO validates the needinfo requestee while CREATING the bug and rejects the WHOLE
    post if it cannot resolve them. Crash f6fe186b got no bug at all because its hg author
    `farre@mozilla.com` is not an account (`code 51`). `report_bug` now resolves a verified
    account, so these paths should not fire — but the filing must survive them anyway, and
    the failure is silent otherwise: it reads as `skipped: bugzilla write failed`."""

    def test_a_rejected_needinfo_is_dropped_and_the_bug_still_files(self):
        calls = []

        def create(payload, token):
            calls.append(payload)
            if payload.get("flags"):
                raise bugzilla_apply.BugzillaRejected(
                    "bugzilla create failed (404): code 51, no user named X", status=404)
            return 999

        bugzilla_apply._create_bug.side_effect = create
        res = self._file()
        self.assertTrue(res["filed"])
        self.assertEqual(res["bug"], 999)
        self.assertIsNone(res["needinfo"])                    # not claimed
        self.assertEqual(res["needinfo_dropped"], "dev@moz.example")
        self.assertEqual(len(calls), 2)                       # with flags, then without
        self.assertNotIn("flags", calls[1])
        # everything else about the bug is unchanged by the retry
        self.assertEqual(calls[1]["summary"], calls[0]["summary"])
        self.assertEqual(self.filed[0][1]["bug"], 999)        # recorded, so no re-file

    def test_a_create_that_fails_for_its_own_reasons_is_not_masked(self):
        # Refused twice: the retry proves the flags were not the problem, so the ORIGINAL
        # rejection must surface (not the retry's), and nothing may be recorded as filed.
        calls = []

        def create(payload, token):
            calls.append(payload)
            raise bugzilla_apply.BugzillaRejected(
                "component is invalid" if payload.get("flags") else "second, less useful",
                status=400)

        bugzilla_apply._create_bug.side_effect = create
        res = self._file()
        self.assertFalse(res["filed"])
        self.assertEqual(len(calls), 2)
        self.assertIn("component is invalid", res["skipped"])
        self.assertNotIn("less useful", res["skipped"])
        self.assertEqual(self.filed, [])

    def test_a_server_error_is_never_retried(self):
        """A 5xx is not a verdict on the payload. Retrying without the flag during a BMO
        deploy would throw away a perfectly good needinfo — and, since a 5xx may have
        half-run, could file the bug twice. Only a 4xx says "nothing was created, and what
        you sent is why"."""
        calls = []

        def create(payload, token):
            calls.append(payload)
            raise bugzilla_apply.BugzillaRejected("bugzilla create failed (503): "
                                                  "<html>gateway</html>", status=503)

        bugzilla_apply._create_bug.side_effect = create
        res = self._file()
        self.assertFalse(res["filed"])
        self.assertEqual(len(calls), 1)                 # posted once, never twice
        self.assertNotIn("needinfo_dropped", res)
        self.assertEqual(self.filed, [])

    def test_a_systemic_second_rejection_is_logged_not_swallowed(self):
        # The first rejection is the one returned (it names what BMO objected to), but if
        # the flag-less retry fails for a DIFFERENT reason, that one is systemic — it would
        # block every filing — and it must not vanish.
        def create(payload, token):
            raise bugzilla_apply.BugzillaRejected(
                "code 51, no user named X" if payload.get("flags")
                else "code 50, you must select a version", status=400)

        bugzilla_apply._create_bug.side_effect = create
        with self.assertLogs(level="ERROR") as logs:
            res = self._file()
        self.assertIn("code 51", res["skipped"])                     # returned: the first
        self.assertTrue(any("must select a version" in m for m in logs.output))  # logged

    def test_a_timeout_is_never_retried(self):
        """The retry exists for a REFUSAL, where BMO told us it did not create the bug. A
        timeout says nothing about whether the POST landed, and re-posting it would file the
        same crash twice — the one outcome ``already_filed``/``record_filed_bug`` exist to
        prevent. Hence ``BugzillaRejected`` is a distinct type and not a message match."""
        calls = []

        def create(payload, token):
            calls.append(payload)
            raise requests.exceptions.ReadTimeout("timed out waiting for BMO")

        bugzilla_apply._create_bug.side_effect = create
        res = self._file()
        self.assertFalse(res["filed"])
        self.assertEqual(len(calls), 1)                 # posted once, never twice
        self.assertIn("timed out", res["skipped"])
        self.assertEqual(self.filed, [])

    def test_a_create_without_flags_is_never_retried(self):
        # Must be a 4xx BugzillaRejected, i.e. a failure that WOULD be retried if flags were
        # present. A plain RuntimeError never enters the retry block at all, so it would
        # pass this test without the flags guard existing.
        calls = []

        def create(payload, token):
            calls.append(payload)
            raise bugzilla_apply.BugzillaRejected("boom", status=400)

        bugzilla_apply._create_bug.side_effect = create
        self.assertFalse(self._file(needinfo=False)["filed"])
        self.assertEqual(len(calls), 1)

    def test_a_failed_needinfo_never_loses_an_already_posted_comment(self):
        # The comment-on-existing path posts FIRST. If the needinfo PUT then raised, the
        # filing went unrecorded and the next run commented on the same bug a second time.
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(4242)]
        bugzilla_apply._put_bug.side_effect = RuntimeError("code 51, no user named X")
        res = self._file()
        self.assertTrue(res["filed"])
        self.assertEqual((res["bug"], res["mode"]), (4242, "comment_on_existing"))
        self.assertIsNone(res["needinfo"])
        self.assertEqual(res["needinfo_failed"], "dev@moz.example")
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(len(self.filed), 1)                  # recorded => no second comment


class TestTokenResolution(unittest.TestCase):
    """Where the write token comes from.

    The reason this class exists: ``AUTOFILE_BUGS`` was armed on 08-05 with the key in
    ``LIBMOZDATA_CFG_BUGZILLA_TOKEN``, and every crash for the next day skipped with "no
    Bugzilla API token configured" — 13 rung-70 verdicts, 0 filings. libmozdata installs
    ``ConfigIni`` as its global provider and never calls ``set_config``, so the
    ``LIBMOZDATA_CFG_*`` variables its ``ConfigEnv`` class documents are read by nobody.
    Nothing about that failure was visible from either end: the variable was set, and
    ``libmozdata.config.get`` returned "" without complaint."""

    def _resolve(self, env, ini=""):
        with mock.patch.dict(os.environ, env, clear=False):
            for k in ("BUGZILLA_TOKEN", "LIBMOZDATA_CFG_BUGZILLA_TOKEN"):
                if k not in env:
                    os.environ.pop(k, None)
            with mock.patch.object(cconfig.libmozdata.config, "get", return_value=ini):
                return cconfig.get_bugzilla_token()

    def test_the_libmozdata_env_var_is_honoured(self):
        # The name libmozdata WOULD read if it read the environment at all.
        self.assertEqual(
            self._resolve({"LIBMOZDATA_CFG_BUGZILLA_TOKEN": "from-lmd-env"}),
            "from-lmd-env")

    def test_our_own_env_var_is_honoured(self):
        # Mirrors SOCORRO_TOKEN, the convention crashclouseau/__init__.py already uses.
        self.assertEqual(self._resolve({"BUGZILLA_TOKEN": "from-env"}), "from-env")

    def test_our_own_env_var_wins(self):
        self.assertEqual(
            self._resolve({"BUGZILLA_TOKEN": "ours",
                           "LIBMOZDATA_CFG_BUGZILLA_TOKEN": "theirs"}), "ours")

    def test_the_ini_still_works(self):
        # Unset environment -> ~/.mozdata.ini, which is how this runs locally.
        self.assertEqual(self._resolve({}, ini="from-ini"), "from-ini")

    def test_nothing_configured_is_empty_not_none(self):
        # `autofile_bug` tests `if not token`, and apply puts it in a header; None there
        # would be a TypeError deep in requests instead of a clean skip.
        self.assertEqual(self._resolve({}, ini=None), "")

    def test_the_writers_go_through_the_resolver(self):
        # The bug was one call site reading the token a way that could not work. Assert
        # no writer has drifted back to libmozdata's config directly.
        for mod in (bugzilla_apply, report_bug):
            src = inspect.getsource(mod)
            self.assertNotIn('"Bugzilla", "token"', src, mod.__name__)


if __name__ == "__main__":
    unittest.main()
