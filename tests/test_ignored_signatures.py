# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""``ignored_signature_patterns``: a deliberate test crash whose exact signature moves.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
        uv run python -m unittest tests.test_ignored_signatures

Fenix's debug drawer has one crash button, ``throw ArithmeticException("Debug drawer triggered
exception.")`` inside a lambda of ``org.mozilla.fenix.debugsettings.crashtools.CrashToolsKt``.
Socorro signs it with the R8-synthesised lambda class AND its remapped line, and both change per
build: 29 reports over the 90 days to 2026-09-15 (nightly 14, release 14, beta 1) came in three
spellings, so the exact ``ignored_signatures`` list -- which holds the about:crash* signature --
cannot pin it. A regex anchored at the start can, and it has to bite where the exact list bites:
through ``config.is_ignored_signature``, the ONE chokepoint the four gates ask (the selector,
the rate path, the spike sweep, a queued agent run). Every gate test here has a CONTROL: a Fenix
signature outside the package that the same harness lets through, so a test cannot pass because
the harness never reached the gate.

No network, no Postgres, no SDK.
"""
import os
import re

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import config, spikes, utils  # noqa: E402
from tests.selection_helpers import (  # noqa: E402
    AgentHarness, SelectorHarness, SpikeSweepHarness, selection_row,
)


_PKG = "java.lang.ArithmeticException: at org.mozilla.fenix.debugsettings.crashtools."
# The three spellings Socorro carried over 90 days to 2026-09-15, loudest first (19 / 9 / 1).
LIVE = (
    _PKG + "CrashToolsKt$$ExternalSyntheticLambda4.invoke(R8$$SyntheticClass:5)",
    _PKG + "CrashToolsKt$$ExternalSyntheticLambda3.invoke(R8$$SyntheticClass:5)",
    _PKG + "CrashToolsKt$$ExternalSyntheticLambda3.invoke(R8$$SyntheticClass:52)",
)
# A REAL Fenix crash with the same exception class: nothing about the class is a test crash.
REAL = "java.lang.ArithmeticException: at org.mozilla.fenix.HomeActivity.foo(HomeActivity.kt:1)"
EXACT = "CrashChannel::OpenContentStream"


def _fresh_cache():
    """The pattern cache reset for one test, restored afterwards."""
    return mock.patch.object(config, "_IGNORED_PATTERNS", None)


class TestThePredicate(unittest.TestCase):
    def test_the_shipped_pattern_matches_every_live_spelling(self):
        for sig in LIVE:
            self.assertTrue(config.is_ignored_signature(sig), sig)

    def test_a_real_fenix_crash_with_the_same_exception_is_not_ignored(self):
        self.assertFalse(config.is_ignored_signature(REAL))
        # The package prefix alone is not enough either: the pattern wants the exception class
        # and `: at ` in front, i.e. a JVM signature, not a native one that happens to name it.
        self.assertFalse(config.is_ignored_signature(
            "org.mozilla.fenix.debugsettings.crashtools.CrashToolsKt.foo"))
        # Anchored at the START: a signature that merely CONTAINS the shape is somebody else's
        # frame under the test package, not the test crash itself.
        self.assertFalse(config.is_ignored_signature("mozilla::Foo | " + LIVE[0]))

    def test_the_exact_list_still_bites_and_only_exactly(self):
        self.assertTrue(config.is_ignored_signature(EXACT))
        self.assertFalse(config.is_ignored_signature(EXACT + "X"))
        self.assertFalse(config.is_ignored_signature(None))
        self.assertFalse(config.is_ignored_signature(""))

    def test_the_patterns_are_compiled_regexes_from_the_config(self):
        pats = config.get_ignored_signature_patterns()
        self.assertIsInstance(pats, tuple)
        self.assertEqual(len(pats), len(config._get_global()["ignored_signature_patterns"]))
        self.assertTrue(all(isinstance(p, re.Pattern) for p in pats))
        for p in pats:
            self.assertTrue(p.pattern.startswith("^"), "every shipped pattern is anchored")

    def test_the_patterns_are_compiled_once(self):
        real_compile = re.compile
        calls = []

        def counting(pattern, *a, **kw):
            calls.append(pattern)
            return real_compile(pattern, *a, **kw)

        with _fresh_cache(), mock.patch.object(config.re, "compile", side_effect=counting):
            for _ in range(5):
                for sig in LIVE + (REAL, EXACT):
                    config.is_ignored_signature(sig)
            self.assertEqual(len(calls), len(config._get_global()["ignored_signature_patterns"]))
            self.assertGreater(len(calls), 0)

    def test_an_invalid_pattern_is_logged_skipped_and_ignores_nothing(self):
        """A typo in the config list must not stop the selector (the `_ENUM_ADDITIONS` failure
        shape) and must not widen the list either."""
        cfg = dict(config._get_global())
        cfg["ignored_signature_patterns"] = ["(unclosed", 42, "^java\\.lang\\.Bogus: at "]
        with _fresh_cache(), mock.patch.object(config, "_get_global", return_value=cfg), \
                mock.patch.object(config.logger, "error") as error:
            pats = config.get_ignored_signature_patterns()
            self.assertEqual(len(pats), 1, "the two bad entries are dropped, the good one kept")
            self.assertEqual(error.call_count, 2)
            named = [c.args[1] for c in error.call_args_list]
            self.assertEqual(named, ["(unclosed", 42])
            # The typo ignores nothing, the good entry and the exact list still bite, and the
            # shipped Fenix pattern -- absent from this config -- does not.
            self.assertFalse(config.is_ignored_signature("(unclosed"))
            self.assertTrue(config.is_ignored_signature("java.lang.Bogus: at x.y.Z.f(Z.kt:1)"))
            self.assertTrue(config.is_ignored_signature(EXACT))
            self.assertFalse(config.is_ignored_signature(LIVE[0]))

    def test_a_valid_pattern_that_matches_everything_is_dropped_with_an_error(self):
        """The worse typo, because it compiles: `""`, `"^"`, `"."`, a trailing empty entry, or
        the key written as a bare string (iterated as characters). Each would pop every
        signature on every product and channel as `ignored`, with the selection log as the
        only trace. Dropped, logged, and the shipped entry beside it still bites."""
        real = "mozilla::dom::Document::GetDocShell"
        shipped = config._get_global()["ignored_signature_patterns"][0]
        for bad in ([""], ["^"], ["."], [shipped, ""], "^java"):
            with self.subTest(bad=bad):
                cfg = dict(config._get_global())
                cfg["ignored_signature_patterns"] = bad
                with mock.patch.object(config, "_IGNORED_PATTERNS", None), \
                        mock.patch.object(config, "_get_global", return_value=cfg), \
                        mock.patch.object(config, "logger") as log:
                    self.assertFalse(config.is_ignored_signature(real))
                    self.assertFalse(config.is_ignored_signature("OOM | small"))
                    self.assertFalse(config.is_ignored_signature("js::gc::Foo"))
                    kept = config.get_ignored_signature_patterns()
                    self.assertGreaterEqual(log.error.call_count, 1)
                    if bad == [shipped, ""]:
                        self.assertEqual(len(kept), 1)
                        self.assertTrue(config.is_ignored_signature(LIVE[0]))
                    else:
                        self.assertEqual(kept, ())

    def test_an_absent_list_ignores_nothing_extra(self):
        cfg = {k: v for k, v in config._get_global().items() if k != "ignored_signature_patterns"}
        with _fresh_cache(), mock.patch.object(config, "_get_global", return_value=cfg):
            self.assertEqual(config.get_ignored_signature_patterns(), ())
            self.assertTrue(config.is_ignored_signature(EXACT))
            self.assertFalse(config.is_ignored_signature(LIVE[0]))


class TestTheSelector(SelectorHarness, unittest.TestCase):
    def test_a_spiking_debug_drawer_crash_is_popped_as_ignored(self):
        builds = [20260911212439, 20260912212439, 20260913212439, 20260914212439]
        population = {b: {REAL: (2, 2)} for b in builds[:3]}
        # The loudest spelling on one earlier build-day, a DIFFERENT spelling on the spike day:
        # the lambda index moved between builds, as it does.
        population[builds[0]] = dict(population[builds[0]], **{LIVE[0]: (3, 3)})
        population[builds[3]] = {REAL: (40, 30), LIVE[1]: (40, 30)}
        data, selection, rising = self._collect(population, builds)

        self.assertNotIn(LIVE[0], data)
        self.assertNotIn(LIVE[1], data)
        mine = [r for r in selection if r["signature"] in LIVE]
        self.assertEqual({r["outcome"] for r in mine}, {utils.IGNORED})
        self.assertEqual(sorted(r["signature"] for r in mine), sorted([LIVE[0], LIVE[1]]))
        self.assertTrue(all(r["picked"] is None and not r["evaluable"] for r in mine))
        # CONTROL: the real Fenix crash with the same exception class is still selected.
        self.assertIn(REAL, data)
        self.assertIn(utils.SELECTED, {r["outcome"] for r in selection if r["signature"] == REAL})
        # And the rate path is told to keep its hands off both spellings.
        already = rising.call_args.args[4]
        self.assertIn(LIVE[0], already)
        self.assertIn(LIVE[1], already)


class TestTheAgentGates(AgentHarness, unittest.TestCase):
    def test_enqueue_agent_returns_without_a_job_for_a_pattern_match(self):
        for sig in LIVE:
            gq, queue = self._enqueue(sig)
            gq.assert_not_called()
            queue.enqueue_call.assert_not_called()

    def test_enqueue_agent_still_enqueues_the_real_crash(self):
        gq, queue = self._enqueue(REAL)
        gq.assert_called_once()
        queue.enqueue_call.assert_called_once()
        self.assertEqual(queue.enqueue_call.call_args.kwargs["args"], ("u-fenix",))

    def test_a_queued_run_on_a_pattern_match_does_not_spend(self):
        self._run(LIVE[2]).assert_not_called()

    def test_a_queued_run_on_the_real_crash_goes_past_the_gate(self):
        self._run(REAL).assert_called_once_with("u-fenix")


class TestTheSpikeSweep(SpikeSweepHarness, unittest.TestCase):
    def test_a_pattern_matched_signature_is_never_escalated(self):
        for sig in LIVE:
            self.assertEqual(self._sweep([selection_row(32, [1, 0, 2], 21, signature=sig)]), 0, sig)
        self.assertEqual(self.created, [])
        self.assertEqual(self.enqueued, [])
        spikes.judge_selection.assert_not_called()

    def test_two_spellings_on_one_build_day_are_each_skipped(self):
        """Two R8 spellings of the one deliberate crash on one build-day: `utils.lambda_family`
        keys on C++ lambda demanglings and leaves `$$ExternalSyntheticLambdaN` alone, so they
        are two families here, each skipped by the pattern before `judge_selection` is asked."""
        rows = [selection_row(32, [1, 0, 2], 21, signature=LIVE[0]), selection_row(9, [0, 0, 1], 7, signature=LIVE[1])]
        self.assertEqual(self._sweep(rows), 0)
        self.assertEqual(self.created, [])
        spikes.judge_selection.assert_not_called()

    def test_the_real_crash_is_escalated_by_the_same_harness(self):
        self.assertEqual(self._sweep([selection_row(32, [1, 0, 2], 21, signature=REAL)]), 1)
        self.assertEqual(self.enqueued, [1])
        self.assertEqual(self.created[0].signature, REAL)
        spikes.judge_selection.assert_called_once()


if __name__ == "__main__":
    unittest.main()
