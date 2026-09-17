# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""The filing-status block on crashstack.html, below the render (tests/test_product_wiring.py
has the page): `bugzilla_apply.open_venues`, the filer's view of the open bugs on a signature
for display, and `html._filing_status`, the dict the template reads.

Motivating case: 8ab28d1a-e446-4afd-b626-f55370260915 (2026-09-17), a culprit at 85 on release
whose record says ``daily cap 2 reached on release`` and nothing else, while dmeehan's bug
2072627 had been open on ``mozilla::a11y::DocAccessibleParent::AddChildDoc`` since the day
before. The page said "filed automatically when enabled"."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import bugzilla_apply, config, html, models  # noqa: E402


def _row(bug_id, product="Core", keywords=(), **kw):
    row = {"id": bug_id, "product": product, "keywords": list(keywords),
           "creation_time": "2026-09-16T13:29:25Z", "regressed_by": []}
    row.update(kw)
    return row


class TestOpenVenues(unittest.TestCase):
    def test_bmo_unreachable_is_none_not_empty(self):
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature", return_value=None):
            self.assertIsNone(bugzilla_apply.open_venues("Foo::bar", "Firefox"))

    def test_the_filers_two_splits_are_applied(self):
        rows = [_row(1), _row(2, product="MailNews Core"), _row(3, keywords=["meta"])]
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature",
                               return_value=rows) as search:
            out = bugzilla_apply.open_venues("Foo::bar", "Firefox")
        self.assertEqual([b["id"] for b in out["venues"]], [1])
        self.assertEqual([b["id"] for b in out["other_app"]], [2])
        self.assertEqual([b["id"] for b in out["metas"]], [3])
        # the PAGE timeout, not the worker's 60 s: one gunicorn worker, router at 30 s
        search.assert_called_once_with("Foo::bar", timeout=bugzilla_apply._PAGE_HTTP_TIMEOUT)
        self.assertLess(bugzilla_apply._PAGE_HTTP_TIMEOUT * 2, 30)

    def test_the_timeout_reaches_every_request_of_the_search(self):
        # Three requests can be made on the venue path (open bugs, their duplicates, and the
        # duplicates' targets); each must honour the caller's timeout, or the page inherits 60 s.
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(timeout)
            resp = mock.Mock()
            resp.raise_for_status.return_value = None
            if params.get("resolution") == "DUPLICATE":
                resp.json.return_value = {"bugs": [
                    {"id": 10, "dupe_of": 20, "cf_crash_signature": "[@ Foo::bar]",
                     "summary": "dup", "cf_last_resolved": "2026-09-10T00:00:00Z"}]}
            elif "id" in params:
                resp.json.return_value = {"bugs": [
                    {"id": 20, "resolution": "", "product": "Core", "keywords": [],
                     "creation_time": "2026-09-01T00:00:00Z"}]}
            else:
                resp.json.return_value = {"bugs": []}
            return resp

        with mock.patch.object(bugzilla_apply.net, "get", side_effect=fake_get):
            out = bugzilla_apply.open_venues("Foo::bar", "Firefox", timeout=5)
        self.assertEqual([b["id"] for b in out["venues"]], [20])
        self.assertEqual(len(calls), 3)
        self.assertEqual(set(calls), {5})


