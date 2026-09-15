# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Fenix nightly Java/Kotlin stacks (plans/16 §13, D7-D9), pinned on the live processed crash
# 3c426d92-3270-4afc-bf1d-32e8a0260911: Fenix 158.0a1 nightly, build 20260910214118 at
# revision 8590488daa5e, `java.security.ProviderException` out of `Keystore.generateKey`. The
# fixture keeps only the fields the pipeline reads (product, build, java_stack_trace,
# java_exception); `tests/java/Keystore.kt` is the file at that revision, truncated after
# `generateKey` with the class brace closed, so every line number up to 233 is the real one.
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#       uv run python -m unittest tests.test_java_fenix
import itertools
import json
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau import config, db, inspector, java, models, utils  # noqa: E402

_HERE = os.path.dirname(__file__)
_FIXTURE = os.path.join(_HERE, "java", "fenix_3c426d92.json")
_KEYSTORE_KT = os.path.join(_HERE, "java", "Keystore.kt")
_NODE = "8590488daa5e"
_FULL_NODE = "8590488daa5e145a97fb79b385c9f5d515271038"
_GIT_SHA = "88fa72d2f463129e64c2eb5c5227ef20b5c08574"

# The five in-tree paths of the crash's frames (real, from the GitHub tree at the build's git
# commit) and the packages they hold.
_AC = "mobile/android/android-components/components/"
_DP = _AC + "lib/dataprotect/src/main/java/mozilla/components/lib/dataprotect/"
_FXA = _AC + "service/firefox-accounts/src/main/java/mozilla/components/service/fxa/"
_COMPOSE = _AC + "compose/base/src/main/java/mozilla/components/compose/base/"
KEYSTORE = _DP + "Keystore.kt"
SECURE_PREFS = _DP + "SecureAbove22Preferences.kt"
ACCOUNT_STORAGE = _FXA + "AccountStorage.kt"
FXA_MANAGER = _FXA + "manager/FxaAccountManager.kt"
ICON_BUTTON = _COMPOSE + "button/IconButton.kt"
_PATHS = [KEYSTORE, SECURE_PREFS, ACCOUNT_STORAGE, FXA_MANAGER, ICON_BUTTON]
DP_PKG = "mozilla.components.lib.dataprotect."
FXA_PKG = "mozilla.components.service.fxa."
LAMBDA0 = DP_PKG + "SecurePreferencesImpl23$$ExternalSyntheticLambda0"
LAMBDA9 = FXA_PKG + "manager.FxaAccountManager$$ExternalSyntheticLambda9"
ICON_KT_LAMBDA = "mozilla.components.compose.base.button.IconButtonKt$$ExternalSyntheticLambda3"


def _get_full_path(name):
    """`models.File.get_full_path`'s contract over the five real paths: the in-tree path whose
    suffix is `name`, or `name` UNCHANGED on a miss."""
    for p in _PATHS:
        if p.endswith("/" + name):
            return p
    return name


def _load():
    with open(_FIXTURE) as f:
        return json.load(f)


def _keystore_source():
    with open(_KEYSTORE_KT) as f:
        return f.read()


def _raw_file(path, rev, channel="nightly", **kw):
    """hg-edge stand-in: only Keystore.kt is readable, the other files 404. Accepts the
    `retries` / `timeout` keywords `_locate_methods` passes (asserted on separately)."""
    return _keystore_source() if path.endswith("/Keystore.kt") else None


def _inspect(data, java_exception="fixture", **kw):
    if java_exception == "fixture":
        java_exception = data["java_exception"]
    with mock.patch.object(java.hgedge, "raw_file", side_effect=_raw_file) as raw:
        frames, files = java.inspect_java_stacktrace(
            data["java_stack_trace"], _NODE, get_full_path=_get_full_path,
            java_exception=java_exception, channel="nightly", **kw)
    return frames, files, raw


def _text_at_lines(data):
    lines = (ln.strip() for ln in data["java_stack_trace"].split("\n"))
    return [ln for ln in lines if ln.startswith("at ")]


def _by(frames, module, function):
    hits = [f for f in frames if f["module"] == module and f["function"] == function]
    assert hits, (module, function)
    return hits[0]


def _files_table(paths=()):
    """A real (sqlite) `files` table holding `paths` -- `create_all` cannot run here (JSONB
    columns elsewhere), so only this table is created."""
    models.File.__table__.create(db.engine, checkfirst=True)
    db.session.query(models.File).delete()
    db.session.commit()
    if paths:
        models.File.get_ids(list(paths))


class TestTheFixture(unittest.TestCase):
    def test_it_is_the_live_fenix_crash(self):
        data = _load()
        self.assertEqual(data["product"], "Fenix")
        self.assertEqual(data["build"], "20260910214118")
        self.assertEqual(data["release_channel"], "nightly")
        values = data["java_exception"]["exception"]["values"]
        self.assertEqual([v["stacktrace"]["type"] for v in values],
                         ["KeyStoreException", "ProviderException"])
        self.assertEqual([len(v["stacktrace"]["frames"]) for v in values], [23, 20])
        self.assertEqual(len(_text_at_lines(data)), 20)
        self.assertNotIn("json_dump", data)


