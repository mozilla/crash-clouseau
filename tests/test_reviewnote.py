# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# The reviewer-correction ingestion loop: `models.ReviewNote` + `feedback._ingest_notes`.
#
# Every number in here was measured on the 51/52-filing panel (BMO
# creator=cdenizet@mozilla.com, creation_time>=2026-08-05, short_desc "Crash in [@" -> 52
# bugs), comments and history pulled 2026-08-21: 244 comments, 58 ours, 186 not ours, 96
# automation, 90 corpus rows across 43 of the 52.
#
# THIS FILE RUNS ON SQLITE, which does not enforce VARCHAR widths at all. The Postgres-only
# failure mode that motivated the feature -- a state name longer than its column, raising
# StringDataRightTruncation inside a scheduled job's commit -- therefore CANNOT be reproduced
# here by writing a row. It is covered instead by reading the width off the column and
# checking every value the code can store against it (TestColumnWidths), which is both
# dialect-independent and the thing that actually rots.
#
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_reviewnote
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import models  # noqa: E402


MARK = "Crash report: https://crash-stats.mozilla.org/report/index/"


def _comment(cid, no, creator, text):
    return {"id": cid, "count": no, "creator": creator, "text": text,
            "creation_time": "2026-08-20T10:00:00Z"}


class TestAuthorKind(unittest.TestCase):
    """Who wrote it -- a total function of (author, text)."""

    def test_bmo_and_ci_machinery_is_automation(self):
        # 96 of the 186 non-ours comments on the panel, and BugBot alone wrote 57 of them.
        for author, text in (
            ("release-mgmt-account-bot@mozilla.tld",
             ":calixte, since this bug is a regression, could you fill the regressed_by field?"),
            ("pulsebot@bmo.tld", "Pushed by jteh@mozilla.com:\nhttps://github.com/..."),
            ("phab-bot@bmo.tld", "### firefox-beta Uplift Approval Request"),
            ("github-automation@bmo.tld", "Created attachment 9624831\n[mozilla/app-services]"),
        ):
            with self.subTest(author=author):
                self.assertEqual(models.ReviewNote.classify_author(author, text), "automation")

    def test_machine_prose_under_a_human_account_is_automation_too(self):
        # 33 of the 186. The account list alone is not enough: BMO writes the duplicate
        # notice, and a release engineer posts a bare landing URL and nothing else.
        for text in ("*** Bug 2063003 has been marked as a duplicate of this bug. ***",
                     "\n\n*** This bug has been marked as a duplicate of bug 2063678 ***",
                     "https://hg.mozilla.org/mozilla-central/rev/59e3c7456e6f",
                     "Created attachment 9627227\nBug 2063892 - Quota manager: stop rescan"):
            with self.subTest(text=text[:40]):
                self.assertEqual(
                    models.ReviewNote.classify_author("dmeehan@mozilla.com", text), "automation")

    def test_a_patch_description_is_NOT_boilerplate(self):
        """The obvious predicate, measured failing.

        Dropping every "Created attachment" comment eats 12 of the 18 in the panel, because
        BMO puts the patch's commit message in the body -- including this one, jstutte's on
        bug 2062119, which explains the mechanism the pipeline had missed. Only the bare
        two-line form is machinery."""
        rich = ("Created attachment 9624672\n"
                "Bug 2062119 - Do not dereference gJarHandler after it has been cleared.\n\n\n"
                "nsJARChannel read the gJarHandler global directly in five places. Since bug\n"
                "1412726 that global is a StaticRefPtr cleared by ClearOnShutdown.")
        self.assertEqual(
            models.ReviewNote.classify_author("jstutte@mozilla.com", rich), "human")

    def test_an_agent_correction_is_kept(self):
        """The counter-example that stops "filter to human authors" being generalised.

        That filter is right for the NEEDINFO channel (17 of 18 were bot sweeps). Applied to
        comments it eats the sharpest refutations in the panel: 3 of these 4 agent comments
        flatly contradict our attribution."""
        for author in ("firefoxmanagerdev@gmail.com", "hackbot@mozilla.tld"):
            with self.subTest(author=author):
                self.assertEqual(
                    models.ReviewNote.classify_author(
                        author, "**Analysis**\n\nThe attribution to bug 2011452 is not right"),
                    "agent")
        self.assertNotIn("agent", ("automation",))

    def test_an_unknown_account_is_a_person(self):
        # The default has to be `human`: a new bot costs visible noise, the other default
        # silently swallows a new reviewer.
        self.assertEqual(
            models.ReviewNote.classify_author("someone.new@example.org", "I disagree"), "human")
        self.assertEqual(models.ReviewNote.classify_author(None, None), "human")


