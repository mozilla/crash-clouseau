"""Hidden-duplicate lookup and filing gates."""

import unittest
from datetime import datetime, timezone
from unittest import mock

from crashclouseau import bugzilla_apply, config
from crashclouseau.agent import spike_escalation as se
from tests import REAL_HIDDEN_DUPLICATE_BUG
from tests.test_autofile import _BUILT, _Base, _FakeBMO, _bug, _cfg
from tests.test_spike_escalation import _FilerBase, _esc

SIG = "nsIFrame::IsLetterFrame"
NOW = datetime(2026, 10, 17, 18, 25, tzinfo=timezone.utc)
RESOLVED = datetime(2026, 10, 17, 18, 0, 29, tzinfo=timezone.utc)
HIT = {"via": 2083233, "resolved": RESOLVED}


def _dup(bid, target, resolved="2026-10-17T18:00:29Z", product="Core", signature=SIG):
    return {"id": bid, "product": product, "dupe_of": target, "cf_last_resolved": resolved,
            "summary": "crash near null in [@ GetFirstLeaf]",
            "cf_crash_signature": "[@ {} ]".format(signature)}


def _unreadable(ids):
    return []


class TestTheLookup(unittest.TestCase):

    def _ask(self, *dups, hops=_unreadable, days=14, signature=SIG, product="Firefox",
             spellings=None):
        bmo = _FakeBMO(*dups)
        with mock.patch.object(bugzilla_apply.net, "get", side_effect=bmo.get), \
                mock.patch.object(bugzilla_apply, "_bugs_by_id", side_effect=hops) as by_id:
            got = REAL_HIDDEN_DUPLICATE_BUG(signature, product, spellings=spellings,
                                            max_age_days=days, now=NOW)
        return got, bmo, by_id

    def test_a_recent_duplicate_of_an_unreadable_bug_is_found(self):
        got, bmo, by_id = self._ask(_dup(2083233, 2079035))
        self.assertEqual(got, HIT)
        params = bmo.params[0]
        self.assertEqual(params["resolution"], "DUPLICATE")
        self.assertIn(SIG, params.values())
        by_id.assert_called_once_with([2079035])

    def test_a_readable_target_is_not_hidden(self):
        for resolution in ("", "FIXED", "WONTFIX"):
            got, _, _ = self._ask(_dup(2083233, 2079035), hops=lambda ids, r=resolution: [
                {"id": 2079035, "resolution": r}])
            self.assertIsNone(got, resolution)

    def test_an_old_duplicate_is_not_asked_about(self):
        got, _, by_id = self._ask(_dup(2083233, 2079035, resolved="2026-10-02T18:00:00Z"))
        self.assertIsNone(got)
        by_id.assert_not_called()

    def test_a_duplicate_resolved_after_now_does_not_count(self):
        got, _, _ = self._ask(_dup(2083233, 2079035, resolved="2026-10-19T18:45:26Z"))
        self.assertIsNone(got)

    def test_the_chain_is_followed_to_the_unreadable_end(self):
        def hops(ids):
            return [{"id": 11, "resolution": "DUPLICATE", "dupe_of": 12}] if 11 in ids else []

        got, _, by_id = self._ask(_dup(10, 11), hops=hops)
        self.assertEqual(got, {"via": 10, "resolved": RESOLVED})
        self.assertEqual(by_id.call_args_list, [mock.call([11]), mock.call([12])])

    def test_an_unfinished_chain_is_not_hidden(self):
        def hops(ids):
            return [{"id": i, "resolution": "DUPLICATE", "dupe_of": 23 - i} for i in ids]

        got, _, by_id = self._ask(_dup(10, 11), hops=hops)
        self.assertIsNone(got)
        self.assertEqual(by_id.call_count, bugzilla_apply._DUP_CHAIN_MAX_HOPS)

    def test_another_applications_duplicate_does_not_count(self):
        got, _, _ = self._ask(_dup(2083233, 2079035, product="Thunderbird"))
        self.assertIsNone(got)

    def test_a_longer_signature_does_not_match(self):
        got, _, _ = self._ask(_dup(2083233, 2079035, signature=SIG + " | nsBidiPresUtils"))
        self.assertIsNone(got)

    def test_the_newest_hidden_duplicate_is_named(self):
        got, _, _ = self._ask(_dup(30, 40, resolved="2026-10-10T00:00:00Z"),
                              _dup(31, 41), _dup(32, 42, resolved="2026-10-12T00:00:00Z"))
        self.assertEqual(got, {"via": 31, "resolved": RESOLVED})

    def test_other_names_of_the_crash_are_searched(self):
        got, bmo, _ = self._ask(_dup(2083233, 2079035, signature="GetFirstLeaf"),
                                spellings=["GetFirstLeaf"])
        self.assertEqual(got, HIT)
        self.assertIn("GetFirstLeaf", bmo.params[0].values())

    def test_a_failed_hop_does_not_suppress(self):
        got, _, _ = self._ask(_dup(2083233, 2079035), hops=lambda ids: None)
        self.assertIsNone(got)

    def test_a_failed_search_does_not_suppress(self):
        with mock.patch.object(bugzilla_apply.net, "get", side_effect=OSError("reset")):
            self.assertIsNone(REAL_HIDDEN_DUPLICATE_BUG(SIG, "Firefox", max_age_days=14, now=NOW))

    def test_disabled_makes_no_request(self):
        for days in (0, None):
            got, bmo, _ = self._ask(_dup(2083233, 2079035), days=days)
            self.assertIsNone(got)
            self.assertEqual(bmo.params, [])

    def test_the_decline_names_the_public_bug_only(self):
        text = bugzilla_apply._hidden_duplicate_result(HIT)["skipped"]
        self.assertIn("bug 2083233", text)
        self.assertIn("2026-10-17", text)
        self.assertNotIn("2079035", text)