class TestFrames(unittest.TestCase):
    def setUp(self):
        self.data = _load()
        self.frames, self.files, self.raw = _inspect(self.data)

    def test_our_packages_are_internal_the_platform_is_not(self):
        ours = [f for f in self.frames if f["internal"]]
        self.assertEqual(len(ours), 10)
        self.assertTrue(all(f["module"].startswith("mozilla.components.") for f in ours))
        self.assertTrue(all(f["node"] == _NODE for f in ours))
        theirs = [f for f in self.frames if not f["internal"]]
        self.assertTrue(theirs)
        for f in theirs:
            self.assertTrue(f["module"].startswith(("android.", "javax.", "kotlin", "java.")),
                            f["module"])
            self.assertEqual(f["node"], "")
            self.assertEqual(f["filename"], "")   # `_crashing_area_experts` must not blame it

    def test_the_frame_dict_has_the_native_keys_plus_line_trusted(self):
        native = {"original", "filename", "changesets", "module", "function", "line", "node",
                  "internal", "stackpos"}
        for f in self.frames:
            self.assertEqual(set(f) - {"method_lines"}, native | {"line_trusted"})
            self.assertEqual(f["changesets"], [])
        self.assertEqual([f["stackpos"] for f in self.frames], list(range(len(self.frames))))

    def test_filenames_resolve_by_package_suffix(self):
        frames = self.frames
        self.assertEqual(_by(frames, DP_PKG + "Keystore", "generateKey")["filename"], KEYSTORE)
        self.assertEqual(_by(frames, DP_PKG + "Keystore", "<init>")["filename"], KEYSTORE)
        get_string = _by(frames, DP_PKG + "SecurePreferencesImpl23", "getString")
        self.assertEqual(get_string["filename"], SECURE_PREFS)
        read = _by(frames, FXA_PKG + "SecureAbove22AccountStorage", "read")
        self.assertEqual(read["filename"], ACCOUNT_STORAGE)
        get_account = _by(frames, FXA_PKG + "manager.FxaAccountManager", "getAccount")
        self.assertEqual(get_account["filename"], FXA_MANAGER)
        # a coroutine lambda class whose file IS given
        start = _by(frames, FXA_PKG + "manager.FxaAccountManager$start$2", "invokeSuspend")
        self.assertEqual(start["filename"], FXA_MANAGER)

    def test_a_synthetic_class_borrows_the_file_of_its_outer_class_sibling(self):
        # `SecurePreferencesImpl23` lives in SecureAbove22Preferences.kt (class != file), and the
        # sibling that says so sits BELOW the synthetic frame in the stack.
        lam = _by(self.frames, LAMBDA0, "invoke")
        self.assertEqual(lam["filename"], SECURE_PREFS)
        self.assertTrue(lam["internal"])
        self.assertEqual(_by(self.frames, LAMBDA9, "invoke")["filename"], FXA_MANAGER)

    def test_a_kt_facade_resolves_to_its_file_without_a_sibling(self):
        kt = _by(self.frames, ICON_KT_LAMBDA, "invoke")
        self.assertEqual(kt["filename"], ICON_BUTTON)
        self.assertEqual(kt["original"],
                         "at " + ICON_KT_LAMBDA + ".invoke(R8$$SyntheticClass:43)")

    def test_the_files_are_the_resolved_paths_only(self):
        self.assertEqual(self.files, set(_PATHS))

    def test_the_line_is_the_reported_int_never_minus_one(self):
        self.assertTrue(all(isinstance(f["line"], int) and f["line"] >= 0 for f in self.frames))
        gk = _by(self.frames, DP_PKG + "Keystore", "generateKey")
        self.assertEqual(gk["line"], 269)   # R8's number; the real `fun generateKey` is line 221
        self.assertEqual(gk["original"],
                         "at " + DP_PKG + "Keystore.generateKey(Keystore.kt:269)")

    def test_line_trusted_follows_the_shipped_pref(self):
        self.assertFalse(config.java_trust_line_numbers())
        self.assertTrue(all(f["line_trusted"] is False for f in self.frames))

    def test_line_trusted_true_when_the_pref_is_flipped_and_no_source_is_read(self):
        with mock.patch.object(java.config, "java_trust_line_numbers", return_value=True):
            frames, _, raw = _inspect(self.data)
        self.assertTrue(all(f["line_trusted"] is True for f in frames))
        self.assertFalse(any("method_lines" in f for f in frames))
        raw.assert_not_called()

    def test_the_cause_chain_frames_follow_the_outer_frames(self):
        # 20 outer frames verbatim (they ARE the text), then the root cause's 4 frames the outer
        # exception does not have; its 19 shared tail frames are not repeated.
        self.assertEqual(len(self.frames), 24)
        self.assertEqual([f["original"] for f in self.frames[:20]], _text_at_lines(self.data))
        self.assertEqual([(f["stackpos"], f["function"], f["line"]) for f in self.frames[20:]],
                         [(20, "getKeyStoreException", 336), (21, "handleExceptions", 57),
                          (22, "generateKey", 145), (23, "engineGenerateKey", 400)])
        self.assertEqual(
            sum(1 for f in self.frames if f["module"] == "javax.crypto.KeyGenerator"), 1)

    def test_the_outer_exception_keeps_its_repeated_frames(self):
        # `SynchronizedLazyImpl.getValue(LazyJVM.kt:21)` three times at different depths is three
        # nested lazy initialisations, not a duplicate.
        lazy = [f for f in self.frames[:20] if f["module"] == "kotlin.SynchronizedLazyImpl"]
        self.assertEqual(len(lazy), 3)

    def test_method_lines_come_from_the_source_at_the_build_revision(self):
        ks = DP_PKG + "Keystore"
        self.assertEqual(_by(self.frames, ks, "generateKey")["method_lines"], (221, 233))
        self.assertEqual(_by(self.frames, ks, "<init>")["method_lines"], (192, 234))
        # read once per (file, node), for the resolved files only; synthetic methods are not
        # looked up at all
        read = sorted({c.args[0] for c in self.raw.call_args_list})
        self.assertIn(KEYSTORE, read)
        self.assertNotIn(ICON_BUTTON, read)           # its only frame is a lambda's `invoke`
        self.assertEqual(sum(1 for c in self.raw.call_args_list if c.args[0] == KEYSTORE), 1)
        for c in self.raw.call_args_list:
            self.assertEqual(c.args[1], _NODE)
        # files hg-edge could not serve (the stand-in 404s them) cost their frames the span
        for f in self.frames:
            if f["filename"] != KEYSTORE:
                self.assertNotIn("method_lines", f, f["original"])

    def test_source_reads_get_one_attempt_and_a_short_timeout(self):
        # On the serial scoring chain (and the web dyno for a trigger) hgedge's agent-sized
        # 5 tries / 60 s would turn one 5xx blip into ~17 s per file.
        self.assertTrue(self.raw.call_args_list)
        for c in self.raw.call_args_list:
            self.assertEqual(c.kwargs["retries"], 1)
            connect, read = c.kwargs["timeout"]
            self.assertEqual(connect, java.SOURCE_TIMEOUT[0])
            self.assertTrue(0 < read <= java.SOURCE_TIMEOUT[1], read)

    def test_the_reads_stop_when_the_wall_clock_budget_is_spent(self):
        # Four frames in four resolvable files; a clock advancing 8 s per reading: the third
        # file finds the 20 s budget spent and is not read, nor is the fourth. Their frames
        # simply carry no `method_lines`; the others are unaffected.
        def frame(module, function, filename):
            return {"module": module, "function": function, "filename": filename,
                    "lineno": 1, "in_app": True}
        exc = {"exception": {"values": [{"stacktrace": {"type": "X", "module": "y", "frames": [
            frame(DP_PKG + "Keystore", "generateKey", "Keystore.kt"),
            frame(DP_PKG + "SecureAbove22Preferences", "getString",
                  "SecureAbove22Preferences.kt"),
            frame(FXA_PKG + "AccountStorage", "read", "AccountStorage.kt"),
            frame(FXA_PKG + "manager.FxaAccountManager", "getAccount", "FxaAccountManager.kt"),
        ]}}]}}
        clock = mock.Mock(monotonic=mock.Mock(side_effect=itertools.count(0, 8)))
        raw = mock.Mock(return_value=_keystore_source())
        with mock.patch.object(java, "time", clock), \
                mock.patch.object(java.hgedge, "raw_file", raw), \
                self.assertLogs(java.logger, "WARNING") as logs:
            frames, _ = java.inspect_java_stacktrace(
                None, _NODE, get_full_path=_get_full_path, java_exception=exc)
        self.assertEqual([c.args[0] for c in raw.call_args_list], [KEYSTORE, SECURE_PREFS])
        # the read timeout shrinks to what is left of the budget: 12 s, then 4 s
        self.assertEqual([c.kwargs["timeout"] for c in raw.call_args_list], [(5, 12), (5, 4)])
        self.assertTrue(any("2 source files not read" in m for m in logs.output), logs.output)
        self.assertEqual(frames[0]["method_lines"], (221, 233))
        self.assertNotIn("method_lines", frames[2])
        self.assertNotIn("method_lines", frames[3])
        self.assertEqual(frames[3]["filename"], FXA_MANAGER)    # resolved, just not read

    def test_locate_methods_false_reads_no_source_at_all(self):
        frames, files, raw = _inspect(self.data, locate_methods=False)
        raw.assert_not_called()
        self.assertFalse(any("method_lines" in f for f in frames))
        # everything else is as before: the files resolve, the lines stay untrusted
        self.assertEqual(files, set(_PATHS))
        self.assertEqual(_by(frames, DP_PKG + "Keystore", "generateKey")["filename"], KEYSTORE)
        self.assertTrue(all(f["line_trusted"] is False for f in frames))

    def test_the_text_keeps_the_first_max_frames_only(self):
        # Socorro caps `java_exception` at 50 frames and the native path clamps at 50; the
        # TEXT is not capped (a release StackOverflowError carries 339 `at` lines).
        text = "java.lang.StackOverflowError\n" + "".join(
            "\tat a.b.C.m{0}(C.java:{0})\n".format(i) for i in range(60))
        frames, _ = java.inspect_java_stacktrace(text, _NODE, get_full_path=_get_full_path)
        self.assertEqual(len(frames), java.MAX_FRAMES)
        self.assertEqual(java.MAX_FRAMES, 50)
        self.assertEqual([f["function"] for f in frames], ["m%d" % i for i in range(50)])
        self.assertEqual([f["stackpos"] for f in frames], list(range(50)))

    def test_the_merged_outer_plus_causes_list_keeps_the_first_max_frames_only(self):
        def block(prefix, n):
            return {"stacktrace": {"type": "X", "module": "y", "frames": [
                {"module": "a.b.C", "function": prefix + str(i), "filename": "C.java",
                 "lineno": i, "in_app": False} for i in range(n)]}}
        exc = {"exception": {"values": [block("cause", 30), block("outer", 30)]}}
        frames, _ = java.inspect_java_stacktrace(None, _NODE, get_full_path=_get_full_path,
                                                 java_exception=exc)
        self.assertEqual(len(frames), 50)
        self.assertEqual([f["function"] for f in frames],
                         ["outer%d" % i for i in range(30)] + ["cause%d" % i for i in range(20)])

    def test_an_unresolvable_synthetic_frame_stays_internal_with_no_filename(self):
        exc = {"exception": {"values": [{"stacktrace": {"type": "X", "module": "y", "frames": [
            {"module": "mozilla.components.nowhere.Nope$$ExternalSyntheticLambda0",
             "function": "invoke", "in_app": True, "lineno": 1, "filename": "R8$$SyntheticClass"},
            {"module": "mozilla.components.nowhere.Nope", "function": "run", "in_app": True,
             "lineno": 7, "filename": "Nope.kt"},
        ]}}]}}
        frames, files, _ = _inspect(self.data, java_exception=exc)
        lam, run = frames
        self.assertTrue(lam["internal"] and run["internal"])
        self.assertEqual(lam["filename"], "")
        # an explicit file that misses keeps its package-relative candidate, as it always did
        self.assertEqual(run["filename"], "mozilla/components/nowhere/Nope.kt")
        self.assertEqual(files, {"mozilla/components/nowhere/Nope.kt"})
        self.assertNotIn("method_lines", run)          # never read from hg-edge

    def test_the_text_fallback_when_java_exception_is_absent(self):
        frames, files, _ = _inspect(self.data, java_exception=None)
        self.assertEqual(len(frames), 20)
        self.assertEqual([f["original"] for f in frames], _text_at_lines(self.data))
        self.assertEqual(sum(1 for f in frames if f["internal"]), 10)
        gk = _by(frames, DP_PKG + "Keystore", "generateKey")
        self.assertEqual(gk["filename"], KEYSTORE)
        self.assertEqual(gk["method_lines"], (221, 233))
        self.assertEqual(files, set(_PATHS))

    def test_the_text_fallback_accepts_native_method_and_unknown_source(self):
        text = ("java.lang.RuntimeException\n"
                "\tat org.mozilla.gecko.GeckoThread.run(Native Method)\n"
                "\tat a.b.C.d(Unknown Source:12)\n"
                "\tat a.b.C.e(Unknown Source)\n"
                "\tat a.b.C.f(C.java)\n")
        frames, files = java.inspect_java_stacktrace(text, _NODE, get_full_path=_get_full_path)
        self.assertEqual(len(frames), 4)
        self.assertEqual([f["line"] for f in frames], [0, 12, 0, 0])
        self.assertTrue(frames[0]["internal"])
        self.assertEqual(frames[0]["filename"], "")
        self.assertEqual((frames[0]["module"], frames[0]["function"]),
                         ("org.mozilla.gecko.GeckoThread", "run"))
        self.assertFalse(frames[1]["internal"])
        self.assertEqual(files, set())

    def test_nothing_gives_nothing(self):
        self.assertEqual(java.inspect_java_stacktrace(None, _NODE, get_full_path=_get_full_path),
                         ([], set()))
        self.assertEqual(java.inspect_java_stacktrace("", _NODE, get_full_path=_get_full_path,
                                                      java_exception={}), ([], set()))


