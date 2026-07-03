# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# DATABASE_URL=sqlite:// python -m unittest tests.test_patch_extract
import os
import unittest
from unittest import mock

from crashclouseau.agent import patch_extract as pe

_FIXTURE = os.path.join(os.path.dirname(__file__), "patches", "cpp_funcctx.diff")

_RENAME = """diff --git a/old/name.cpp b/new/name.cpp
rename from old/name.cpp
rename to new/name.cpp
"""

_NEWFILE = """diff --git a/added.txt b/added.txt
new file mode 100644
--- /dev/null
+++ b/added.txt
@@ -0,0 +1,2 @@
+hello
+world
"""

_BINARY = """diff --git a/img.png b/img.png
index abc123..def456 100644
Binary files a/img.png and b/img.png differ
"""

_COSMETIC = """diff --git a/f.cpp b/f.cpp
--- a/f.cpp
+++ b/f.cpp
@@ -1,3 +1,3 @@ void foo()
-    int x = 1;
+  int x = 1;
"""

_NULLCHECK = """diff --git a/g.cpp b/g.cpp
--- a/g.cpp
+++ b/g.cpp
@@ -10,4 +10,3 @@ nsresult Bar::Do()
   Foo* ptr = Get();
-  if (!ptr) return NS_ERROR_FAILURE;
   ptr->Use();
"""


class TestRealFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(_FIXTURE) as handle:
            cls.files = pe.parse_hunks(handle.read())

    def test_parsed_multiple_files_with_a_new_file(self):
        self.assertGreaterEqual(len(self.files), 2)
        self.assertTrue(any(f.status == "added" for f in self.files))
        self.assertTrue(any(f.filename.endswith("HTMLEditor.cpp") for f in self.files))

    def test_enclosing_functions_for_free(self):
        allnames = {n for names in pe.enclosing_functions(self.files).values() for n in names}
        # clean (paren survived the truncation) + truncated-but-qualified names
        self.assertIn("HTMLEditor::OnFocus", allnames)
        self.assertIn("HTMLEditor::IsAcceptableInputEvent", allnames)

    def test_touched_identifiers(self):
        idents = pe.touched_identifiers(self.files)
        self.assertIn("GetFocusedElement", idents)
        self.assertNotIn("if", idents)      # keyword filtered
        self.assertNotIn("rv", idents)      # sub-min_identifier_len (3)


class TestFileMetadata(unittest.TestCase):
    def test_rename(self):
        files = pe.parse_hunks(_RENAME)
        self.assertEqual(files[0].status, "renamed")
        self.assertEqual(files[0].filename, "new/name.cpp")
        self.assertTrue(pe.file_is_cosmetic(files[0]))  # rename, no content

    def test_new_file(self):
        files = pe.parse_hunks(_NEWFILE)
        self.assertEqual(files[0].status, "added")
        self.assertEqual(len(files[0].hunks[0].added_lines), 2)
        self.assertEqual(files[0].hunks[0].added_lines[0], (1, "hello"))

    def test_binary(self):
        files = pe.parse_hunks(_BINARY)
        self.assertTrue(files[0].is_binary)


class TestDerivedSignals(unittest.TestCase):
    def test_cosmetic_reindent(self):
        files = pe.parse_hunks(_COSMETIC)
        self.assertTrue(pe.is_cosmetic(files[0].hunks[0]))
        self.assertTrue(pe.file_is_cosmetic(files[0]))

    def test_logic_change_not_cosmetic_and_null_check_tag(self):
        files = pe.parse_hunks(_NULLCHECK)
        self.assertFalse(pe.file_is_cosmetic(files[0]))
        self.assertIn("null_check", pe.change_tags(files))

    def test_churn(self):
        files = pe.parse_hunks(_NEWFILE)
        c = pe.churn(files)
        self.assertEqual(c["added"], 2)
        self.assertEqual(c["files"], 1)


class TestFetchDegrade(unittest.TestCase):
    def test_fetch_failure_degrades_to_none(self):
        pe._RAW_CACHE.clear()
        with mock.patch.object(pe.requests, "get", side_effect=OSError("boom")):
            self.assertIsNone(pe.fetch_raw_diff("node-deadbeef", "nightly"))

    def test_extract_never_raises_on_fetch_failure(self):
        pe._RAW_CACHE.clear()
        with mock.patch.object(pe.requests, "get", side_effect=OSError("boom")):
            result = pe.extract("node-deadbeef2", "nightly")
        self.assertTrue(result.is_empty())
        self.assertIsNone(result.raw_diff)
        self.assertEqual(result.change_tags(), {"other"})


if __name__ == "__main__":
    unittest.main()
