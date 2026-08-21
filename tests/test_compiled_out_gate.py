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


class TestThePageSaysWhatHappened(unittest.TestCase):
    """crashstack.html had abstain branches for both backout suppressions and none for this one,
    so a suppressed run rendered "Insufficient evidence -- no culprit identified." followed by
    the reason that says we DID find a changeset. The template's own comment calls that "reads
    as a failure to look"."""

    def _page(self):
        import pathlib
        here = pathlib.Path(__file__).resolve().parent.parent
        return (here / "templates" / "crashstack.html").read_text()

    def test_there_is_a_branch_for_the_compiled_out_suppression(self):
        page = self._page()
        self.assertIn("corrob.get('compiled_out_suppressed')", page)
        head = page.index("Insufficient evidence")
        self.assertLess(page.index("corrob.get('compiled_out_suppressed')"), head)

    def test_the_wording_follows_the_provenance(self):
        page = self._page()
        self.assertIn("corrob.get('compiled_out_provenance') == 'mechanism'", page)
        self.assertIn("the mechanism found rests on", page)
        self.assertIn("is mostly about", page)


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


# `moz.configure` at build node 8e966e6c894a, lines 140-178, verbatim modulo blank lines.
MOZ_CONFIGURE = '''\
option(
    "--enable-debug",
    nargs="?",
    help="Enable building with developer debug info (using the given compiler flags)",
)


@depends("--enable-debug")
def moz_debug(debug):
    if debug:
        return bool(debug)


set_config("MOZ_DEBUG", moz_debug)
set_define("MOZ_DEBUG", moz_debug)

set_config(
    "MOZ_DIAGNOSTIC_ASSERT_ENABLED",
    True,
    when=moz_debug | milestone.is_nightly | moz_dev_edition,
)
set_define(
    "MOZ_DIAGNOSTIC_ASSERT_ENABLED",
    True,
    when=moz_debug | milestone.is_nightly | moz_dev_edition,
)
'''


class TestTheDenyListIsTheLock(unittest.TestCase):
    """`GUARD_DENY` is load-bearing, not a second lock on a door that is already shut.

    An earlier version of `compiled_out`'s comment justified the list with "DEBUG /
    MOZ_DIAGNOSTIC_ASSERT_ENABLED have no `set_define` at all". That is false for the second one
    (moz.configure:174), and the pair below shows what actually saves us: the bare-identifier
    REGEX. The very `option("--enable-debug")` with no `default=` that sits above it resolves
    perfectly well for the neighbouring macro whose `set_define` IS a bare name."""

    def _client(self, snippet):
        client = mock.MagicMock()
        client.search.return_value = [mock.MagicMock(file="moz.configure", text=snippet)]
        return client

    def test_the_three_lists_still_add_up_to_the_one_the_gate_uses(self):
        self.assertEqual(co.GUARD_DENY, co.BUILD_TYPE_DENY | co.PLATFORM_DENY)
        self.assertLess(co.CHANNEL_ON_DENY, co.BUILD_TYPE_DENY)
        self.assertFalse(co.BUILD_TYPE_DENY & co.PLATFORM_DENY)

    def test_a_bare_identifier_set_define_resolves_all_the_way_to_default_off(self):
        with mock.patch.object(co, "_configure_text", return_value=MOZ_CONFIGURE):
            self.assertTrue(co._option_is_default_off(
                "MOZ_DEBUG", self._client('set_define("MOZ_DEBUG", moz_debug)')))

    def test_the_diagnostic_assert_macro_is_refused_by_the_regex_not_by_absence(self):
        snippet = ('set_define(\n    "MOZ_DIAGNOSTIC_ASSERT_ENABLED",\n    True,\n'
                   "    when=moz_debug | milestone.is_nightly | moz_dev_edition,\n)")
        self.assertIn("set_define", snippet)          # it exists; the old comment said it did not
        with mock.patch.object(co, "_configure_text", return_value=MOZ_CONFIGURE):
            self.assertFalse(co._option_is_default_off(
                "MOZ_DIAGNOSTIC_ASSERT_ENABLED", self._client(snippet)))
        self.assertIn("MOZ_DIAGNOSTIC_ASSERT_ENABLED", co.CHANNEL_ON_DENY)


