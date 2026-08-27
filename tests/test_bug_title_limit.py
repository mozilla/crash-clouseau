# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""BMO refuses a summary over 255 characters, and 1.3% of signatures produce one.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        uv run python -m unittest tests.test_bug_title_limit

Crash 86cbca8b reached a `lead` at rung 70 with every gate passed, and BMO answered
`400 code 104 "The text you entered in the Summary field is too long (268 characters, above the
maximum length allowed of 255)"`. Its signature is `AsyncShutdownTimeout |
profile-change-teardown | Extension shutdown: <17 add-on ids>` — 255 characters, Socorro's own
cap — and `Crash in [@ ]` adds 13 more.

Two defects, one visible and one not. The title was never capped; and a rejected create returned
without writing anything, so the two earlier losses (2026-08-06 and 2026-08-13, both rung 70)
left nothing behind but a log line on a dyno with a ~2h window.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import bugzilla_apply as ba, models, report_bug as rb  # noqa: E402

# The real one, verbatim from prod (255 chars, Socorro already truncated it).
LONG_SIG = (
    "AsyncShutdownTimeout | profile-change-teardown | Extension shutdown: "
    "addons-search-detection@mozilla.com,Extension shutdown: default-theme@mozilla.org,"
    "Extension shutdown: formautofill@mozilla.org,Extension shutdown: "
    "ipp-activator@mozilla.com,Extension ...")


class TestTheTitleFits(unittest.TestCase):
    def test_the_real_signature_that_was_rejected(self):
        self.assertEqual(len(LONG_SIG), 255)
        self.assertEqual(len("Crash in [@ {}]".format(LONG_SIG)), 268)   # what BMO refused
        self.assertEqual(len(rb.bug_title(LONG_SIG)), 255)

    def test_it_still_reads_as_a_crash_bug_summary(self):
        t = rb.bug_title(LONG_SIG)
        self.assertTrue(t.startswith("Crash in [@ "))
        self.assertTrue(t.endswith("...]"))
        self.assertIn("AsyncShutdownTimeout | profile-change-teardown", t)

    def test_a_short_signature_is_untouched(self):
        self.assertEqual(rb.bug_title("nsAtom::IsStatic"), "Crash in [@ nsAtom::IsStatic]")

    def test_the_boundary(self):
        # 242 is the largest signature the wrapper leaves room for.
        for n in (240, 241, 242, 243, 244, 300, 1000):
            t = rb.bug_title("a" * n)
            self.assertLessEqual(len(t), 255, n)
            self.assertEqual(t == "Crash in [@ {}]".format("a" * n), n <= 242, n)

    def test_socorros_own_ellipsis_is_not_doubled(self):
        # 21 of the 23 over-long prod signatures sit at Socorro's cap with a trailing " ...".
        self.assertNotIn("... ...", rb.bug_title(LONG_SIG))
        self.assertNotIn("......", rb.bug_title("x" * 250 + " ..."))

    def test_nothing_to_title(self):
        for sig in (None, "", "   "):
            self.assertEqual(rb.bug_title(sig), "Crash in [@ ]", sig)

    def test_the_preview_uses_it(self):
        uuid_info = {"uuid": "u-1", "signature": LONG_SIG, "channel": "nightly",
                     "product": "Firefox", "buildid": "20260826091205"}
        dossier = {"verdict": {"decision": "lead"},
                   "candidate": {"node": "n", "bug": 1, "author": "A"}}
        with mock.patch.object(rb, "fetch_signature_stats", return_value=(True, "")), \
             mock.patch.object(rb, "fetch_crash_reason", return_value={}), \
             mock.patch.object(rb, "resolve_product_component",
                               return_value=("Core", "XPCOM")), \
             mock.patch.object(rb, "_needinfo_person", return_value={}), \
             mock.patch.object(rb.models.UUID, "get_info", return_value={}):
            p = rb.build_bug_preview(uuid_info, {"frames": []}, dossier)
        self.assertEqual(len(p["title"]), 255)
        # ...and the SIGNATURE FIELD keeps the whole thing, which is what dedupes the bug.
        self.assertEqual(p["cf_crash_signature"], "[@ {}]".format(LONG_SIG))


