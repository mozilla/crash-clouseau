# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# dashboard.html: `impact` derivation, summary and page.
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_impact
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import app, html, impact  # noqa: E402

BOT = "clouseau-bot@mozilla.tld"
LEGACY = "cdenizet@mozilla.com"
MARK = "Crash report: https://crash-stats.mozilla.org/report/index/abc"
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def _event(when, who, *changes):
    return {"when": when, "who": who,
            "changes": [{"field_name": f, "removed": r, "added": a} for f, r, a in changes]}


def _ni(when, who, removed="", added=""):
    return _event(when, who, ("flagtypes.name", removed, added))


def _comment(no, who, when, text="a comment"):
    return {"count": no, "creator": who, "creation_time": when, "text": text}


def _bug(**kw):
    bug = {"id": 1, "summary": "Crash in [@ foo]", "status": "NEW", "resolution": "",
           "creator": BOT, "creation_time": "2026-10-01T10:00:00Z",
           "last_change_time": "2026-10-02T10:00:00Z", "product": "Core",
           "component": "DOM", "assigned_to": "nobody@mozilla.org", "groups": [],
           "severity": "--", "priority": "--", "duplicates": []}
    bug.update(kw)
    return bug


class TestNeedinfos(unittest.TestCase):

    def _one(self, history, comments=()):
        asks = impact._needinfos(history, list(comments), [])
        self.assertEqual(len(asks), 1)
        return asks[0]

    def test_cleared_by_the_requestee_is_answered(self):
        ask = self._one([_ni("2026-10-01T10:00:00Z", BOT, added="needinfo?(dev@moz.example)"),
                         _ni("2026-10-02T10:00:00Z", "dev@moz.example",
                             removed="needinfo?(dev@moz.example)")])
        self.assertEqual(ask["outcome"], "answered")
        self.assertEqual(ask["closed_at"], "2026-10-02T10:00:00+00:00")

    def test_removed_by_someone_else_without_a_comment_is_cancelled(self):
        ask = self._one([_ni("2026-10-01T10:00:00Z", BOT, added="needinfo?(dev@moz.example)"),
                         _ni("2026-10-02T10:00:00Z", "triager@moz.example",
                             removed="needinfo?(dev@moz.example)")])
        self.assertEqual(ask["outcome"], "cancelled")
        self.assertEqual(ask["by"], "triager@moz.example")

    def test_removed_by_someone_who_commented_is_answered_by_them(self):
        ask = self._one(
            [_ni("2026-10-01T10:00:00Z", BOT, added="needinfo?(dev@moz.example)"),
             _ni("2026-10-02T10:00:00Z", "peer@moz.example", removed="needinfo?(dev@moz.example)")],
            [_comment(3, "peer@moz.example", "2026-10-02T10:00:01Z")])
        self.assertEqual(ask["outcome"], "answered_by_other")

    def test_a_flag_moved_to_another_person_is_redirected(self):
        ask = self._one([_ni("2026-10-01T10:00:00Z", BOT, added="needinfo?(dev@moz.example)"),
                         _ni("2026-10-02T10:00:00Z", "dev@moz.example",
                             removed="needinfo?(dev@moz.example)",
                             added="needinfo?(owner@moz.example)")])
        self.assertEqual(ask["outcome"], "redirected")

    def test_cleared_by_automation(self):
        ask = self._one([_ni("2026-10-01T10:00:00Z", BOT, added="needinfo?(dev@moz.example)"),
                         _ni("2026-10-05T10:00:00Z", "release-mgmt-account-bot@mozilla.tld",
                             removed="needinfo?(dev@moz.example)")])
        self.assertEqual(ask["outcome"], "auto_cleared")

    def test_a_renamed_requestee_still_answers(self):
        ask = self._one([_ni("2026-08-14T10:00:00Z", BOT, added="needinfo?(stransky@redhat.com)"),
                         _ni("2026-08-14T19:30:00Z", "stransky@anakreon.cz",
                             removed="needinfo?(stransky@redhat.com)")])
        self.assertEqual(ask["outcome"], "answered")

    def test_withdrawn_by_the_legacy_account(self):
        ask = self._one(
            [_ni("2026-08-05T10:00:00Z", BOT, added="needinfo?(dev@moz.example)"),
             _ni("2026-08-10T10:48:00Z", LEGACY, removed="needinfo?(dev@moz.example)")],
            [_comment(2, LEGACY, "2026-08-10T10:48:00Z", "Sorry, wrong person.")])
        self.assertEqual(ask["outcome"], "auto_cleared")

    def test_still_set_is_pending(self):
        ask = self._one([_ni("2026-10-01T10:00:00Z", BOT, added="needinfo?(dev@moz.example)")])
        self.assertEqual(ask["outcome"], "pending")

    def test_a_needinfo_somebody_else_set_is_not_ours(self):
        self.assertEqual(impact._needinfos(
            [_ni("2026-10-01T10:00:00Z", "triager@moz.example", added="needinfo?(dev@moz.example)"),
             _ni("2026-10-02T10:00:00Z", "dev@moz.example", removed="needinfo?(dev@moz.example)")],
            [], []), [])

    def test_the_legacy_account_counts_only_next_to_one_of_our_comments(self):
        ours = [datetime(2026, 8, 5, 16, 9, 9, tzinfo=timezone.utc)]
        history = [_ni("2026-08-05T16:09:10Z", LEGACY, added="needinfo?(a@moz.example)"),
                   _ni("2026-08-20T09:00:00Z", LEGACY, added="needinfo?(b@moz.example)")]
        asks = impact._needinfos(history, [], ours)
        self.assertEqual([a["to"] for a in asks], ["a@moz.example"])


