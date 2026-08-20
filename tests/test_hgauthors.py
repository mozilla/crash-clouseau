# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# An author string we cannot parse must cost us that author's name and nothing else:
# `analyze_author` is called from the pushlog collect, which `update()` runs *before*
# `put_crashes`, so anything raising here stops crash ingestion for every signature.
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_hgauthors
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import hgauthors, pushlog  # noqa: E402


class TestNameWithNoLetters(unittest.TestCase):
    """A name token that holds no ascii letter — an emoji, a CJK given name — is empty
    once to_ascii_form is done with it. cmp_name_email indexes tok[0] on the two-token
    path, so such a token used to raise IndexError. Live case: `Kana <fish> <...>` landed
    on mozilla-central 2026-08-19 and stopped ingestion for 21 hours."""

    def test_emoji_author_is_parsed(self):
        self.assertEqual(
            hgauthors.analyze_author("Kana \U0001F420 <rekanacoding@gmail.com>"),
            [("rekanacoding@gmail.com", "Kana \U0001F420", "")],
        )

    def test_letterless_tokens_never_index_out_of_range(self):
        for name in ("Kana \U0001F420", "\U0001F420 Kana", "Kana 123", "张 三"):
            for email in ("rekanacoding", "", "kana"):
                # the contract is a bool, the point is that it returns at all
                self.assertIsInstance(hgauthors.cmp_name_email(name, email), bool)

    def test_empty_email_is_not_a_match(self):
        # "" is a substring of everything: an author whose name vanishes must not be
        # declared a match for an arbitrary address.
        self.assertFalse(hgauthors.cmp_name_email("\U0001F420", ""))

    def test_known_shapes_still_parse(self):
        for author, expected in (
            ("Jean-Luc Picard <jlpicard@mozilla.com>", "jlpicard@mozilla.com"),
            ("Doe, John <jdoe@mozilla.com>", "jdoe@mozilla.com"),
            ("Foo Bar (:foobar) <foo@mozilla.com>", "foo@mozilla.com"),
        ):
            self.assertEqual(hgauthors.analyze_author(author)[0][0], expected)


class TestAnalyzeAuthorIsTotal(unittest.TestCase):
    """analyze_author already documents its failure mode: log and return []. That must
    hold for an *unexpected* exception too, or the next unparseable author takes the
    whole pipeline down again."""

    def test_unexpected_exception_degrades_to_empty(self):
        with mock.patch.object(
            hgauthors, "analyze_author_helper", side_effect=IndexError("boom")
        ):
            with mock.patch.object(hgauthors.logger, "error") as log:
                self.assertEqual(hgauthors.analyze_author("whoever <a@b.c>"), [])
        self.assertTrue(log.called)


class TestPushlogSurvivesABadAuthor(unittest.TestCase):
    """The containment that actually matters: one bad author in a push must not drop the
    other 588 changesets of the window."""

    def test_collect_keeps_every_changeset(self):
        data = {
            "pushes": {
                "45121": {
                    "date": 1755595421,
                    "changesets": [
                        {
                            "node": "157cd04c4caa" + "0" * 28,
                            "desc": "Bug 1 - do a thing r=someone",
                            "author": "Kana \U0001F420 <rekanacoding@gmail.com>",
                            "files": ["gfx/thebes/gfxFont.cpp"],
                            "parents": ["0" * 40],
                        },
                        {
                            "node": "abcdef012345" + "0" * 28,
                            "desc": "Bug 2 - do another thing r=someone",
                            "author": "Jean-Luc Picard <jlpicard@mozilla.com>",
                            "files": ["gfx/thebes/gfxFont.cpp"],
                            "parents": ["0" * 40],
                        },
                    ],
                }
            }
        }
        res = pushlog.collect(data, lambda f: True)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["author"], [("rekanacoding@gmail.com", "Kana \U0001F420", "")])
        self.assertEqual(res[0]["bug"], 1)


if __name__ == "__main__":
    unittest.main()