class TestTheOrdinaryFiler(_Base):

    def setUp(self):
        super().setUp()
        bugzilla_apply.config.get_agent_autofile.return_value = _cfg(hidden_duplicate_days=14)
        p = mock.patch.object(bugzilla_apply, "_hidden_duplicate_bug", return_value=HIT)
        self.hidden = p.start()
        self.addCleanup(p.stop)

    def _file(self):
        return bugzilla_apply.autofile_bug("u-1", _BUILT, {}, {"candidate": {"node": "n"}},
                                           "lead", 70)

    def test_a_new_bug_is_declined(self):
        res = self._file()
        self.assertFalse(res["filed"])
        self.assertEqual(res["bug"], 2083233)
        self.assertEqual(res["hidden_duplicate"],
                         {"via": 2083233, "resolved": RESOLVED.isoformat()})
        self.assertIn("not public", res["skipped"])
        self.assertEqual((self.created, self.comments, self.filed), ([], [], []))
        self.hidden.assert_called_once_with("Foo::Bar", "Firefox", max_age_days=14)

    def test_an_open_venue_still_gets_the_comment(self):
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(2063862)]
        res = self._file()
        self.assertEqual((res["filed"], res["mode"]), (True, "comment_on_existing"))
        self.hidden.assert_not_called()

    def test_a_bucket_holder_signature_is_not_asked(self):
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(9, keywords=["meta"])]
        res = self._file()
        self.assertFalse(res["filed"])
        self.assertIn("[meta] bug 9", res["skipped"])
        self.hidden.assert_not_called()

    def test_nothing_hidden_files_as_before(self):
        self.hidden.return_value = None
        res = self._file()
        self.assertEqual((res["filed"], res["mode"]), (True, "new_bug"))
        self.assertEqual(len(self.created), 1)


class TestTheSpikeFiler(_FilerBase):

    def setUp(self):
        super().setUp()
        p = mock.patch.object(bugzilla_apply, "_hidden_duplicate_bug", return_value=HIT)
        self.hidden = p.start()
        self.addCleanup(p.stop)

    def test_a_new_spike_bug_is_declined(self):
        res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertFalse(res["filed"])
        self.assertEqual(res["bug"], 2083233)
        self.assertIn("not public", res["skipped"])
        self.assertEqual((self.created, self.comments), ([], []))
        kw = self.hidden.call_args.kwargs
        self.assertEqual(kw["max_age_days"],
                         config.get_agent_autofile("nightly")["hidden_duplicate_days"])
        self.assertIn("mozilla::Foo::Bar", kw["spellings"])

    def test_an_open_bug_still_gets_the_spike(self):
        with mock.patch.object(bugzilla_apply, "_open_bugs_for_signature", return_value=[
                {"id": 55, "creation_time": "2026-08-20T00:00:00Z", "product": "Core",
                 "keywords": [], "regressed_by": []}]):
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual((res["filed"], res["bug"], res["mode"]), (True, 55, "spike_comment"))
        self.hidden.assert_not_called()

    def test_nothing_hidden_files_as_before(self):
        self.hidden.return_value = None
        res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual((res["filed"], res["mode"]), (True, "spike_new_bug"))

    def test_a_product_override_is_honoured(self):
        real = config.get_agent_autofile

        def autofile(channel=None, product=None):
            cfg = real(channel, product=product)
            return dict(cfg, hidden_duplicate_days=0) if product == "Firefox" else cfg

        self.hidden.side_effect = lambda *a, **kw: HIT if kw.get("max_age_days") else None
        with mock.patch.object(config, "get_agent_autofile", side_effect=autofile):
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertEqual(self.hidden.call_args.kwargs["max_age_days"], 0)
        self.assertEqual((res["filed"], res["mode"]), (True, "spike_new_bug"))


if __name__ == "__main__":
    unittest.main()