class TestOurOwnCommentsAreFoundByBodyNotAuthor(unittest.TestCase):
    """8 cdenizet comments sit past comment 0 across 7 of the 52 filings -- and 6 of them are
    the filer commenting again, not a reviewer. An author-email rule mislabels all 8."""

    EIGHT = [  # the real ones, abbreviated
        (2060924, MARK + "364a5e4f-80df"),
        (2062062, MARK + "90dfe0ba-0934"),
        (2062934, MARK + "29c40f7d-33ca"),
        (2062934, MARK + "4d4632a0-e55c"),
        (2063862, MARK + "ac6dd031-e6c7"),
        (2064537, MARK + "30002dc2-a8d0"),
        (2062219, "*** Bug 2063003 has been marked as a duplicate of this bug. ***"),
        (2063003, "Same defect as bug 2062219, two lines apart: both are the dangling "
                  "`nsDisplayTextOverflowMarker::mString`"),
    ]

    def test_the_marker_splits_them_six_two(self):
        from crashclouseau import feedback as fb

        ours = [t for _, t in self.EIGHT if fb._COMMENT_MARK in t]
        self.assertEqual(len(ours), 6)

    def test_the_two_residuals_are_handled_honestly(self):
        # One is BMO's duplicate notice (machinery), one is a genuine human note the operator
        # wrote -- a human DID reply, so it stays in the corpus under his own name.
        kinds = [models.ReviewNote.classify_author("cdenizet@mozilla.com", t)
                 for _, t in self.EIGHT[-2:]]
        self.assertEqual(kinds, ["automation", "human"])


class TestScopeToOurOwnBugs(unittest.TestCase):
    def test_comment_on_existing_rows_are_skipped_and_counted(self):
        """`bugzilla_apply.py:693` sets `filed: True` for a comment on SOMEBODY ELSE's bug.
        The one such bug in the public record (2057980) carries 29 non-ours comments -- 24%
        of the corpus -- of Thunderbird contributors discussing their own bug."""
        from crashclouseau import feedback as fb

        filed = [{"bug_id": 1, "mode": "new_bug"},
                 {"bug_id": 2057980, "mode": "comment_on_existing"},
                 {"bug_id": 3, "mode": "some_future_mode"},
                 {"bug_id": 4}]
        fetched = {b["bug_id"]: {"comment_count": 5, "creator": "cdenizet@mozilla.com"}
                   for b in filed}
        with mock.patch.object(fb, "_fetch_comments", return_value=[]) as fetch, \
             mock.patch.object(models.SweepMark, "get", return_value=0), \
             mock.patch.object(models.SweepMark, "set"):
            out = fb._ingest_notes(filed, fetched)
        self.assertEqual([c.args[0] for c in fetch.call_args_list], [1])
        self.assertEqual((out["eligible"], out["skipped_mode"]), (1, 3))

    def test_an_unknown_mode_is_skipped_not_trusted(self):
        # An allowlist, so a mode added later is a visible gap in the counts rather than 29
        # rows of somebody else's conversation quietly entering the corpus.
        from crashclouseau import feedback as fb

        self.assertEqual(fb._NOTE_MODES, ("new_bug",))


