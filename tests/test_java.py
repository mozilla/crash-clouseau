# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# The Fennec-era fixtures (2017 `org.mozilla.gecko` release stacks, parsed from the
# `java_stack_trace` TEXT): they pin the shape a TRUSTED line produces -- `line_trusted` True,
# no `method_lines`, the reported line kept as-is -- so the pref is mocked true here. The
# shipped pref is false (Fenix APKs are R8-minified) and `tests/test_java_fenix.py` pins that
# shape on a live Fenix crash.
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#       uv run python -m unittest tests.test_java
from functools import partial
import json
from os import listdir
from os.path import join
import re
import unittest
from unittest import mock
from crashclouseau import buildhub, config, java


class JavaTest(unittest.TestCase):
    # Show the whole diff output when assertion fails
    maxDiff = None

    def readfile(self, filename):
        with open(filename, "r") as In:
            return json.load(In)

    def get_files(self, path):
        pat = re.compile(r"stack\.[0-9]+\.json")
        for f in listdir(path):
            if pat.match(f):
                full = join(path, f)
                yield full

    @staticmethod
    def get_full_path(java_files, filename):
        pat = re.compile(r".*/" + filename)
        for f in java_files:
            if pat.match(f):
                return f

    def test(self):
        java_files = [
            "mobile/android/base/java/org/mozilla/gecko/GeckoApp.java",
            "mobile/android/base/java/org/mozilla/gecko/BrowserApp.java",
            "mobile/android/base/java/org/mozilla/gecko/home/BrowserSearch.java",
            "mobile/android/base/java/org/mozilla/gecko/home/TwoLinePageRow.java",
            "mobile/android/base/java/org/mozilla/gecko/home/MultiTypeCursorAdapter.java",
            "mobile/android/base/java/org/mozilla/gecko/widget/themed/ThemedListView.java",
            "mobile/android/base/java/org/mozilla/gecko/widget/RecyclerViewClickSupport.java",
            "mobile/android/base/java/org/mozilla/gecko/activitystream/homepanel/StreamRecyclerAdapter.java",
        ]

        for f in self.get_files("./tests/java"):
            data = self.readfile(f)
            with mock.patch.object(config, "java_trust_line_numbers", return_value=True):
                stack, files = java.inspect_java_stacktrace(
                    data["stack"],
                    "tip",
                    get_full_path=partial(JavaTest.get_full_path, java_files),
                )
            self.assertEqual(stack, data["frames"])
            self.assertEqual(list(sorted(files)), data["files"])
            # stack.2 has 72 `at` lines: the text is not capped by Socorro (a release
            # StackOverflowError carries 339), the frames are, like a native stack's 50.
            at_lines = [ln for ln in data["stack"].split("\n") if ln.strip().startswith("at ")]
            self.assertEqual(len(stack), min(len(at_lines), java.MAX_FRAMES))
            self.assertTrue(all(frame["line_trusted"] for frame in stack))
            self.assertFalse(any("method_lines" in frame for frame in stack))

            # Fennec builds were `FennecAndroid` on Buildhub; `Fenix` is the default now.
            reformatted = java.reformat_java_stacktrace(
                data["stack"],
                data["channel"],
                data["buildid"],
                product="FennecAndroid",
                get_full_path=partial(JavaTest.get_full_path, java_files),
                get_changeset=buildhub.get_rev_from,
            )
            self.assertEqual(reformatted, data["reformatted"])
