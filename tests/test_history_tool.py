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