class TestHash(unittest.TestCase):
    def test_untrusted_lines_do_not_reach_the_hash(self):
        data = _load()
        frames, _, _ = _inspect(data)
        h = inspector.get_simplified_hash(frames)
        self.assertTrue(h)
        for f in frames:
            f["line"] += 100
        self.assertEqual(inspector.get_simplified_hash(frames), h)
        # ... but the method does: a different top method is a different stack
        frames[2]["function"] = "somethingElse"
        self.assertNotEqual(inspector.get_simplified_hash(frames), h)

    def test_trusted_lines_hash_the_line_as_before(self):
        native = [{"stackpos": 0, "filename": "dom/foo.cpp", "line": 12},
                  {"stackpos": 1, "filename": "", "line": -1},
                  {"stackpos": 2, "filename": "dom/bar.cpp", "line": 7}]
        self.assertEqual(inspector.get_simplified_hash(native),
                         utils.hash("0\ndom/foo.cpp\n12\n2\ndom/bar.cpp\n7\n"))
        java_trusted = [dict(native[0], module="m", function="f", line_trusted=True)]
        self.assertEqual(inspector.get_simplified_hash(java_trusted),
                         utils.hash("0\ndom/foo.cpp\n12\n"))
        java_untrusted = [dict(native[0], module="m", function="f", line_trusted=False)]
        self.assertEqual(inspector.get_simplified_hash(java_untrusted),
                         utils.hash("0\ndom/foo.cpp\nm\nf\n"))


