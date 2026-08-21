# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# A mechanism resting on code that is not in the build cannot be what crashed.
#
# See `crashclouseau/compiled_out.py` for the measurement. The short version: three of the four
# module-owner refutations are this, the obvious detector (look at the citation's own line) fires
# on 0 of 3, and the thing that is actually guarded is a HOLLOW SYMBOL -- `js::gc::AutoMarkingLock`,
# a real class whose every function body is inside `#ifdef JS_GC_CONCURRENT_MARKING`.
#
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_compiled_out_gate
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import compiled_out as co  # noqa: E402
from crashclouseau.agent import orchestrator as orch  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    Candidate,
    Claim,
    Confidence,
    Decision,
    DiffLineCitation,
    Dossier,
    SearchfoxCitation,
    Verdict,
)

# `js/src/gc/Cell.h`, verbatim modulo whitespace. Every body is guarded; the class is not.
AUTO_MARKING_LOCK = '''\
// This is a no op outside concurrent marking builds.
class MOZ_RAII AutoMarkingLock {
#ifdef JS_GC_CONCURRENT_MARKING
  MarkingLock* lock = nullptr;
  JSRuntime* runtime = nullptr;
#endif

  AutoMarkingLock(const AutoMarkingLock& other) = delete;

 public:
  AutoMarkingLock(JS::Zone* zone, MarkingLock& markingLock) {
#ifdef JS_GC_CONCURRENT_MARKING
    auto* shadowZone = JS::shadow::Zone::from(zone);
    if (shadowZone->needsMarkingBarrier(JS::shadow::Zone::Concurrent)) {
      lock = &markingLock;
      lock->lock(runtime);
    }
#endif
  }

  ~AutoMarkingLock() {
#ifdef JS_GC_CONCURRENT_MARKING
    if (lock) {
      lock->unlock(runtime);
    }
#endif
  }
};
'''

# Real, always-compiled code: `js::jit::CacheIRStubInfo::fieldType`, the symbol bug 2063782
# actually cited. A rule that looked here would find nothing -- which is the point.
FIELD_TYPE = '''\
  StubField::Type fieldType(uint32_t i) const {
    static_assert(sizeof(StubField::Type) == sizeof(uint8_t));
    const uint8_t* fieldTypes = code() + codeLength_;
    return static_cast<StubField::Type>(fieldTypes[i]);
  }
'''

# The shape that must NOT be called hollow: an #else branch keeps the body alive when the macro
# is off, so the function still does something in a default build.
HAS_ELSE = '''\
  void doWork() {
#ifdef SOME_FEATURE
    fancyPath();
#else
    plainPath();
#endif
  }
'''


class TestHollowDetection(unittest.TestCase):
    def test_the_canonical_hollow_symbol(self):
        self.assertEqual(co.guard_macros(AUTO_MARKING_LOCK), ["JS_GC_CONCURRENT_MARKING"])
        self.assertEqual(co.hollow_functions(AUTO_MARKING_LOCK, "JS_GC_CONCURRENT_MARKING"),
                         ["AutoMarkingLock", "~AutoMarkingLock"])

    def test_ordinary_code_is_not_hollow(self):
        self.assertEqual(co.guard_macros(FIELD_TYPE), [])
        self.assertEqual(co.hollow_functions(FIELD_TYPE, "JS_GC_CONCURRENT_MARKING"), [])

    def test_an_else_branch_is_not_hollow(self):
        # The body still runs with the macro off, so this is a build VARIANT, not a no-op.
        self.assertEqual(co.hollow_functions(HAS_ELSE, "SOME_FEATURE"), [])

    def test_lines_with_macro_off_keeps_unrelated_conditions(self):
        text = "a();\n#ifdef OTHER\nb();\n#endif\n#ifdef X\nc();\n#endif\n"
        kept = "\n".join(co.lines_with_macro_off(text, "X"))
        self.assertIn("a();", kept)
        self.assertIn("b();", kept)   # we cannot evaluate OTHER, so we keep it
        self.assertNotIn("c();", kept)

    def test_platform_and_channel_macros_are_never_considered(self):
        # Being wrong about these is expensive and being right adds nothing; they are also
        # unreachable through the moz.configure walk, so this is the second of two locks.
        for macro in ("NIGHTLY_BUILD", "DEBUG", "XP_WIN", "MOZ_DIAGNOSTIC_ASSERT_ENABLED"):
            self.assertIn(macro, co.GUARD_DENY)
            text = "  void f() {\n#ifdef %s\n    g();\n#endif\n  }\n" % macro
            self.assertEqual(co.guard_macros(text), [])


