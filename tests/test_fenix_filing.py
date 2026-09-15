# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Fenix nightly (plans/16 §13): triaged, FILING HELD -- and what the filers, the spike sweep,
the bug preview and the venue split do with a product that shares desktop's channel label.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
        uv run python -m unittest tests.test_fenix_filing

Pure logic against the SHIPPED config (`agent.autofile.products.Fenix`: armed 2026-09-15 evening
at `skip`, cap 2, on the first culprit -- 35e32be2, `nsTSubstring<T>::Truncate |
gfxPlatform::ReportTelemetry` at 85; `default_product: Firefox`) and against a HELD Fenix through
a patched config: a product hold binds the two unattended filers and the sweep, Firefox files
exactly as before, a JVM report's R8 lines are never printed as fact, and desktop `Firefox` bugs
are foreign to a Fenix crash. No network.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import bugzilla_apply, config, feedback, models, report_bug, sigage  # noqa: E402
from crashclouseau import inspector  # noqa: E402
from crashclouseau.agent import orchestrator, spike_escalation as se  # noqa: E402
from crashclouseau.logger import logger  # noqa: E402
from tests.test_autofile import _INFO, _Base, _bug  # noqa: E402
from tests.test_spike_escalation import _FilerBase, _esc  # noqa: E402

# Crash 3c426d92-3270-4afc-bf1d-32e8a0260911 (Fenix nightly 158.0a1, 2026-09-11): the same
# signature/channel as `_INFO` so every mock of `_Base` applies, and Fenix's own product.
_FENIX_INFO = {**_INFO, "product": "Fenix", "version": "158.0a1",
               "buildid": "20260910214118", "java": True}
_HELD = "autofile held for product 'Fenix' (triage-only)"

# `java_exception` of that crash, trimmed to the frames the shape needs: the cause first, the
# THROWN exception last (its frames are `java_stack_trace`'s), one R8 synthetic frame among
# them. Fetched anonymously from the ProcessedCrash API on 2026-09-15.
_JAVA_RAW = {
    "product": "Fenix", "release_channel": "nightly", "version": "158.0a1",
    "signature": "java.security.ProviderException: at android.security.keystore2."
                 "AndroidKeyStoreKeyGeneratorSpi.engineGenerateKey("
                 "AndroidKeyStoreKeyGeneratorSpi.java)",
    "java_stack_trace": "java.security.ProviderException\n\tat android.security.keystore2."
                        "AndroidKeyStoreKeyGeneratorSpi.engineGenerateKey("
                        "AndroidKeyStoreKeyGeneratorSpi.java:413)\n",
    "java_exception": {"exception": {"values": [
        {"stacktrace": {"type": "KeyStoreException", "module": "android.security", "frames": [
            {"module": "android.security.KeyStore2", "function": "getKeyStoreException",
             "in_app": True, "lineno": 336, "filename": "KeyStore2.java"},
        ]}},
        {"stacktrace": {"type": "ProviderException", "module": "java.security", "frames": [
            {"module": "android.security.keystore2.AndroidKeyStoreKeyGeneratorSpi",
             "function": "engineGenerateKey", "in_app": True, "lineno": 413,
             "filename": "AndroidKeyStoreKeyGeneratorSpi.java"},
            {"module": "javax.crypto.KeyGenerator", "function": "generateKey",
             "in_app": True, "lineno": 612, "filename": "KeyGenerator.java"},
            {"module": "mozilla.components.lib.dataprotect.Keystore", "function": "generateKey",
             "in_app": True, "lineno": 269, "filename": "Keystore.kt"},
            {"module": "mozilla.components.lib.dataprotect.Keystore", "function": "<init>",
             "in_app": True, "lineno": 51, "filename": "Keystore.kt"},
            {"module": "mozilla.components.lib.dataprotect.SecurePreferencesImpl23"
                       "$$ExternalSyntheticLambda0",
             "function": "invoke", "in_app": True, "lineno": 14, "filename": "R8$$SyntheticClass"},
            {"module": "kotlin.SynchronizedLazyImpl", "function": "getValue",
             "in_app": True, "lineno": 21, "filename": "LazyJVM.kt"},
        ]}},
    ]}},
}