class TestAnUnreadableConfigureIsNotAnAnswer(unittest.TestCase):
    """The whole gate is one empty `pin_rev` away from dead, and used to be silent about it.

    `_configure_text` returning "" makes `_option_is_default_off` `continue`, which is
    indistinguishable from "this macro is not established off": no suppression, no corroboration,
    no log line. m-c `tip` is periodically a `.hgtags`-only commit whose manifest holds no
    `js/moz.configure`, and `rev or "tip"` is exactly what an empty `pin_rev` falls back to."""

    def test_empty_is_logged(self):
        with mock.patch("crashclouseau.hgedge.raw_file", return_value=""), \
             self.assertLogs(level="WARNING") as caught:
            self.assertEqual(co._configure_text("js/moz.configure", "nightly", ""), "")
        said = "\n".join(caught.output)
        self.assertIn("came back EMPTY", said)
        self.assertIn("pin_rev", said)

    def test_a_real_read_says_nothing(self):
        with mock.patch("crashclouseau.hgedge.raw_file", return_value="option(...)"):
            self.assertEqual(co._configure_text("js/moz.configure", "nightly", "b" * 12),
                             "option(...)")


# `js/moz.configure` at buildid 20260810154837 (rev 1e704c6738f4) and at 20260811085340 (rev
# 11b07d869739). ABRIDGED, not verbatim: the real `gc_concurrent_marking` body also `die()`s off
# x86_64 and there is a `set_config` beside the `set_define`, none of which the walk reads. What
# is verbatim is every line the walk touches. The ONLY difference between the two is the deleted
# `default=False,`
# -- the feature is default-off on both sides, and the shipped walk answered False on the left
# and True on the right for three weeks.
GC_CONFIGURE_BEFORE = '''\
option(
    "--enable-gc-concurrent-marking",
    default=False,
    help="{Enable|Disable} experimental support for GC concurrent marking",
)


@depends("--enable-gc-concurrent-marking", target)
def gc_concurrent_marking(value, target):
    if not value:
        return
    return True


set_define("JS_GC_CONCURRENT_MARKING", gc_concurrent_marking)
'''
GC_CONFIGURE_AFTER = GC_CONFIGURE_BEFORE.replace("    default=False,\n", "")


class TestALiteralDefaultIsEvidence(unittest.TestCase):
    """`default=False` is the strongest thing a switch can say and the walk used to refuse it.

    Measured over the 52-filing panel's 26 build revs: the shipped predicate answers False at 14
    and True at 12, flipping at 11b07d869739 where `default=False,` was DELETED. 27 of the 52
    filings sit on the blind side of that edit, which changed a coding style and nothing else."""

    def _client(self, snippet='set_define("JS_GC_CONCURRENT_MARKING", gc_concurrent_marking)'):
        client = mock.MagicMock()
        client.search.return_value = [mock.MagicMock(file="js/moz.configure", text=snippet)]
        return client

    def test_the_answer_is_the_same_on_both_sides_of_the_style_edit(self):
        for label, text in (("before", GC_CONFIGURE_BEFORE), ("after", GC_CONFIGURE_AFTER)):
            with self.subTest(rev=label), \
                 mock.patch.object(co, "_configure_text", return_value=text):
                self.assertEqual(co._default_off_switch("JS_GC_CONCURRENT_MARKING",
                                                        self._client()),
                                 "--enable-gc-concurrent-marking")

    def test_the_shipped_predicate_really_did_differ(self):
        # The counter-example, executable: `default=` used to be an unconditional refusal.
        self.assertIn("default=False", GC_CONFIGURE_BEFORE)
        self.assertNotIn("default=", GC_CONFIGURE_AFTER)

    def test_every_non_literal_default_is_still_declined(self):
        for value in ("milestone.is_nightly", "depends(when=moz_debug)", "jit_default",
                      "True", "1"):
            with self.subTest(default=value):
                call = '("--enable-x", default=%s, help="h")' % value
                self.assertFalse(co._default_is_off(call))

    def test_the_literals_it_does_read(self):
        for value in ("False", "0", "None", '""', "''"):
            with self.subTest(default=value):
                self.assertTrue(co._default_is_off('("--enable-x", default=%s)' % value))
        self.assertTrue(co._default_is_off('("--enable-x", help="h")'))

    def test_a_default_written_inside_a_help_string_is_not_the_argument(self):
        # A false "off" costs a real lead, so the one direction that must not happen.
        self.assertFalse(co._default_is_off(
            '("--enable-x", default=jit_default, help="off when default=False")'))

    def test_the_boolean_face_still_answers_for_the_deny_list(self):
        # `_option_is_default_off` is what every docstring and the prompt talk about; it is now
        # `bool(_default_off_switch(...))`. Verified 20/20 against the real GUARD_DENY list at
        # build node 477c0df9965c; here, the shape that matters.
        with mock.patch.object(co, "_configure_text", return_value=MOZ_CONFIGURE):
            self.assertTrue(co._option_is_default_off(
                "MOZ_DEBUG", self._client('set_define("MOZ_DEBUG", moz_debug)')))
        with mock.patch.object(co, "_configure_text", return_value=""):
            self.assertFalse(co._option_is_default_off(
                "MOZ_DEBUG", self._client('set_define("MOZ_DEBUG", moz_debug)')))


