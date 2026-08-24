# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# DATABASE_URL=sqlite:// python -m unittest tests.test_history_tool
import asyncio
import unittest
from unittest import mock

from crashclouseau.agent.tools import history as H


def _run(coro):
    return asyncio.run(coro)


CTX = H.HistoryCtx(channel="beta")


class TestFileHistory(unittest.TestCase):
    def test_formats_entries_with_bug_and_backout(self):
        data = {"foo.cpp": {"entries": [
            {"node": "a" * 40, "date": [1780161713.0, 0],
             "desc": "Bug 123 - do a thing\n\nDetails", "user": "Jane Doe <jane@x.org>"},
            {"node": "b" * 40, "date": [1700000000.0, 0],
             "desc": "Backed out changeset deadbeef for bug 999", "user": "bob@y.org"},
        ]}}
        with mock.patch.object(H.FileInfo, "get", return_value=data) as m:
            out = _run(H.file_history(CTX, "foo.cpp"))
        m.assert_called_once_with("foo.cpp", "beta", "tip")   # channel defaults to ctx
        self.assertIn("aaaaaaaaaaaa", out)        # short node (12 chars)
        self.assertNotIn("a" * 16, out)           # not the full 40
        self.assertIn("bug 123", out)
        self.assertIn(H._date([1780161713.0, 0]), out)
        self.assertIn("Jane Doe", out)
        self.assertIn("BACKOUT", out)             # 2nd entry flagged
        self.assertIn("bug 999", out)

    def test_channel_override_beats_ctx(self):
        with mock.patch.object(H.FileInfo, "get", return_value={"f": {"entries": []}}) as m:
            _run(H.file_history(CTX, "f", channel="nightly"))
        m.assert_called_once_with("f", "nightly", "tip")

    def test_empty(self):
        with mock.patch.object(H.FileInfo, "get", return_value={"f": {"entries": []}}):
            self.assertIn("no changesets", _run(H.file_history(CTX, "f")))

    def test_fetch_failure_is_graceful(self):
        with mock.patch.object(H.FileInfo, "get", side_effect=RuntimeError("boom")):
            out = _run(H.file_history(CTX, "f"))
        self.assertIn("fetch failed", out)
        self.assertIn("RuntimeError", out)


class TestBlame(unittest.TestCase):
    def test_selects_line_range(self):
        rows = {"annotate": [
            {"lineno": 9, "node": "c" * 40, "author": "A <a@x>", "desc": "bug 5 x", "line": "line9\n"},
            {"lineno": 10, "node": "d" * 40, "author": "b@y", "desc": "no bug", "line": "line10\n"},
            {"lineno": 11, "node": "e" * 40, "author": "c@z", "desc": "y", "line": "line11\n"},
        ]}
        with mock.patch.object(H.Annotate, "get", return_value={"f.cpp": rows}):
            out = _run(H.blame(CTX, "f.cpp", 10, 11))
        self.assertNotIn("L9:", out)              # 9 is outside [10, 11]
        self.assertIn("L10:", out)
        self.assertIn("L11:", out)
        self.assertIn("dddddddddddd", out)
        self.assertIn("line10", out)

    def test_single_line_default_end(self):
        rows = {"annotate": [{"lineno": 5, "node": "f" * 40, "author": "x@y",
                              "desc": "bug 7", "line": "L\n"}]}
        with mock.patch.object(H.Annotate, "get", return_value={"f": rows}):
            out = _run(H.blame(CTX, "f", 5))
        self.assertIn("L5:", out)
        self.assertIn("bug 7", out)

    def test_a_backout_is_tagged(self):
        """A revert permanently OWNS every line it re-adds, so blaming a crashing line lands on
        the revert rather than on whoever wrote the line. Untagged, this read presented a
        sheriff's revert as the line's author and handed over the REVERTED patch's bug number
        as if it were the revert's own — how `00b44d2a-4343-4caa-9e12-907550260802` came to
        name a backout as its culprit. Same tags as `file_history`."""
        rows = {"annotate": [
            {"lineno": 408, "node": "65b7ea25c7db" + "0" * 28, "author": "S <s@m.org>",
             "desc": 'Revert "Bug 2046861 - Reject it r=x" for leaks', "line": "MOZ_CRASH\n"},
            {"lineno": 409, "node": "e471a805a3b5" + "0" * 28, "author": "P <p@m.org>",
             "desc": "Bug 1955060 - Implement the ONNX API r=y", "line": "}\n"},
        ]}
        with mock.patch.object(H.Annotate, "get", return_value={"f.cpp": rows}):
            out = _run(H.blame(CTX, "f.cpp", 408, 409))
        self.assertIn("bug 2046861 BACKOUT", out)
        self.assertIn("bug 1955060", out)
        self.assertNotIn("bug 1955060 BACKOUT", out)