class TestDerive(unittest.TestCase):

    def test_a_bug_we_filed(self):
        history = [_ni("2026-10-01T10:00:00Z", BOT, added="needinfo?(dev@moz.example)"),
                   _event("2026-10-01T12:00:00Z", "dev@moz.example",
                          ("assigned_to", "nobody@mozilla.org", "dev@moz.example")),
                   _event("2026-10-04T10:00:00Z", "sheriff@moz.example",
                          ("status", "ASSIGNED", "RESOLVED"), ("resolution", "", "FIXED"))]
        comments = [_comment(0, BOT, "2026-10-01T10:00:00Z", MARK),
                    _comment(1, "dev@moz.example", "2026-10-01T15:00:00Z", "Looking."),
                    _comment(2, "release-mgmt-account-bot@mozilla.tld", "2026-10-02T10:00:00Z"),
                    _comment(3, "pulsebot@bmo.tld", "2026-10-03T10:00:00Z",
                             "Pushed by dev@moz.example:\nhttps://github.com/x")]
        facts = impact.derive(_bug(status="RESOLVED", resolution="FIXED",
                                   assigned_to="dev@moz.example"),
                              comments, history, [{"mode": "new_bug"}])
        self.assertEqual(facts["kind"], "filed")
        self.assertEqual(facts["first_at"], "2026-10-01T10:00:00+00:00")
        self.assertEqual(facts["human_comments"], 1)
        self.assertEqual(facts["first_human_at"], "2026-10-01T12:00:00+00:00")
        self.assertTrue(facts["landed"])
        self.assertEqual(facts["fixed_at"], "2026-10-04T10:00:00+00:00")
        self.assertEqual(impact.outcome(facts), "fixed")

    def test_a_bug_we_commented_on_starts_at_our_comment(self):
        comments = [_comment(0, "reporter@moz.example", "2021-09-01T16:00:00Z", MARK),
                    _comment(1, "dev@moz.example", "2022-01-01T00:00:00Z"),
                    _comment(2, BOT, "2026-10-05T13:34:56Z", "Clouseau analysed..."),
                    _comment(3, "dev@moz.example", "2026-10-06T13:34:56Z")]
        facts = impact.derive(_bug(creator="reporter@moz.example",
                                   creation_time="2021-09-01T16:00:00Z"),
                              comments, [], [])
        self.assertEqual(facts["kind"], "commented")
        self.assertEqual(facts["first_at"], "2026-10-05T13:34:56+00:00")
        self.assertEqual(facts["human_comments"], 1)
        self.assertEqual(impact.outcome(facts), "open")

    def test_a_legacy_filing_is_ours_by_its_first_comment(self):
        comments = [_comment(0, LEGACY, "2026-08-05T16:09:09Z", MARK)]
        facts = impact.derive(_bug(creator=LEGACY), comments, [], [{"source": "feedback"}])
        self.assertEqual(facts["kind"], "filed")

    def test_a_closed_bug_we_commented_on_is_already_closed(self):
        history = [_event("2025-01-01T00:00:00Z", "dev@moz.example",
                          ("status", "NEW", "RESOLVED"), ("resolution", "", "WONTFIX"))]
        comments = [_comment(0, "reporter@moz.example", "2024-01-01T00:00:00Z"),
                    _comment(1, BOT, "2026-10-05T00:00:00Z", "spike")]
        bug = _bug(creator="reporter@moz.example", creation_time="2024-01-01T00:00:00Z",
                   status="RESOLVED", resolution="WONTFIX")
        facts = impact.derive(bug, comments, history, [{"mode": "spike_comment"}])
        self.assertEqual(impact.outcome(facts), "closed_before")
        history.append(_event("2026-10-06T00:00:00Z", "dev@moz.example",
                              ("status", "RESOLVED", "REOPENED"), ("resolution", "WONTFIX", "")))
        facts = impact.derive(_bug(creator="reporter@moz.example",
                                   creation_time="2024-01-01T00:00:00Z", status="REOPENED"),
                              comments, history, [{"mode": "spike_comment"}])
        self.assertEqual(impact.outcome(facts), "open")

    def test_channel_and_product_are_read_from_our_text(self):
        text = (MARK + "\n\nThere are 3 crashes (from 1 installation) in nightly 160 with "
                "buildid 20261010084543.\n\nIn the last 30 days, Fenix has 70 crashes")
        facts = impact.derive(_bug(), [_comment(0, BOT, "2026-10-01T10:00:00Z", text)], [], [])
        self.assertEqual((facts["parsed_channel"], facts["parsed_product"]), ("nightly", "Fenix"))

    def test_the_footer_names_the_channel(self):
        text = (MARK + "\n\n_Filed automatically by [Clouseau](https://github.com/mozilla/"
                "crash-clouseau), which analyses beta and Developer Edition crashes with an LLM._")
        facts = impact.derive(_bug(), [_comment(0, BOT, "2026-10-01T10:00:00Z", text)], [], [])
        self.assertEqual((facts["parsed_channel"], facts["parsed_product"]), ("beta", "Firefox"))


