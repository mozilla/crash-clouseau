# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Fenix (Firefox for Android) in the evidence agent: plan 16 §13, decisions D3, D7, D13, D15
and D18, on the agent side.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
        uv run python -m unittest tests.test_fenix_agent

Two things are pinned here, and the second matters as much as the first. (1) A Fenix / Java seed
gets the product gate, the R8 line-number rule, the product-neutral wording and the Android-hostile
noise helpers skipped or re-keyed. (2) A DESKTOP seed renders BYTE-IDENTICALLY to before: the
strings below were captured from HEAD before any of this landed, and every new path is behind
the product, the `java` flag or the pref. The live shapes (the 2026-09-15 Fenix example
3c426d92: `cpu_info == "unknown"`, `java_exception.exception.values` cause-first, Keystore.kt:269
for a method at line 221) are the fixtures' source."""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import config, machine, population, searchfox as sf, sigage  # noqa: E402
from crashclouseau.agent import orchestrator as orch, roles, second_opinion, triage  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    Candidate, Confidence, Decision, Dossier, Verdict,
)

_KT = ("mobile/android/android-components/components/lib/dataprotect/src/main/java/mozilla/"
       "components/lib/dataprotect/Keystore.kt")

# The stored Java frames a Fenix seed reads (`CrashStack.get_by_uuid` stamps `line_trusted`).
_JAVA_FRAMES = [
    {"stackpos": 0, "function": "mozilla.components.lib.dataprotect.Keystore.generateKey",
     "filename": _KT, "line": 269, "line_trusted": False,
     "changesets": {"abc": {"score": 8}}},
    {"stackpos": 1, "function": "mozilla.components.lib.dataprotect.Keystore.<init>",
     "filename": _KT, "line": 51, "line_trusted": False, "changesets": {}},
]

# The native frames of a desktop seed, and how HEAD rendered them (captured 2026-09-15).
_NATIVE_FRAMES = [
    {"stackpos": 0, "function": "mozilla::dom::Foo::Bar", "filename": "dom/base/Foo.cpp",
     "line": 42, "changesets": {"abc": {"score": 3}}, "inlines": ["HashString"]},
    {"stackpos": 1, "function": "nsThread::ProcessNextEvent",
     "filename": "xpcom/threads/nsThread.cpp", "line": 1100, "changesets": {}},
]
_NATIVE_STACK_HEAD = (
    "#0 mozilla::dom::Foo::Bar  dom/base/Foo.cpp:42  [inlined: HashString]\n"
    "#1 nsThread::ProcessNextEvent  xpcom/threads/nsThread.cpp:1100"
)

# The live example's processed crash, reduced to the fields the prompts read.
_FENIX_RAW = {
    "product": "Fenix", "release_channel": "nightly", "os_name": "Android", "os_version": "33",
    "os_pretty_version": "Android 33", "cpu_arch": "amd64", "cpu_info": "unknown",
    "android_manufacturer": "Google", "android_model": "octopus", "android_version": "33 (REL)",
    "android_cpu_abi": "x86_64", "process_type": "parent", "report_type": "crash",
    "install_time": 1789110875, "date_processed": "2026-09-15T10:00:00+00:00",
    # Cause FIRST, outer exception LAST -- Socorro's order, verified live.
    "java_exception": {"exception": {"values": [
        {"stacktrace": {"type": "KeyStoreException", "module": "android.security",
                        "frames": []}},
        {"stacktrace": {"type": "ProviderException", "module": "java.security",
                        "frames": []}},
    ]}},
}

_JAVA_SEED = {
    "uuid": "u-java", "channel": "nightly", "product": "Fenix", "buildid": "20260910214118",
    "version": "158.0a1", "java": True, "line_numbers_trusted": False,
    "signature": "java.security.ProviderException: at android.security.keystore2."
                 "AndroidKeyStoreKeyGeneratorSpi.engineGenerateKey(AndroidKeyStoreKeyGeneratorSpi"
                 ".java)",
    "raw_crash": _FENIX_RAW,
    "stack": orch._stack_text(_JAVA_FRAMES, line_numbers_trusted=False),
    "candidates": [{"node": "abc", "score": 8, "bug": 1, "backedout": False, "pushdate": None,
                    "noise": False}],
}