class TestFilingStatus(unittest.TestCase):
    UUID = "8ab28d1a-e446-4afd-b626-f55370260915"
    INFO = {"uuid": UUID, "signature": "mozilla::a11y::DocAccessibleParent::AddChildDoc",
            "channel": "release", "product": "Firefox"}
    POLICY = {"enabled": True, "comment_on_existing": "skip", "daily_cap": 2,
              "min_confidence": 70, "verdicts": ["lead", "culprit"], "needinfo": True,
              "comment_max_bug_age_days": 30, "summary_prefix": "", "nominate_tracking": True}

    def _status(self, evidence, venues=None, prior=None, policy=None):
        if venues is None:
            venues = {"venues": [], "other_app": [], "metas": []}
        with mock.patch.object(config, "get_agent_autofile",
                               return_value=policy or self.POLICY) as pol, \
                mock.patch.object(models.Dossier, "already_filed_for_signature",
                                  return_value=prior) as own, \
                mock.patch.object(bugzilla_apply, "open_venues", return_value=venues) as ven:
            out = html._filing_status(self.UUID, self.INFO, evidence)
        pol.assert_called_once_with("release", "Firefox")
        own.assert_called_once_with(self.INFO["signature"])
        ven.assert_called_once_with(self.INFO["signature"], "Firefox")
        return out

    def test_the_motivating_record(self):
        ev = {"status": "done",
              "filing_declined": {"skipped": "daily cap 2 reached on release",
                                  "at": "2026-09-17T03:44:44+00:00"}}
        out = self._status(ev, venues={"venues": [_row(2072627)], "other_app": [], "metas": []})
        self.assertIsNone(out["filed"])
        self.assertEqual(out["declined"], {"reason": "daily cap 2 reached on release",
                                           "at": "2026-09-17T03:44:44+00:00", "bug": None})
        self.assertIsNone(out["error"])
        self.assertIsNone(out["own_prior"])
        self.assertEqual([b["id"] for b in out["open_bugs"]], [2072627])
        self.assertEqual(out["policy"], {"enabled": True, "mode": "skip", "daily_cap": 2})
        self.assertFalse(out["undecided"])

    def test_a_decline_naming_a_bug_carries_it_from_the_key_or_the_prose(self):
        keyed = self._status({"filing_declined": {"skipped": "open bug 5 exists", "bug": 5}})
        self.assertEqual(keyed["declined"]["bug"], "5")
        prose = self._status({"filing_declined": {"skipped": "already fixed by bug 7 (x)"}})
        self.assertEqual(prose["declined"]["bug"], "7")

    def test_a_filed_bug_is_dropped_from_the_open_list(self):
        ev = {"status": "done", "filed_bug": {"filed": True, "bug": 2072627, "mode": "new_bug"}}
        out = self._status(ev, venues={"venues": [_row(2072627), _row(2072630)],
                                       "other_app": [], "metas": []})
        self.assertEqual(out["filed"]["bug"], 2072627)
        self.assertEqual([b["id"] for b in out["open_bugs"]], [2072630])
        self.assertFalse(out["undecided"])

    def test_a_skipped_record_under_filed_bug_is_not_a_filing(self):
        # Four skip paths write under the same key with `filed: False` (see
        # `Dossier.record_filing_decline`); only `filed: true` is a filing.
        out = self._status({"status": "done",
                            "filed_bug": {"filed": False, "skipped": "already filed", "bug": 9}})
        self.assertIsNone(out["filed"])
        self.assertTrue(out["undecided"])

    def test_own_prior_excludes_this_crash_and_the_sentinel(self):
        same = self._status({"status": "done"}, prior={"uuid": self.UUID, "bug": "1"})
        self.assertIsNone(same["own_prior"])
        failed = self._status({"status": "done"}, prior={"skipped": "prior-filing lookup failed"})
        self.assertIsNone(failed["own_prior"])
        other = self._status({"status": "done"}, prior={"uuid": "other", "bug": 1})
        self.assertEqual(other["own_prior"], {"uuid": "other", "bug": "1"})
        # ...and our own filing of THIS crash is `filed`, not a prior
        filed = self._status({"status": "done",
                              "filed_bug": {"filed": True, "bug": 1, "mode": "new_bug"}},
                             prior={"uuid": "other", "bug": "1"})
        self.assertIsNone(filed["own_prior"])

    def test_bmo_unreachable_is_none_and_the_splits_are_empty(self):
        with mock.patch.object(config, "get_agent_autofile", return_value=self.POLICY), \
                mock.patch.object(models.Dossier, "already_filed_for_signature",
                                  return_value=None), \
                mock.patch.object(bugzilla_apply, "open_venues", return_value=None):
            out = html._filing_status(self.UUID, self.INFO, {"status": "done"})
        self.assertIsNone(out["open_bugs"])
        self.assertEqual(out["other_app_bugs"], [])
        self.assertEqual(out["meta_bugs"], [])

    def test_undecided_only_for_a_finished_run(self):
        self.assertTrue(self._status({"status": "done"})["undecided"])
        self.assertFalse(self._status({"status": "running"})["undecided"])
        self.assertFalse(self._status({"status": "done",
                                       "filing_error": {"error": "boom"}})["undecided"])

    def test_no_signature_asks_nobody(self):
        info = dict(self.INFO, signature="")
        with mock.patch.object(config, "get_agent_autofile", return_value=self.POLICY), \
                mock.patch.object(models.Dossier, "already_filed_for_signature") as own, \
                mock.patch.object(bugzilla_apply, "open_venues") as ven:
            out = html._filing_status(self.UUID, info, {"status": "done"})
        own.assert_not_called()
        ven.assert_not_called()
        self.assertEqual(out["open_bugs"], [])


if __name__ == "__main__":
    unittest.main()