class TestWhichSentenceMayBePublished(unittest.TestCase):
    """`statement_provenance` reads the text we actually posted to the bug.

    Panel: 2 of 2 firing filings name `AutoMarkingLock` in their mechanism statement, 0 of the
    other 50 do. The wrong-direction case is the common one -- 45 of 52 filings carry at least
    one diff-derived symbol the published prose never names."""

    def test_the_statement_names_it(self):
        mech = Claim(statement="the narrowed `gc::AutoMarkingLock` scope lets the stub be freed",
                     citations=[SearchfoxCitation(permalink="https://searchfox.org/x#1",
                                                  symbol_id="s", repo="mozilla-central")])
        self.assertEqual(co.statement_provenance("gc::AutoMarkingLock", mech), "mechanism")

    def test_the_last_component_counts_because_prose_drops_the_namespace(self):
        mech = Claim(statement="AutoMarkingLock is taken before the stub is attached",
                     citations=[SearchfoxCitation(permalink="https://searchfox.org/x#1",
                                                  symbol_id="s", repo="mozilla-central")])
        self.assertEqual(co.statement_provenance("gc::AutoMarkingLock", mech), "mechanism")

    def test_a_longer_identifier_is_not_a_mention(self):
        # Bug 2063782's own case: a plain `in` test reads `AttachBaselineCacheIRStubLocked` as a
        # mention of `AttachBaselineCacheIRStub`. 15 slots on the panel, 13% -> 19%, no truth.
        mech = Claim(statement="AttachBaselineCacheIRStubLocked drops the lock too early",
                     citations=[SearchfoxCitation(permalink="https://searchfox.org/x#1",
                                                  symbol_id="s", repo="mozilla-central")])
        self.assertEqual(
            co.statement_provenance("js::jit::AttachBaselineCacheIRStub", mech), "diff")

    def test_a_boundary_does_not_break_the_qualified_case(self):
        # `::` is not a word character, so naming the type is still naming it.
        mech = Claim(statement="JS::shadow::Zone is read after the sweep",
                     citations=[SearchfoxCitation(permalink="https://searchfox.org/x#1",
                                                  symbol_id="s", repo="mozilla-central")])
        self.assertEqual(co.statement_provenance("gc::Zone", mech), "mechanism")

    def test_no_statement_is_diff(self):
        self.assertEqual(co.statement_provenance("gc::AutoMarkingLock", None), "diff")
        self.assertEqual(co.statement_provenance("gc::AutoMarkingLock", {}), "diff")
        self.assertEqual(co.statement_provenance("gc::AutoMarkingLock",
                                                 {"statement": "unrelated"}), "diff")


