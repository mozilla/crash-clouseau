# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""A RELEASE filing is titled "[new in release] Crash in [@ ...]" and nominates the crash's own
version for tracking: `cf_tracking_firefox<major>` = ?, in its own PUT after the create.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        uv run python -m unittest tests.test_release_filing

Both marks come from `config.get_agent_autofile(channel)` through the preview; this file tests
the FILER's half -- that the title reaches the posted body and that the nomination is a separate,
best-effort write -- with the preview mocked the way `tests.test_autofile` does. The preview's
half (prefix and flag out of the shipped config) is in tests/test_bug_title_limit.py.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402

from crashclouseau import bugzilla_apply, report_bug  # noqa: E402
from tests.test_autofile import _INFO, _PREVIEW, _Base, _cfg  # noqa: E402

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

    def test_the_version_is_nominated_for_tracking_in_its_own_put(self):
        res = self._file_release()
        self.assertIn((999, {"cf_tracking_firefox155": "?"}), self.puts)
        self.assertEqual(res["tracking_nominated"], "cf_tracking_firefox155")
        # Its OWN PUT -- shares nobody's, because a PUT is atomic across fields...
        for _, changes in self.puts:
            if "cf_tracking_firefox155" in changes:
                self.assertEqual(list(changes), ["cf_tracking_firefox155"])
        # ...and AFTER the create, never inside it: an unknown field rejects a create whole.
        self.assertNotIn("cf_tracking_firefox155", self.created[0])
        self.assertEqual(res["regressed_by"], [42])

    def test_a_refused_nomination_costs_the_flag_not_the_bug(self):
        # The ordinary failure: a release crash from a version whose flag BMO has retired.
        def put(bug, changes, token):
            if any(k.startswith("cf_tracking_firefox") for k in changes):
                raise RuntimeError("There is no field named 'cf_tracking_firefox150'")
            self.puts.append((bug, changes))
            return bug
        bugzilla_apply._put_bug.side_effect = put
        res = self._file_release(
            preview={**_RELEASE_PREVIEW, "tracking_flag": "cf_tracking_firefox150"})
        self.assertTrue(res["filed"])
        self.assertEqual(res["tracking_failed"], "cf_tracking_firefox150")
        self.assertNotIn("tracking_nominated", res)
        self.assertEqual(res["regressed_by"], [42])      # the other PUTs were untouched
        self.assertEqual(len(self.filed), 1)             # and the filing is on record

    def test_no_flag_on_the_preview_means_no_nomination(self):
        res = self._file_release(preview={**_RELEASE_PREVIEW, "tracking_flag": None})
        self.assertTrue(res["filed"])
        self.assertFalse(any(k.startswith("cf_tracking") for _, c in self.puts for k in c))
        self.assertNotIn("tracking_nominated", res)
        self.assertNotIn("tracking_failed", res)

    def test_a_nightly_filing_is_byte_identical_to_before(self):
        # `_PREVIEW` is nightly's: no prefix, no flag, and the filer adds neither on its own.
        res = bugzilla_apply.autofile_bug(
            "u-1", _INFO, {}, {"candidate": {"node": "n"}}, "lead", 70)
        self.assertTrue(res["filed"])
        self.assertEqual(self.created[0]["summary"], "Crash in [@ Foo::Bar]")
        self.assertFalse(any(k.startswith("cf_tracking") for _, c in self.puts for k in c))
        self.assertNotIn("tracking_nominated", res)


_ESR_INFO = {**_INFO, "channel": "esr140", "version": "140.15.0esr",
             "buildid": "20260826142222"}
_ESR_PREVIEW = {**_PREVIEW, "title": "[new in esr] Crash in [@ Foo::Bar]",
                "tracking_flag": "cf_tracking_firefox_esr140"}
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
        self.assertIn((999, {"cf_tracking_firefox_esr140": "?"}), self.puts)
        self.assertEqual(res["tracking_nominated"], "cf_tracking_firefox_esr140")
        self.assertNotIn("cf_tracking_firefox_esr140", self.created[0])   # its own PUT
        self.assertEqual(res["channel"], "esr140")

    def test_the_filer_asks_for_the_lines_own_policy(self):
        self._file_esr()
        bugzilla_apply.config.get_agent_autofile.assert_called_once_with("esr140")


if __name__ == "__main__":
    unittest.main()
