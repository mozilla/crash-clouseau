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


FENIX = "mobile/android/fenix/app/src/main/java/org/mozilla/fenix"
AC = "mobile/android/android-components/components"
ANDROID_RULES = {"mobile/": ["Firefox for Android", "General"],
                 "mobile/android/geckoview/": ["GeckoView", "General"]}
ACTIVE = {"Accounts and Sync", "Downloads", "Tabs", "Contextual AI", "General"}


def _commits(*bugs, files=2):
    return [{"messageHeadline": "Bug {} - change. r=x".format(b), "changedFilesIfAvailable": files}
            for b in bugs]


FFA = "Firefox for Android"
ADDONS = FENIX + "/addons/InstalledAddonDetailsFragment.kt"
TABS_UI = FENIX + "/tabstray/ui/TabManagementFragment.kt"
THEME = FENIX + "/theme/FirefoxTheme.kt"
KEYSTORE = AC + "/lib/dataprotect/src/main/java/mozilla/components/lib/dataprotect/Keystore.kt"
STORAGE = AC + "/service/firefox-accounts/src/main/java/mozilla/components/service/fxa/" \
    "AccountStorage.kt"
ICON = AC + "/compose/base/src/main/java/mozilla/components/compose/base/button/IconButton.kt"
RELAY = AC + "/service/fxrelay/src/main/java/mozilla/components/service/fxrelay/FxRelay.kt"
GECKO_RESULT = "mobile/android/geckoview/src/main/java/org/mozilla/geckoview/GeckoResult.java"
BINDINGS = "toolkit/components/uniffi-bindgen-gecko-js/android/components/relay/android/src/" \
    "main/java/mozilla/appservices/relay/relay.kt"
JVM_RULES = {
    "mobile/": [FFA, "General"],
    FENIX + "/addons/": [FFA, "WebExtensions"],
    FENIX + "/tabstray/": [FFA, "Tabs"],
    FENIX + "/theme/": [FFA, "Design System and Theming"],
    AC + "/compose/base/": [FFA, "Design System and Theming"],
    AC + "/service/firefox-accounts/": [FFA, "Accounts and Sync"],
    AC + "/service/fxrelay/": [FFA, "Privacy"],
    "mobile/android/geckoview/": ["GeckoView", "General"],
    "toolkit/components/uniffi-bindgen-gecko-js/": ["Toolkit", "UniFFI Bindings"],
}


def _jf(module, function, filename="", internal=True):
    return {"module": module, "function": function, "filename": filename, "internal": internal}


class TestJvmRanking(unittest.TestCase):
    def _rank(self, *chain):
        frame, files = bc.jvm_ranking(list(chain), JVM_RULES)
        return (frame and "{}.{}".format(frame["module"], frame["function"])), files

    def test_the_innermost_exception_with_our_code_wins(self):
        cause = [_jf("org.mozilla.fenix.addons.InstalledAddonDetailsFragment", "enableButtons",
                     ADDONS),
                 _jf("org.mozilla.geckoview.GeckoResult", "lambda$thenInternal$6", GECKO_RESULT)]
        outer = [_jf("org.mozilla.geckoview.GeckoResult", "dispatchLocked", GECKO_RESULT)] * 8
        self.assertEqual(self._rank(cause, outer),
                         ("org.mozilla.fenix.addons.InstalledAddonDetailsFragment.enableButtons",
                          [ADDONS, GECKO_RESULT]))

    def test_an_exception_without_our_code_defers_to_the_next_one(self):
        cause = [_jf("android.security.KeyStore2", "getSecurityLevel", internal=False)]
        outer = [_jf("android.security.keystore2.Spi", "engineGenerateKey", internal=False),
                 _jf("mozilla.components.lib.dataprotect.Keystore", "generateKey", KEYSTORE)]
        self.assertEqual(self._rank(cause, outer)[0],
                         "mozilla.components.lib.dataprotect.Keystore.generateKey")

    def test_generated_bindings_and_r8_synthetics_rank_last(self):
        frames = [
            _jf("mozilla.appservices.relay.RelayKt", "uniffiCheckCallStatus", BINDINGS),
            _jf("mozilla.components.service.fxa.Storage$$ExternalSyntheticApiModelOutline0",
                "m", STORAGE),
            # No other frame shares this lambda's outer class.
            _jf("mozilla.components.compose.base.button.IconButtonKt"
                "$$ExternalSyntheticLambda3", "invoke", ICON),
            _jf("mozilla.components.service.fxrelay.FxRelayImpl", "fetchProfile", RELAY),
        ]
        self.assertEqual(self._rank(frames),
                         ("mozilla.components.service.fxrelay.FxRelayImpl.fetchProfile",
                          [RELAY, BINDINGS, STORAGE, ICON]))

    def test_a_lambda_whose_class_has_other_frames_counts(self):
        frames = [_jf("org.mozilla.fenix.tabstray.ui.TabManagementFragment"
                      "$$ExternalSyntheticLambda11", "invoke", TABS_UI),
                  _jf("org.mozilla.fenix.theme.FirefoxThemeKt", "FirefoxTheme", THEME),
                  _jf("org.mozilla.fenix.tabstray.ui.TabManagementFragment"
                      "$$ExternalSyntheticLambda3", "invoke", TABS_UI)]
        self.assertEqual(bc.component_for(bc.jvm_ranking([frames], JVM_RULES)[0]["filename"],
                                          JVM_RULES), (FFA, "Tabs"))

    def test_skipped_frames_alone_give_the_first_of_them(self):
        frames = [_jf("java.lang.String", "substring", internal=False),
                  _jf("mozilla.appservices.relay.RelayKt", "uniffiCheckCallStatus", BINDINGS)]
        self.assertEqual(self._rank(frames),
                         ("mozilla.appservices.relay.RelayKt.uniffiCheckCallStatus", [BINDINGS]))

    def test_a_general_frame_is_not_traded_for_an_owned_caller(self):
        frames = [_jf("mozilla.components.lib.dataprotect.Keystore", "generateKey", KEYSTORE),
                  _jf("mozilla.components.service.fxa.SecureAbove22AccountStorage", "read",
                      STORAGE)]
        self.assertEqual(self._rank(frames),
                         ("mozilla.components.lib.dataprotect.Keystore.generateKey",
                          [KEYSTORE, STORAGE]))

    def test_unresolved_and_platform_frames_give_nothing(self):
        frames = [_jf("android.os.Parcel", "createException", internal=False),
                  _jf("org.mozilla.fenix.Gone", "run", "org/mozilla/fenix/Gone.kt"),
                  _jf("org.mozilla.fenix.Synthetic$$ExternalSyntheticLambda0", "invoke")]
        self.assertEqual(self._rank(frames), (None, []))
        self.assertEqual(self._rank(), (None, []))

    def test_ranked_files_give_their_first_component_not_the_most_common(self):
        stack = [KEYSTORE, STORAGE, STORAGE, ICON]
        self.assertEqual(bc.file_components([], stack, JVM_RULES)["stack"],
                         [FFA, "Accounts and Sync"])
        fc = bc.file_components([ICON, STORAGE], stack, JVM_RULES, ranked=True)
        self.assertEqual(fc["stack"], [FFA, "General"])
        self.assertEqual(fc["overlap"], [FFA, "Accounts and Sync"])


