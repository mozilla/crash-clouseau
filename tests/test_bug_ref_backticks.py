# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""A bug reference in backticks is not a bug reference to Bugzilla.

BMO renders the comment as markdown, so ``` `bug 123456` ``` is a CODE SPAN and its linkifier
leaves it as literal text — the reader gets a bug number they cannot click. A developer
complained about exactly that. Measured over the dossiers behind our filings: **33 of the 72
filed bugs (46%)** carry at least one, so it is nearly every other bug we file.

The delicacy is what must NOT change. The same corpus contains quoted COMMIT TITLES
(``` `Bug 2061686 Part 3` ```, ``` `Bug 2064209 - Fix DeviceResetDetectPlace` ```) where the
backticks are correct and the text is a quotation rather than a reference, and field tokens
(``` `bug=2024012` ```) that BMO would not linkify unbackticked either. A looser rule mangles
all three.
"""
# The package builds a Flask app at import, so a URL must exist first.
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau.report_bug import _unbacktick_bug_refs as strip  # noqa: E402


class TestABareReferenceLosesItsBackticks(unittest.TestCase):

    def test_the_shape_that_is_46_percent_of_our_filings(self):
        self.assertEqual(strip("caused by `bug 2058411` on trunk"),
                         "caused by bug 2058411 on trunk")

    def test_case_and_padding_do_not_matter(self):
        for src in ("`Bug 2045970`", "`bug 2045970`", "` bug  2045970 `", "`BUG 2045970`"):
            with self.subTest(src=src):
                self.assertEqual(strip(src), "bug 2045970")

    def test_every_member_of_a_list_gets_its_own_bug(self):
        """"bug 1 / 2" linkifies only the first, so the separator is kept and the word
        repeated. No plural form occurs in the corpus (0 of 601) — this half is written to the
        requirement, not fitted to data."""
        self.assertEqual(strip("`bugs 2058411 / 1993981`"), "bug 2058411 / bug 1993981")
        self.assertEqual(strip("`bugs 2058411, 1993981`"), "bug 2058411, bug 1993981")
        self.assertEqual(strip("`bugs 111111 and 222222`"), "bug 111111 and bug 222222")

    def test_several_spans_in_one_comment(self):
        self.assertEqual(strip("`bug 111111` then `bug 222222`"), "bug 111111 then bug 222222")


class TestWhatMustSurviveUntouched(unittest.TestCase):

    def test_a_quoted_commit_title_keeps_its_backticks(self):
        """Both of these are real, from the corpus. The backticks are correct: the span is a
        quotation of a commit summary, not a reference, and unbackticking it would run the
        title into the prose."""
        for src in ("`Bug 2061686 Part 3`",
                    "`Bug 2064209 - Fix DeviceResetDetectPlace`",
                    "`Bug 2051881 Part 6`"):
            with self.subTest(src=src):
                self.assertEqual(strip(src), src)

    def test_a_field_token_is_not_a_reference(self):
        """`bug=NNNN` is a query/field fragment; BMO does not linkify it unbackticked either,
        so stripping would lose the code formatting and gain nothing."""
        self.assertEqual(strip("`bug=2024012`"), "`bug=2024012`")

    def test_a_phrase_that_merely_contains_a_reference_is_left_alone(self):
        self.assertEqual(strip("`see bug 111111`"), "`see bug 111111`")
        self.assertEqual(strip("`bug 111111 was backed out`"), "`bug 111111 was backed out`")

    def test_a_fenced_block_is_literal_output(self):
        """A crash stack or a quoted diff is not prose; a number in one is not ours to rewrite,
        and the stack is fenced precisely so BMO leaves it alone."""
        src = "before `bug 111111`\n\n```\nassert failed, see `bug 222222`\n```\n\nafter"
        out = strip(src)
        self.assertIn("before bug 111111", out)
        self.assertIn("see `bug 222222`", out)

    def test_ordinary_code_spans_are_untouched(self):
        src = "`--enable-gc-concurrent-marking` guards `js::gc::AutoMarkingLock`"
        self.assertEqual(strip(src), src)

    def test_text_with_no_backticks_is_returned_unchanged(self):
        src = "bug 111111 is already plain"
        self.assertIs(strip(src), src)


class TestItReachesTheFiledComment(unittest.TestCase):

    def test_build_bug_comment_applies_it(self):
        """The normaliser runs where the comment is ASSEMBLED, not where the model is prompted,
        so a re-comment or a re-filing of a dossier stored before this landed comes out right
        too — which a prompt instruction could never do."""
        from crashclouseau import report_bug

        # One section is made to carry the two shapes, so this pins the SEAM — that whatever
        # the sections produce is normalised on the way out — rather than one dossier layout.
        prose = "regressed by `bug 2058411`, see `Bug 999 Part 2`"
        with mock.patch.object(report_bug, "build_stats_sentence", return_value=prose):
            comment = report_bug.build_bug_comment(
                {"uuid": "u-1", "signature": "Foo::bar", "channel": "nightly",
                 "buildid": "20260101000000", "product": "Firefox", "version": "156.0a1"},
                [], {"verdict": {"decision": "abstain"}})
        self.assertIn("regressed by bug 2058411,", comment)
        self.assertNotIn("`bug 2058411`", comment)
        # ...and the quoted commit title in the same string kept its backticks.
        self.assertIn("`Bug 999 Part 2`", comment)


if __name__ == "__main__":
    unittest.main()