class TestTheTwoRefutationsAndTheSeventeenControls(unittest.TestCase):
    """The counter-examples, case by case.

    Bugs 2063782 and 2063902 (jcoppeard, "It does not" / "Concurrent marking is not compiled in
    by default") must STILL be suppressed -- note both name the SAME candidate 3f0439a2aec8, so
    "2 true positives" is really n=1. BOTH take the `mechanism` branch (their statements name the
    symbol); the `diff` branch has NO instance on the panel, so its test below pins a wording,
    not a case. The 17 filings a human FIXED or DUPLICATED must stay
    unsuppressed; on the replay each of them resolves 1-10 symbols and 0 hollow ones, so the gate
    never reaches the switch walk at all. Bug 2062114 is the documented miss and stays uncaught
    (its statement names `BufferAllocator::TraceEdge`; nothing in its 5 symbols is hollow)."""

    _FOUND = {"symbol": "gc::AutoMarkingLock", "macro": "JS_GC_CONCURRENT_MARKING",
              "functions": ["AutoMarkingLock", "~AutoMarkingLock"],
              "switch": "--enable-gc-concurrent-marking",
              "rev": "477c0df9965c2460b1582048e8ff9cb333dd2556"}

    def _suppressed(self, provenance):
        d = _dossier()
        seed = {"uuid": "u", "compiled_out": dict(self._FOUND, provenance=provenance)}
        orch._apply_compiled_out_gate(d, seed)
        return d

    def test_2063902_the_statement_names_it_so_the_mechanism_wording_stands(self):
        d = self._suppressed("mechanism")
        self.assertEqual(d.verdict.decision, Decision.abstain)
        self.assertIn("the mechanism rests on `gc::AutoMarkingLock`", d.verdict.abstain_reason)
        self.assertEqual(d.corroborations["compiled_out_provenance"], "mechanism")

    def test_the_diff_derived_wording_suppresses_but_claims_less(self):
        # NOT bug 2063782: its mechanism paragraph names `gc::AutoMarkingLock`, so
        # `statement_provenance` puts it on the `mechanism` branch too and the diff branch fires
        # on 0 of the 52 -- this pins the wording for the 234-of-269 case that has not happened
        # yet. Clamping instead is refused because `min_confidence: 70` means a clamp to
        # `probable` still files the bug.
        d = self._suppressed("diff")
        self.assertEqual(d.verdict.decision, Decision.abstain)
        said = d.verdict.abstain_reason
        self.assertIn("the candidate's changeset is mostly about", said)
        self.assertIn("doubt the CANDIDATE", said)
        self.assertNotIn("the mechanism rests on", said)
        self.assertEqual(d.corroborations["compiled_out_provenance"], "diff")

    def test_both_wordings_name_the_switch_and_the_build_rev(self):
        for provenance in ("mechanism", "diff"):
            with self.subTest(provenance=provenance):
                said = self._suppressed(provenance).verdict.abstain_reason
                self.assertIn("--enable-gc-concurrent-marking", said)
                self.assertIn("477c0df9965c", said)

    def test_the_rev_is_recorded_so_the_answer_can_be_reproduced(self):
        d = self._suppressed("mechanism")
        self.assertEqual(d.corroborations["compiled_out_rev"],
                         "477c0df9965c2460b1582048e8ff9cb333dd2556")

    def test_an_unresolved_switch_or_rev_degrades_to_the_old_wording(self):
        d = _dossier()
        orch._apply_compiled_out_gate(d, {"uuid": "u", "compiled_out": {
            "symbol": "gc::AutoMarkingLock", "macro": "JS_GC_CONCURRENT_MARKING",
            "functions": ["AutoMarkingLock"], "provenance": "mechanism"}})
        self.assertIn("off unless someone asks for it", d.verdict.abstain_reason)

    def test_the_seventeen_controls_never_reach_the_switch_walk(self):
        # A filing with no hollow symbol leaves `seed["compiled_out"]` unset, which is the only
        # thing the gate reads -- so the relaxation cannot touch any of the 17.
        d = _dossier()
        orch._apply_compiled_out_gate(d, {"uuid": "u"})
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertNotIn("compiled_out_suppressed", d.corroborations or {})

    def test_the_resolver_stamps_provenance_and_rev_onto_the_seed(self):
        d, seed = _dossier(), {"uuid": "u", "channel": "nightly", "pin_rev": "c" * 12}
        hollow = {"gc::AutoMarkingLock": {"macro": "JS_GC_CONCURRENT_MARKING",
                                          "functions": ["AutoMarkingLock"],
                                          "switch": "--enable-gc-concurrent-marking"}}
        with mock.patch.object(co, "hollow_symbols", return_value=hollow), \
             mock.patch("crashclouseau.agent.patch_extract.fetch_raw_diff", return_value=""):
            orch._resolve_compiled_out(d, seed)
        self.assertEqual(seed["compiled_out"]["rev"], "c" * 12)
        # `_dossier()`'s mechanism has no `statement`, which is the diff-derived case.
        self.assertEqual(seed["compiled_out"]["provenance"], "diff")