class TestMethodSpan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ks = _keystore_source()

    def test_generate_key_starts_at_221(self):
        self.assertEqual(java.method_span(self.ks, "generateKey", "Keystore"), (221, 233))
        self.assertEqual(java.method_span(self.ks, "generateKey"), (221, 233))

    def test_init_is_the_class_body(self):
        self.assertEqual(java.method_span(self.ks, "<init>", "Keystore"), (192, 234))
        self.assertEqual(java.method_span(self.ks, "<init>", "KeyStoreWrapper"), (46, 173))
        self.assertIsNone(java.method_span(self.ks, "<init>", ""))

    def test_expression_bodied_functions_span_their_statement(self):
        self.assertEqual(java.method_span(self.ks, "available", "Keystore"), (212, 212))
        self.assertEqual(java.method_span(self.ks, "getKey", "Keystore"), (204, 204))
        self.assertEqual(java.method_span(self.ks, "getKeyFor", "KeyStoreWrapper"), (73, 79))

    def test_synthetic_and_unknown_names_are_refused(self):
        for name in ("invoke", "invokeSuspend", "<clinit>", "lambda$foo$0", "access$100",
                     "nope", ""):
            self.assertIsNone(java.method_span(self.ks, name, "Keystore"), name)
        self.assertIsNone(java.method_span("", "generateKey", "Keystore"))

    def test_the_class_narrows_a_name_declared_in_several_classes(self):
        src = ("interface Prefs {\n"
               "    fun getString(key: String): String?\n"
               "}\n"
               "class Above22(private val impl: Prefs) : Prefs {\n"
               "    override fun getString(key: String) = impl.getString(key)\n"
               "}\n"
               "private class Impl23(\n"
               "    context: Context,\n"
               ") : Prefs {\n"
               "    override fun getString(key: String): String? {\n"
               "        val v = prefs.getString(key, null)\n"
               "        return v\n"
               "    }\n"
               "}\n")
        self.assertEqual(java.method_span(src, "getString", "Impl23"), (10, 13))
        self.assertEqual(java.method_span(src, "getString", "Above22"), (5, 5))
        self.assertIsNone(java.method_span(src, "getString", ""))       # ambiguous
        self.assertEqual(java.method_span(src, "<init>", "Impl23"), (7, 14))
        self.assertEqual(java.method_span(src, "<init>", "Impl23$Companion"), (7, 14))

    def test_a_property_accessor_spans_the_member_property_not_a_local(self):
        # `FxaAccountManager.getAccount` is `private val account by lazy { ... }` at 8590488daa5e,
        # and a `val account = ...` inside a method is a different variable.
        src = ("open class Manager(\n"
               "    private val ctx: Context,\n"
               ") {\n"
               "    private val account by lazy {\n"
               "        onDisk.account()\n"
               "    }\n"
               "    val isReady: Boolean = false\n"
               "    fun use(): Boolean {\n"
               "        val account = authenticated() ?: return false\n"
               "        return account.ok\n"
               "    }\n"
               "}\n")
        self.assertEqual(java.method_span(src, "getAccount", "Manager"), (4, 6))
        self.assertEqual(java.method_span(src, "setAccount", "Manager"), (4, 6))
        self.assertEqual(java.method_span(src, "isReady", "Manager"), (7, 7))
        self.assertEqual(java.method_span(src, "getAccount"), (4, 6))
        self.assertIsNone(java.method_span(src, "getNothing", "Manager"))

    def test_java_declarations_not_calls(self):
        src = ("package a;\n"
               "public class Foo {\n"
               "    private int bar(int x) {\n"
               "        return baz(x) + bar2(x);\n"
               "    }\n"
               "    public static synchronized Map<String, Foo> baz(int x)\n"
               "            throws IOException {\n"
               "        bar(x);\n"
               "        return null;\n"
               "    }\n"
               "    abstract void qux(int x);\n"
               "}\n")
        self.assertEqual(java.method_span(src, "bar", "Foo"), (3, 5))
        self.assertEqual(java.method_span(src, "baz", "Foo"), (6, 10))
        self.assertIsNone(java.method_span(src, "qux", "Foo"))       # no body to crash in
        self.assertEqual(java.method_span(src, "<init>", "Foo"), (2, 12))

    def test_overloads_are_ambiguous(self):
        src = ("class Foo {\n"
               "    fun go(x: Int) = x\n"
               "    fun go(x: String) = x.length\n"
               "}\n")
        self.assertIsNone(java.method_span(src, "go", "Foo"))

    def test_a_declaration_quoted_in_a_comment_or_a_string_is_not_one(self):
        # The four verified misfires: each used to yield a span (2, 2) / (3, 3) / (2, 3).
        commented = ("class A {\n"
                     "    // legacy: fun foo() { bar() }\n"
                     "    fun other() {}\n"
                     "}\n")
        self.assertIsNone(java.method_span(commented, "foo", "A"))
        self.assertIsNone(java.method_span(commented, "foo"))
        quoted = ("class A {\n"
                  "    val legacy = \"fun foo() { bar() }\"\n"
                  "    fun other() {}\n"
                  "}\n")
        self.assertIsNone(java.method_span(quoted, "foo", "A"))
        asserted = ("class A {\n"
                    "    void other() {\n"
                    "        assert foo(1) == 2;\n"
                    "    }\n"
                    "}\n")
        self.assertIsNone(java.method_span(asserted, "foo", "A"))   # `==` is no body `=`
        elsewhere = ("class A {\n"
                     "    fun a() = 1\n"
                     "    elsewhere()\n"
                     "    fun b() = 2\n"
                     "}\n")
        self.assertEqual(java.method_span(elsewhere, "a", "A"), (2, 2))   # not `else`
        self.assertEqual(java.method_span(elsewhere, "b", "A"), (4, 4))
        # ... and a real continuation still continues
        chained = ("class A {\n"
                   "    fun a() = if (x) 1\n"
                   "    else 2\n"
                   "    fun b() = 2\n"
                   "}\n")
        self.assertEqual(java.method_span(chained, "a", "A"), (2, 3))

    def test_a_commented_copy_does_not_make_the_real_declaration_ambiguous(self):
        # `_pick` returns None on two candidates: the comment's bogus span used to hide the
        # real `fun foo` right below it.
        src = ("class A {\n"
               "    // was: fun foo() = old()\n"
               "    fun foo() {\n"
               "        new()\n"
               "    }\n"
               "    /* fun foo() { } */\n"
               "}\n")
        self.assertEqual(java.method_span(src, "foo", "A"), (3, 5))
        # the positive control on the real file: KDoc above `generateKey` mentions the key
        self.assertEqual(java.method_span(self.ks, "generateKey", "Keystore"), (221, 233))
        # a class name inside a comment does not confuse the class lookup either
        src = "// class Foo was here\nclass Foo {\n    fun go() = 1\n}\n"
        self.assertEqual(java.method_span(src, "go", "Foo"), (3, 3))
        self.assertEqual(java.method_span(src, "<init>", "Foo"), (2, 4))

    def test_masking_keeps_every_offset_and_newline(self):
        src = 'a "x\ny" b // c\n/* d\ne */ f \'}\' g\n'
        masked = java._mask_literals(src)
        self.assertEqual(len(masked), len(src))
        self.assertEqual(masked.count("\n"), src.count("\n"))
        self.assertEqual(masked, 'a   \n   b     \n    \n     f     g\n')

    def test_braces_in_strings_and_comments_do_not_count(self):
        src = ("class Foo {\n"
               "    fun go(): String {\n"
               "        val s = \"${x} } {\"  // } stray\n"
               "        /* { */\n"
               "        return s + '}'\n"
               "    }\n"
               "    fun other() = 1\n"
               "}\n")
        self.assertEqual(java.method_span(src, "go", "Foo"), (2, 6))
        self.assertEqual(java.method_span(src, "other", "Foo"), (7, 7))


