# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""An invented `diff_line.side` must not cost a whole analysis.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        uv run python -m unittest tests.test_side_is_total

`_SIDE_ALIASES` was a partial map: an unrecognized label fell through to the raw string, failed
`Literal["added","deleted","context"]`, and `_salvage` threw the verdict away. Measured over 30
days of prod, 14 runs still lose their verdict to validation and 5 are `diff_line.side` alone —
each an otherwise complete analysis, at a mean $3.00 against a $1.99 fleet average.

`FailureClass._missing_` in the same module already makes this argument, and names this very map
as the other finite list that "still miss[es] values".
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402

from crashclouseau.agent.schema import (  # noqa: E402
    _normalize_citations, _SIDE_ALIASES, _SIDE_FALLBACK, DiffLineCitation,
)


def _side(v):
    return _normalize_citations({"kind": "diff_line", "side": v})["side"]


class TestTheKnownVocabularyIsUnchanged(unittest.TestCase):
    def test_every_alias_still_maps_where_it_did(self):
        for raw, want in _SIDE_ALIASES.items():
            self.assertEqual(_side(raw), want, raw)

    def test_case_and_padding(self):
        self.assertEqual(_side("  Removed "), "deleted")
        self.assertEqual(_side("ADDED"), "added")


class TestTheUnknownIsNoLongerFatal(unittest.TestCase):
    def test_an_invented_label_becomes_context(self):
        # The next label nobody predicted. Before: a `Literal` failure that destroyed the
        # dossier. After: a valid pointer that asserts nothing about the line changing.
        for invented in ("both", "modified", "changed", "+", "-", "?", "", "left", "right",
                         "before", "after", "new", "old", "source"):
            self.assertEqual(_side(invented), _SIDE_FALLBACK, invented)

    def test_the_fallback_is_the_non_asserting_member(self):
        """`context` is the whole reason this is safe: an unknown side can never claim a line
        was ADDED or DELETED, so it cannot manufacture a change the agent did not observe."""
        self.assertEqual(_SIDE_FALLBACK, "context")

    def test_the_citation_then_validates(self):
        c = DiffLineCitation(**_normalize_citations({
            "kind": "diff_line", "node": "abcdef123456", "filename": "a.cpp",
            "line": 7, "side": "modified", "content": "x"}))
        self.assertEqual(c.side, "context")

    def test_a_non_string_side_is_left_alone(self):
        # Only strings are normalized; a null or a number still fails loudly rather than
        # being silently coerced into a real diff side.
        for bad in (None, 3, ["added"]):
            self.assertEqual(
                _normalize_citations({"kind": "diff_line", "side": bad})["side"], bad)


class TestKindIsStillPartialOnPurpose(unittest.TestCase):
    """`ref` is a POINTER, not a non-assertion, so an unrecognized kind is NOT defaulted —
    `RefCitation`'s own note calls that total mapping unmeasured."""

    def test_an_unknown_kind_passes_through_to_fail(self):
        self.assertEqual(_normalize_citations({"kind": "wat"})["kind"], "wat")

    def test_but_the_measured_invented_kinds_still_map(self):
        for raw in ("source_line", "source_raw_file", "pinned_source", "changeset",
                    "history_changeset"):
            self.assertEqual(_normalize_citations({"kind": raw})["kind"], "ref", raw)


if __name__ == "__main__":
    unittest.main()