class TestWhatGroundsAFail(unittest.TestCase):
    """`is_build_flag_ground` decides whether a skeptic `fail` may sink a lead. Firing means the
    `fail` stops binding and the deterministic gate decides instead.

    THE PANEL IS REAL. Every note below is copied out of `spike/_dossier_dump.jsonl` (1996 prod
    dossiers, 2026-07-06..08-05), which is also where the predicate was fitted: the first draft
    fired on 21 of 1765 `fail`s and changed 2 of the 216 stored binding vetoes -- and BOTH of
    those two were wrong, which is what removed its pref arm and its `default=` clause. The
    shipped version fires on 5 of 1765 and changes 0 of 216."""

    #: The shapes the rule exists for -- a configure switch, a cargo feature, a bare guard macro.
    FIRES = [
        # The jcoppeard shape, twice, as the skeptic actually wrote it.
        "`gc::AutoMarkingLock`/`JS_GC_CONCURRENT_MARKING` is disabled by default "
        "(`--enable-gc-concurrent-marking` default=False, x86_64 CI-only variant) so the lock is "
        "a no-op on official Nightly",
        "every touched line sits behind concurrent marking, which is compile-time gated off "
        "(`JS_GC_CONCURRENT_MARKING` undefined) and runtime pref-gated off",
        # The "other 11 of 39" bucket: correct kills the deterministic gate cannot see. Unbinding
        # them is the cost this predicate knowingly pays.
        "All three patches touch code reachable only via the WebRender `replay` cargo feature. "
        "Not compiled into / not exercised by an ordinary Nightly user's crash.",
        # The macro the walk resolves and gets WRONG: it is on in every Nightly.
        "MOZ_DIAGNOSTIC_ASSERT_ENABLED is not defined in this build, so the assertion is gone",
    ]
    #: The wrong direction. Every one of these MUST keep its teeth.
    KEEPS = [
        # THE counter-example: crash 560c0f2f-07cc-46c6-950c-1d8240260731, a BINDING and CORRECT
        # veto, and the shape of 15 of the 39 build-guard fails / 3 of the 8 binding vetoes.
        "GTK-gated Linux ibus/fcitx key-event plumbing, not compiled into Windows builds",
        # An opt Nightly really is a non-DEBUG build (mfbt/Assertions.h:563). 4 real notes, all
        # correct, and none of them needs a moz.configure walk.
        "The entire changed assertion block is inside `#ifdef DEBUG`, which is compiled out of "
        "Nightly's shipped opt build",
        "Removes a `MOZ_ASSERT`-only invariant check; compiled out of release builds, zero "
        "runtime behavior change",
        # The two the first draft got WRONG, both structural kills with a build-flag aside.
        "`mcp__history__blame` on the exact crashing functions attributes every line to older, "
        "unrelated bugs -- none to 9005591b06bb. The patch only removes now-dead "
        "`#ifdef ENABLE_EXPLICIT_RESOURCE_MANAGEMENT` guards around a compile flag whose default "
        "was already `True` on nightly.",
        "it only enlarges `RoundedRect`'s per-element size, adds no new append call, doesn't "
        "touch `nsFlexContainerFrame`, and its feature is pref-gated off by default",
        # The skeptic REFUTING a pref argument. A pref arm read this as a pref kill.
        "Pref actually defaults to 2 (pool enabled by default on nightly) per "
        "StaticPrefList.yaml -- the 'disabled by default' mitigating argument was wrong.",
        # Ordinary contradictions -- no build-flag token at all, so the predicate never looks.
        "the cited diff line 2165 is not present in changeset ff789e9f149e",
        "mLength is not defined on that type; field_layout returns no field at 0x8",
        "the candidate landed after the build that crashed",
        "",
    ]

    def test_the_shapes_it_exists_for(self):
        for note in self.FIRES:
            self.assertTrue(co.is_build_flag_ground(note), note)

    def test_the_wrong_direction(self):
        for note in self.KEEPS:
            self.assertFalse(co.is_build_flag_ground(note), note)

    def test_a_platform_word_vetoes_the_veto_even_beside_a_configure_switch(self):
        # Checked before the predicate can fire, and deliberately wider than PLATFORM_DENY
        # because the skeptic writes prose. A platform claim is one of the two build questions
        # the crash report answers by itself, off the `OS:` line `triage._crash_facts` emits.
        self.assertFalse(co.is_build_flag_ground(
            "MOZ_WIDGET_GTK is not defined in a Windows build (widget/moz.configure)"))

    def test_a_channel_on_macro_beats_the_build_type_veto(self):
        # Order matters: "MOZ_DIAGNOSTIC_ASSERT_ENABLED is off" is wrong by construction even
        # when the same note also says `#ifdef DEBUG`.
        self.assertTrue(co.is_build_flag_ground(
            "the block is under `#ifdef DEBUG` / MOZ_DIAGNOSTIC_ASSERT_ENABLED, not compiled in"))

    def test_ndebug_is_on_in_an_opt_build_so_a_fail_resting_on_it_must_unbind(self):
        """`NDEBUG` reads like `DEBUG`'s twin and is its inverse.

        `moz.configure`'s `debug_defines` returns `["NDEBUG", "TRIMMED"]` for a non-debug
        build, so the official opt Nightly DEFINES it -- the one claim in the 8901-claim prod
        dump that mentions it says exactly that ("Nightly defines NDEBUG", a `pass`, on ANGLE
        asserts). If `NDEBUG` were grouped with `DEBUG` the prompt would tell the model the
        opposite of the truth AND `_BUILD_TYPE_GROUND` would then make the resulting `fail`
        BIND, turning a live lead into an abstain nothing downstream can count."""
        self.assertTrue(co.is_build_flag_ground(
            "the changed block sits inside `#ifdef NDEBUG`, not compiled into this build"))
        self.assertIn("NDEBUG", co.CHANNEL_ON_DENY)
        # ... while its twin, which really is off in an opt build, keeps its teeth.
        self.assertFalse(co.is_build_flag_ground(
            "the changed block sits inside `#ifdef DEBUG`, not compiled into this build"))

    def test_a_lowercase_debug_is_not_the_debug_macro(self):
        # `_BUILD_TYPE_GROUND` is case-sensitive on purpose: "replay debug tooling" is a cargo
        # feature note and must still unbind.
        self.assertTrue(co.is_build_flag_ground(
            "gated behind `#[cfg(feature = \"replay\")]` -- a dev-only capture/replay debug "
            "path never compiled into the normal compositing hot path"))

    def test_a_citation_can_carry_the_ground(self):
        self.assertTrue(co.is_build_flag_ground(
            "the guard is not enabled",
            [{"kind": "source", "path": "js/moz.configure", "line": 1077}]))

    def test_it_needs_both_halves(self):
        # A macro name alone is not a compile-flag CLAIM, and an "off" word alone is not either.
        self.assertFalse(co.is_build_flag_ground("JS_GC_CONCURRENT_MARKING narrows the barrier"))
        self.assertFalse(co.is_build_flag_ground("that branch is not taken for this input"))


if __name__ == "__main__":
    unittest.main()