class TestGetCrashInfo(unittest.TestCase):
    """The Java branch of `inspector.get_crash_info`, with the REAL `File.get_full_path` over a
    sqlite `files` table holding the five paths; the native branch is untouched."""

    def setUp(self):
        _files_table(_PATHS)
        self.data = _load()
        self.mindate = datetime(2026, 9, 7, tzinfo=timezone.utc)
        self.buildid = utils.get_build_date(self.data["build"])

    def _run(self, data, filelog, chgsets=None, **kw):
        with mock.patch.object(java.hgedge, "raw_file", side_effect=_raw_file) as raw:
            self.raw = raw
            return inspector.get_crash_info(
                data, "3c426d92-3270-4afc-bf1d-32e8a0260911", self.buildid, "nightly",
                self.mindate, _NODE, filelog, chgsets if chgsets is not None else set(), **kw)

    def _offstack(self, enabled):
        cfg = dict(config.get_agent_offstack(), enabled=enabled)
        return mock.patch.object(inspector.config, "get_agent_offstack", return_value=cfg)

    def test_stored_offstack_when_no_candidate_touched_a_frame_file_and_offstack_is_on(self):
        with self._offstack(True):
            res = self._run(self.data, lambda *a, **k: {})
        self.assertEqual(set(res), {"java"})
        self.assertTrue(res["java"]["offstack"])
        self.assertTrue(res["java"]["hash"])
        frames = res["java"]["frames"]
        self.assertEqual(len(frames), 24)
        self.assertEqual(res["java"]["hash"], inspector.get_simplified_hash(frames))
        # the real resolver, on a real table: the borrow and the Kt facade both landed
        self.assertEqual(_by(frames, LAMBDA0, "invoke")["filename"], SECURE_PREFS)
        self.assertEqual(_by(frames, ICON_KT_LAMBDA, "invoke")["filename"], ICON_BUTTON)

    def test_dropped_when_no_candidate_touched_a_frame_file_and_offstack_is_off(self):
        with self._offstack(False):
            self.assertEqual(self._run(self.data, lambda *a, **k: {}), {})

    def test_the_filelog_is_asked_for_the_resolved_files_and_a_hit_is_onstack(self):
        asked = {}

        def filelog(files, mindate, buildid, channel):
            asked.update(files=set(files), mindate=mindate, buildid=buildid, channel=channel)
            return {KEYSTORE: ["abcdef123456"]}

        chgsets = set()
        with self._offstack(False):
            res = self._run(self.data, filelog, chgsets)
        self.assertEqual(asked, {"files": set(_PATHS), "mindate": self.mindate,
                                 "buildid": self.buildid, "channel": "nightly"})
        self.assertFalse(res["java"]["offstack"])
        self.assertEqual(chgsets, {"abcdef123456"})
        scored = [f for f in res["java"]["frames"] if f["changesets"]]
        self.assertEqual({f["filename"] for f in scored}, {KEYSTORE})
        self.assertEqual(len(scored), 2)                       # generateKey and <init>
        self.assertEqual(scored[0]["method_lines"], (221, 233))    # what the scorer reads next

    def test_source_reads_false_stores_the_stack_without_touching_hg_edge(self):
        # The method rung is a refinement of the score, not a prerequisite: a caller on the
        # web dyno (the trigger API) gets the same stack, minus `method_lines`, with no HTTP.
        with self._offstack(True):
            res = self._run(self.data, lambda *a, **k: {}, source_reads=False)
        self.raw.assert_not_called()
        self.assertEqual(set(res), {"java"})
        frames = res["java"]["frames"]
        self.assertEqual(len(frames), 24)
        self.assertFalse(any("method_lines" in f for f in frames))
        self.assertEqual(_by(frames, DP_PKG + "Keystore", "generateKey")["filename"], KEYSTORE)
        # the default still reads
        with self._offstack(True):
            self._run(self.data, lambda *a, **k: {})
        self.raw.assert_called()

    def test_get_crash_forwards_source_reads(self):
        with mock.patch.object(inspector, "get_crash_data", return_value=self.data), \
                mock.patch.object(inspector, "get_crash_info", return_value={}) as info:
            inspector.get_crash("u", self.buildid, "nightly", self.mindate, _NODE,
                                models.Changeset.find, set(), source_reads=False)
            self.assertEqual(info.call_args.kwargs, {"source_reads": False})
            inspector.get_crash("u", self.buildid, "nightly", self.mindate, _NODE,
                                models.Changeset.find, set())
            self.assertEqual(info.call_args.kwargs, {"source_reads": True})

    def test_no_internal_frame_and_no_json_dump_is_none(self):
        platform = {"java_stack_trace": ("java.lang.OutOfMemoryError\n"
                                         "\tat java.util.Arrays.copyOf(Arrays.java:3161)\n"
                                         "\tat java.lang.Thread.run(Thread.java:1012)\n")}
        with self._offstack(True):
            self.assertIsNone(self._run(platform, lambda *a, **k: {}))

    def test_no_internal_frame_with_a_json_dump_falls_through_to_the_native_stack(self):
        data = {
            "java_stack_trace": ("java.lang.OutOfMemoryError\n"
                                 "\tat java.lang.Thread.run(Thread.java:1012)\n"),
            "json_dump": {"crash_info": {"crashing_thread": 0}, "threads": [{"frames": [
                {"function": "mozilla::Foo", "line": 10,
                 "file": "hg:hg.mozilla.org/mozilla-central:dom/foo.cpp:" + _NODE},
            ]}]},
        }
        with self._offstack(True):
            res = self._run(data, lambda *a, **k: {})
        self.assertEqual(set(res), {"nonjava"})
        self.assertEqual(res["nonjava"]["frames"][0]["filename"], "dom/foo.cpp")

    def test_an_internal_java_frame_wins_over_a_json_dump(self):
        dump = {"crash_info": {"crashing_thread": 0},
                "threads": [{"frames": [{"function": "x", "line": 1}]}]}
        with self._offstack(True):
            res = self._run(dict(self.data, json_dump=dump), lambda *a, **k: {})
        self.assertEqual(set(res), {"java"})