class TestHistoryComponent(unittest.TestCase):
    """Bug numbers encode their component: 1xx Accounts and Sync, 2xx Downloads, 3xx Tabs,
    4xx Contextual AI, 5xx General, 6xx GeckoView :: General."""
    SYNC = AC + "/service/firefox-accounts/src/main/java/mozilla/components/service/fxa/FxA.kt"
    STORE = AC + "/lib/dataprotect/src/main/java/mozilla/components/lib/dataprotect/Store.kt"

    def setUp(self):
        self.histories = {}
        p = mock.patch.object(bc, "_path_histories",
                              side_effect=lambda paths: {k: self.histories.get(k, [])
                                                         for k in paths})
        self.fetch = p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _components(ids):
        names = {1: "Accounts and Sync", 2: "Downloads", 3: "Tabs", 4: "Contextual AI",
                 5: "General"}
        return {b: ("GeckoView", "General") if b // 100 == 6 else
                ("Firefox for Android", names[b // 100]) for b in ids}

    def _run(self, paths, active=ACTIVE):
        return bc.history_component("Firefox for Android", paths, self._components,
                                    lambda product: active, rules=ANDROID_RULES)

    def test_the_bugs_that_changed_the_files_name_the_component(self):
        self.histories = {self.SYNC: _commits(101, 102, 103, 301, 501),
                          self.STORE: _commits(104, 105, 106, 107, 201)}
        self.assertEqual(self._run(["android.os.Foo", self.STORE, self.SYNC]), "Accounts and Sync")

    def test_bulk_changes_and_reverts_do_not_vote(self):
        bulk = _commits(501, 502, 503, 504, 505, 506, 507, files=2426)
        reverts = [{"messageHeadline": 'Revert "Bug 508 - x"', "changedFilesIfAvailable": 1},
                   {"messageHeadline": "Backed out changeset abc (Bug 509)",
                    "changedFilesIfAvailable": 1},
                   {"messageHeadline": "No Bug - Import translations", "changedFilesIfAvailable": 3}]
        self.histories = {self.SYNC: bulk + reverts + _commits(101, 102, 103, 104, 105)}
        self.assertEqual(self._run([self.SYNC]), "Accounts and Sync")
        self.histories = {self.SYNC: bulk + reverts + _commits(101), FENIX: _commits(102)}
        self.assertIsNone(self._run([self.SYNC]))

    def test_one_busy_file_does_not_outvote_the_others(self):
        # Each file gets one vote regardless of its number of bugs; ties have no winner.
        browser, closed = FENIX + "/browser/BrowserFragment.kt", AC + "/feature/recentlyclosed/R.kt"
        self.histories = {browser: _commits(*range(401, 411)), closed: _commits(301, 302, 303,
                                                                                304, 305)}
        self.assertIsNone(self._run([browser, closed]))

    def test_general_a_minority_or_too_few_bugs_name_nothing(self):
        self.histories = {self.SYNC: _commits(501, 502, 503, 101, 102)}
        self.assertIsNone(self._run([self.SYNC]))
        self.histories = {self.SYNC: _commits(101, 102, 201, 202, 301, 302, 401, 402)}
        self.assertIsNone(self._run([self.SYNC]))
        self.histories = {self.SYNC: _commits(101, 102)}
        self.assertIsNone(self._run([self.SYNC]))

    def test_shared_bugs_count_once_toward_the_minimum(self):
        paths = [self.SYNC, self.STORE, self.STORE.replace("Store.kt", "Other.kt")]
        for bugs in ((101,), (101, 102)):
            with self.subTest(bugs=bugs):
                self.histories = {p: _commits(*bugs) for p in paths}
                self.assertIsNone(self._run(paths))
        self.histories = {p: _commits(101, 102, 103) for p in paths}
        self.assertEqual(self._run(paths), "Accounts and Sync")

    def test_an_inactive_or_unknown_component_names_nothing(self):
        self.histories = {self.SYNC: _commits(101, 102, 103, 104, 105)}
        self.assertIsNone(self._run([self.SYNC], active={"General", "Tabs"}))
        self.assertIsNone(self._run([self.SYNC], active=None))

    def test_a_thin_file_borrows_its_nearest_directories_history(self):
        fxa = self.SYNC.rsplit("/", 1)[0]
        self.histories = {self.SYNC: _commits(101), fxa: _commits(102, 103, 104, 105, 301)}
        self.assertEqual(self._run([self.SYNC]), "Accounts and Sync")
        dirs = self.fetch.call_args_list[1][0][0]
        self.assertIn(fxa, dirs)
        self.assertEqual(len(dirs), bc.HISTORY_DIR_LEVELS)
        self.assertEqual(bc._parent_dirs("dom/media/platforms/wmf/WMF.cpp",
                                         {"dom/media/": ["Core", "Audio/Video"]}),
                         ["dom/media/platforms/wmf", "dom/media/platforms", "dom/media"])

    def test_other_products_files_and_bugs_do_not_vote(self):
        gv = "mobile/android/geckoview/src/main/java/org/mozilla/geckoview/GeckoRuntime.java"
        self.histories = {gv: _commits(601, 602, 603, 604, 605),
                          self.SYNC: _commits(101, 102, 103, 601, 602)}
        self.assertEqual(self._run([gv, self.SYNC]), "Accounts and Sync")
        self.assertNotIn(gv, self.fetch.call_args_list[0][0][0])
        self.assertIsNone(self._run([gv]))
        self.assertIsNone(self._run(["mobile/android/fenix/app/src/test/java/T.kt"]))

    def test_unavailable_history_or_bugs_name_nothing(self):
        self.fetch.side_effect = None
        self.fetch.return_value = None
        self.assertIsNone(self._run([self.SYNC]))
        self.fetch.side_effect = lambda paths: {k: _commits(101, 102, 103, 104, 105) for k in paths}
        self.assertIsNone(bc.history_component("Firefox for Android", [self.SYNC],
                                               mock.Mock(side_effect=RuntimeError("bmo down")),
                                               lambda product: ACTIVE, rules=ANDROID_RULES))


class TestPathHistories(unittest.TestCase):
    def setUp(self):
        from tests import REAL_PATH_HISTORIES
        self.real = REAL_PATH_HISTORIES

    def test_without_a_token_github_is_not_asked(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch("crashclouseau.net.post") as post:
            os.environ.pop("GITHUB_TOKEN", None)
            self.assertIsNone(self.real(["a/b.kt"]))
        post.assert_not_called()

    def test_one_query_maps_each_path_to_its_commits(self):
        r = mock.Mock(status_code=200)
        r.json.return_value = {"data": {"repository": {"object": {
            "h0": {"nodes": _commits(1)}, "h1": {"nodes": []}}}}}
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t"}), \
                mock.patch("crashclouseau.net.post", return_value=r) as post:
            self.assertEqual(self.real(["a/b.kt", "a"]), {"a/b.kt": _commits(1), "a": []})
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["variables"], {"p0": "a/b.kt", "p1": "a"})
        self.assertIn("history(first: {}, path: $p1)".format(bc.HISTORY_COMMITS), body["query"])
        self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer t"})

    def test_a_refusal_is_no_history(self):
        for status, payload in ((502, None), (200, {"errors": [{"message": "rate limited"}]})):
            r = mock.Mock(status_code=status, text="<html>502</html>")
            r.json.return_value = payload
            with self.subTest(status=status), mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t"}), \
                    mock.patch("crashclouseau.net.post", return_value=r):
                self.assertIsNone(self.real(["a/b.kt"]))


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