# `tests/test_prompt_budget.py`'s plain deref, and what HEAD printed for it.
_PLAIN = {
    "uuid": "u-plain", "signature": "mozilla::dom::Foo::Bar", "channel": "nightly",
    "product": "Firefox", "buildid": "20260819092600", "version": "156.0a1",
    "raw_crash": {
        "reason": "EXCEPTION_ACCESS_VIOLATION_READ",
        "json_dump": {"crash_info": {"type": "EXCEPTION_ACCESS_VIOLATION_READ",
                                     "address": "0x0", "crashing_thread": 0}},
    },
    "stack": _NATIVE_STACK_HEAD,
    "candidates": [{"node": "abc", "score": 3, "bug": 1, "backedout": False, "pushdate": None,
                    "noise": False}],
}
_PLAIN_FACTS_HEAD = [
    "Product: Firefox",
    "Version: 156.0a1",
    "Build ID: 20260819092600",
    "Crash type: EXCEPTION_ACCESS_VIOLATION_READ",
    "Fault address: 0x0",
    "Analysed thread (the stack below is THIS thread): 0",
    "Crash reason: EXCEPTION_ACCESS_VIOLATION_READ",
]


def _no_network():
    """Every Socorro / hg read `build_seed` makes, stubbed. `test_orchestrator` lets them fail
    quietly; here a Fenix seed must be built with no request at all."""
    return [
        mock.patch("crashclouseau.inspector.get_crash_data", return_value=dict(_FENIX_RAW)),
        mock.patch("crashclouseau.sigage.signature_history", return_value={
            "first_seen": None, "first_seen_channel": None, "first_seen_any": None,
            "total": None, "total_other_channels": None}),
        mock.patch("crashclouseau.sigage.first_seen_ever_facts", return_value={}),
        mock.patch.object(orch, "_signature_trend", return_value={}),
        mock.patch.object(orch, "_hardware_noise", return_value=dict(sigage.NO_HARDWARE_NOISE)),
        mock.patch.object(orch, "_version_rates", return_value={}),
        mock.patch.object(orch, "_install_history", return_value={
            "distinct_signatures": None, "distinct_cpus": None, "crashes": None,
            "span_seconds": None}),
        mock.patch.object(orch, "_matching_archetypes", return_value=[]),
        mock.patch.object(orch.models.Node, "authors_for", return_value={}),
    ]


def _fenix_info(java=True):
    return {"uuid": "u-1", "id": 1, "signature": "S", "channel": "nightly", "product": "Fenix",
            "version": "158.0a1", "java": java, "node": "deadbeef"}