_NATIVE_RAW = {
    "product": "Firefox", "version": "158.0a1",
    "json_dump": {"crash_info": {"crashing_thread": 0}, "threads": [{"frames": [
        {"function": "Foo::Bar", "file": "hg:hg.mozilla.org/mozilla-central:dom/Foo.cpp:abc123",
         "line": 3, "module": "xul.dll"}]}]},
}


def _held_fenix():
    """The shipped agent block with Fenix HELD (`enabled: false`), as it shipped on 2026-09-15
    afternoon before Calixte armed it that evening."""
    agent = dict(config.get_agent())
    autofile = dict(agent["autofile"])
    autofile["products"] = {**autofile["products"], "Fenix": {"enabled": False}}
    agent["autofile"] = autofile
    return mock.patch.object(config, "get_agent", return_value=agent)


class TestTheShippedConfig(unittest.TestCase):
    def test_fenix_is_declared_and_armed_and_firefox_is_the_default(self):
        self.assertTrue(config.autofile_product_declared("Fenix"))
        self.assertFalse(config.autofile_product_held("Fenix"))
        self.assertTrue(config.autofile_product_declared("Firefox"))
        self.assertFalse(config.autofile_product_held("Firefox"))
        with _held_fenix():
            self.assertTrue(config.autofile_product_declared("Fenix"))
            self.assertTrue(config.autofile_product_held("Fenix"))
        # A product nobody has decided about, and no product at all: undeclared, not held --
        # two different silences, and the filer fails closed on the first.
        for product in ("Focus", "Thunderbird", None, ""):
            with self.subTest(product=product):
                self.assertFalse(config.autofile_product_declared(product))
                self.assertFalse(config.autofile_product_held(product))

    def test_the_global_arm_arms_fenix_at_its_own_policy_and_does_not_arm_a_held_product(self):
        with mock.patch.dict(os.environ, {"AUTOFILE_BUGS": "1"}):
            self.assertTrue(config.autofile_globally_enabled())
            self.assertTrue(config.get_agent_autofile("nightly")["enabled"])
            self.assertTrue(config.get_agent_autofile("nightly", product="Firefox")["enabled"])
            fenix = config.get_agent_autofile("nightly", product="Fenix")
            self.assertTrue(fenix["enabled"])
            self.assertEqual((fenix["comment_on_existing"], fenix["daily_cap"]), ("skip", 2))
            with _held_fenix():
                self.assertFalse(config.get_agent_autofile("nightly", product="Fenix")["enabled"])


class _FenixBase(_Base):
    def setUp(self):
        super().setUp()
        # No dossier row on sqlite: `run_options` would log a traceback and answer `{}` anyway.
        ro = mock.patch.object(models.Dossier, "run_options", return_value={})
        ro.start()
        self.addCleanup(ro.stop)


class TestTheOrdinaryFilerFilesFenixAsShipped(_FenixBase):
    """Armed 2026-09-15 evening: a Fenix lead, against the SHIPPED config, passes the product
    gate and is filed (every BMO write stubbed by `_Base`)."""

    def test_the_shipped_armed_fenix_files_a_lead(self):
        with mock.patch.dict(os.environ, {"AUTOFILE_BUGS": "1"}):
            res = bugzilla_apply.autofile_bug("u-1", _FENIX_INFO, {},
                                              {"candidate": {"node": "n"}}, "lead", 90)
        self.assertTrue(res["filed"], res.get("skipped"))
        self.assertEqual((res["bug"], res["product"]), (999, "Fenix"))
        self.assertEqual(len(self.created), 1)