class TestSymbolSources(unittest.TestCase):
    """Both sources are needed: the corpus shows citations alone reach 1 of the 2 catchable
    refutations and the candidate's diff reaches both."""

    def test_a_diff_line_citation_yields_the_symbol_in_its_content(self):
        # This is literally how bug 2063902's dossier reaches `gc::AutoMarkingLock`.
        mech = Claim(summary="s", citations=[DiffLineCitation(
            filename="js/src/jit/BaselineCacheIRCompiler.cpp", line=2165, side="deleted",
            node="3f0439a2aec8",
            content="gc::AutoMarkingLock lock(cx->zone(), icScript->markingLock());")])
        self.assertIn("gc::AutoMarkingLock", co.mechanism_symbols(mech))

    def test_the_diff_is_ranked_by_occurrences_and_capped(self):
        lines = ["+  gc::AutoMarkingLock lock;"] * 13
        lines += ["+  ns::Other x;"] * 2
        lines += ["+  ns::Thing%d y;" % i for i in range(20)]
        diff = "\n".join(lines)
        got = co.mechanism_symbols(None, diff)
        self.assertEqual(got[0], "gc::AutoMarkingLock")
        self.assertLessEqual(len(got), co.MAX_DIFF_SYMBOLS)

    def test_diff_context_lines_are_ignored(self):
        # Only ADDED/REMOVED lines say what a patch is about; context is just neighbourhood.
        self.assertEqual(co.mechanism_symbols(None, "   gc::AutoMarkingLock lock;\n"), [])

    def test_unqualified_identifiers_are_not_looked_up(self):
        self.assertEqual(co.mechanism_symbols(None, "+  AutoMarkingLock lock;\n"), [])


def _dossier(citations=None, node="a" * 12):
    return Dossier(
        crash={"uuid": "u", "signature": "sig", "frames": []},
        verdict=Verdict(
            decision=Decision.lead, confidence=Confidence.probable,
            mechanism=Claim(summary="the marking lock's scope was narrowed",
                            citations=citations or [SearchfoxCitation(
                                permalink="https://searchfox.org/x#1",
                                symbol_id="gc::AutoMarkingLock", repo="mozilla-central")]),
        ),
        candidate=Candidate(node=node, bug=2061686, author="Jon Coppeard", channel="nightly"),
    )


