# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Stale-bug eligibility, preview, writes, and recording for actionable crashes."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import bugzilla_apply, config, report_bug  # noqa: E402
from tests.test_autofile import _INFO, _PREVIEW, _Base, _bug  # noqa: E402

_DECLINE = "open bug 1961865 exists; an actionable crash is filed only where no bug is"
_OWNER = {"nick": "segun", "name": "", "email": "owner@moz.example",
          "account": "owner@moz.example", "account_name": "Segun"}
_REAL_PERSON_FOR_ACCOUNT = report_bug._person_for_account
_REAL_BUGZILLA_USER = report_bug._bugzilla_user
_STALE_PREVIEW = dict(_PREVIEW, comment="the stale-bug comment",
                      needinfo=":segun, as triage owner, can you have a look please?",
                      needinfo_email="owner@moz.example")


def _ago(days):
    return datetime.now(timezone.utc) - timedelta(days=days)


class _WakeBase(_Base):
    def setUp(self):
        super().setUp()
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(1961865)]
        self.activity = {1961865: {"last_human": _ago(529), "ours": False}}
        for p in (
            mock.patch.object(report_bug, "fetch_signature_stats",
                              return_value=(True, {"count": 160, "installs": 22})),
            mock.patch.object(bugzilla_apply, "_bug_activity",
                              side_effect=lambda b: self.activity.get(b)),
            mock.patch.object(bugzilla_apply, "_triage_owner", return_value="owner@moz.example"),
            mock.patch.object(report_bug, "_bugzilla_user", return_value={
                "exists": True, "nick": "segun", "real": "Segun", "askable": True}),
            mock.patch.object(report_bug, "_person_for_account", return_value=dict(_OWNER)),
        ):
            p.start()
            self.addCleanup(p.stop)
        report_bug.build_bug_preview.return_value = _STALE_PREVIEW

    def _wake(self, mode="shadow", dossier=None, **over):
        over = dict({"wake_stale": mode, "wake_stale_days": 180}, **over)
        return self._file(verdict="actionable", confidence=70,
                          dossier=dossier or {"candidate": {"node": "c065adeeac27", "bug": 1}},
                          **over)


class TestTheModes(_WakeBase):
    def test_off_declines_without_reading_the_bug(self):
        res = self._wake(mode="off")
        self.assertEqual((res["filed"], res["skipped"]), (False, _DECLINE))
        self.assertNotIn("wake_stale", res)
        bugzilla_apply._bug_activity.assert_not_called()

    def test_a_config_without_the_key_is_off(self):
        res = self._file(verdict="actionable", confidence=70,
                         dossier={"candidate": {"node": "n", "bug": 1}})
        self.assertNotIn("wake_stale", res)
        self.assertEqual(self.comments, [])

    def test_shadow_records_the_comment_and_writes_nothing(self):
        res = self._wake()
        self.assertEqual((res["filed"], res["bug"], res["skipped"]), (False, 1961865, _DECLINE))
        wake = res["wake_stale"]
        self.assertEqual((wake["mode"], wake["bug"], wake["triage_owner"], wake["needinfo"]),
                         ("shadow", 1961865, "owner@moz.example", "owner@moz.example"))
        self.assertEqual(wake["since"], _ago(529).date().isoformat())
        self.assertEqual(wake["comment"], "the stale-bug comment")
        self.assertEqual((self.comments, self.puts, self.created, self.filed), ([], [], [], []))
        stale = report_bug.build_bug_preview.call_args.kwargs["stale"]
        self.assertEqual(stale, {"since": wake["since"], "person": _OWNER})

    def test_comment_and_needinfo_are_one_update(self):
        res = self._wake(mode="comment")
        self.assertEqual((res["filed"], res["bug"], res["mode"], res["needinfo"]),
                         (True, 1961865, "comment_on_existing", "owner@moz.example"))
        self.assertEqual(res["wake_stale"]["since"], _ago(529).date().isoformat())
        self.assertEqual(self.puts, [(1961865, dict(
            {"comment": {"body": "the stale-bug comment"}},
            **bugzilla_apply._needinfo_changes("owner@moz.example")))])
        self.assertEqual((self.comments, self.created), ([], []))
        self.assertEqual([u for u, _ in self.filed], ["u-1"])

    def test_a_triage_owner_already_asked_is_not_asked_twice(self):
        bugzilla_apply._existing_needinfos.return_value = {"owner@moz.example"}
        res = self._wake(mode="comment")
        self.assertEqual(res["needinfo_already_set"], "owner@moz.example")
        self.assertEqual(self.puts, [(1961865, {"comment": {"body": "the stale-bug comment"}})])

    def test_unreadable_flags_write_nothing(self):
        bugzilla_apply._existing_needinfos.return_value = None
        res = self._wake(mode="comment")
        self.assertEqual((res["filed"], res["wake_stale"]["skipped"]),
                         (False, "could not read the flags on bug 1961865"))
        self.assertEqual((self.puts, self.comments, self.filed), ([], [], []))

    def test_a_refused_update_is_recorded_and_not_filed(self):
        bugzilla_apply._put_bug.side_effect = RuntimeError("400 needinfo refused")
        with mock.patch.object(bugzilla_apply.models.Dossier, "record_filing_error") as err:
            res = self._wake(mode="comment")
        self.assertFalse(res["filed"])
        self.assertIn("bugzilla write failed", res["skipped"])
        self.assertEqual(err.call_args.args[1]["mode"], "wake_stale")
        self.assertEqual((self.comments, self.filed), ([], []))

    def test_lead_verdicts_do_not_reach_it(self):
        res = self._file(verdict="lead", confidence=70, comment_on_existing="skip",
                         wake_stale="comment")
        self.assertEqual(res["skipped"], "open bug 1961865 exists")
        bugzilla_apply._bug_activity.assert_not_called()