def _tree(entries):
    return {"sha": "t", "url": "u", "truncated": False,
            "tree": [{"path": p, "type": t, "sha": "s", "mode": "100644"} for p, t in entries]}


class TestRefreshFileIndex(unittest.TestCase):
    """`refresh_file_index` against a real (sqlite) `files` table; HTTP and Lando mocked."""

    ENTRIES = [(KEYSTORE[len(java.JVM_ROOT):], "blob"),
               ("fenix/app/src/main/java/org/mozilla/fenix/Legacy.java", "blob"),
               ("fenix/app/build.gradle.kts", "blob"),                      # not a source
               ("fenix/app/src/main/res/values/strings.xml", "blob"),      # not JVM
               ("android-components/components", "tree"),                  # a directory
               ("fenix/app/src/main/java/org/mozilla/fenix/Dir.kt", "tree")]  # a dir named .kt
    EXPECTED = {KEYSTORE, "mobile/android/fenix/app/src/main/java/org/mozilla/fenix/Legacy.java"}

    def setUp(self):
        _files_table()
        self.lando = mock.Mock()
        self.lando.hg2git.return_value = mock.Mock(git_hash=_GIT_SHA, hg_hash=_FULL_NODE)
        self.urls = []
        self.headers = []        # the headers of each GitHub request, in order
        self.marks = {}          # the `sweepmarks` table (Postgres-only upsert, so a dict)
        self.row_id = 7          # `builds.id` of the newest Fenix build

    def _get(self, tree=None, tree_status=200, rev_status=200, remaining="0"):
        def get(url, **kw):
            self.urls.append(url)
            r = mock.Mock()
            if "/json-rev/" in url:
                r.status_code = rev_status
                r.json.return_value = {"node": _FULL_NODE}
            else:
                self.headers.append(kw.get("headers") or {})
                r.status_code = tree_status
                r.headers = {"X-RateLimit-Remaining": remaining}
                r.json.return_value = tree if tree is not None else _tree(self.ENTRIES)
            return r
        return get

    def _tree_requests(self):
        return [u for u in self.urls if "api.github.com" in u]

    def _refresh(self, get, bid="20260910214118", node=_NODE):
        bid = utils.get_build_date(bid) if bid else None

        def set_mark(name, position, commit=True):
            self.marks[name] = position

        with mock.patch.object(models.Build, "get_max_buildid", return_value=bid), \
                mock.patch.object(models.Build, "get_changeset", return_value=node), \
                mock.patch.object(java, "_build_row_id", return_value=self.row_id), \
                mock.patch.object(models.SweepMark, "get",
                                  side_effect=lambda name: self.marks.get(name, 0)), \
                mock.patch.object(models.SweepMark, "set", side_effect=set_mark), \
                mock.patch.object(java, "_LANDO", self.lando), \
                mock.patch("crashclouseau.net.get", side_effect=get):
            return java.refresh_file_index("nightly", "Fenix")

    def _names(self):
        return {r[0] for r in db.session.query(models.File.name)}

    def test_only_jvm_sources_are_indexed_under_mobile_android_and_a_rerun_adds_nothing(self):
        self.assertEqual(self._refresh(self._get()), 2)
        self.assertEqual(self._names(), self.EXPECTED)
        self.assertEqual(models.File.get_full_path(DP_PKG.replace(".", "/") + "Keystore.kt"),
                         KEYSTORE)
        # the 12-char build node is expanded before Lando sees it, and the tree is pinned to
        # the build's git commit
        self.lando.hg2git.assert_called_once_with(_FULL_NODE)
        self.assertTrue(any("/json-rev/" + _NODE in u for u in self.urls))
        tree_url = java.GITHUB_TREE_URL.format(_GIT_SHA)
        self.assertTrue(any(u.startswith(tree_url) for u in self.urls))
        self.assertEqual(self._refresh(self._get()), 0)
        self.assertEqual(self._names(), self.EXPECTED)

    def test_chunks(self):
        paths = ["mobile/android/x/F{}.kt".format(i) for i in range(1001)]
        self.assertEqual(java._add_missing_files(paths), 1001)
        self.assertEqual(len(self._names()), 1001)
        self.assertEqual(java._add_missing_files(paths + ["mobile/android/x/New.java"]), 1)

    def test_a_rate_limited_or_missing_tree_logs_and_adds_nothing(self):
        for status in (403, 404):
            with self.assertLogs(java.logger, "WARNING") as logs:
                self.assertEqual(self._refresh(self._get(tree_status=status)), 0)
            self.assertTrue(any("GitHub tree" in m for m in logs.output))
        self.assertEqual(self._names(), set())
        self.assertEqual(self.marks, {})

    def test_the_same_newest_build_is_indexed_once_per_build_not_per_tick(self):
        # `update_builds` calls this every tick (3/h): the mark makes the second call free.
        self.assertEqual(self._refresh(self._get()), 2)
        self.assertEqual(self._refresh(self._get()), 0)
        self.assertEqual(self._refresh(self._get()), 0)
        self.assertEqual(len(self._tree_requests()), 1)
        self.assertEqual(sum(1 for u in self.urls if "/json-rev/" in u), 1)  # no Lando either
        (name, position), = self.marks.items()
        self.assertTrue(name.startswith(java.FILE_INDEX_MARK), name)
        self.assertIn("Fenix", name)
        self.assertLessEqual(len(name), 32)                  # sweepmarks.name is String(32)
        self.assertEqual(position, self.row_id)
        # a NEW build (a new builds.id) is indexed again
        self.row_id = 8
        self.assertEqual(self._refresh(self._get()), 0)      # same tree: nothing new to add
        self.assertEqual(len(self._tree_requests()), 2)
        self.assertEqual(self.marks[name], 8)

    def test_a_403_leaves_the_mark_unset_so_the_next_tick_retries(self):
        with self.assertLogs(java.logger, "WARNING") as logs:
            self.assertEqual(self._refresh(self._get(tree_status=403, remaining="0")), 0)
        self.assertTrue(any("X-RateLimit-Remaining 0" in m for m in logs.output), logs.output)
        self.assertEqual(self.marks, {})
        self.assertEqual(self._refresh(self._get()), 2)      # the next tick
        self.assertEqual(len(self._tree_requests()), 2)
        self.assertEqual(list(self.marks.values()), [self.row_id])

    def test_the_github_token_is_sent_only_when_set(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_t0k"}):
            self._refresh(self._get())
        self.assertEqual(self.headers, [{"Authorization": "Bearer ghp_t0k"}])
        self.row_id = 8
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
        with mock.patch.dict(os.environ, env, clear=True):
            self._refresh(self._get())
        self.assertEqual(len(self.headers), 2)
        self.assertNotIn("Authorization", self.headers[1])

    def test_a_refused_token_falls_back_to_the_anonymous_request(self):
        """Live 2026-09-15, first token set on the app: `/rate_limit` said 5,000 remaining and
        every repo endpoint answered 403 "The 'Mozilla Corporation' enterprise forbids access
        via a fine-grained personal access tokens if the token's lifetime is greater than 366
        days". A refused token must not be worse than none: the tree is public."""
        message = ("The 'Mozilla Corporation' enterprise forbids access via a fine-grained "
                   "personal access tokens if the token's lifetime is greater than 366 days.")
        plain = self._get()

        def get(url, **kw):
            if "api.github.com" in url and (kw.get("headers") or {}).get("Authorization"):
                self.urls.append(url)
                self.headers.append(kw["headers"])
                r = mock.Mock()
                r.status_code = 403
                r.headers = {"X-RateLimit-Remaining": "4996"}
                r.json.return_value = {"message": message, "status": "403"}
                return r
            return plain(url, **kw)

        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "github_pat_long_lived"}), \
                self.assertLogs(java.logger, "WARNING") as logs:
            self.assertEqual(self._refresh(get), 2)
        self.assertEqual(len(self._tree_requests()), 2)      # refused with, then without
        self.assertIn("Authorization", self.headers[0])
        self.assertNotIn("Authorization", self.headers[1])
        self.assertTrue(any("366 days" in m and "anonymously" in m for m in logs.output),
                        logs.output)
        self.assertEqual(list(self.marks.values()), [self.row_id])
        # An anonymous 403 (the shared-IP budget) is still a miss, with GitHub's reason.
        self.marks.clear()

        def refused_twice(url, **kw):
            r = plain(url, **kw)
            if "api.github.com" in url:
                r.status_code = 403
                r.json.return_value = {"message": "API rate limit exceeded for 1.2.3.4."}
            return r

        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "github_pat_long_lived"}), \
                self.assertLogs(java.logger, "WARNING") as logs:
            self.assertEqual(self._refresh(refused_twice), 0)
        self.assertTrue(any("rate limit exceeded" in m for m in logs.output), logs.output)
        self.assertEqual(self.marks, {})

    def test_no_build_row_no_request(self):
        self.assertEqual(self._refresh(self._get(), bid=None), 0)
        self.assertEqual(self.urls, [])
        self.assertEqual(self._refresh(self._get(), node=None), 0)
        self.assertEqual(self.urls, [])

    def test_a_node_lando_does_not_know_adds_nothing(self):
        from libmozdata.lando import LandoMissingCommit
        self.lando.hg2git.side_effect = LandoMissingCommit("nope")
        with self.assertLogs(java.logger, "WARNING"):
            self.assertEqual(self._refresh(self._get()), 0)
        self.assertFalse(any("api.github.com" in u for u in self.urls))

    def test_it_never_raises(self):
        def boom(url, **kw):
            raise OSError("network down")
        with self.assertLogs(java.logger, "WARNING"):
            self.assertEqual(self._refresh(boom), 0)

    def test_a_truncated_tree_is_indexed_and_warned_about(self):
        tree = dict(_tree(self.ENTRIES), truncated=True)
        with self.assertLogs(java.logger, "WARNING") as logs:
            self.assertEqual(self._refresh(self._get(tree=tree)), 2)
        self.assertTrue(any("truncated" in m for m in logs.output))

    def test_populate_java_files_is_the_fenix_nightly_refresh(self):
        with mock.patch.object(java, "refresh_file_index", return_value=3) as refresh:
            self.assertEqual(java.populate_java_files(), 3)
        refresh.assert_called_once_with("nightly", "Fenix")


if __name__ == "__main__":
    unittest.main()
