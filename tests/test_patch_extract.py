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

_COMMENT = """diff --git a/c.cpp b/c.cpp
--- a/c.cpp
+++ b/c.cpp
@@ -1,1 +1,3 @@ void f()
   int x = 1;
+  // explain the thing
+  /* and another */
"""

_DOC = """diff --git a/docs/readme.md b/docs/readme.md
--- a/docs/readme.md
+++ b/docs/readme.md
@@ -1,1 +1,2 @@
 title
+more prose
"""

# Regression (#15 review): `*out = x;` (pointer deref) and `#define` (preprocessor)
# must NOT be classified as comment-only — they are real, semantic code changes.
_PTR_WRITE = """diff --git a/p.cpp b/p.cpp
--- a/p.cpp
+++ b/p.cpp
@@ -1,1 +1,1 @@ void f(int* out)
-  *out = 1;
+  *out = 2;
"""

_PREPROC = """diff --git a/q.cpp b/q.cpp
--- a/q.cpp
+++ b/q.cpp
@@ -1,1 +1,1 @@
-#define MAX 10
+#define MAX 20
"""

# Extract-method refactor (the canary FP class, bug 2023670): a block is DELETED from the
# caller (hunk 1, replaced by a call) and re-ADDED as a new function (hunk 2). Per-hunk it is
# not cosmetic (add != delete within a hunk), but across the patch the lines are the same code
# relocated -> code motion, very unlikely to be a regressor.
_EXTRACT_METHOD = """diff --git a/e.cpp b/e.cpp
--- a/e.cpp
+++ b/e.cpp
@@ -10,5 +10,1 @@ void Caller::run()
-  int x = compute(a, b);
-  if (x > 0) { doThing(x); }
-  logResult(x, name);
-  total += finalize(x);
-  helper(x, y, z);
+  runExtracted(a, b, name);
@@ -40,0 +37,7 @@ void Caller::run()
+int Caller::runExtracted(int a, int b, Name name) {
+  int x = compute(a, b);
+  if (x > 0) { doThing(x); }
+  logResult(x, name);
+  total += finalize(x);
+  helper(x, y, z);
+}
"""

# A real behavior change touching the same area: only a couple of lines change and the added
# lines are genuinely new logic (not relocated) -> NOT code motion.
_REAL_CHANGE = """diff --git a/r.cpp b/r.cpp
--- a/r.cpp
+++ b/r.cpp
@@ -10,4 +10,6 @@ void Bar::Do()
   Foo* ptr = Get();
-  ptr->Use();
+  if (!ptr) { return NS_ERROR_FAILURE; }
+  ptr->Use();
+  ptr->Flush();
+  Cleanup(ptr);
   Done();
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

    def test_comment_only_and_doc(self):
        cfiles = pe.parse_hunks(_COMMENT)
        self.assertTrue(pe.is_comment_only(cfiles[0].hunks[0]))
        self.assertTrue(pe.file_is_comment_only(cfiles[0]))
        self.assertFalse(pe.file_is_cosmetic(cfiles[0]))  # added comments != reflow
        self.assertTrue(pe.file_is_doc(pe.parse_hunks(_DOC)[0]))

    def test_pointer_deref_and_preproc_not_comment(self):
        self.assertFalse(pe.is_comment_only(pe.parse_hunks(_PTR_WRITE)[0].hunks[0]))
        self.assertFalse(pe.is_comment_only(pe.parse_hunks(_PREPROC)[0].hunks[0]))
        ext = pe.PatchExtraction(node="n", channel="c", raw_diff="x",
                                 files=pe.parse_hunks(_PTR_WRITE))
        self.assertFalse(ext.is_inert())

    def test_is_inert_and_is_cosmetic(self):
        def ext(raw):
            return pe.PatchExtraction(node="n", channel="nightly", raw_diff="x",
                                      files=pe.parse_hunks(raw))
        self.assertTrue(ext(_COMMENT).is_inert())        # comment-only
        self.assertFalse(ext(_COMMENT).is_cosmetic())    # not a pure reflow
        self.assertTrue(ext(_COSMETIC).is_cosmetic())
        self.assertTrue(ext(_COSMETIC).is_inert())       # cosmetic implies inert
        self.assertTrue(ext(_DOC).is_inert())            # doc-only
        self.assertFalse(ext(_NULLCHECK).is_inert())     # real logic change
        self.assertFalse(ext("").is_inert())             # empty is not "inert"

    def test_is_refactor_code_motion(self):
        def ext(raw):
            return pe.PatchExtraction(node="n", channel="nightly", raw_diff="x",
                                      files=pe.parse_hunks(raw))
        # extract-method: across-hunk relocation -> refactor, but NOT cosmetic (per-hunk add
        # != delete). This is the canary refactor-blame FP class we want to down-rank.
        em = ext(_EXTRACT_METHOD)
        self.assertTrue(em.is_refactor())
        self.assertFalse(em.is_cosmetic())
        # a real behavior change in the same area is NOT flagged as motion
        self.assertFalse(ext(_REAL_CHANGE).is_refactor())
        # a null-check add (a couple of new lines) is not motion either
        self.assertFalse(ext(_NULLCHECK).is_refactor())
        # tiny diffs never trip it
        self.assertFalse(ext(_COSMETIC).is_refactor())

    def test_refactor_note_surfaced_in_diff_tool(self):
        from crashclouseau.agent.tools.patch import _fmt_patch
        em = pe.PatchExtraction(node="n", channel="nightly", raw_diff="x",
                                files=pe.parse_hunks(_EXTRACT_METHOD))
        out = _fmt_patch(em, "n")
        self.assertIn("code MOTION", out)          # refactor NOTE fires
        self.assertNotIn("cosmetic-only", out)     # not mislabeled as cosmetic

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
        with mock.patch.object(pe.RawRevision, "get_revision", side_effect=OSError("boom")):
            self.assertIsNone(pe.fetch_raw_diff("node-deadbeef", "nightly"))

    def test_extract_never_raises_on_fetch_failure(self):
        pe._RAW_CACHE.clear()
        with mock.patch.object(pe.RawRevision, "get_revision", side_effect=OSError("boom")):
            result = pe.extract("node-deadbeef2", "nightly")
        self.assertTrue(result.is_empty())
        self.assertIsNone(result.raw_diff)
        self.assertEqual(result.change_tags(), {"other"})


if __name__ == "__main__":
    unittest.main()
