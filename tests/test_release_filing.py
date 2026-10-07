# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""Release and ESR filing tests for title prefixes and tracking nominations.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        uv run python -m unittest tests.test_release_filing

The nomination normally rides the create. After a client rejection, the filer retries the
create without train fields and attempts each field separately.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import bugzilla_apply, report_bug, spikes  # noqa: E402
from tests.test_autofile import _INFO, _OLD, _PREVIEW, _Base, _bug, _cfg  # noqa: E402

_RELEASE_INFO = {**_INFO, "channel": "release", "version": "155.0.1",
                 "buildid": "20260903215306"}
_RELEASE_PREVIEW = {**_PREVIEW, "title": "[new in release] Crash in [@ Foo::Bar]",
                    "tracking_flag": "cf_tracking_firefox155"}
_RELEASE_POLICY = {"enabled": True, "comment_on_existing": "skip", "daily_cap": 2,
                   "summary_prefix": "[new in release]", "nominate_tracking": True}


class TestAReleaseFilingIsTitledAndNominated(_Base):

    def _file_release(self, preview=None, **cfg_over):
        cfg = dict(_RELEASE_POLICY)
        cfg.update(cfg_over)
        bugzilla_apply.config.get_agent_autofile.return_value = _cfg(**cfg)
        report_bug.build_bug_preview.return_value = preview or _RELEASE_PREVIEW
        return bugzilla_apply.autofile_bug(
            "u-1", _RELEASE_INFO, {}, {"candidate": {"node": "n"}}, "lead", 70)

    def test_the_prefixed_title_reaches_the_posted_summary(self):
        res = self._file_release()
        self.assertTrue(res["filed"])
        self.assertEqual(res["mode"], "new_bug")
        self.assertEqual(self.created[0]["summary"], "[new in release] Crash in [@ Foo::Bar]")
        # The signature FIELD is untouched by the prefix: it is what dedupes the bug.
        self.assertEqual(self.created[0]["cf_crash_signature"], "[@ Foo::Bar]")

    def test_the_version_is_nominated_for_tracking_in_the_create(self):
        res = self._file_release()
        # The nomination uses its BMO field name in the create body.
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.created[0]["cf_tracking_firefox155"], "?")
        self.assertEqual(res["tracking_nominated"], "cf_tracking_firefox155")
        self.assertFalse(any("cf_tracking_firefox155" in c for _, c in self.puts))
        self.assertEqual(res["regressed_by"], [42])

    def test_a_refused_nomination_costs_the_flag_not_the_bug(self):
        # Simulate an unknown field: retry the create without it, then attempt it separately.
        def create(payload, token):
            self.created.append(payload)
            if "cf_tracking_firefox150" in payload:
                raise bugzilla_apply.BugzillaRejected(
                    "bugzilla create failed (400): code 53, Can't use cf_tracking_firefox150 "
                    "as a field name", status=400)
            return 999

        def put(bug, changes, token):
            if any(k.startswith("cf_tracking_firefox") for k in changes):
                raise RuntimeError("Can't use cf_tracking_firefox150 as a field name")
            self.puts.append((bug, changes))
            return bug
        bugzilla_apply._create_bug.side_effect = create
        bugzilla_apply._put_bug.side_effect = put
        res = self._file_release(
            preview={**_RELEASE_PREVIEW, "tracking_flag": "cf_tracking_firefox150"})
        self.assertTrue(res["filed"])
        self.assertEqual(len(self.created), 4)           # -regressed_by, -blocks, then -flag
        self.assertNotIn("cf_tracking_firefox150", self.created[-1])
        self.assertIn("flags", self.created[-1])         # the needinfo stayed aboard
        self.assertEqual(res["tracking_failed"], "cf_tracking_firefox150")
        self.assertNotIn("tracking_nominated", res)
        self.assertEqual(res["regressed_by"], [42])      # the other PUTs were untouched
        self.assertEqual(len(self.filed), 1)             # and the filing is on record

    def test_no_flag_on_the_preview_means_no_nomination(self):
        res = self._file_release(preview={**_RELEASE_PREVIEW, "tracking_flag": None})
        self.assertTrue(res["filed"])
        self.assertFalse(any(k.startswith("cf_tracking") for k in self.created[0]))
        self.assertFalse(any(k.startswith("cf_tracking") for _, c in self.puts for k in c))
        self.assertNotIn("tracking_nominated", res)
        self.assertNotIn("tracking_failed", res)

    def test_a_nightly_filing_is_byte_identical_to_before(self):
        # `_PREVIEW` is nightly's: no prefix, no flag, and the filer adds neither on its own.
        res = bugzilla_apply.autofile_bug(
            "u-1", _INFO, {}, {"candidate": {"node": "n"}}, "lead", 70)
        self.assertTrue(res["filed"])
        self.assertEqual(self.created[0]["summary"], "Crash in [@ Foo::Bar]")
        self.assertFalse(any(k.startswith("cf_tracking") for k in self.created[0]))
        self.assertFalse(any(k.startswith("cf_tracking") for _, c in self.puts for k in c))
        self.assertNotIn("tracking_nominated", res)


def _upstream(dropped, count=98, total=9599, base_count=341, base_total=19750):
    return {"channel": "beta", "major": 156, "count": count, "total": total, "base_major": 155,
            "base_count": base_count, "base_total": base_total, "z": -5.94 if dropped else -1.0,
            "dropped": dropped}