class TestTheConditions(_WakeBase):
    def _reason(self, res):
        self.assertEqual((res["filed"], res["skipped"]), (False, _DECLINE))
        self.assertNotIn("comment", res["wake_stale"])
        return res["wake_stale"]["skipped"]

    def test_a_recent_human_comment(self):
        self.activity[1961865]["last_human"] = _ago(12)
        self.assertEqual(self._reason(self._wake()), "bug 1961865 has a human comment from "
                         "{}".format(_ago(12).date().isoformat()))

    def test_the_threshold_is_the_configured_days(self):
        self.activity[1961865]["last_human"] = _ago(100)
        self.assertIn("comment", self._wake(wake_stale_days=90)["wake_stale"])

    def test_every_open_bug_must_be_stale(self):
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(1961865), _bug(2000000)]
        self.activity[2000000] = {"last_human": _ago(3), "ours": False}
        self.assertIn("bug 2000000 has a human comment", self._reason(self._wake()))

    def test_a_bug_with_our_comment(self):
        self.activity[1961865]["ours"] = True
        self.assertEqual(self._reason(self._wake()),
                         "bug 1961865 already has a Clouseau comment")

    def test_a_failed_comment_read(self):
        self.activity.clear()
        self.assertEqual(self._reason(self._wake()),
                         "could not read the comments on bug 1961865")

    def test_a_memory_safety_signal(self):
        with mock.patch.object(bugzilla_apply.sensitive, "is_withheld", return_value=True):
            reason = self._reason(self._wake())
        self.assertEqual(reason, "the crash report shows a memory-safety signal")

    def test_a_nonpublic_origin_bug(self):
        with mock.patch("crashclouseau.disclosure.public_bugs", return_value=set()):
            self.assertEqual(self._reason(self._wake()), "the origin bug is not public")

    def test_no_askable_triage_owner(self):
        report_bug._person_for_account.return_value = {}
        self.assertEqual(self._reason(self._wake()),
                         "bug 1961865 has no triage owner who can be asked")

    def test_a_failed_triage_owner_read(self):
        bugzilla_apply._triage_owner.return_value = None
        self.assertEqual(self._reason(self._wake()),
                         "could not read the triage owner of bug 1961865")

    def test_a_comment_naming_a_nonpublic_bug(self):
        with mock.patch("crashclouseau.disclosure.screen",
                        return_value={"text": "x", "withdrawn": [], "left": [7]}):
            self.assertEqual(self._reason(self._wake()),
                             "the analysis names a bug that is not public")