class TestWrites(unittest.TestCase):

    def test_merge_fills_gaps_by_uuid(self):
        merged = impact.merge_writes(
            [{"uuid": "u1", "at": "2026-08-05T00:00:00+00:00", "mode": None, "source": "feedback"}],
            [{"uuid": "u1", "at": "2026-08-05T00:00:01+00:00", "mode": "new_bug",
              "channel": "nightly", "source": "dossier"},
             {"uuid": "u2", "at": "2026-08-06T00:00:00+00:00", "mode": "comment_on_existing"}])
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["mode"], "new_bug")
        self.assertEqual(merged[0]["channel"], "nightly")

    def test_a_wake_stale_comment_is_its_own_mode(self):
        w = impact._write({"mode": "comment_on_existing", "wake_stale": {"bug": 1}}, "dossier")
        self.assertEqual(w["mode"], "wake_stale")


class TestBuild(unittest.TestCase):

    def test_an_unchanged_bug_is_not_reread(self):
        bug = _bug(id=5)
        stored = {5: {"bug_id": 5, "writes": [], "last_change": bug["last_change_time"],
                      "facts": {"readable": True, "kind": "filed", "status": "NEW"}}}
        with mock.patch.object(impact, "_fetch_bugs", return_value=({5: bug}, set())), \
                mock.patch.object(impact, "_history") as history:
            rows, stats = impact.build({5}, {}, stored, "")
        history.assert_not_called()
        self.assertEqual(stats, {"unchanged": 1})
        self.assertEqual(rows[0]["facts"]["status"], "NEW")

    def test_a_bug_we_can_no_longer_read_keeps_what_we_knew(self):
        stored = {6: {"bug_id": 6, "writes": [], "last_change": "x",
                      "facts": {"readable": True, "kind": "filed", "summary": "old"}}}
        with mock.patch.object(impact, "_fetch_bugs", return_value=({}, set())):
            rows, _ = impact.build({6}, {}, stored, "")
        self.assertFalse(rows[0]["facts"]["readable"])
        self.assertEqual(rows[0]["facts"]["summary"], "old")
        self.assertEqual(impact.outcome(rows[0]["facts"]), "unreadable")

    def test_a_failed_lookup_keeps_the_stored_row(self):
        stored = {7: {"bug_id": 7, "writes": [], "last_change": "x",
                      "facts": {"readable": True, "kind": "filed", "status": "NEW"}}}
        with mock.patch.object(impact, "_fetch_bugs", return_value=({}, {7})):
            rows, stats = impact.build({7}, {}, stored, "")
        self.assertTrue(rows[0]["facts"]["readable"])
        self.assertEqual(stats, {"failed": 1})


class TestRefresh(unittest.TestCase):

    def test_every_source_is_merged_and_stored(self):
        stored = [{"bug_id": 1, "writes": [], "facts": {}, "last_change": None}]
        searches = iter([{2, 3}, {3, 4}])
        with mock.patch.object(impact.models.Impact, "rows", return_value=stored), \
                mock.patch.object(impact.models.Impact, "store") as store, \
                mock.patch.object(impact, "collect_writes", return_value={5: [{"mode": "new_bug"}]}), \
                mock.patch.object(impact, "_search", side_effect=lambda *a: next(searches)), \
                mock.patch.object(impact, "build", return_value=([{"bug_id": 1}], {})) as build:
            stats = impact.refresh(token="t")
        self.assertEqual(build.call_args[0][0], {1, 2, 3, 4, 5})
        self.assertEqual(build.call_args[0][3], "t")
        store.assert_called_once_with([{"bug_id": 1}])
        self.assertEqual((stats["bugs"], stats["from_search"]), (5, 3))