class TestSeedFlags(unittest.TestCase):
    """`build_seed` is the one place the language flag is born (D7)."""

    def _seed(self, frames, uuid_info, product, trust=False):
        res = {"frames": [dict(f) for f in frames]}
        patches = _no_network() + [
            mock.patch.object(orch.models.CrashStack, "get_by_uuid",
                              return_value=(res, uuid_info)),
            mock.patch.object(orch.models.UUID, "get_info", return_value={
                "signature": "S", "channel": "nightly", "product": product,
                "buildid": "x", "version": "1"}),
            mock.patch.object(orch.config, "java_trust_line_numbers", return_value=trust),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return orch.build_seed("u-1")

    def test_a_java_seed_says_so_and_renders_no_trusted_line(self):
        seed = self._seed(_JAVA_FRAMES, _fenix_info(java=True), "Fenix")
        self.assertTrue(seed["java"])
        self.assertFalse(seed["line_numbers_trusted"])
        self.assertNotIn("Keystore.kt:269", seed["stack"])
        self.assertIn("(reported line 269: R8-remapped, unreliable)", seed["stack"])
        self.assertEqual(seed["product"], "Fenix")

    def test_the_pref_restores_the_line(self):
        seed = self._seed(_JAVA_FRAMES, _fenix_info(java=True), "Fenix", trust=True)
        self.assertTrue(seed["java"])
        self.assertTrue(seed["line_numbers_trusted"])
        self.assertIn("Keystore.kt:269", seed["stack"])

    def test_a_native_seed_is_exactly_false_true_and_byte_identical(self):
        info = dict(_fenix_info(java=False), product="Firefox")
        seed = self._seed(_NATIVE_FRAMES, info, "Firefox")
        self.assertIs(seed["java"], False)
        self.assertIs(seed["line_numbers_trusted"], True)
        self.assertEqual(seed["stack"], _NATIVE_STACK_HEAD)

    def test_stack_text_default_path_is_byte_identical(self):
        self.assertEqual(orch._stack_text(_NATIVE_FRAMES), _NATIVE_STACK_HEAD)
        self.assertEqual(orch._stack_text(_NATIVE_FRAMES, line_numbers_trusted=True),
                         _NATIVE_STACK_HEAD)

    def test_stack_text_untrusted_keeps_position_function_file_and_inlines(self):
        text = orch._stack_text(_JAVA_FRAMES, line_numbers_trusted=False)
        self.assertEqual(text.splitlines()[0],
                         "#0 mozilla.components.lib.dataprotect.Keystore.generateKey  {}  "
                         "(reported line 269: R8-remapped, unreliable)".format(_KT))
        frames = [dict(_NATIVE_FRAMES[0])]
        text = orch._stack_text(frames, line_numbers_trusted=False)
        self.assertIn("[inlined: HashString]", text)
        self.assertNotIn("Foo.cpp:42", text)


class TestOffstackBlameSkipped(unittest.TestCase):
    """`_crashing_area_experts` picks `ann[line - 1]`; on an R8 line that is a comment's author."""

    def _offstack_seed(self, frames, uuid_info, product, blame):
        res = {"frames": [dict(f, changesets={}) for f in frames]}
        window = [{"node": "w1", "score": None, "bug": None, "backedout": False,
                   "pushdate": None, "noise": False, "desc": "d"}]
        patches = _no_network() + [
            mock.patch.object(orch.models.CrashStack, "get_by_uuid",
                              return_value=(res, uuid_info)),
            mock.patch.object(orch.models.UUID, "get_info", return_value={
                "signature": "S", "channel": "nightly", "product": product,
                "buildid": "x", "version": "1"}),
            mock.patch.object(orch.config, "get_agent_offstack", return_value=dict(
                config.get_agent_offstack(), enabled=True, prior_signature=False)),
            mock.patch.object(orch, "_offstack_window", return_value=None),
            mock.patch.object(orch, "_offstack_candidates", return_value=window),
            mock.patch.object(orch, "_crashing_area_experts", blame),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return orch.build_seed("u-1")

    def test_java_off_stack_does_not_blame_the_reported_line(self):
        blame = mock.MagicMock(return_value=[{"name": "x"}])
        with self.assertLogs(level="INFO") as cm:
            seed = self._offstack_seed(_JAVA_FRAMES, _fenix_info(java=True), "Fenix", blame)
        self.assertTrue(seed["is_offstack"])
        blame.assert_not_called()
        self.assertEqual(seed["experts"], [])
        self.assertIn("R8-remapped", "\n".join(cm.output))

    def test_native_off_stack_still_blames(self):
        blame = mock.MagicMock(return_value=[{"name": "x"}])
        info = dict(_fenix_info(java=False), product="Firefox")
        seed = self._offstack_seed(_NATIVE_FRAMES, info, "Firefox", blame)
        self.assertTrue(seed["is_offstack"])
        blame.assert_called_once()
        self.assertEqual(seed["experts"], [{"name": "x"}])


class TestProductGate(unittest.TestCase):
    """D3: `AGENT_PRODUCTS` is the product half of the money switch, at all three doors."""

    def _enqueue(self, env, **kwargs):
        q = mock.MagicMock()
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(orch.config, "get_agent_enabled", return_value=True), \
             mock.patch.object(orch.config, "get_agent_channels", return_value=["nightly"]), \
             mock.patch.object(orch.models.UUID, "get_signature", return_value="S"), \
             mock.patch.object(orch, "_proto_already_triaged", return_value=False), \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            orch.enqueue_agent("u-1", "nightly", **kwargs)
        return q

    def test_enqueue_refuses_fenix_when_agent_products_is_firefox(self):
        q = self._enqueue({"AGENT_PRODUCTS": "Firefox"}, product="Fenix")
        q.enqueue_call.assert_not_called()

    def test_enqueue_accepts_fenix_when_the_variable_is_unset(self):
        env = {k: v for k, v in os.environ.items() if k != "AGENT_PRODUCTS"}
        q = mock.MagicMock()
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(orch.config, "get_agent_enabled", return_value=True), \
             mock.patch.object(orch.config, "get_agent_channels", return_value=["nightly"]), \
             mock.patch.object(orch.models.UUID, "get_signature", return_value="S"), \
             mock.patch.object(orch, "_proto_already_triaged", return_value=False), \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            self.assertIn("Fenix", config.get_agent_products())   # shipped default (D1/D3)
            orch.enqueue_agent("u-1", "nightly", product="Fenix")
        q.enqueue_call.assert_called_once()

    def test_enqueue_looks_the_product_up_when_not_given(self):
        with mock.patch.object(orch.models.UUID, "get_info",
                               return_value={"product": "Fenix", "channel": "nightly"}):
            q = self._enqueue({"AGENT_PRODUCTS": "Firefox"})
        q.enqueue_call.assert_not_called()
        with mock.patch.object(orch.models.UUID, "get_info",
                               return_value={"product": "Firefox", "channel": "nightly"}):
            q = self._enqueue({"AGENT_PRODUCTS": "Firefox"})
        q.enqueue_call.assert_called_once()

    def test_a_failed_lookup_filters_nothing(self):
        # `None` is "not filtered", like an unknown channel: a DB hiccup must not drop a crash.
        with mock.patch.object(orch.models.UUID, "get_info", side_effect=RuntimeError("db")):
            q = self._enqueue({"AGENT_PRODUCTS": "Firefox"})
        q.enqueue_call.assert_called_once()

    def test_force_bypasses_the_product_gate(self):
        q = self._enqueue({"AGENT_PRODUCTS": "Firefox"}, product="Fenix", force=True)
        q.enqueue_call.assert_called_once()

    def test_update_style_call_passes_product_by_keyword(self):
        # `update.put_report` calls `enqueue_agent(uuid, channel, product=product)`.
        q = self._enqueue({"AGENT_PRODUCTS": "Firefox Fenix"}, product="Fenix")
        q.enqueue_call.assert_called_once()

    def _run(self, products, product):
        seed = mock.MagicMock(return_value=None)   # None: the run returns right after the seed
        with mock.patch.object(orch.models.UUID, "get_channel", return_value="nightly"), \
             mock.patch.object(orch.models.UUID, "get_info", return_value={"product": product}), \
             mock.patch.object(orch.models.UUID, "get_signature", return_value="S"), \
             mock.patch.object(orch.config, "get_agent_channels", return_value=["nightly"]), \
             mock.patch.object(orch.config, "get_agent_products", return_value=products), \
             mock.patch.object(orch.config, "get_agent_skip_if_existing", return_value=False), \
             mock.patch.object(orch, "build_seed", seed):
            orch.run_evidence_agent("u-1")
        return seed

    def test_a_queued_fenix_job_does_not_run_once_the_product_is_dropped(self):
        self._run(["Firefox"], "Fenix").assert_not_called()

    def test_a_named_product_and_an_unknown_product_run(self):
        self._run(["Firefox", "Fenix"], "Fenix").assert_called_once()
        self._run(["Firefox"], None).assert_called_once()

    def test_the_sweep_restricts_the_fetch_to_the_agent_products(self):
        sweep_cfg = {"enabled": True, "min_age_s": 1, "max_age_s": 10, "max_per_run": 3}
        with mock.patch.object(orch.config, "get_agent_sweep", return_value=sweep_cfg), \
             mock.patch.object(orch.config, "get_agent_channels", return_value=["nightly"]), \
             mock.patch.object(orch.config, "get_agent_products", return_value=["Firefox"]), \
             mock.patch.object(orch.models.SweepMark, "get", return_value=0), \
             mock.patch.object(orch.models.UUID, "untriaged", return_value=[]) as q:
            orch.sweep_untriaged_crashes()
        self.assertEqual(q.call_args.kwargs["products"], ["Firefox"])
        self.assertEqual(q.call_args.kwargs["channels"], ["nightly"])


class TestCalibrationIsPerProduct(unittest.TestCase):
    """D13: no "N% worth investigating" for Fenix, whose channel label is the desktop fit's."""

    def _dossier(self):
        return Dossier(candidate=Candidate(node="abc123def456", bug=42),
                       verdict=Verdict(decision=Decision.lead, confidence=Confidence.medium,
                                       needinfo_draft="?"))

    def test_fenix_nightly_gets_no_probability(self):
        d = self._dossier()
        orch._apply_worth_investigating(d, {"channel": "nightly", "product": "Fenix"})
        self.assertIsNone(d.verdict.p_worth_investigating)

    def test_firefox_nightly_still_does(self):
        d = self._dossier()
        orch._apply_worth_investigating(d, {"channel": "nightly", "product": "Firefox"})
        self.assertIsNotNone(d.verdict.p_worth_investigating)
        d2 = self._dossier()
        orch._apply_worth_investigating(d2, {"channel": "nightly"})   # legacy seed, no product
        self.assertEqual(d2.verdict.p_worth_investigating, d.verdict.p_worth_investigating)


def _capturing_supersearch(seen):
    class Fake:
        def __init__(self, params=None, handler=None, handlerdata=None, **kw):
            seen.append(params)
            handler({"hits": [], "total": 0}, handlerdata)

        def wait(self):
            return None
    return Fake


class TestInstallHistorySkippedOffFirefox(unittest.TestCase):
    """D15: on Fenix `install_time` collides (51% clock resets), so the query is not paid and no
    colliding-id diagnostic is recorded."""

    def test_fenix_makes_no_request(self):
        seen = []
        with mock.patch.object(machine.socorro, "SuperSearch", _capturing_supersearch(seen)), \
             self.assertLogs(level="INFO") as cm:
            got = orch._install_history(dict(_FENIX_RAW))
        self.assertEqual(seen, [])
        self.assertEqual(got, {"distinct_signatures": None, "distinct_cpus": None,
                               "crashes": None, "span_seconds": None})
        self.assertIn("not asked for product 'Fenix'", "\n".join(cm.output))

    def test_firefox_still_asks(self):
        seen = []
        raw = {"install_time": "1755500000", "product": "Firefox", "release_channel": "nightly",
               "date_processed": "2026-08-19T10:00:00+00:00"}
        with mock.patch.object(machine.socorro, "SuperSearch", _capturing_supersearch(seen)):
            orch._install_history(raw)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["product"], "Firefox")


class TestUnknownCpuIsUnknown(unittest.TestCase):
    """`cpu_info == "unknown"` on 6,489 of 16,368 Fenix reports: a placeholder, not one CPU."""

    def _search(self, hits):
        class Fake:
            def __init__(self, params=None, handler=None, handlerdata=None):
                handler({"hits": hits, "total": len(hits)}, handlerdata)

            def wait(self):
                return None
        return Fake

    def test_only_placeholders_means_no_cpu(self):
        hits = [{"signature": "A", "cpu_info": "unknown", "date": "2026-09-08T00:00:00+00:00"},
                {"signature": "B", "cpu_info": "Unknown ", "date": "2026-09-09T00:00:00+00:00"},
                {"signature": "C", "cpu_info": "", "date": "2026-09-10T00:00:00+00:00"}]
        with mock.patch.object(machine.socorro, "SuperSearch", self._search(hits)):
            got = machine.install_history(1)
        self.assertEqual(got["distinct_signatures"], 3)
        self.assertIsNone(got["distinct_cpus"])
        self.assertEqual(got["crashes"], 3)

    def test_a_placeholder_beside_a_real_cpu_does_not_add_a_second_one(self):
        hits = [{"signature": "A", "cpu_info": "unknown", "date": "2026-09-08T00:00:00+00:00"},
                {"signature": "B", "cpu_info": "GenuineIntel family 6 model 183 stepping 1",
                 "date": "2026-09-09T00:00:00+00:00"}]
        with mock.patch.object(machine.socorro, "SuperSearch", self._search(hits)):
            got = machine.install_history(1)
        self.assertEqual(got["distinct_cpus"], 1)


class TestPopulationRatesArePerProduct(unittest.TestCase):
    """D15: the Firefox-nightly background is not quoted beside a Fenix share."""

    def test_fenix_is_unmeasured(self):
        self.assertEqual(sigage._population("nightly", "Fenix"), {})
        self.assertIsNone(sigage.population_bit_flip_rate("nightly", "Fenix"))
        self.assertIsNone(sigage.population_broken_cpu_rate("nightly", "Fenix"))
        self.assertIsNone(sigage.population_top_cpu_share_median("nightly", "Fenix"))
        self.assertEqual(sigage.population_label("nightly", "Fenix"), "Fenix")
        self.assertEqual(sigage.population_label(None, "Fenix"), "Fenix")

    def test_firefox_and_none_are_unchanged(self):
        for product in (None, "Firefox"):
            self.assertEqual(sigage.population_bit_flip_rate("nightly", product), 0.025)
            self.assertEqual(sigage.population_broken_cpu_rate("beta", product), 0.0582)
            self.assertEqual(sigage.population_label("nightly", product), "Firefox-nightly")
            self.assertEqual(sigage.population_label("release", product), "Firefox")
            self.assertIsNone(sigage.population_bit_flip_rate("release", product))

    def test_the_prompt_drops_the_comparison_for_fenix(self):
        noise = {"reports": 20, "bit_flip_rate": 0.5, "broken_cpu_rate": 0.1}
        fenix = "\n".join(triage._hardware_noise_lines(
            {"channel": "nightly", "product": "Fenix", "hardware_noise": noise}))
        self.assertIn("50% carry a Socorro bit-flip annotation", fenix)
        self.assertNotIn("population", fenix)
        firefox = "\n".join(triage._hardware_noise_lines(
            {"channel": "nightly", "product": "Firefox", "hardware_noise": noise}))
        self.assertIn("(Firefox-nightly population: 2%)", firefox)


class TestPopulationClockResetBound(unittest.TestCase):
    """D15: 8,389 of 16,368 Fenix install_times are before 2010 (48 before 2005)."""

    NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
    RESET = 1230768000          # 2009-01-01: passes the 2005 bound, fails the 2010 one
    REAL = 1789110875           # the live example's install_time

    def _facets(self):
        return [{"term": str(self.RESET), "count": 300}, {"term": str(self.REAL), "count": 2}]

    def test_summarize_takes_a_bound_and_defaults_to_2005(self):
        default = population.summarize(self._facets(), total=302, now=self.NOW)
        self.assertEqual((default["installs"], default["dropped"]), (2, 0))
        fenix = population.summarize(self._facets(), total=302, now=self.NOW,
                                     min_install_time=population._FENIX_MIN_INSTALL_TIME)
        self.assertEqual((fenix["installs"], fenix["dropped"]), (1, 1))
        self.assertIn("dropped", fenix)

    def _for_crash(self, product):
        info = {"uuid": "3c426d92-3270-4afc-bf1d-32e8a0260911", "signature": "S",
                "buildid": self.NOW, "channel": "nightly", "product": product}
        responses = [{"total": 302, "facets": {"install_time": self._facets()}},
                     {"hits": [{"install_time": self.REAL}]}]

        def fake_search(**kwargs):
            for q, resp in zip(kwargs.get("queries") or [], responses):
                q.handler(resp, q.handlerdata)
            return mock.Mock(wait=lambda: None)

        def fake_supersearch(**kw):
            return mock.Mock(wait=lambda: fake_search(**kw))

        with mock.patch.object(population.socorro, "SuperSearch", side_effect=fake_supersearch), \
             mock.patch.object(population, "_build_stats", return_value=None):
            return population.for_crash(info)

    def test_for_crash_passes_the_2010_bound_for_fenix_only(self):
        self.assertEqual(population._FENIX_MIN_INSTALL_TIME, 1262304000)
        fenix = self._for_crash("Fenix")
        self.assertEqual((fenix["installs"], fenix["dropped"]), (1, 1))
        self.assertEqual(fenix["own"]["rank"], 1)
        firefox = self._for_crash("Firefox")
        self.assertEqual((firefox["installs"], firefox["dropped"]), (2, 0))


class TestJavaPrompt(unittest.TestCase):
    """The R8 rule reaches the system prompt, the user prompt, the facts and the roles; the
    desktop prompt is byte-identical."""

    def test_desktop_facts_and_prompt_opener_are_unchanged(self):
        self.assertEqual(triage._crash_facts(_PLAIN), _PLAIN_FACTS_HEAD)
        self.assertTrue(triage._user_prompt(_PLAIN).startswith(
            "Investigate this Firefox crash to get the RIGHT PERSON INVESTIGATING it"))
        self.assertEqual(triage._java_lines(_PLAIN), [])
        self.assertNotIn("Java/Kotlin", triage._system_prompt())
        self.assertNotIn("Java/Kotlin", triage._system_prompt("beta"))

    def test_java_facts_carry_the_exception_chain_outermost_first_and_the_device(self):
        facts = triage._crash_facts(_JAVA_SEED)
        self.assertIn("Java exception chain (outermost first): java.security.ProviderException "
                      "caused by android.security.KeyStoreException", facts)
        self.assertIn("Android device: Google octopus", facts)
        self.assertIn("Android version: 33 (REL)", facts)
        self.assertIn("Android ABI: x86_64", facts)
        self.assertIn("OS: Android 33", facts)

    def test_java_lines_follow_the_flag_and_the_pref(self):
        lines = triage._java_lines(_JAVA_SEED)
        self.assertTrue(lines and "R8-remapped and UNRELIABLE" in lines[1])
        self.assertIn("FILE and METHOD", lines[1])
        self.assertIn("mozilla::components::lib::dataprotect::Keystore::generateKey", lines[3])
        self.assertEqual(triage._java_lines(dict(_JAVA_SEED, line_numbers_trusted=True)), [])
        self.assertEqual(triage._java_lines(dict(_JAVA_SEED, java=False)), [])

    def test_the_user_prompt_names_the_product_and_the_ranking(self):
        prompt = triage._user_prompt(_JAVA_SEED)
        self.assertTrue(prompt.startswith("Investigate this Fenix crash"))
        self.assertIn("JAVA/KOTLIN STACK", prompt)
        self.assertLess(prompt.index("JAVA/KOTLIN STACK"), prompt.index("\nStack:\n"))
        self.assertIn("ranked by whether they touch a crash frame's FILE and METHOD", prompt)
        self.assertNotIn("ranked by proximity to the crash", prompt)
        self.assertNotIn("Keystore.kt:269", prompt)
        native = triage._user_prompt(_PLAIN)
        self.assertIn("already ranked by proximity to the crash", native)

    def test_the_system_prompt_inverts_the_drift_rule_under_the_drift_section(self):
        text = triage._system_prompt(None, True)
        base = triage._system_prompt()
        self.assertIn(triage._AFTER_DRIFT_HEADING, base)   # the anchor is still in system.md
        self.assertIn("## Java/Kotlin stacks (R8-remapped line numbers)", text)
        drift = text.index("## Revision drift")
        java = text.index("## Java/Kotlin stacks")
        after = text.index(triage._AFTER_DRIFT_HEADING)
        self.assertLess(drift, java)
        self.assertLess(java, after)
        self.assertIn("INVERTED", text)
        # Same insertion on a non-central channel; the beta rewrite still applies.
        beta = triage._system_prompt("beta", True)
        self.assertIn("## Java/Kotlin stacks", beta)
        self.assertIn("mozilla-beta", beta)
        self.assertEqual(text.replace(triage._JAVA_SYSTEM_SECTION, ""), base)

    def test_build_options_passes_java_to_the_prompt_and_the_roles(self):
        opts = triage.build_options(_JAVA_SEED, searchfox_client=object())
        self.assertIn("## Java/Kotlin stacks", opts.system_prompt)
        self.assertIn(roles._JAVA_ROLE_NOTE, opts.agents["skeptic"].prompt)
        native = triage.build_options(_PLAIN, searchfox_client=object())
        self.assertNotIn("## Java/Kotlin stacks", native.system_prompt)
        self.assertNotIn(roles._JAVA_ROLE_NOTE, native.agents["skeptic"].prompt)


class TestRoles(unittest.TestCase):
    def test_java_appends_one_note_to_four_roles(self):
        java = roles.build_roles(java=True)
        base = roles.build_roles()
        for name in ("crash-interpreter", "patch-scout", "data-flow-tracer", "skeptic"):
            self.assertEqual(java[name].prompt, base[name].prompt + roles._JAVA_ROLE_NOTE, name)
        self.assertEqual(java["call-graph-explorer"].prompt, base["call-graph-explorer"].prompt)
        self.assertIn("field_layout` does not apply", roles._JAVA_ROLE_NOTE)
        self.assertIn("mozilla::components::lib::dataprotect::Keystore::generateKey",
                      roles._JAVA_ROLE_NOTE)

    def test_the_default_is_byte_identical(self):
        for name in roles.role_names():
            self.assertEqual(roles.make_role(name).prompt,
                             roles.make_role(name, java=False).prompt)
            self.assertEqual(roles.make_role(name, channel="beta").prompt,
                             roles.make_role(name, channel="beta", java=False).prompt)


class TestSecondOpinion(unittest.TestCase):
    def test_the_reviewer_is_told_which_product(self):
        self.assertIs(second_opinion._system_prompt("Firefox"), second_opinion._SYSTEM)
        self.assertIs(second_opinion._system_prompt(None), second_opinion._SYSTEM)
        fenix = second_opinion._system_prompt("Fenix")
        self.assertIn("second reviewer of a Fenix crash", fenix)
        self.assertNotIn("Firefox crash", fenix)
        opts = second_opinion.build_options(_JAVA_SEED, None, searchfox_client=object())
        self.assertIn("second reviewer of a Fenix crash", opts.system_prompt)

    def test_the_reviewer_reads_the_same_r8_rule(self):
        prompt = second_opinion._user_prompt(_JAVA_SEED, {"node": "abc", "bug": 1})
        self.assertIn("JAVA/KOTLIN STACK", prompt)
        self.assertLess(prompt.index("JAVA/KOTLIN STACK"), prompt.index("\nStack:\n"))
        self.assertNotIn("JAVA/KOTLIN STACK",
                         second_opinion._user_prompt(_PLAIN, {"node": "abc", "bug": 1}))


class TestSearchfoxJvmNames(unittest.TestCase):
    """D18: Kotlin semantics are on mozilla-central (verified 2026-09-15, `--define` -> line
    221); the only conversion needed is the dotted Socorro spelling to `::`."""

    def test_a_dotted_jvm_name_becomes_colon_joined(self):
        self.assertEqual(
            sf._clean_symbol("mozilla.components.lib.dataprotect.Keystore.generateKey"),
            "mozilla::components::lib::dataprotect::Keystore::generateKey")
        self.assertEqual(sf._clean_symbol("org.mozilla.fenix.HomeActivity.onCreate"),
                         "org::mozilla::fenix::HomeActivity::onCreate")

    def test_cpp_and_rust_names_are_untouched(self):
        self.assertEqual(sf._clean_symbol("NS_ProcessNextEvent(nsIThread*, bool)"),
                         "NS_ProcessNextEvent")
        self.assertEqual(sf._clean_symbol("mozilla::Maybe<T>::ref"), "mozilla::Maybe::ref")
        self.assertEqual(sf._clean_symbol("core::ptr::drop_in_place"), "core::ptr::drop_in_place")
        self.assertEqual(sf._clean_symbol("libxul.so"), "libxul.so")
        # An R8 synthetic (`$$`) is not a source symbol and is left alone.
        self.assertEqual(sf._clean_symbol("mozilla.components.Foo$$ExternalSyntheticLambda0.run"),
                         "mozilla.components.Foo$$ExternalSyntheticLambda0.run")


if __name__ == "__main__":
    unittest.main()