class TestReleaseCommentsUnlessItDroppedOnBeta(_Base):
    def _file(self, venues, upstream):
        bugzilla_apply.config.get_agent_autofile.return_value = _cfg(
            **dict(_RELEASE_POLICY, comment_on_existing="comment_unless_dropped"))
        report_bug.build_bug_preview.return_value = _RELEASE_PREVIEW
        bugzilla_apply._open_bugs_for_signature.return_value = venues
        with mock.patch.object(spikes, "upstream_drop", return_value=upstream) as drop:
            res = bugzilla_apply.autofile_bug(
                "u-1", _RELEASE_INFO, {}, {"candidate": {"node": "n", "bug": 42}}, "lead", 70)
        return res, drop

    def test_no_drop_comments_on_the_bug_about_this_regression(self):
        # Skip the old bug and use the recent venue.
        res, drop = self._file([_bug(1429978, created=_OLD), _bug(2072627)], _upstream(False))
        self.assertTrue(res["filed"])
        self.assertEqual((res["bug"], res["mode"]), (2072627, "comment_on_existing"))
        self.assertEqual(self.comments, [(2072627, "the whole bug opener")])
        self.assertEqual(self.created, [])
        self.assertEqual(drop.call_args.args[1:], ("Firefox", "release", "155.0.1"))
        self.assertIn("Foo::Bar", drop.call_args.args[0])

    def test_a_venue_naming_a_regressor_still_declines(self):
        res, _d = self._file([_bug(1943005, created=_OLD), _bug(2069097, regressed_by=[42])],
                             _upstream(False))
        self.assertFalse(res["filed"])
        self.assertEqual(res["bug"], 2069097)
        self.assertIn("already names its regressor", res["skipped"])
        self.assertEqual((self.comments, self.created, self.puts), ([], [], []))

    def test_a_drop_on_beta_writes_nothing(self):
        res, _d = self._file([_bug(2072627)], _upstream(True))
        self.assertFalse(res["filed"])
        self.assertEqual(res["skipped"], "open bug 2072627 exists; it dropped on beta 156 "
                                         "(98 in 9599 crash reports, against 341 in 19750 on 155)")
        self.assertTrue(res["upstream"]["dropped"])
        self.assertEqual((self.comments, self.created, self.puts), ([], [], []))

    def test_an_unreadable_beta_writes_nothing(self):
        res, _d = self._file([_bug(2072627)], None)
        self.assertFalse(res["filed"])
        self.assertEqual(res["skipped"], "open bug 2072627 exists; its trend on the upstream "
                                         "channel could not be read")
        self.assertEqual((self.comments, self.created, self.puts), ([], [], []))

    def test_no_bug_about_this_regression_files_nothing(self):
        # An unsuitable open venue must prevent a new public bug.
        res, _d = self._file([_bug(1429978, created=_OLD)], _upstream(False))
        self.assertFalse(res["filed"])
        self.assertEqual(res["skipped"],
                         "open bug 1429978 exists and none can be about this regression")
        self.assertEqual((self.comments, self.created, self.puts), ([], [], []))

    def test_no_open_bug_files_a_new_one_without_asking_beta(self):
        res, drop = self._file([], _upstream(True))
        self.assertTrue(res["filed"])
        self.assertEqual(res["mode"], "new_bug")
        drop.assert_not_called()


_ESR_INFO = {**_INFO, "channel": "esr153", "version": "153.2.0esr",
             "buildid": "20260826022508"}
_ESR_PREVIEW = {**_PREVIEW, "title": "[new in esr] Crash in [@ Foo::Bar]",
                "tracking_flag": "cf_tracking_firefox_esr153"}
_ESR_POLICY = {"enabled": True, "comment_on_existing": "skip", "daily_cap": 2,
               "summary_prefix": "[new in esr]", "nominate_tracking": True}


class TestAnEsrFilingIsTitledAndNominatedLikeRelease(_Base):
    """The ESR family carries release's two marks: `[new in esr]` in the title and the crash's
    own ESR line nominated for tracking -- `cf_tracking_firefox_esr<major>`, a different flag
    FAMILY on BMO (`cf_tracking_firefox140` is Firefox 140's long-retired release flag). The
    filer is the same code path; what changes is the preview and the policy it is handed."""

    def _file_esr(self, preview=None):
        bugzilla_apply.config.get_agent_autofile.return_value = _cfg(**_ESR_POLICY)
        report_bug.build_bug_preview.return_value = preview or _ESR_PREVIEW
        return bugzilla_apply.autofile_bug(
            "u-1", _ESR_INFO, {}, {"candidate": {"node": "n"}}, "lead", 70)

    def test_the_esr_title_and_the_esr_flag(self):
        res = self._file_esr()
        self.assertTrue(res["filed"])
        self.assertEqual(self.created[0]["summary"], "[new in esr] Crash in [@ Foo::Bar]")
        self.assertEqual(self.created[0]["cf_crash_signature"], "[@ Foo::Bar]")
        self.assertEqual(self.created[0]["cf_tracking_firefox_esr153"], "?")   # in the create
        self.assertEqual(res["tracking_nominated"], "cf_tracking_firefox_esr153")
        self.assertFalse(any("cf_tracking_firefox_esr153" in c for _, c in self.puts))
        self.assertEqual(res["channel"], "esr153")

    def test_the_filer_asks_for_the_lines_own_policy(self):
        self._file_esr()
        # The line's label, not its family (`get_agent_autofile` resolves the family itself),
        # and the crash's product (byte-identical policy for Firefox; see test_fenix_filing).
        bugzilla_apply.config.get_agent_autofile.assert_called_once_with(
            "esr153", product="Firefox")


if __name__ == "__main__":
    unittest.main()