def _row(bug_id, kind="filed", first_at="2026-10-01T10:00:00+00:00", writes=None, **facts):
    base = {"readable": True, "kind": kind, "first_at": first_at, "status": "NEW",
            "resolution": None, "summary": "Crash in [@ s{}]".format(bug_id),
            "product": "Core", "component": "DOM", "needinfos": []}
    base.update(facts)
    return {"bug_id": bug_id, "kind": kind, "first_at": first_at,
            "writes": writes if writes is not None else [
                {"mode": "new_bug" if kind == "filed" else "comment_on_existing",
                 "channel": "nightly", "product": "Firefox"}],
            "facts": base, "checked_at": "2026-10-12T06:00:00+00:00"}


class TestSummarize(unittest.TestCase):

    def setUp(self):
        self.rows = [
            _row(1, status="RESOLVED", resolution="FIXED",
                 fixed_at="2026-10-03T10:00:00+00:00",
                 needinfos=[{"to": "a@x", "at": "2026-10-01T10:00:00+00:00",
                             "outcome": "answered", "closed_at": "2026-10-01T16:00:00+00:00"}]),
            _row(2, status="RESOLVED", resolution="DUPLICATE", dupe_of=9,
                 dupe_target={"id": 9, "resolution": "FIXED"}),
            _row(3, restricted=True, summary="secret", assignee="dev@x",
                 needinfos=[{"to": "b@x", "at": "2026-10-01T10:00:00+00:00",
                             "outcome": "pending"}]),
            _row(4, kind="commented", first_at="2026-09-20T10:00:00+00:00",
                 status="RESOLVED", resolution="FIXED", open_at_first=True,
                 changed_after=True, writes=[{"mode": "spike_comment", "channel": "beta",
                                              "product": "Firefox"}]),
        ]

    def test_tiles(self):
        view = impact.summarize(self.rows, {1: "correct"}, now=NOW)
        t = view["tiles"]
        self.assertEqual((t["filed"], t["commented"], t["fixed"], t["duplicate"]), (3, 1, 1, 1))
        self.assertEqual((t["dupe_fixed"], t["fixed_after_comment"], t["open"]), (1, 1, 1))
        self.assertEqual((t["ni_asked"], t["ni_answered"], t["ni_pending"]), (2, 1, 1))
        self.assertEqual(t["median_fix_days"], 2.0)
        self.assertEqual(view["attribution"], {"correct": 1})
        self.assertEqual([s["key"] for s in view["filed_outcomes"]],
                         ["fixed", "duplicate", "open_assigned"])

    def test_a_restricted_bug_is_redacted_for_the_public(self):
        public = {b["id"]: b for b in impact.summarize(self.rows, now=NOW)["bugs"]}
        self.assertIsNone(public[3]["summary"])
        self.assertIsNone(public[3]["needinfos"][0]["to"])
        signed_in = {b["id"]: b for b in
                     impact.summarize(self.rows, now=NOW, public=False)["bugs"]}
        self.assertEqual(signed_in[3]["summary"], "secret")

    def test_filters(self):
        view = impact.summarize(self.rows, now=NOW, channel="beta")
        self.assertEqual([b["id"] for b in view["bugs"]], [4])
        view = impact.summarize(self.rows, now=NOW, days=14)
        self.assertEqual(sorted(b["id"] for b in view["bugs"]), [1, 2, 3])
        self.assertEqual(view["channels"], ["beta", "nightly"])

    def test_weeks_run_from_the_first_write_to_now(self):
        view = impact.summarize(self.rows, now=NOW)
        weeks = [w["week"].isoformat() for w in view["weekly"]]
        self.assertEqual(weeks[0], "2026-09-14")
        self.assertEqual(weeks[-1], "2026-10-12")
        self.assertEqual(view["weekly"][2]["filed"]["total"], 3)


class TestPage(unittest.TestCase):

    def _get(self, rows, url="/dashboard.html"):
        with mock.patch.object(html.models.Impact, "rows", return_value=rows), \
                mock.patch.object(html.models.Feedback, "attributions", return_value={}):
            rv = app.test_client().get(url)
        self.assertEqual(rv.status_code, 200)
        return rv.get_data(as_text=True)

    def test_renders(self):
        body = self._get([_row(1, status="RESOLVED", resolution="FIXED"),
                          _row(3, restricted=True, summary="secret"),
                          _row(4, kind="commented")])
        self.assertIn("show_bug.cgi?id=1", body)
        self.assertIn("Bugs Clouseau filed (2)", body)
        self.assertNotIn("secret", body)

    def test_an_empty_table_renders(self):
        self.assertIn("Nothing recorded yet", self._get([]))


if __name__ == "__main__":
    unittest.main()