class TestTheOrdinaryFilerHoldsFenix(_FenixBase):
    """`_Base` arms the policy (`get_agent_autofile` -> enabled) and stubs every BMO request, so
    what stops a Fenix filing here is the product predicate alone -- read from the 2026-09-15
    afternoon config (Fenix HELD) through `_held_fenix`; the shipped one is armed."""

    def setUp(self):
        super().setUp()
        held = _held_fenix()
        held.start()
        self.addCleanup(held.stop)

    def _file_fenix(self, verdict="lead", confidence=90):
        return bugzilla_apply.autofile_bug("u-1", _FENIX_INFO, {}, {"candidate": {"node": "n"}},
                                           verdict, confidence)

    def test_a_fenix_lead_is_held_and_nothing_is_written(self):
        with mock.patch.dict(os.environ, {"AUTOFILE_BUGS": "1"}):
            res = self._file_fenix()
        self.assertFalse(res["filed"])
        self.assertEqual(res["skipped"], _HELD)
        self.assertEqual(res["product"], "Fenix")
        self.assertEqual((self.created, self.comments, self.puts), ([], [], []))
        # Distinct from the string `orchestrator._autofile` suppresses, so the decline is
        # RECORDED: the held week's count is what the arm decision will be made on.
        self.assertNotEqual(res["skipped"], "autofile disabled")

    def test_the_hold_binds_a_trigger_with_file_bug_true(self):
        # `POST /api/tasks/trigger {"file_bug": true}` sets `run_options.autofile: True`; the
        # hold is read BEFORE the run options, so nothing an operator can post gets past it.
        for opts in ({"autofile": True}, {"autofile": False}, {}):
            with self.subTest(opts=opts), \
                    mock.patch.object(models.Dossier, "run_options", return_value=opts), \
                    mock.patch.object(config, "autofile_channel_declared",
                                      side_effect=AssertionError("the channel gate ran")):
                res = self._file_fenix()
            self.assertEqual(res["skipped"], _HELD)
            self.assertEqual(self.created, [])

    def test_the_hold_reaches_the_persisted_decline(self):
        # The orchestrator's choke point records every skip but "autofile disabled".
        recorded = []
        with mock.patch.object(models.CrashStack, "get_by_uuid",
                               return_value=({"frames": []}, dict(_FENIX_INFO))), \
                mock.patch.object(models.Dossier, "record_filing_decline",
                                  side_effect=lambda u, i: recorded.append(i)):
            orchestrator._autofile("u-1", {"dossier": {"candidate": {"node": "n"}}},
                                   {"verdict": "lead", "confidence": 90})
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["skipped"], _HELD)
        self.assertEqual(recorded[0]["channel"], "nightly")

    def test_a_firefox_crash_takes_the_pre_existing_path(self):
        res = self._file()
        self.assertTrue(res["filed"])
        self.assertEqual((res["bug"], res["mode"], res["product"]), (999, "new_bug", "Firefox"))
        self.assertEqual(len(self.created), 1)
        # The policy and the cap are asked for the crash's own product: `product="Firefox"`
        # merges no overlay, so both answers are what the one-argument calls gave.
        bugzilla_apply.config.get_agent_autofile.assert_called_once_with(
            "nightly", product="Firefox")
        models.Dossier.filed_bugs_since.assert_called_once_with(
            mock.ANY, channel="nightly", product="Firefox")
        # ...and the product is on the persisted record, next to the channel.
        self.assertEqual(self.filed[0][1]["product"], "Firefox")

    def test_the_gate_order_is_unchanged_for_firefox(self):
        # The operator's `file_bug: false` is still the first gate a Firefox run meets
        # (tests/test_trigger_api pins the same), so the product hold sits above it without
        # displacing it.
        with mock.patch.object(models.Dossier, "run_options", return_value={"autofile": False}), \
                mock.patch.object(config, "autofile_channel_declared",
                                  side_effect=AssertionError("the channel gate ran first")):
            res = self._file()
        self.assertIn("filing disabled for this run", res["skipped"])
        self.assertEqual(self.created, [])

    def test_an_undeclared_product_files_nothing(self):
        # Fails CLOSED like the channel gate: Focus the day it is ingested, or a `uuid_info`
        # with no product, must not inherit nightly's armed policy through the shared label.
        for product in ("Focus", "Thunderbird", None, ""):
            with self.subTest(product=product):
                info = {**_INFO, "product": product}
                res = bugzilla_apply.autofile_bug("u-1", info, {}, {"candidate": {"node": "n"}},
                                                  "lead", 90)
                self.assertFalse(res["filed"])
                self.assertEqual(res["skipped"],
                                 "product {!r} has no autofile configuration".format(product))
                self.assertEqual(self.created, [])