class TestARejectedWriteLeavesARecord(unittest.TestCase):
    class _Dossier:
        def __init__(self):
            self.payload = {"dossier": {}}

    def test_it_is_written_under_its_own_key(self):
        d = self._Dossier()
        with mock.patch.object(models.Dossier, "get_by_uuid", return_value=d), \
             mock.patch.object(models.db.session, "add"), \
             mock.patch.object(models.db.session, "commit"):
            ok = models.Dossier.record_filing_error("u-1", {"error": "code 104"})
        self.assertTrue(ok)
        self.assertEqual(d.payload["filing_error"], {"error": "code 104"})

    def test_it_is_not_the_idempotence_key(self):
        """Recording the failure as `filed_bug` would make a retrigger read as "already filed"
        and permanently close a crash whose bug was never created."""
        d = self._Dossier()
        with mock.patch.object(models.Dossier, "get_by_uuid", return_value=d), \
             mock.patch.object(models.db.session, "add"), \
             mock.patch.object(models.db.session, "commit"):
            models.Dossier.record_filing_error("u-1", {"error": "x"})
        self.assertNotIn("filed_bug", d.payload)

    def test_it_does_not_survive_a_later_successful_run(self):
        # Absent from the sticky set on purpose: a stale error beside a real filing is worse
        # than no error at all.
        self.assertNotIn("filing_error", models.Dossier._STICKY_PAYLOAD_KEYS)

    def test_no_dossier_no_write(self):
        with mock.patch.object(models.Dossier, "get_by_uuid", return_value=None):
            self.assertFalse(models.Dossier.record_filing_error("u-1", {}))

    def test_the_filer_records_what_bmo_refused(self):
        """End to end: every gate passes, BMO rejects the create, and the rejection is now a
        row rather than a log line. This is the exact 2026-08-27 failure."""
        rejected = ba.BugzillaRejected(
            "bugzilla create failed (400): The text you entered in the Summary field is too "
            "long (268 characters, above the maximum length allowed of 255 characters).", 400)
        preview = {"title": "Crash in [@ {}]".format(LONG_SIG), "comment": "c",
                   "product": "Core", "component": "XPCOM", "version": "Trunk",
                   "type": "defect", "keywords": ["crash"],
                   "cf_crash_signature": "[@ {}]".format(LONG_SIG), "blocked": [],
                   "needinfo": None, "needinfo_email": None}
        seen = {}
        from crashclouseau import report_bug
        with mock.patch.object(ba.config, "get_agent_autofile", return_value={
                "enabled": True, "min_confidence": 70, "verdicts": ["lead", "culprit"],
                "needinfo": True, "daily_cap": 10, "comment_on_existing": "comment",
                "comment_max_bug_age_days": 30}), \
             mock.patch.object(ba.config, "autofile_channel_declared", return_value=True), \
             mock.patch.object(ba.config, "get_bugzilla_token", return_value="tok"), \
             mock.patch.object(ba.models.Dossier, "already_filed", return_value=None), \
             mock.patch.object(ba.models.Dossier, "filed_bugs_since", return_value=0), \
             mock.patch.object(ba, "_open_bugs_for_signature", return_value=[]), \
             mock.patch.object(ba, "_fixed_after_build_bug", return_value=None), \
             mock.patch.object(report_bug, "build_bug_preview", return_value=preview), \
             mock.patch.object(ba, "_create_bug_keeping_the_bug", side_effect=rejected), \
             mock.patch.object(ba.models.Dossier, "record_filing_error",
                               side_effect=lambda u, i: seen.update(i)):
            res = ba.autofile_bug(
                "u-1", {"uuid": "u-1", "signature": LONG_SIG, "channel": "nightly",
                        "product": "Firefox", "buildid": "20260826091205"},
                {}, {"candidate": {"node": "n"}, "corroborations": {}}, "lead", 70)
        self.assertFalse(res["filed"])
        self.assertIn("too long", res["skipped"])
        self.assertIn("too long", seen["error"])
        self.assertEqual(seen["title_len"], 268)
        self.assertEqual(seen["mode"], "new_bug")
        self.assertEqual(seen["signature"], LONG_SIG)


if __name__ == "__main__":
    unittest.main()