class TestTheGate(unittest.TestCase):
    """Resolve online, decide offline. `apply_deterministic_gates` is shared with the eval
    runner and must stay network-free, so the lookup lives in `_resolve_compiled_out` (beside
    `_resolve_candidate_backout`) and the gate only reads what it left on the seed."""

    _HOLLOW = {"gc::AutoMarkingLock": {"macro": "JS_GC_CONCURRENT_MARKING",
                                       "functions": ["AutoMarkingLock", "~AutoMarkingLock"]}}

    def _seed(self):
        return {"uuid": "u", "channel": "nightly", "pin_rev": "b" * 12}

    def _run(self, dossier, hollow, seed=None):
        """Resolve (mocked network) then decide, which is what a real run does."""
        seed = self._seed() if seed is None else seed
        with mock.patch.object(co, "hollow_symbols", return_value=hollow) as hs, \
             mock.patch("crashclouseau.agent.patch_extract.fetch_raw_diff", return_value=""):
            orch._resolve_compiled_out(dossier, seed)
        orch._apply_compiled_out_gate(dossier, seed)
        return hs

    def test_the_gate_itself_never_touches_the_network(self):
        # The offline eval runner calls `apply_deterministic_gates` and must stay network-free;
        # an unresolved seed simply lacks the key and the gate no-ops, like `prior_hints`.
        d = _dossier()
        with mock.patch.object(co, "hollow_symbols", side_effect=AssertionError("no network!")), \
             mock.patch("crashclouseau.agent.patch_extract.fetch_raw_diff",
                        side_effect=AssertionError("no network!")):
            orch._apply_compiled_out_gate(d, {"uuid": "u", "channel": "nightly"})
        self.assertEqual(d.verdict.decision, Decision.lead)

    def test_it_suppresses_and_says_why(self):
        d = _dossier()
        self._run(d, self._HOLLOW)
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertIn("gc::AutoMarkingLock", d.verdict.abstain_reason)
        self.assertIn("JS_GC_CONCURRENT_MARKING", d.verdict.abstain_reason)
        self.assertTrue(d.corroborations["compiled_out_suppressed"])
        self.assertEqual(d.corroborations["compiled_out_macro"], "JS_GC_CONCURRENT_MARKING")

    def test_the_suppression_keeps_no_needinfo_draft(self):
        # `Verdict._consistency_rule` rejects an abstain carrying a draft outright.
        d = _dossier()
        self._run(d, self._HOLLOW)
        self.assertIsNone(d.verdict.needinfo_draft)

    def test_nothing_hollow_changes_nothing(self):
        d = _dossier()
        self._run(d, {})
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.verdict.confidence, Confidence.probable)
        self.assertNotIn("compiled_out_suppressed", d.corroborations or {})

    def test_an_abstain_costs_nothing(self):
        """The gate runs last precisely because it spends network. ~80% of runs abstain, and
        those must not pay for a lookup."""
        d = _dossier()
        d.verdict = Verdict(decision=Decision.abstain, confidence=Confidence.low,
                            abstain_reason="nothing found")
        hs = self._run(d, self._HOLLOW)
        hs.assert_not_called()

    def test_a_lookup_failure_leaves_the_verdict_alone(self):
        d, seed = _dossier(), self._seed()
        with mock.patch.object(co, "hollow_symbols", side_effect=RuntimeError("searchfox down")), \
             mock.patch("crashclouseau.agent.patch_extract.fetch_raw_diff", return_value=""):
            orch._resolve_compiled_out(d, seed)
        orch._apply_compiled_out_gate(d, seed)
        self.assertNotIn("compiled_out", seed)
        self.assertEqual(d.verdict.decision, Decision.lead)

    def test_it_reads_the_moz_configure_of_the_crash_build(self):
        # Pinned reads only started working in `2d71e11`; a switch's default can change, and the
        # build that crashed is the one whose configure decides.
        d = _dossier()
        hs = self._run(d, self._HOLLOW)
        self.assertEqual(hs.call_args.kwargs["rev"], "b" * 12)
        self.assertEqual(hs.call_args.kwargs["channel"], "nightly")

    def test_no_mechanism_no_check(self):
        d = _dossier()
        d.verdict = Verdict(decision=Decision.lead, confidence=Confidence.medium,
                            mechanism=None,
                            needinfo_draft="Could you take a look?")
        hs = self._run(d, self._HOLLOW)
        hs.assert_not_called()
        self.assertEqual(d.verdict.decision, Decision.lead)


class TestItIsActuallyWiredIn(unittest.TestCase):
    """This repo has shipped three recovery paths that never fired in prod, so the wiring gets
    its own test rather than being assumed. It also earns the right for the other pipeline test
    modules to stub the resolver out: if they hid a wiring regression, this would still catch it."""

    def test_run_evidence_agent_resolves_compiled_out_before_the_gates(self):
        from crashclouseau.agent.result import CrashTriageResult

        seed = {"uuid": "u-1", "signature": "S", "channel": "nightly", "stack": "#0 f a:1",
                "candidates": [], "experts": [], "raw_crash": {}, "is_offstack": False,
                "build_node": "bn", "pin_rev": "bn"}
        result = CrashTriageResult(num_turns=1, total_cost_usd=0.1, result="ok",
                                   dossier=_dossier(), actions=[])
        MDoss = mock.MagicMock()
        MDoss.get_by_uuid.return_value = None
        MDoss.skip_triage.return_value = False
        MDoss.claim_running.return_value = True

        async def fake(**kw):
            return result

        with mock.patch.object(orch, "_resolve_compiled_out") as resolve, \
             mock.patch.object(orch, "_proto_already_triaged", return_value=False), \
             mock.patch.object(orch.models, "Dossier", MDoss), \
             mock.patch.object(orch.models, "Verdict", mock.MagicMock()), \
             mock.patch.object(orch.models, "commit"), \
             mock.patch.object(orch, "build_seed", return_value=seed), \
             mock.patch.object(orch, "_seed_score", return_value=5), \
             mock.patch.object(orch, "_resolve_candidate_backout"), \
             mock.patch.object(orch, "_resolve_candidate_git_commit"), \
             mock.patch("crashclouseau.agent.triage.run_crash_triage", fake):
            orch.run_evidence_agent("u-1")
        resolve.assert_called_once()
        self.assertIs(resolve.call_args.args[1], seed)


if __name__ == "__main__":
    unittest.main()