class TestTheSpikeFilerHoldsFenix(_FilerBase):
    """`_FilerBase` turns the global switch ON and stubs BMO, so only the product predicate
    stands between a Fenix spike and a bug. The per-channel hold is bypassed for spikes by
    design (`config.autofile_globally_enabled`); the per-product one is not."""

    def test_a_held_fenix_spike_is_held(self):
        with _held_fenix():
            res = se.file_spike_bug(_esc(product="Fenix"), dict(self.brief, product="Fenix"),
                                    self.findings, grounded=True)
        self.assertFalse(res["filed"])
        self.assertEqual(res["skipped"], _HELD)
        self.assertEqual((self.created, self.comments), ([], []))
        # Not a transient decline: `_retry_filings` re-calls this for `retry: True` rows and a
        # hold clears by a config edit, not by waiting.
        self.assertNotIn("retry", res)

    def test_the_shipped_fenix_spike_passes_the_product_gate(self):
        res = se.file_spike_bug(_esc(product="Fenix"), dict(self.brief, product="Fenix"),
                                self.findings, grounded=True)
        self.assertNotEqual(res.get("skipped"), _HELD)

    def test_an_undeclared_product_is_held_the_same_way(self):
        res = se.file_spike_bug(_esc(product="Focus"), dict(self.brief, product="Focus"),
                                self.findings, grounded=True)
        self.assertEqual(res["skipped"], "autofile held for product 'Focus' (triage-only)")
        self.assertEqual(self.created, [])

    def test_a_firefox_spike_still_files(self):
        with mock.patch.object(config, "autofile_product_held",
                               wraps=config.autofile_product_held) as held:
            res = se.file_spike_bug(_esc(), self.brief, self.findings, grounded=True)
        self.assertTrue(res["filed"], res.get("skipped"))
        held.assert_called_once_with("Firefox")
        # `product` on the spike record stays the BMO destination, as `html.py` reads it.
        self.assertEqual((res["product"], self.created[0]["product"]), ("Core", "Core"))


