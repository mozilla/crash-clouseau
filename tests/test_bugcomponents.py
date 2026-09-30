# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_bugcomponents
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import json  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import bugcomponents as bc  # noqa: E402

A11Y, IPC, LAYOUT, XPCOM, WPT = 0, 1, 2, 3, 4
NORMALIZED = {
    "components": {
        "0": ["Core", "Disability Access APIs"],
        "1": ["Core", "DOM: Content Processes"],
        "2": ["Core", "Layout"],
        "3": ["Core", "XPCOM"],
        "4": ["Testing", "web-platform-tests"],
    },
    "paths": {
        "accessible": {
            "generic": {"LocalAccessible.cpp": A11Y, "DocAccessible.cpp": A11Y},
            "ipc": {"DocAccessibleChild.cpp": A11Y, "DocAccessibleParent.cpp": A11Y},
            "tests": {"browser.toml": A11Y},
        },
        "dom": {"ipc": {"BrowserBridgeParent.cpp": IPC, "PBrowserBridge.ipdl": IPC,
                        "WindowGlobalParent.cpp": IPC, "Odd.cpp": LAYOUT}},
        "layout": {"base": {"nsRefreshDriver.cpp": LAYOUT}},
        "xpcom": {"ds": {"nsAtomTable.cpp": XPCOM}, "threads": {"nsThread.cpp": XPCOM}},
        "mozglue": {"misc": {"RWLock_posix.cpp": XPCOM}},
        "testing": {"web-platform": {"a.html": WPT, "b.html": WPT, "c.html": WPT,
                                     "d.html": WPT, "e.html": WPT, "f.html": WPT,
                                     "g.html": WPT, "h.html": WPT, "i.html": WPT}},
        ".clang-format": XPCOM,
    },
}
RULES = bc.compact(NORMALIZED)


class TestCompact(unittest.TestCase):
    def test_every_listed_path_resolves_to_its_component(self):
        def walk(node, prefix):
            for name, v in node.items():
                if isinstance(v, dict):
                    yield from walk(v, prefix + name + "/")
                else:
                    yield prefix + name, tuple(NORMALIZED["components"][str(v)])
        for path, pc in walk(NORMALIZED["paths"], ""):
            with self.subTest(path=path):
                self.assertEqual(bc.component_for(path, RULES), pc)

    def test_a_new_file_takes_its_directory_component(self):
        self.assertEqual(bc.component_for("dom/ipc/NewActor.cpp", RULES),
                         ("Core", "DOM: Content Processes"))
        self.assertEqual(bc.component_for("accessible/new/Thing.cpp", RULES),
                         ("Core", "Disability Access APIs"))

    def test_an_unknown_top_level_directory_resolves_to_nothing(self):
        # The most common component, web-platform-tests, must not become a root fallback.
        self.assertIsNone(bc.component_for("nonexistent/x.cpp", RULES))
        self.assertIsNone(bc.component_for("", RULES))

    def test_a_file_that_differs_from_its_directory_keeps_its_own(self):
        self.assertEqual(bc.component_for("dom/ipc/Odd.cpp", RULES), ("Core", "Layout"))


class TestFileComponents(unittest.TestCase):
    CHANGESET = ["accessible/generic/DocAccessible.cpp", "accessible/ipc/DocAccessibleParent.cpp",
                 "dom/ipc/BrowserBridgeParent.cpp", "dom/ipc/PBrowserBridge.ipdl",
                 "dom/ipc/WindowGlobalParent.cpp", "accessible/tests/browser.toml"]
    STACK = [None, "mozglue/misc/RWLock_posix.cpp", "xpcom/ds/nsAtomTable.cpp",
             "accessible/generic/LocalAccessible.cpp", "accessible/ipc/DocAccessibleChild.cpp",
             "accessible/ipc/DocAccessibleChild.cpp", "accessible/generic/LocalAccessible.cpp",
             "layout/base/nsRefreshDriver.cpp"]

    def test_changeset_stack_and_overlap(self):
        fc = bc.file_components(self.CHANGESET, self.STACK, RULES)
        # Excluding the manifest leaves 3 dom/ipc files and 2 accessible files.
        self.assertEqual(fc["changeset"], ["Core", "DOM: Content Processes"])
        # mozglue is skipped; 4 accessible frames outvote XPCOM and Layout.
        self.assertEqual(fc["stack"], ["Core", "Disability Access APIs"])
        self.assertNotIn("overlap", fc)      # no stack file is in the changeset

    def test_overlap_is_over_stack_files_the_changeset_touches(self):
        fc = bc.file_components(["accessible/ipc/DocAccessibleChild.cpp", "dom/ipc/Odd.cpp"],
                                self.STACK, RULES)
        self.assertEqual(fc["overlap"], ["Core", "Disability Access APIs"])

    def test_the_stack_window_counts_resolved_frames_only(self):
        stack = ["unknown/a.cpp"] * 20 + ["layout/base/nsRefreshDriver.cpp"]
        self.assertEqual(bc.file_components([], stack, RULES), {"stack": ["Core", "Layout"]})
        stack = ["layout/base/nsRefreshDriver.cpp"] * bc.STACK_WINDOW + \
            ["xpcom/ds/nsAtomTable.cpp"] * 20
        self.assertEqual(bc.file_components([], stack, RULES)["stack"], ["Core", "Layout"])

    def test_a_tie_goes_to_the_first_file_listed(self):
        self.assertEqual(bc.majority(["layout/base/nsRefreshDriver.cpp",
                                      "xpcom/ds/nsAtomTable.cpp"], RULES), ("Core", "Layout"))

    def test_nothing_to_map_does_not_load_the_rules(self):
        with mock.patch.object(bc, "_load") as load:
            self.assertEqual(bc.file_components([], [None, "mozglue/misc/x.cpp"]), {})
            self.assertEqual(bc.file_components(["testing/web-platform/a.html"], []), {})
        load.assert_not_called()

    def test_no_rules_means_no_components(self):
        with mock.patch.object(bc, "_load", return_value={}):
            self.assertEqual(bc.file_components(self.CHANGESET, self.STACK), {})


class TestLoad(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for name, value in (("_CACHE_FILE", os.path.join(self.dir.name, "rules.json")),
                            ("_rules", None), ("_rules_at", 0.0)):
            p = mock.patch.object(bc, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _response(self):
        r = mock.Mock()
        r.json.return_value = NORMALIZED
        return r

    def test_fetches_once_then_reads_the_cache_file(self):
        with mock.patch("crashclouseau.net.get", return_value=self._response()) as get:
            self.assertEqual(bc._load(), RULES)
        get.assert_called_once()
        with open(bc._CACHE_FILE) as f:
            self.assertEqual(json.load(f), RULES)
        bc._rules = None                       # simulate a fresh process
        with mock.patch("crashclouseau.net.get") as get:
            self.assertEqual(bc._load(), RULES)
        get.assert_not_called()

    def test_a_failed_fetch_returns_no_rules(self):
        with mock.patch("crashclouseau.net.get", side_effect=RuntimeError("tc down")):
            self.assertEqual(bc._load(), {})
            self.assertIsNone(bc.component_for("dom/ipc/x.cpp"))


if __name__ == "__main__":
    unittest.main()