class TestTheCommentCountGate(unittest.TestCase):
    def test_only_a_changed_count_costs_a_get(self):
        """There is no bulk comment endpoint -- /rest/bug/comment?ids=... answers error 100 --
        so an ungated pass is 52-and-growing serial GETs a tick. Measured arrival rate over
        the panel's last 7 days: 2.2 bugs per 6h bucket, busiest 9."""
        from crashclouseau import feedback as fb

        filed = [{"bug_id": 10, "mode": "new_bug"}, {"bug_id": 11, "mode": "new_bug"}]
        fetched = {10: {"comment_count": 4, "creator": "c@m"},
                   11: {"comment_count": 7, "creator": "c@m"}}
        marks = {"revnote:10": 4, "revnote:11": 4}
        with mock.patch.object(fb, "_fetch_comments", return_value=[]) as fetch, \
             mock.patch.object(models.SweepMark, "get", side_effect=marks.get), \
             mock.patch.object(models.SweepMark, "set"):
            out = fb._ingest_notes(filed, fetched)
        self.assertEqual([c.args[0] for c in fetch.call_args_list], [11])
        self.assertEqual((out["unchanged"], out["scanned"]), (1, 1))

    def test_the_watermark_advances_only_after_the_rows_are_in(self):
        # `SweepMark.set` never moves backwards, so a pass that dies half way simply re-reads
        # next tick; `record` is a no-op on an id already stored, so that costs one GET.
        from crashclouseau import feedback as fb

        filed = [{"bug_id": 12, "mode": "new_bug"}]
        fetched = {12: {"comment_count": 2, "creator": "c@m"}}
        with mock.patch.object(fb, "_fetch_comments",
                               return_value=[_comment(1, 1, "r@m", "boom")]), \
             mock.patch.object(fb, "_needinfo_setters", return_value=set()), \
             mock.patch.object(models.ReviewNote, "record", side_effect=RuntimeError("nope")), \
             mock.patch.object(models.SweepMark, "get", return_value=0), \
             mock.patch.object(models.SweepMark, "set") as mark, \
             mock.patch.object(models.db, "session", mock.Mock()):
            out = fb._ingest_notes(filed, fetched)
        mark.assert_not_called()
        self.assertEqual(out["failed"], 1)


class TestNeedinfoIsAPriorityHintNotAChannel(unittest.TestCase):
    def test_it_is_read_from_history_additions_which_are_durable(self):
        """17 of the 18 needinfos aimed at us were mass sweeps by release-mgmt-account-bot (4
        on 2026-08-06, 13 on 2026-08-10); exactly one is a human, jstutte on bug 2065373 --
        the review that started the audit. Read from the flag ADDITION because it survives the
        flag being cleared: the live `flags` field would have found 1 of the 18."""
        from crashclouseau import feedback as fb

        payload = {"bugs": [{"history": [
            {"who": "release-mgmt-account-bot@mozilla.tld",
             "changes": [{"field_name": "flagtypes.name",
                          "added": "needinfo?(cdenizet@mozilla.com)"}]},
            {"who": "jstutte@mozilla.com",
             "changes": [{"field_name": "flagtypes.name",
                          "added": "needinfo?(cdenizet@mozilla.com)"}]},
            {"who": "cdenizet@mozilla.com",
             "changes": [{"field_name": "flagtypes.name",
                          "added": "needinfo?(someone.else@mozilla.com)"}]},
        ]}]}
        resp = mock.Mock(json=mock.Mock(return_value=payload))
        resp.raise_for_status = mock.Mock()
        with mock.patch.object(fb.net, "get", return_value=resp):
            got = fb._needinfo_setters(2065373, "cdenizet@mozilla.com")
        self.assertEqual(got, {"release-mgmt-account-bot@mozilla.tld", "jstutte@mozilla.com"})

    def test_it_never_costs_a_get_of_its_own(self):
        # All 18 needinfos arrived with a comment from the same author within ten minutes (0
        # orphans), so the comment gate already sees every one of them -- and a bug with no
        # new comments is not asked about at all.
        from crashclouseau import feedback as fb

        filed = [{"bug_id": 20, "mode": "new_bug"}]
        fetched = {20: {"comment_count": 1, "creator": "c@m"}}
        with mock.patch.object(fb, "_fetch_comments", return_value=[]), \
             mock.patch.object(fb, "_needinfo_setters") as ni, \
             mock.patch.object(models.SweepMark, "get", return_value=0), \
             mock.patch.object(models.SweepMark, "set"):
            fb._ingest_notes(filed, fetched)
        ni.assert_not_called()