class TestTheSweepSkipsAHeldProduct(unittest.TestCase):
    def setUp(self):
        self.swept = []
        patches = [
            mock.patch.object(se, "_reap_stale", return_value=None),
            mock.patch.object(se, "_retry_filings", return_value=0),
            mock.patch.object(se, "_sweep_channel",
                              side_effect=lambda p, c, cfg, room: self.swept.append((p, c)) or 0),
            mock.patch.object(config, "get_agent_spike_escalation",
                              return_value=dict(config.get_agent_spike_escalation(),
                                                enabled=True)),
            mock.patch.object(config, "get_agent_channels", return_value=["nightly", "beta"]),
            mock.patch.object(config, "get_agent_products", return_value=["Firefox", "Fenix"]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_fenix_is_not_swept_while_its_filing_is_held(self):
        # An escalation exists to FILE: an Opus-xhigh run whose every result `file_spike_bug`
        # would decline is money for a brief nobody reads. Logged, because the tick leaves no
        # other trace of a product it left out. (The shipped config is armed; this is the
        # 2026-09-15 afternoon hold, through a patched config.)
        with _held_fenix(), self.assertLogs(logger, level="INFO") as logs:
            se.sweep_real_spikes()
        self.assertEqual(self.swept, [("Firefox", "nightly"), ("Firefox", "beta")])
        self.assertTrue(any("Fenix is not swept" in line and "held" in line
                            for line in logs.output), logs.output)

    def test_the_armed_fenix_is_swept_on_its_own_channels_only(self):
        # Shipped: Fenix is nightly-only (`product_channels`), so a beta sweep for it would
        # read an empty selection log.
        se.sweep_real_spikes()
        self.assertEqual(self.swept,
                         [("Firefox", "nightly"), ("Firefox", "beta"), ("Fenix", "nightly")])

    def test_the_sweep_reads_the_agents_products_not_the_enum(self):
        # `get_products` also defines `PRODUCT_TYPE`, so Fenix entered it before it could be
        # ingested at all; `AGENT_PRODUCTS=Firefox` must stop the spike spend too.
        with mock.patch.object(config, "get_agent_products", return_value=["Firefox"]), \
                mock.patch.object(config, "get_products", return_value=["Firefox", "Fenix"]), \
                mock.patch.object(config, "autofile_product_held", return_value=False):
            se.sweep_real_spikes()
        self.assertEqual(self.swept, [("Firefox", "nightly"), ("Firefox", "beta")])

    def test_an_undeclared_product_is_not_swept_either(self):
        with mock.patch.object(config, "get_agent_products", return_value=["Firefox", "Focus"]), \
                self.assertLogs(logger, level="INFO") as logs:
            se.sweep_real_spikes()
        self.assertEqual(self.swept, [("Firefox", "nightly"), ("Firefox", "beta")])
        self.assertTrue(any("Focus is not swept" in line and "no autofile configuration" in line
                            for line in logs.output), logs.output)


class TestTheSpikeBriefKnowsAJavaStack(unittest.TestCase):
    def test_frames_come_from_the_thrown_exception_without_r8_synthetics(self):
        frames = se._frames_from_dump(_JAVA_RAW)
        functions = [f["function"] for f in frames]
        # `values[-1]`, the ProviderException that was thrown -- not the KeyStoreException
        # cause at `values[0]` -- and in Socorro's innermost-first order.
        self.assertEqual(functions[0], "android.security.keystore2."
                                       "AndroidKeyStoreKeyGeneratorSpi.engineGenerateKey")
        self.assertNotIn("android.security.KeyStore2.getKeyStoreException", functions)
        self.assertIn("mozilla.components.lib.dataprotect.Keystore.generateKey", functions)
        # The R8 synthetic lambda class has no source file and is dropped; the numbering stays
        # contiguous because it is the prompt's `#n`, not Socorro's.
        self.assertNotIn("R8$$SyntheticClass", [f["filename"] for f in frames])
        self.assertEqual([f["stackpos"] for f in frames], list(range(len(frames))))
        keystore = next(f for f in frames if f["function"].endswith("Keystore.generateKey"))
        self.assertEqual((keystore["filename"], keystore["line"], keystore["module"],
                          keystore["node"], keystore["changesets"]),
                         ("Keystore.kt", 269, "", "", {}))
        # The line is what Socorro reports and the frame says it is not to be trusted while
        # the pref is off; flipping the pref re-labels it.
        self.assertFalse(config.java_trust_line_numbers())
        self.assertFalse(keystore["line_trusted"])
        with mock.patch.object(config, "java_trust_line_numbers", return_value=True):
            self.assertTrue(se._frames_from_dump(_JAVA_RAW)[0]["line_trusted"])

    def test_a_native_report_takes_the_minidump_path(self):
        frames = se._frames_from_dump(_NATIVE_RAW)
        self.assertEqual([(f["function"], f["filename"], f["line"], f["module"]) for f in frames],
                         [("Foo::Bar", "dom/Foo.cpp", 3, "xul.dll")])
        self.assertNotIn("line_trusted", frames[0])
        # No minidump AND no Java exception: nothing to read.
        self.assertEqual(se._frames_from_dump({"product": "Fenix"}), [])
        self.assertEqual(se._frames_from_dump(None), [])

    def _seed(self, raw, uuid_info, stack_text):
        patches = [
            mock.patch.object(inspector, "get_crash_data", return_value=raw),
            mock.patch.object(models.CrashStack, "get_by_uuid", return_value=({}, uuid_info)),
            mock.patch.object(orchestrator, "_stack_text", side_effect=stack_text),
            mock.patch.object(orchestrator, "_signature_trend", return_value={}),
            mock.patch.object(orchestrator, "_version_rates", return_value={}),
            mock.patch.object(orchestrator, "_hardware_noise", return_value={}),
            mock.patch.object(sigage, "signature_history", return_value={}),
            mock.patch.object(sigage, "first_seen_ever", return_value={}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        product = (raw or {}).get("product") or "Firefox"
        return se._minimal_seed("u-1", uuid_info, _esc(product=product))

    def test_the_minimal_seed_carries_the_two_java_keys(self):
        calls = []

        def stack_text(frames, **kw):
            calls.append(kw)
            return "rendered"
        # No stored stack (`java` False on the row) but a JVM report: the raw decides.
        seed = self._seed(_JAVA_RAW, {"java": False, "node": ""}, stack_text)
        self.assertTrue(seed["java"])
        self.assertFalse(seed["line_numbers_trusted"])
        self.assertEqual(seed["frames"][0]["filename"], "AndroidKeyStoreKeyGeneratorSpi.java")
        self.assertEqual(seed["stack"], "rendered")
        # The orchestrator's renderer is told the lines are R8-remapped (its Fenix contract).
        self.assertEqual(calls, [{"line_numbers_trusted": False}])

    def test_a_desktop_seed_is_byte_identical(self):
        calls = []

        def stack_text(frames, **kw):
            calls.append(kw)
            return "rendered"
        seed = self._seed(_NATIVE_RAW, {"java": False, "node": ""}, stack_text)
        self.assertFalse(seed["java"])
        self.assertTrue(seed["line_numbers_trusted"])
        # The one-argument call, exactly as before.
        self.assertEqual(calls, [{}])

    def test_a_stored_java_stack_is_java_even_when_the_fetch_fails(self):
        seed = self._seed(None, {"java": True, "node": ""}, lambda frames, **kw: "")
        self.assertTrue(seed["java"])
        self.assertFalse(seed["line_numbers_trusted"])
        self.assertEqual(seed["stack"], "")


class TestThePreviewHidesAnUntrustedLine(unittest.TestCase):
    _STACK = {"frames": [
        {"stackpos": 0, "function": "mozilla.components.lib.dataprotect.Keystore.generateKey",
         "filename": "mobile/android/android-components/components/lib/dataprotect/src/main/"
                     "java/mozilla/components/lib/dataprotect/Keystore.kt",
         "line": 269, "module": "", "line_trusted": False},
        {"stackpos": 1, "function": "Foo::bar", "filename": "dom/Foo.cpp", "line": 51,
         "module": "xul.dll"},
    ]}

    def test_an_r8_line_is_not_printed_as_fact_and_a_native_one_is(self):
        block = report_bug.build_frames_block(self._STACK)
        self.assertIn("Keystore.kt", block)
        self.assertNotIn("Keystore.kt:269", block)
        self.assertNotIn(":269", block)
        self.assertIn("1  xul.dll  Foo::bar  dom/Foo.cpp:51", block)

    def test_a_trusted_java_line_is_printed(self):
        # `CrashStack.get_by_uuid` stamps the flag from the pref at read time, so flipping
        # `java.trust_line_numbers` re-labels history -- and the preview follows the flag, not
        # the language.
        stack = {"frames": [dict(self._STACK["frames"][0], line_trusted=True)]}
        self.assertIn("Keystore.kt:269", report_bug.build_frames_block(stack))


class TestTheVenueSplitForAFenixCrash(unittest.TestCase):
    """The android family's extra clause (`config._FAMILY_ONLY_FOREIGN`): desktop `Firefox` is
    foreign to a Fenix crash -- bug 1681745 `Firefox :: Installer` was a Fenix venue before --
    while `Firefox for Android`, `GeckoView` and `Core` are ours. Desktop's set is unchanged."""

    _BUGS = [_bug(1681745, product="Firefox"), _bug(1855806, product="Firefox for Android"),
             _bug(3, product="GeckoView"), _bug(4, product="Core"),
             _bug(2057980, product="MailNews Core")]

    def _ids(self, pair):
        return tuple([b["id"] for b in side] for side in pair)

    def test_desktop_firefox_is_foreign_to_a_fenix_crash(self):
        ours, theirs = self._ids(bugzilla_apply._split_by_application(self._BUGS, "Fenix"))
        self.assertEqual(ours, [1855806, 3, 4])
        self.assertEqual(theirs, [1681745, 2057980])

    def test_a_firefox_crash_and_an_unknown_product_are_unchanged(self):
        for product in ("Firefox", None):
            with self.subTest(product=product):
                ours, theirs = self._ids(
                    bugzilla_apply._split_by_application(self._BUGS, product))
                self.assertEqual(ours, [1681745, 1855806, 3, 4])
                self.assertEqual(theirs, [2057980])

    def test_resolve_product_component_refuses_a_desktop_pair_for_fenix(self):
        pairs = {1681745: ("Firefox", "Installer"), 1855806: ("Firefox for Android", "General")}
        with mock.patch.object(report_bug, "_bugs_product_component",
                               side_effect=lambda ids: {i: pairs[i] for i in ids if i in pairs}), \
                mock.patch.object(models.Node, "authors_for", return_value={}):
            desktop = {"bug": 1681745, "node": "n"}
            mobile = {"bug": 1855806, "node": "n"}
            self.assertEqual(report_bug.resolve_product_component(desktop, "nightly", "Fenix"),
                             (None, None))
            self.assertEqual(report_bug.resolve_product_component(desktop, "nightly", "Firefox"),
                             ("Firefox", "Installer"))
            self.assertEqual(report_bug.resolve_product_component(mobile, "nightly", "Fenix"),
                             ("Firefox for Android", "General"))
            # GeckoView is shared, so a desktop crash may still land there (bug 1855806's
            # signature is one of the measured colliders).
            self.assertEqual(report_bug.resolve_product_component(mobile, "nightly", "Firefox"),
                             ("Firefox for Android", "General"))

    def test_improve_does_not_retarget_a_fenix_draft_at_desktop(self):
        bzdata = {"bugs": [{"product": "Firefox", "component": "Installer",
                            "assigned_to": "dev@moz.example"}]}
        query = {"keywords": ["crash"]}
        self.assertEqual(report_bug.improve(query, bzdata, 1681745, product="Fenix"),
                         "dev@moz.example")
        self.assertNotIn("product", query)
        query = {"keywords": ["crash"]}
        report_bug.improve(query, bzdata, 1681745, product="Firefox")
        self.assertEqual((query["product"], query["component"]), ("Firefox", "Installer"))


class TestAnArmedFenixFilerWouldFilePastADesktopBug(_FenixBase):
    """What the venue decision does the day the hold is lifted (the hold predicate mocked off,
    everything else the shipped config): a desktop `Firefox` bug on the shared signature is
    filed PAST, not commented into, and the new bug cross-references it."""

    def test_the_desktop_bug_is_other_app_for_a_fenix_crash(self):
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(1681745, product="Firefox")]
        with mock.patch.object(config, "autofile_product_held", return_value=False), \
                mock.patch("crashclouseau.report_bug.build_bug_preview",
                           return_value=dict(report_bug.build_bug_preview.return_value)) as pv:
            res = bugzilla_apply.autofile_bug("u-1", _FENIX_INFO, {}, {"candidate": {"node": "n"}},
                                              "lead", 90)
        self.assertTrue(res["filed"], res.get("skipped"))
        self.assertEqual((res["mode"], res["product"]), ("new_bug", "Fenix"))
        self.assertEqual(res["other_app_bugs"], [1681745])
        self.assertEqual([b["id"] for b in pv.call_args.kwargs["other_app_bugs"]], [1681745])
        # ...while the same bug IS the venue for a Firefox crash (tests/test_autofile pins the
        # comment path); here just that nothing in the Fenix path leaked into desktop's split.
        self.assertEqual(self.comments, [])


class TestFeedbackCarriesTheProduct(unittest.TestCase):
    def test_the_filed_row_names_its_product_when_the_record_has_one(self):
        rows = [{"uuid": "u-1", "dossier": {},
                 "filed_bug": {"bug": 9, "filed": True, "product": "Fenix",
                               "channel": "nightly", "at": None}},
                # A filing from before the filer recorded the product: absent, not guessed.
                {"uuid": "u-2", "dossier": {},
                 "filed_bug": {"bug": 8, "filed": True, "channel": "nightly", "at": None}}]
        with mock.patch.object(models.Dossier, "filed_bug_rows", return_value=rows):
            got = feedback._filed_bugs()
        self.assertEqual([(r["bug_id"], r["product"]) for r in got], [(9, "Fenix"), (8, None)])


if __name__ == "__main__":
    unittest.main()