class TestTheTriageOwnerLookup(_WakeBase):
    """Exercise both account helpers with mocked HTTP responses."""

    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(report_bug, "_person_for_account", _REAL_PERSON_FOR_ACCOUNT),
                  mock.patch.object(report_bug, "_bugzilla_user", _REAL_BUGZILLA_USER),
                  mock.patch.dict(report_bug._USER_CACHE, clear=True)):
            p.start()
            self.addCleanup(p.stop)

    def _user(self, **fields):
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"users": [dict(
            {"name": "owner@moz.example", "nick": "segun", "real_name": "Segun",
             "can_login": True, "requests": {"needinfo": {"blocked": False}}}, **fields)]}
        return mock.patch.object(report_bug.net, "get", return_value=resp)

    def test_a_failed_lookup_declines(self):
        with mock.patch.object(report_bug.net, "get", side_effect=TimeoutError("timed out")):
            res = self._wake(mode="comment")
        self.assertEqual((res["filed"], res["skipped"]), (False, _DECLINE))
        self.assertEqual(res["wake_stale"]["skipped"], "could not check whether the triage "
                         "owner of bug 1961865 can be asked")
        self.assertEqual((self.comments, self.puts, self.filed), ([], [], []))

    def test_a_verified_owner_is_asked(self):
        with self._user():
            res = self._wake(mode="comment")
        self.assertEqual((res["filed"], res["needinfo"]), (True, "owner@moz.example"))
        stale = report_bug.build_bug_preview.call_args.kwargs["stale"]
        self.assertEqual(stale["person"]["nick"], "segun")

    def test_a_disabled_or_blocked_owner_declines(self):
        for fields in ({"can_login": False}, {"requests": {"needinfo": {"blocked": True}}}):
            report_bug._USER_CACHE.clear()
            with self._user(**fields):
                res = self._wake(mode="comment")
            self.assertEqual(res["wake_stale"]["skipped"],
                             "bug 1961865 has no triage owner who can be asked")
        self.assertEqual((self.puts, self.comments), ([], []))


class TestTheReads(unittest.TestCase):
    def _activity(self, comments):
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"bugs": {"5": {"comments": comments}}}
        with mock.patch.object(bugzilla_apply.net, "get", return_value=resp):
            return bugzilla_apply._bug_activity(5)

    def test_bot_comments_are_not_human_activity(self):
        out = self._activity([
            {"creator": "dev@mozilla.com", "creation_time": "2025-04-22T16:51:00Z", "text": "c0"},
            {"creator": "release-mgmt-account-bot@mozilla.tld",
             "creation_time": "2025-05-15T12:17:28Z", "text": "topcrash"},
        ])
        self.assertEqual(out, {"last_human": datetime(2025, 4, 22, 16, 51, tzinfo=timezone.utc),
                               "ours": False})

    def test_a_bot_filed_bug_falls_back_to_comment_0(self):
        out = self._activity([{"creator": "bot@mozilla.tld",
                               "creation_time": "2026-10-01T00:00:00Z", "text": "c0"}])
        self.assertEqual(out["last_human"], datetime(2026, 10, 1, tzinfo=timezone.utc))

    def test_our_account_or_link_is_ours(self):
        human = {"creator": "dev@mozilla.com", "creation_time": "2025-04-22T16:51:00Z",
                 "text": "c0"}
        self.assertTrue(self._activity([human, dict(human, creator="clouseau-bot@mozilla.tld",
                                                    text="Adding [@ x] to this bug.")])["ours"])
        self.assertTrue(self._activity([human, dict(
            human, text="Filed by [Clouseau](https://github.com/mozilla/crash-clouseau)")])["ours"])

    def test_no_comments_is_a_failed_read(self):
        self.assertIsNone(self._activity([]))

    def test_the_triage_owner(self):
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"bugs": [{"id": 5, "triage_owner": "owner@moz.example"}]}
        with mock.patch.object(bugzilla_apply.net, "get", return_value=resp) as get:
            self.assertEqual(bugzilla_apply._triage_owner(5), "owner@moz.example")
        self.assertEqual(get.call_args.kwargs["params"]["include_fields"], "id,triage_owner")
        resp.json.return_value = {"bugs": [{"id": 5, "triage_owner": ""}]}
        with mock.patch.object(bugzilla_apply.net, "get", return_value=resp):
            self.assertEqual(bugzilla_apply._triage_owner(5), "")