class TestTheCorpusIsNotAVerdict(unittest.TestCase):
    def test_ingesting_notes_never_writes_the_attribution_column(self):
        """`Feedback.attribution` is the causal verdict and feeds `by_archetype`. Two filings
        that get notes already carry a reviewer-set `regressed_by` -- 2061975 [2023197] set by
        dtownsend, 2063892 [2058982] set by dmeehan -- and a second state written over them
        destroys the only verdicts the table has."""
        from crashclouseau import feedback as fb

        filed = [{"bug_id": 2061975, "mode": "new_bug"}]
        fetched = {2061975: {"comment_count": 7, "creator": "cdenizet@mozilla.com"}}
        with mock.patch.object(fb, "_fetch_comments",
                               return_value=[_comment(1, 1, "dtownsend@mozilla.com", "no")]), \
             mock.patch.object(fb, "_needinfo_setters", return_value=set()), \
             mock.patch.object(models.ReviewNote, "record"), \
             mock.patch.object(models.SweepMark, "get", return_value=0), \
             mock.patch.object(models.SweepMark, "set"), \
             mock.patch.object(models.Feedback, "record") as record:
            fb._ingest_notes(filed, fetched)
        record.assert_not_called()

    def test_the_verdict_of_those_two_filings_is_unchanged(self):
        # Their attribution comes from a `regressed_by` somebody else set, and stays there.
        self.assertEqual(
            models.Feedback.classify(None, named_bug=2023197, regressed_by=[2023197],
                                     claimed=[]), "correct")
        self.assertEqual(
            models.Feedback.classify(None, named_bug=999999, regressed_by=[2058982],
                                     claimed=[]), "wrong")

    def test_human_replied_is_derived_never_stored(self):
        # `_ensure_tables` creates missing TABLES, never missing COLUMNS, so a new column on
        # the long-lived `feedback` table would silently not exist in prod. It is also the
        # right shape: a reply is a fact about comments, and it lives with the comments.
        self.assertNotIn("human_replied", models.Feedback.__table__.c)
        self.assertNotIn("corrected", models.Feedback.__table__.c)

    def test_a_reply_is_not_a_correction(self):
        """The predicate fires on 43 of the 52 filings (18 of the 27 still open) and two of
        them are outright endorsements. Only `error_class` may assert we were wrong."""
        rows = [
            mock.Mock(bug_id=2060920, author="docfaraday@gmail.com", author_kind="human",
                      comment_no=1, needinfo=False, error_class=None,
                      body="Seems like an easy enough fix... I'll probably do it all in one go"),
            mock.Mock(bug_id=2063892, author="abienner@mozilla.com", author_kind="human",
                      comment_no=2, needinfo=False, error_class=None,
                      body="I have a fix almost ready"),
        ]
        query = mock.Mock()
        query.filter.return_value.order_by.return_value.all.return_value = rows
        with mock.patch.object(models.db, "session",
                               mock.Mock(query=mock.Mock(return_value=query))):
            replied = models.ReviewNote.replied()
        self.assertEqual(sorted(replied), [2060920, 2063892])
        self.assertEqual([e["labels"] for e in replied.values()], [{}, {}])