class TestChangeset(unittest.TestCase):
    def test_metadata_and_files(self):
        rev = {"node": "a" * 40, "git_commit": "9" * 40, "date": [1780161713.0, 0],
               "user": "Jane <j@x>", "desc": "Bug 42 - fix\n\nbody", "backedoutby": "z" * 40,
               "files": [{"file": "a.cpp", "status": "modified"},
                         {"file": "b.h", "status": "added"}]}
        with mock.patch.object(H.inspector, "git2hg", return_value=None), \
             mock.patch.object(H.Revision, "get_revision", return_value=rev) as m:
            out = _run(H.changeset(CTX, "a" * 40))
        m.assert_called_once_with("beta", "a" * 40)   # git2hg->None, node passthrough
        self.assertIn("git: 999999999999", out)
        self.assertIn("bug: 42", out)
        self.assertIn("BACKED OUT BY: zzzzzzzzzzzz", out)
        self.assertIn("modified a.cpp", out)
        self.assertIn("added b.h", out)

    def test_parents_are_printed_because_the_parent_settles_provenance(self):
        """`blame` and a `+` line both prove only that a changeset TOUCHED a line -- a pure MOVE
        re-owns it in blame exactly as a backout does. Reading the file at the PARENT is the one
        cheap check that separates "introduced here" from "last touched here", and on bug 2065373
        it was one GET away with a tool the agent already had. hg returns `parents` in the same
        response and this tool used to discard it, so the check was unreachable."""
        rev = {"node": "a" * 40, "date": [1780161713.0, 0], "user": "J <j@x>",
               "desc": "Bug 42 - fix", "parents": ["e" * 40], "files": []}
        with mock.patch.object(H.inspector, "git2hg", return_value=None), \
             mock.patch.object(H.Revision, "get_revision", return_value=rev):
            out = _run(H.changeset(CTX, "a" * 40))
        self.assertIn("parents: eeeeeeeeeeee", out)
        self.assertNotIn("e" * 16, out)                 # short node, not the full 40
        self.assertIn("prove a line is NEW", out)        # says what it is FOR

    def test_a_merge_joins_both_parents_rather_than_branching(self):
        # 0 of 800 sampled candidate changesets had two parents, so this is a join, not a
        # code path with its own behaviour.
        rev = {"node": "a" * 40, "date": [1780161713.0, 0], "user": "J <j@x>",
               "desc": "merge", "parents": ["e" * 40, "f" * 40], "files": []}
        with mock.patch.object(H.inspector, "git2hg", return_value=None), \
             mock.patch.object(H.Revision, "get_revision", return_value=rev):
            out = _run(H.changeset(CTX, "a" * 40))
        self.assertIn("parents: eeeeeeeeeeee, ffffffffffff", out)

    def test_a_missing_parent_prints_no_line_at_all(self):
        # An absent parent must not render an empty promise; hg omits it for rev 0 / partial
        # responses, and "parents:" with nothing after it reads as "there is no parent".
        rev = {"node": "a" * 40, "date": [1780161713.0, 0], "user": "J <j@x>",
               "desc": "root", "files": []}
        with mock.patch.object(H.inspector, "git2hg", return_value=None), \
             mock.patch.object(H.Revision, "get_revision", return_value=rev):
            out = _run(H.changeset(CTX, "a" * 40))
        self.assertNotIn("parents:", out)

    def test_not_found(self):
        with mock.patch.object(H.inspector, "git2hg", return_value=None), \
             mock.patch.object(H.Revision, "get_revision", return_value={}):
            self.assertIn("not found", _run(H.changeset(CTX, "x")))


class TestSchema(unittest.TestCase):
    def test_tools_registered(self):
        self.assertEqual({t.name for t in H.TOOLS}, {"file_history", "blame", "changeset"})

    def test_optional_params_have_defaults(self):
        by = {t.name: t for t in H.TOOLS}
        fh = by["file_history"].input_schema
        self.assertIn("path", fh["required"])                 # required (no default)
        self.assertNotIn("node", fh.get("required", []))      # optional (default "tip")
        self.assertNotIn("channel", fh.get("required", []))


if __name__ == "__main__":
    unittest.main()