class TestTheComment(unittest.TestCase):
    _DOSSIER = {"verdict": {"decision": "actionable",
                            "mechanism": {"statement": "`putString` does not catch it."}},
                "candidate": {"node": "c065adeeac27", "author": "Grisha Kruglov"}}

    def test_the_stale_opener_and_the_comment_footer(self):
        with mock.patch.object(report_bug, "build_stats_sentence", return_value=None), \
                mock.patch.object(report_bug, "changeset_links", return_value="c065adeeac27"):
            text = report_bug.build_actionable_comment(
                dict(_INFO, uuid="u-1"), {}, self._DOSSIER, stale_since="2025-04-22",
                needinfo=":segun, as triage owner, can you have a look please?")
        self.assertTrue(text.startswith("This bug has had no comment from a person since "
                                        "2025-04-22."))
        self.assertIn("The failing code comes from c065adeeac27 by Grisha Kruglov.", text)
        self.assertIn(":segun, as triage owner, can you have a look please?", text)
        self.assertTrue(text.endswith("_Posted automatically by [Clouseau](https://github.com/"
                                      "mozilla/crash-clouseau), which analyses nightly crashes "
                                      "with an LLM. Nothing above was written or checked by a "
                                      "human._"))
        self.assertNotIn("INVALID", text)
        self.assertNotIn("resolve THIS bug", text)

    def test_without_stale_the_filed_bug_footer_stays(self):
        with mock.patch.object(report_bug, "build_stats_sentence", return_value=None), \
                mock.patch.object(report_bug, "changeset_links", return_value="c065adeeac27"):
            text = report_bug.build_actionable_comment(dict(_INFO, uuid="u-1"), {}, self._DOSSIER)
        self.assertNotIn("no comment from a person", text)
        self.assertTrue(text.endswith(report_bug._provenance("nightly")))

    def test_the_needinfo_line_names_the_role(self):
        self.assertEqual(report_bug._needinfo_line(_OWNER, role="triage owner"),
                         ":segun, as triage owner, can you have a look please?")
        self.assertEqual(report_bug._needinfo_line(_OWNER), ":segun, can you have a look please?")
        self.assertIsNone(report_bug._needinfo_line({}, role="triage owner"))

    def test_the_preview_asks_the_triage_owner_and_names_the_author(self):
        author = {"nick": "grisha", "name": "Grisha Kruglov", "email": "g@moz.example",
                  "account": "g@moz.example", "account_name": ""}
        with mock.patch.object(report_bug, "_needinfo_person", return_value=author), \
                mock.patch.object(report_bug, "resolve_product_component",
                                  return_value=("Firefox for Android", "Accounts and Sync")), \
                mock.patch.object(report_bug, "fetch_signature_stats", return_value=(True, None)), \
                mock.patch.object(report_bug, "fetch_crash_reason", return_value={}), \
                mock.patch.object(report_bug, "changeset_links", return_value="c065adeeac27"):
            preview = report_bug.build_bug_preview(
                dict(_INFO, uuid="u-1", version="159.0a1"), {}, self._DOSSIER,
                stale={"since": "2025-04-22", "person": _OWNER})
        self.assertEqual(preview["needinfo_email"], "owner@moz.example")
        self.assertEqual(preview["needinfo"],
                         ":segun, as triage owner, can you have a look please?")
        self.assertIn("comes from c065adeeac27 by :grisha.", preview["comment"])
        self.assertIn("no comment from a person since 2025-04-22", preview["comment"])


class TestTheConfig(unittest.TestCase):
    def test_wake_mode(self):
        self.assertEqual([config.wake_mode(v) for v in ("shadow", " Comment ", "off", None,
                                                        "yes", True)],
                         ["shadow", "comment", "off", "off", "off", "off"])

    def test_defaults_and_the_shipped_value(self):
        with mock.patch.object(config, "get_agent", return_value={"autofile": {}}):
            cfg = config.get_agent_autofile("nightly")
        self.assertEqual((cfg["wake_stale"], cfg["wake_stale_days"]), ("off", 180))
        shipped = config.get_agent_autofile("nightly", product="Fenix")
        self.assertEqual((shipped["wake_stale"], shipped["wake_stale_days"]), ("comment", 180))


class TestTheDeclineRecord(unittest.TestCase):
    def test_it_keeps_the_stale_bug_check(self):
        from crashclouseau.agent import orchestrator
        seen = []
        wake = {"mode": "shadow", "bug": 1961865, "comment": "c"}
        with mock.patch.object(orchestrator.models.CrashStack, "get_by_uuid",
                               return_value=({}, {"channel": "nightly", "product": "Fenix",
                                                  "signature": "S", "buildid": None})), \
                mock.patch.object(bugzilla_apply, "autofile_bug", return_value={
                    "filed": False, "bug": 1961865, "skipped": _DECLINE, "wake_stale": wake}), \
                mock.patch.object(orchestrator.models.Dossier, "record_filing_decline",
                                  side_effect=lambda u, i: seen.append(i)):
            orchestrator._autofile("u-1", {"dossier": {}},
                                   {"verdict": "actionable", "confidence": 70})
        self.assertEqual(seen[0]["wake_stale"], wake)


if __name__ == "__main__":
    unittest.main()