class TestColumnWidths(unittest.TestCase):
    """The Postgres-only trap, covered on sqlite.

    sqlite ignores VARCHAR lengths, so no row written here can reproduce
    StringDataRightTruncation. Read the width off the column instead: that is dialect-free,
    and it is the check that keeps working when somebody adds a tenth error class."""

    def test_the_motivating_value_would_not_have_fitted(self):
        # `needinfo_returned` is 17 chars; `Feedback.attribution` is db.String(16). This is
        # why the new states are not in that column, written down as a test so the reason
        # survives the person who found it.
        self.assertEqual(models.Feedback.__table__.c.attribution.type.length, 16)
        self.assertGreater(len("needinfo_returned"),
                           models.Feedback.__table__.c.attribution.type.length)

    def test_every_error_class_fits_its_column(self):
        width = models.ReviewNote.__table__.c.error_class.type.length
        for value in models.ERROR_CLASSES:
            with self.subTest(value=value):
                self.assertLessEqual(len(value), width)

    def test_every_author_kind_fits_its_column(self):
        width = models.ReviewNote.__table__.c.author_kind.type.length
        for value in models.AUTHOR_KINDS:
            with self.subTest(value=value):
                self.assertLessEqual(len(value), width)
        # ...and the classifier can only ever return one of them.
        for author, text in (("x@y.z", "hi"), ("pulsebot@bmo.tld", "Pushed by"),
                             ("hackbot@mozilla.tld", "**Analysis**"), (None, None)):
            self.assertIn(models.ReviewNote.classify_author(author, text),
                          models.AUTHOR_KINDS)

    def test_the_cursor_name_fits_sweepmark(self):
        from crashclouseau import feedback as fb

        self.assertLessEqual(len(fb._NOTE_CURSOR.format(999999999)),
                             models.SweepMark.__table__.c.name.type.length)

    def test_an_over_long_value_is_clamped_rather_than_killing_the_commit(self):
        # The general form: clamp to the width the column itself declares. A truncated email
        # is still identifiable; a truncation ERROR costs the whole six-hourly tick.
        long_author = "a" * 400 + "@mozilla.com"
        got = models._fit_column(models.ReviewNote, "author", long_author)
        self.assertEqual(len(got), models.ReviewNote.__table__.c.author.type.length)
        self.assertIsNone(models._fit_column(models.ReviewNote, "author", None))
        # A Text column has no length and must not be touched.
        self.assertEqual(models._fit_column(models.ReviewNote, "body", "x" * 5000),
                         "x" * 5000)


class TestLabelling(unittest.TestCase):
    def test_a_value_outside_the_vocabulary_is_refused(self):
        with self.assertRaises(ValueError):
            models.ReviewNote.label(1, "definitely_wrong")

    def test_a_refetch_never_overwrites_a_hand_label(self):
        existing = mock.Mock(error_class="wrong_regressor")
        query = mock.Mock()
        query.filter.return_value.one_or_none.return_value = existing
        with mock.patch.object(models.db, "session",
                               mock.Mock(query=mock.Mock(return_value=query))) as session:
            got, created = models.ReviewNote.record(1, 42, body="edited text")
        self.assertIs(got, existing)
        self.assertFalse(created)
        self.assertEqual(existing.error_class, "wrong_regressor")
        session.add.assert_not_called()


class TestItIsWiredUp(unittest.TestCase):
    def test_the_new_table_is_in_added_tables(self):
        """`models.create()` only calls `create_all()` on a FRESH database, so a table missing
        from `_ADDED_TABLES` silently never exists in prod -- the same way the `archetypes`
        feature was dead for weeks. Verified against the mechanism: with `lastdate` present,
        `create()` issues DDL for `_ADDED_TABLES` members only."""
        self.assertIn("reviewnote", models._ADDED_TABLES)
        self.assertIn("reviewnote", models.db.Model.metadata.tables)

    def test_the_schedule_actually_runs_it_and_six_hourly(self):
        """`refresh()`'s docstring has always said "safe to run on a schedule" and it never
        was on one: three jobs, none of them feedback. Six hours, not one -- BMO rate-limits
        an IP for ~45 minutes. Read as source because bin/schedule.py starts a scheduler at
        import time."""
        import ast

        path = os.path.join(os.path.dirname(__file__), "..", "bin", "schedule.py")
        with open(path) as f:
            tree = ast.parse(f.read())
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            calls = [ast.dump(c.func) for c in ast.walk(node) if isinstance(c, ast.Call)]
            if not any("refresh" in c for c in calls):
                continue
            for deco in node.decorator_list:
                if not isinstance(deco, ast.Call):
                    continue
                found += [(kw.arg, ast.literal_eval(kw.value)) for kw in deco.keywords]
        self.assertIn(("hours", 6), found,
                      "feedback.refresh() must be scheduled, and six-hourly")
        self.assertNotIn("hours", [k for k, v in found if k == "hours" and v != 6])


if __name__ == "__main__":
    unittest.main()
