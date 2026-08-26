# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# The build-flag deny lists were NIGHTLY'S, and on a beta crash they are wrong in BOTH
# directions -- which is the whole reason no single relaxation could have fixed them.
#
# Read from mozilla-beta's OWN source on 2026-08-25 (plan #18 item 14):
# `build/moz.configure/init.configure` -- "if we have 'a1' in GRE_MILESTONE, we're building
# Nightly (define NIGHTLY_BUILD) - otherwise, we're building Release/Beta";
# `set_define("RELEASE_OR_BETA", milestone.is_release_or_beta)`; and
# `is_early_beta_or_earlier = is_nightly` ("EARLY_BETA_OR_EARLIER is an alias for
# NIGHTLY_BUILD, pending its removal"). `moz.configure` --
# `set_define("MOZ_DIAGNOSTIC_ASSERT_ENABLED", True, when=moz_debug | milestone.is_nightly |
# moz_dev_edition)`.
#
# So THREE of the five "ON, never conclude off" macros are OFF on beta (NIGHTLY_BUILD,
# EARLY_BETA_OR_EARLIER, MOZ_DIAGNOSTIC_ASSERT_ENABLED) and ONE of the three "genuinely off"
# ones is ON (RELEASE_OR_BETA).
#
# WHAT EACH DIRECTION COSTS, because they are not symmetric. A note that matches the ON list
# makes `is_build_flag_ground` return True, which UNBINDS the skeptic's `fail`: the lead
# survives and gets filed, i.e. a needinfo a human closes -- loud, recoverable. A note that
# matches the OFF list returns False, which BINDS: `Dossier._skeptic_veto` turns the lead into
# an ABSTAIN, and an abstain files nothing, skips the second opinion (orchestrator.py:2317) and
# never reaches `Feedback` -- so a wrong bind is invisible forever. That asymmetry is why the
# binding half lives in code rather than in the prompt, and why it gets a test per direction.
#
# THE NIGHTLY REPLAY SAYS NOTHING ABOUT BETA, and this file measures that rather than assuming
# it. Over spike/_dossier_dump.jsonl (1,996 prod dossiers 2026-07-06..08-05; 8,901 skeptic
# claims, 1,765 `fail`s), measured 2026-08-25:
#
#   * 0 of the 1,765 `fail`s change `is_build_flag_ground`'s answer between the nightly and
#     the beta partition. Nightly is provably untouched at the note level, and beta's firing
#     rate is unmeasured -- exactly as item 14 warns ("measure the new firing rate on beta
#     before trusting it"; the v109 audit had all five `compiled_out_*` corroborations firing
#     0/197 on nightly).
#   * 34 claims (15 of them `fail`s) name NIGHTLY_BUILD -- and all 15 are pref-flip notes
#     ("`@IS_NIGHTLY_BUILD@` already evaluates true on the Nightly channel this crash is on")
#     that never reach the partition at all: neither channel's answer gets past the grounding
#     check. The macro is common in prose and absent from the decision.
#   * 4 claims (1 `fail`) name EARLY_BETA_OR_EARLIER. **0 of 8,901 name RELEASE_OR_BETA**, so
#     the direction that BINDS on beta has no back-test anywhere and rests entirely on the
#     source read above.
#   * 37 claims (2 `fail`s) name MOZ_DIAGNOSTIC_ASSERT: 30 with the bare spelling, 7 with
#     `_ENABLED`, 0 with both. See TestDiagnosticAssertKeysOnTheRawChannel for why only the
#     `_ENABLED` spelling is pinned here.
#
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_beta_compiled_out
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import contextvars  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import compiled_out as co, config, utils  # noqa: E402
from crashclouseau.agent import orchestrator as orch, roles  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    Candidate,
    Claim,
    Confidence,
    Decision,
    Dossier,
    SearchfoxCitation,
    SkepticResult,
    SkepticStatus,
    Verdict,
)

_SF = SearchfoxCitation(
    permalink="https://searchfox.org/x#1", symbol_id="_Z1", repo="mozilla-beta"
)

# The two note strings plan #18 item 14 names, verbatim. Both pass the grounding check, so both
# reach the partition -- which is the only reason they can demonstrate the inversion at all
# (contrast the 15 real NIGHTLY_BUILD `fail`s in the dump, which do not).
NIGHTLY_GATED_NOTE = "behind `#ifdef NIGHTLY_BUILD`, not compiled into this build"
RELEASE_OR_BETA_NOTE = "this `#ifdef RELEASE_OR_BETA` code is not in the build"

# The verbatim ground of a binding veto we got RIGHT on nightly (crash
# 560c0f2f-07cc-46c6-950c-1d8240260731, `mozilla::FileBlockCache::Flush`, Windows NT 10.0.19044,
# candidate ff789e9f149e touching 4 of 6 files under widget/gtk/). Kept here because the
# per-channel partition must not have opened a hole under it: 15 of the 39 build-guard `fail`s
# in the month are this shape and a platform claim is channel-INDEPENDENT.
GTK_NOTE = "GTK-gated Linux ibus/fcitx key-event plumbing, not compiled into Windows builds"

# `js/src/gc/Cell.h`'s `AutoMarkingLock` SHAPE -- a class whose every function body sits inside
# one `#ifdef` -- with the guard swapped for the one that matters off nightly. Deliberately not
# a real symbol name: nobody sampled a NIGHTLY_BUILD-hollow symbol out of beta's tree for this
# test, and naming one would be a fact this file cannot back. What is real is the shape: on the
# three confirmed nightly owner refutations the guarded thing was a hollow class every time.
NIGHTLY_ONLY_PROBE = '''\
// This is a no op outside nightly builds.
class MOZ_RAII AutoNightlyProbe {
#ifdef NIGHTLY_BUILD
  Probe* probe = nullptr;
#endif

  AutoNightlyProbe(const AutoNightlyProbe& other) = delete;

 public:
  AutoNightlyProbe(JS::Zone* zone) {
#ifdef NIGHTLY_BUILD
    probe = Probe::For(zone);
    probe->enter();
#endif
  }

  ~AutoNightlyProbe() {
#ifdef NIGHTLY_BUILD
    if (probe) {
      probe->exit();
    }
#endif
  }
};
'''

_ALL_CHANNELS = ("nightly", "beta", "aurora", "release")


def _fake_searchfox(source):
    """A searchfox client that answers `define` with *source* and screams if the
    `moz.configure` walk is attempted -- because for a milestone-gated macro there is no
    `option()` to find, so reaching the walk at all is the bug."""
    client = mock.MagicMock()
    client.define.return_value = mock.MagicMock(source=source)
    client.search.side_effect = AssertionError(
        "the moz.configure walk was entered for a milestone-gated macro"
    )
    return client


def _lead_with(note, channel="beta"):
    """A model-emitted LEAD whose single skeptic entry is a `fail` on *note* -- the shape
    `_skeptic_veto` rule (1c) decides, and the shape bug 2063782 had."""
    return Dossier(
        crash={"uuid": "u", "signature": "sig", "frames": []},
        verdict=Verdict(
            decision=Decision.lead,
            confidence=Confidence.probable,
            mechanism=Claim(summary="nightly-gated probe reentry", citations=[_SF]),
        ),
        candidate=Candidate(node="a" * 12, bug=1, author="A", channel=channel),
        skeptic=[SkepticResult(status=SkepticStatus.failed, claim_ref="mechanism", note=note)],
    )


def _run_channel_seen(raw_crash=None, channel="beta"):
    """Run `orchestrator.run_evidence_agent` over a stubbed pipeline and report the build
    channel -- and the partition -- that was live at the moment the dossier was parsed, which
    is the moment `Dossier._skeptic_veto` runs.

    Everything that would touch the network or the db is stubbed; the `contextvars` copy keeps
    the run's channel out of the rest of the suite, because `run_evidence_agent` deliberately
    never resets it (one RQ job is one crash)."""
    from crashclouseau.agent.result import CrashTriageResult

    seed = {"uuid": "u-b", "signature": "S", "channel": channel, "stack": "#0 f a:1",
            "candidates": [], "experts": [], "raw_crash": raw_crash or {},
            "is_offstack": False, "build_node": "bn", "pin_rev": "bn"}
    seen = {}

    async def fake(**kw):
        seen.update(channel=co.build_channel(), on=co.channel_on_deny(),
                    off=co.channel_off(), guard=co.guard_deny())
        return CrashTriageResult(num_turns=1, total_cost_usd=0.1, result="ok",
                                 dossier=_lead_with(GTK_NOTE), actions=[])

    MDoss = mock.MagicMock()
    MDoss.get_by_uuid.return_value = None
    MDoss.skip_triage.return_value = False
    MDoss.claim_running.return_value = True
    # `_autofile` is stubbed rather than left live: a channel-wiring test has no business in
    # the filing path, and it swallows its own exceptions, so a real call would be silent noise
    # with a Bugzilla shape.
    with mock.patch.object(orch, "_resolve_compiled_out"), \
         mock.patch.object(orch, "_autofile"), \
         mock.patch.object(orch, "_proto_already_triaged", return_value=False), \
         mock.patch.object(orch.models, "Dossier", MDoss), \
         mock.patch.object(orch.models, "Verdict", mock.MagicMock()), \
         mock.patch.object(orch.models, "commit"), \
         mock.patch.object(orch, "build_seed", return_value=seed), \
         mock.patch.object(orch, "_seed_score", return_value=5), \
         mock.patch.object(orch, "_resolve_candidate_backout"), \
         mock.patch.object(orch, "_resolve_candidate_git_commit"), \
         mock.patch("crashclouseau.agent.triage.run_crash_triage", fake):
        contextvars.copy_context().run(orch.run_evidence_agent, "u-b")
    return seen


def _prompt_lists(text):
    """The two macro lists the skeptic prompt actually renders, parsed the way
    `test_compiled_out_guard.py` already parses them."""
    always_on = text.split('Never conclude "off" for ')[1].split(": they are ON")[0]
    the_opposite = text.split(" are the opposite")[0].rsplit(". ", 1)[-1]
    return always_on, the_opposite


class TestTheChannelPartition(unittest.TestCase):
    """One table per channel, and `nightly` must be the old one to the byte.

    The three lists are the gate's whole vocabulary: `is_build_flag_ground` reads them apart,
    `guard_macros` subtracts one of them, and `roles._compiled_out_text` renders all of them
    into the skeptic prompt. A channel that quietly loses a macro therefore fails open in a
    different way in each of the three, so the partition is pinned as a partition -- both
    halves, every channel -- and not as five membership assertions."""

    def test_the_channel_partition(self):
        # Beta: what init.configure says, in the terms the gate uses.
        for macro in ("NIGHTLY_BUILD", "EARLY_BETA_OR_EARLIER", "MOZ_DIAGNOSTIC_ASSERT_ENABLED"):
            with self.subTest(macro=macro, channel="beta"):
                self.assertIn(macro, co.channel_off("beta"))
                self.assertNotIn(macro, co.channel_on_deny("beta"))
        self.assertIn("RELEASE_OR_BETA", co.channel_on_deny("beta"))
        self.assertNotIn("RELEASE_OR_BETA", co.channel_off("beta"))

        # ...and the exact inverse on nightly, which is the claim "no single relaxation fixes
        # it" made executable: every one of the four macros swaps sides.
        for macro in ("NIGHTLY_BUILD", "EARLY_BETA_OR_EARLIER", "MOZ_DIAGNOSTIC_ASSERT_ENABLED"):
            with self.subTest(macro=macro, channel="nightly"):
                self.assertIn(macro, co.channel_on_deny("nightly"))
                self.assertNotIn(macro, co.channel_off("nightly"))
        self.assertIn("RELEASE_OR_BETA", co.channel_off("nightly"))
        self.assertNotIn("RELEASE_OR_BETA", co.channel_on_deny("nightly"))

    def test_nightly_did_not_move(self):
        """The legacy constants ARE the nightly entry. A year of docstrings, the shipped
        skeptic prompt and `tests/test_compiled_out_guard.py` all read them, so this is the
        assertion that keeps a per-channel refactor from being a nightly behaviour change."""
        self.assertEqual(co.channel_on_deny("nightly"), co.CHANNEL_ON_DENY)
        self.assertEqual(co.build_type_deny("nightly"), co.BUILD_TYPE_DENY)
        self.assertEqual(co.guard_deny("nightly"), co.GUARD_DENY)

    def test_only_an_off_macro_may_be_released_from_the_guard_deny_list(self):
        """`guard_macros` subtracting less is how a hollow symbol becomes detectable, and it is
        also how a wrong "off" gets published. So the release is bounded: a channel may only
        hand back macros IT considers off, never an ON one (concluding those are off is simply
        the wrong answer) and never a PLATFORM one (answered by the report's `OS:` line, and
        `_PLATFORM_GROUND` is deliberately wider than the macro list)."""
        for channel in _ALL_CHANNELS:
            released = (co.build_type_deny(channel) | co.PLATFORM_DENY) - co.guard_deny(channel)
            with self.subTest(channel=channel):
                self.assertLessEqual(released, co.channel_off(channel))
                self.assertFalse(released & co.channel_on_deny(channel))
                self.assertFalse(released & co.PLATFORM_DENY)
                self.assertFalse(co.build_type_deny(channel) & co.PLATFORM_DENY)
        # Nothing at all is released on nightly (so nightly cannot regress), and exactly the two
        # milestone macros are released on beta -- without them the `#ifdef NIGHTLY_BUILD` hollow
        # symbol stays undetectable there, which was the whole bug.
        self.assertFalse((co.BUILD_TYPE_DENY | co.PLATFORM_DENY) - co.guard_deny("nightly"))
        self.assertEqual(
            (co.build_type_deny("beta") | co.PLATFORM_DENY) - co.guard_deny("beta"),
            {"NIGHTLY_BUILD", "EARLY_BETA_OR_EARLIER"},
        )

    def test_no_channel_may_drop_or_double_book_a_macro(self):
        """A macro that is in NEITHER half is silently reasoned about by the `moz.configure`
        walk, which cannot answer a milestone predicate; a macro in BOTH is answered by
        whichever branch of `is_build_flag_ground` runs first. Both are invisible in prod, so
        they are pinned here: same 8 macros on every channel, halves always disjoint."""
        expected = co.CHANNEL_ON_DENY | co.channel_off("nightly")
        self.assertEqual(len(expected), 8)
        for channel in _ALL_CHANNELS:
            on, off = co.channel_on_deny(channel), co.channel_off(channel)
            with self.subTest(channel=channel):
                self.assertEqual(on | off, expected)
                self.assertFalse(on & off)

    def test_an_unknown_channel_degrades_to_nightly_rather_than_to_nothing(self):
        """`esr` is the channel the enum defect (`_ensure_enum_values` can never ALTER) would
        add next. An empty partition would make EVERY build-flag claim unbind, so the
        documented degradation is nightly's table -- the behaviour of the last year."""
        self.assertEqual(co.channel_on_deny("esr"), co.CHANNEL_ON_DENY)
        self.assertEqual(co.channel_off("esr"), co.channel_off("nightly"))


class TestIsBuildFlagGroundInvertsPerChannel(unittest.TestCase):
    """The predicate that decides whether a skeptic `fail` may abstain a lead, per channel.

    Item 14's two sentences, and both are load-bearing. Before this change a beta note "behind
    `#ifdef NIGHTLY_BUILD`, not compiled into this build" matched nightly's ON list, returned
    True and DISCARDED a correct noise-kill; "this `#ifdef RELEASE_OR_BETA` code is not in the
    build" matched the nightly off-side regex, returned False and let a claim that is wrong on
    beta ABSTAIN a good lead. One relaxation cannot fix opposite errors."""

    def test_is_build_flag_ground_inverts_per_channel(self):
        # BINDS on beta (the claim is TRUE there: nightly-gated code is absent from a beta
        # build), UNBINDS on nightly (it is false there, and an abstain is the harshest and
        # least visible thing we do).
        self.assertFalse(co.is_build_flag_ground(NIGHTLY_GATED_NOTE, channel="beta"))
        self.assertTrue(co.is_build_flag_ground(NIGHTLY_GATED_NOTE, channel="nightly"))
        # ...and exactly the other way round for the macro that is ON on beta. Note the
        # denominator: 0 of 8,901 prod claims name RELEASE_OR_BETA, so this direction has no
        # back-test and rests on `set_define("RELEASE_OR_BETA", milestone.is_release_or_beta)`.
        self.assertTrue(co.is_build_flag_ground(RELEASE_OR_BETA_NOTE, channel="beta"))
        self.assertFalse(co.is_build_flag_ground(RELEASE_OR_BETA_NOTE, channel="nightly"))

    def test_binding_is_what_turns_a_lead_into_an_abstain(self):
        """What "BIND" costs, spelled out through the consumer rather than asserted about the
        predicate. `_skeptic_veto` is the only reader, an abstain files nothing and reaches no
        scoreboard, and the same note therefore has to be right per channel."""
        # A binding ground (the nightly answer for the RELEASE_OR_BETA note): abstain.
        bound = Dossier(
            crash={"uuid": "u", "signature": "sig", "frames": []},
            verdict=Verdict(decision=Decision.lead, confidence=Confidence.probable,
                            mechanism=Claim(summary="m", citations=[_SF])),
            candidate=Candidate(node="a" * 12, bug=1, author="A", channel="nightly"),
            skeptic=[SkepticResult(status=SkepticStatus.failed, claim_ref="mechanism",
                                   note=RELEASE_OR_BETA_NOTE)],
        )
        self.assertEqual(bound.verdict.decision, Decision.abstain)
        self.assertNotIn("skeptic_build_flag_unbound", bound.corroborations)
        # An unbinding one keeps the lead and SAYS SO, so the rule stays countable.
        kept = _lead_with(NIGHTLY_GATED_NOTE, channel="nightly")
        self.assertEqual(kept.verdict.decision, Decision.lead)
        self.assertEqual(kept.corroborations["skeptic_build_flag_unbound"], ["mechanism"])

    def test_a_platform_ground_still_binds_on_every_channel(self):
        """The counter-example the per-channel split must not have eaten. A platform claim is
        channel-independent -- `widget/gtk` is absent from a Windows build whatever the
        milestone is -- and the deterministic gate is designed never to see these, so if the
        predicate ate them nothing else would catch it."""
        for channel in _ALL_CHANNELS:
            with self.subTest(channel=channel):
                self.assertFalse(co.is_build_flag_ground(GTK_NOTE, channel=channel))

    def test_the_opt_build_ground_still_binds_on_every_channel(self):
        """DEBUG / MOZ_ASSERT are off on every official opt build, so they do not move -- 4 of
        the 21 notes the nightly predicate fires on are this shape and all four are true. The
        `MOZ_ASSERT\\w*` widening (a note says "MOZ_ASSERT", rarely "MOZ_ASSERT_ENABLED") has
        to survive the move from a hardcoded regex to a per-channel one."""
        for note in ("this `#ifdef DEBUG` assertion is not compiled into the shipped binary",
                     "the MOZ_ASSERT is compiled out of an opt build, so it cannot fire here"):
            for channel in _ALL_CHANNELS:
                with self.subTest(note=note[:24], channel=channel):
                    self.assertFalse(co.is_build_flag_ground(note, channel=channel))

    def test_ndebug_still_unbinds_on_every_channel(self):
        """The wrong-direction case that reads as a typo until you check: an opt build DEFINES
        `NDEBUG`, so "this `#ifdef NDEBUG` code is not in the build" is wrong by construction
        and must never be given a veto that makes it bind. It stays on the ON side of all four
        partitions, which is why it is asserted across them rather than once."""
        note = "this `#ifdef NDEBUG` fast path is not compiled into the build"
        for channel in _ALL_CHANNELS:
            with self.subTest(channel=channel):
                self.assertIn("NDEBUG", co.channel_on_deny(channel))
                self.assertTrue(co.is_build_flag_ground(note, channel=channel))


class TestDiagnosticAssertKeysOnTheRawChannel(unittest.TestCase):
    """`MOZ_DIAGNOSTIC_ASSERT_ENABLED` is `when=moz_debug | milestone.is_nightly |
    moz_dev_edition`, so Developer Edition has it ON while plain beta has it OFF -- and Socorro
    files Developer Edition as `aurora`, which is 36-41% of the channel (three independent
    measurements: 36.6% over 30 d, 38.5% over 08-18..25, 41% over 364 d in
    `sigage.hardware_noise`'s own docstring). Keying this on our stored `beta` label would get
    it wrong for a third of the channel, in the direction that abstains a good lead."""

    def test_diagnostic_assert_keys_on_the_raw_channel(self):
        self.assertIn("MOZ_DIAGNOSTIC_ASSERT_ENABLED", co.channel_on_deny("aurora"))
        self.assertNotIn("MOZ_DIAGNOSTIC_ASSERT_ENABLED", co.channel_off("aurora"))
        self.assertIn("MOZ_DIAGNOSTIC_ASSERT_ENABLED", co.channel_off("beta"))
        self.assertNotIn("MOZ_DIAGNOSTIC_ASSERT_ENABLED", co.channel_on_deny("beta"))
        # The two labels must not be collapsed: `get_search_channel("beta")` deliberately
        # queries ["beta", "aurora"] together, and folding them here would be the same move
        # applied to a question where the answer differs.
        self.assertNotEqual(co.channel_on_deny("aurora"), co.channel_on_deny("beta"))

    def test_the_predicate_follows_the_dev_edition_answer(self):
        """The consequence, not the table: the same note has to bind on beta (true there) and
        unbind on aurora (false there). Only the `_ENABLED` spelling is pinned -- of the 37
        prod claims that name the macro, 30 use the bare `MOZ_DIAGNOSTIC_ASSERT` and 7 the
        `_ENABLED` one, but only 2 of the 1,765 `fail`s name it at all and neither reaches the
        partition, so a bare-spelling rule would be fitted on 0 measured cases."""
        note = ("the `MOZ_DIAGNOSTIC_ASSERT_ENABLED` check is not compiled into this build, so "
                "the assertion cannot be what crashed")
        self.assertFalse(co.is_build_flag_ground(note, channel="beta"))
        self.assertTrue(co.is_build_flag_ground(note, channel="aurora"))
        self.assertTrue(co.is_build_flag_ground(note, channel="nightly"))

    def test_aurora_is_not_a_channel_we_can_ever_have_stored(self):
        """Why keying this on our stored label cannot work, stated as the fact it rests on.

        `get_search_channel("beta")` deliberately queries `["beta", "aurora"]` and ingestion
        writes the LOOP's channel, so every DevEdition report lands as `channel="beta"`; and
        `CHANNEL_TYPE` is built from `config.get_channels()`, which has no `aurora` member, so
        the column could not hold one anyway. The raw value is not lost -- it is on the
        processed crash, where `orchestrator._install_history` already reads it
        (`raw.get("release_channel")`) for exactly this reason."""
        self.assertNotIn("aurora", config.get_channels())
        self.assertEqual(utils.get_search_channel("beta"), ["beta", "aurora"])

    # DEFECT (plan #18 item 14, `compiled_out.py:150` / `orchestrator.py:3460`).
    #
    # Item 14 says it in one sentence -- "Key `MOZ_DIAGNOSTIC_ASSERT_ENABLED` on the raw
    # `release_channel` (`aurora` = DevEdition = ON; `beta` = OFF), NOT on our stored channel
    # label" -- and the table was added while the keying was not. Every producer of a channel
    # for this module passes `seed["channel"]`, i.e. the stored label
    # (`set_build_channel(seed.get("channel"))`, `hollow_symbols(channel=channel)`,
    # `roles.build_roles(channel=channel)`), and by the test above that label can never be
    # `aurora`. So `_CHANNEL_MACROS["aurora"]` and `_CHANNEL_OFF_HOLLOW["aurora"]` are
    # unreachable from any production path, and the 36-41% of the channel that is Developer
    # Edition (36.6% / 38.5% / 41% by three independent measurements) gets plain beta's answer:
    # `MOZ_DIAGNOSTIC_ASSERT_ENABLED` treated as OFF where it is ON.
    #
    # Both halves of that are the invisible direction. A DevEdition `fail` reading "the
    # diagnostic assert is not compiled into this build" is FALSE and now BINDS -> abstain ->
    # no filing, no second opinion, no `Feedback` row. And the skeptic prompt tells the same run
    # the macro is absent while dropping the 9-11% prevalence figure -- on the one slice of the
    # beta channel where MOZ_DIAGNOSTIC_ASSERT crashes actually exist.
    #
    # The fix is the raw field the seed already carries, not a new lookup. Asserted as the
    # behaviour rather than as the label, so it stays true whichever way the channel is threaded.
    def test_a_dev_edition_crash_gets_the_dev_edition_partition(self):
        seen = _run_channel_seen({"release_channel": "aurora", "product": "Firefox"})
        self.assertIn("MOZ_DIAGNOSTIC_ASSERT_ENABLED", seen["on"])
        self.assertNotIn("MOZ_DIAGNOSTIC_ASSERT_ENABLED", seen["off"])

    def test_dev_edition_keeps_the_prevalence_figure_and_plain_beta_drops_it(self):
        """The prompt half of the same fact. "9-11% of the crashes we analyse are
        MOZ_DIAGNOSTIC_ASSERT crashes" is a statement about a build where the macro is ON; on
        plain beta those crashes cannot exist, so printing the figure is a false fact about the
        build in hand."""
        self.assertIn("9-11%", roles._compiled_out_text("aurora"))
        self.assertIn("9-11%", roles._compiled_out_text("nightly"))
        self.assertNotIn("9-11%", roles._compiled_out_text("beta"))
        self.assertNotIn("9-11%", roles._compiled_out_text("release"))


class TestGuardMacrosCanFindANightlyBuildHollowSymbolOnBeta(unittest.TestCase):
    """On beta a symbol whose whole body sits inside `#ifdef NIGHTLY_BUILD` is the commonest
    way a symbol is genuinely hollow -- and it was UNDETECTABLE, because NIGHTLY_BUILD sat in
    the deny list `guard_macros` subtracts. `_CHANNEL_OFF_HOLLOW` puts it back in scope off
    nightly only, so `guard_deny("nightly") == GUARD_DENY` and nightly cannot regress."""

    def test_guard_macros_can_find_a_nightly_build_hollow_symbol_on_beta(self):
        self.assertEqual(co.guard_macros(NIGHTLY_ONLY_PROBE, "beta"), ["NIGHTLY_BUILD"])
        self.assertEqual(co.guard_macros(NIGHTLY_ONLY_PROBE, "nightly"), [])
        # The guard is only useful if the bodies really are empty without it.
        self.assertEqual(co.hollow_functions(NIGHTLY_ONLY_PROBE, "NIGHTLY_BUILD"),
                         ["AutoNightlyProbe", "~AutoNightlyProbe"])

    def test_hollow_symbols_reports_it_on_beta_and_not_on_nightly(self):
        """End to end through the function the resolver calls, because `guard_macros` alone
        proves nothing: `hollow_symbols` also has to get a switch answer for the macro, and a
        milestone-gated macro has no `option()` behind it."""
        client = _fake_searchfox(NIGHTLY_ONLY_PROBE)
        found = co.hollow_symbols(["AutoNightlyProbe"], client=client, channel="beta")
        self.assertEqual(found["AutoNightlyProbe"]["macro"], "NIGHTLY_BUILD")
        self.assertEqual(found["AutoNightlyProbe"]["functions"],
                         ["AutoNightlyProbe", "~AutoNightlyProbe"])
        # It read the CRASH'S OWN TREE. Reading firefox-main for a beta crash is how a symbol
        # that never existed in the beta build gets cited as if it had.
        self.assertEqual(client.define.call_args.kwargs["repo"], "mozilla-beta")
        # Unchanged on nightly: the macro is denied there, so nothing is even looked up.
        self.assertEqual(co.hollow_symbols(["AutoNightlyProbe"],
                                           client=_fake_searchfox(NIGHTLY_ONLY_PROBE),
                                           channel="nightly"), {})

    def test_the_channel_answers_without_walking_moz_configure(self):
        """`set_define("NIGHTLY_BUILD", milestone.is_nightly)` has no `option()` behind it, so
        the walk answers "" and the hollow symbol would go undetected. The fake client raises
        if `search` is touched, so this asserts the short-circuit rather than its output."""
        answer = co._default_off_switch("NIGHTLY_BUILD", _fake_searchfox(""), "beta")
        self.assertTrue(co.is_channel_off_answer(answer))
        self.assertIn("milestone is a nightly", co.channel_off_phrase(answer))
        self.assertIn("this crash is on beta", co.channel_off_phrase(answer))
        alias = co._default_off_switch("EARLY_BETA_OR_EARLIER", _fake_searchfox(""), "beta")
        self.assertTrue(co.is_channel_off_answer(alias))
        self.assertIn("alias for `NIGHTLY_BUILD`", co.channel_off_phrase(alias))
        # A real `--enable-x` answer is NOT a channel answer, so the renderer keeps both shapes.
        self.assertFalse(co.is_channel_off_answer("--enable-gc-concurrent-marking"))
        self.assertEqual(co.channel_off_phrase("--enable-gc-concurrent-marking"),
                         "--enable-gc-concurrent-marking")

    def _suppressed_reason(self, switch, rev="b" * 40):
        dossier = Dossier(
            crash={"uuid": "u", "signature": "sig", "frames": []},
            verdict=Verdict(decision=Decision.lead, confidence=Confidence.probable,
                            mechanism=Claim(summary="rests on AutoNightlyProbe",
                                            citations=[_SF])),
            candidate=Candidate(node="a" * 12, bug=1, author="A", channel="beta"),
        )
        seed = {"uuid": "u", "channel": "beta", "compiled_out": {
            "symbol": "AutoNightlyProbe", "macro": "NIGHTLY_BUILD",
            "functions": ["AutoNightlyProbe", "~AutoNightlyProbe"],
            "switch": switch, "provenance": "mechanism", "rev": rev}}
        orch._apply_compiled_out_gate(dossier, seed)
        return dossier

    def test_the_published_sentence_reads_as_a_sentence(self):
        """This string goes into a bug comment a module owner reads. Rendering a channel answer
        through the switch template would publish "`NIGHTLY_BUILD` is off unless someone passes
        `is defined only when the milestone is a nightly (`a1`)...`", which is gibberish -- and
        gibberish in the one place we ask a human for their time."""
        dossier = self._suppressed_reason(
            co._default_off_switch("NIGHTLY_BUILD", _fake_searchfox(""), "beta"))
        reason = dossier.verdict.abstain_reason
        self.assertEqual(dossier.verdict.decision, Decision.abstain)
        self.assertIn(
            "`NIGHTLY_BUILD` is defined only when the milestone is a nightly (`a1`), and this "
            "crash is on beta", reason)
        self.assertNotIn("off unless someone passes `is defined", reason)
        # A milestone answer has no build rev to quote: the channel is the whole evidence, and
        # a rev would imply the answer was read out of that build's moz.configure.
        self.assertNotIn("read from the moz.configure", reason)
        self.assertTrue(dossier.corroborations["compiled_out_suppressed"])

    def test_a_real_switch_still_names_the_switch_and_the_build_rev(self):
        """The other branch of the same `if`. The switch name and the rev were both added
        because "a moz.configure switch that is off unless someone asks for it" is not
        something an owner can check -- so the new channel branch must not have cost them."""
        reason = self._suppressed_reason("--enable-gc-concurrent-marking",
                                         rev="1e704c6738f4aaaa").verdict.abstain_reason
        self.assertIn("is off unless someone passes `--enable-gc-concurrent-marking`", reason)
        self.assertIn("rev `1e704c6738f4`", reason)


class TestTheSkepticPromptPartitionMatchesTheGate(unittest.TestCase):
    """The prompt and the gate must partition the macros the same way, on every channel.

    Not a style rule: the model's `fail` and the code's bind/unbind decision are two halves of
    one mechanism, and the failure mode of a disagreement is the invisible one -- the prompt
    talks the model into a `fail` the code then binds, or out of one the code would have kept.
    That is why the lists are rendered from `compiled_out` rather than written twice."""

    def test_the_skeptic_prompt_partition_matches_the_gate(self):
        for channel in _ALL_CHANNELS:
            on, off = _prompt_lists(roles._compiled_out_text(channel))
            with self.subTest(channel=channel):
                self.assertEqual(on, ", ".join(sorted(co.channel_on_deny(channel))))
                self.assertEqual(off, ", ".join(sorted(co.channel_off(channel))))

    def test_the_beta_prompt_says_the_thing_that_was_previously_forbidden(self):
        """The old text told a beta skeptic to refuse the correct conclusion about
        NIGHTLY_BUILD-gated code. Off nightly the prompt has to say the opposite out loud,
        because "never conclude off for NIGHTLY_BUILD" was not merely absent from beta's list
        -- it was the instruction."""
        beta = roles._compiled_out_text("beta")
        on, off = _prompt_lists(beta)
        self.assertNotIn("NIGHTLY_BUILD", on)
        self.assertIn("NIGHTLY_BUILD", off)
        self.assertIn("This crash is on beta, NOT nightly", beta)
        self.assertIn("genuinely ABSENT from this build", beta)
        # RELEASE_OR_BETA moved the other way, and the free veto it used to hand out is gone.
        self.assertIn("RELEASE_OR_BETA", on)
        self.assertNotIn("RELEASE_OR_BETA", off)
        # Nightly keeps its own sentence and must NOT acquire beta's.
        self.assertNotIn("NOT nightly", roles._compiled_out_text("nightly"))

    def test_all_twenty_macros_are_still_in_every_channels_prompt(self):
        """Extends `test_compiled_out_guard.py`'s existing claim rather than duplicating it:
        21 of the 39 build-guard `fail`s in the month name a macro the code refuses to reason
        about, and the prompt named none of them. Moving a macro between the ON and OFF halves
        must not drop it from the prompt altogether -- which is exactly what a naive
        per-channel render would do to the two macros `guard_deny` no longer denies on beta."""
        for channel in _ALL_CHANNELS:
            text = roles._compiled_out_text(channel)
            for macro in co.GUARD_DENY:
                with self.subTest(channel=channel, macro=macro):
                    self.assertIn(macro, text)

    def test_the_nightly_prompt_is_byte_identical_to_before(self):
        """`make_role("skeptic")` with no channel is the prompt that has been in prod; the
        channel render is a targeted swap so `_ROLES` stays a real, readable nightly prompt.
        A drift here would silently re-baseline every existing prompt test and the budget."""
        self.assertEqual(roles._compiled_out_text("nightly"), roles._COMPILED_OUT)
        self.assertEqual(roles.make_role("skeptic").prompt,
                         roles._ROLES["skeptic"]["prompt"])
        self.assertEqual(roles.make_role("skeptic", channel="nightly").prompt,
                         roles._ROLES["skeptic"]["prompt"])

    def test_a_beta_run_gets_a_different_skeptic_and_only_the_skeptic(self):
        self.assertNotEqual(roles.make_role("skeptic", channel="beta").prompt,
                            roles.make_role("skeptic").prompt)
        self.assertIn("This crash is on beta, NOT nightly",
                      roles.make_role("skeptic", channel="beta").prompt)
        # Every other role is channel-independent; a swap that leaked would be a silent
        # rewrite of five prompts nobody measured.
        for name in roles.role_names():
            if name == "skeptic":
                continue
            with self.subTest(role=name):
                self.assertEqual(roles.make_role(name, channel="beta").prompt,
                                 roles.make_role(name).prompt)
        # And `build_roles` carries the channel through, which is what `triage.build_options`
        # actually calls.
        self.assertEqual(roles.build_roles(channel="beta")["skeptic"].prompt,
                         roles.make_role("skeptic", channel="beta").prompt)


class TestTheRunChannelReachesTheSkepticVeto(unittest.TestCase):
    """`Dossier._skeptic_veto` is a pydantic validator: it cannot be handed an argument, and
    the dossier has no channel field on purpose (a channel the MODEL could set is the class of
    field `parse_and_validate` strips). So the channel travels as a ContextVar that
    `orchestrator.run_evidence_agent` sets once per run.

    THIS IS THE ONE CONSUMER THE CONTEXTVAR EXISTS FOR, per its own docstring -- so the test is
    about the veto's behaviour, not about the variable's value."""

    def test_the_default_is_nightly(self):
        """A path that forgets to set the channel must behave exactly as the pipeline did for
        the last year, not in some new way."""
        self.assertEqual(contextvars.copy_context().run(co.build_channel), "nightly")
        self.assertEqual(co.channel_on_deny(), co.CHANNEL_ON_DENY)
        self.assertEqual(co.guard_deny(), co.GUARD_DENY)

    def test_set_build_channel_moves_the_channel_keyed_helpers(self):
        """The half that works: everything whose `channel=None` default falls back to
        `build_channel()`."""
        def probe():
            co.set_build_channel("beta")
            return (co.build_channel(), co.channel_on_deny(), co.channel_off(),
                    co.guard_deny(), co.build_type_deny())
        channel, on, off, guard, both = contextvars.copy_context().run(probe)
        self.assertEqual(channel, "beta")
        self.assertEqual(on, co.channel_on_deny("beta"))
        self.assertEqual(off, co.channel_off("beta"))
        self.assertEqual(guard, co.guard_deny("beta"))
        self.assertEqual(both, co.build_type_deny("beta"))
        # It survives a `reset` with a foreign token rather than raising mid-run.
        co.reset_build_channel(contextvars.copy_context().run(co.set_build_channel, "beta"))
        self.assertEqual(co.build_channel(), "nightly")

    def test_run_evidence_agent_sets_the_channel_before_the_dossier_is_parsed(self):
        """The wiring, asserted at the moment it has to be true: the channel must be live by
        the time `run_crash_triage` returns a dossier, because `_skeptic_veto` runs inside
        `Dossier`'s construction -- setting it any later would be a no-op for its one
        consumer."""
        seen = _run_channel_seen(channel="beta")
        self.assertEqual(seen.get("channel"), "beta")
        self.assertEqual(seen.get("on"), co.channel_on_deny("beta"))
        self.assertEqual(seen.get("guard"), co.guard_deny("beta"))
        self.assertEqual(co.build_channel(), "nightly")   # nothing leaked into the suite
        # A nightly run is unchanged, which is what makes the ContextVar's default a
        # degradation rather than a guess.
        self.assertEqual(_run_channel_seen(channel="nightly").get("guard"), co.GUARD_DENY)

    # DEFECT (plan #18 item 14, `compiled_out.py:920` / `agent/schema.py:709`).
    #
    # `is_build_flag_ground(note, citations, channel=None)` calls `_partition(channel)`
    # DIRECTLY, and `_partition` maps a falsy channel to nightly's table -- unlike
    # `channel_on_deny` / `channel_off` / `build_type_deny` / `guard_deny`, which every one of
    # them default to `channel or build_channel()`. `_skeptic_veto` is the only caller and it
    # passes no channel (it has none to pass -- that is why the ContextVar was introduced), so
    # the beta partition NEVER reaches the veto and the ContextVar's single documented consumer
    # reads nightly's table for a beta crash.
    #
    # The consequence is precisely the direction item 14 calls the invisible one: on a beta
    # crash a correct "this is behind `#ifdef NIGHTLY_BUILD`" noise-kill still UNBINDS, the
    # lead survives, and `skeptic_build_flag_unbound` is recorded as though the rule had done
    # its job. Verified: with the ContextVar set to "beta", `_skeptic_veto` returns
    # `Decision.lead` -- byte-identical to the nightly run.
    #
    # The fix is one line (`_partition(channel or build_channel())`, matching its four
    # neighbours), so this asserts the CORRECT behaviour and is marked expected-failure rather
    # than weakened; it will fail as an UNEXPECTED SUCCESS the moment the line lands, which is
    # the signal to delete this decorator.
    def test_the_run_channel_reaches_the_skeptic_veto(self):
        def probe():
            co.set_build_channel("beta")
            return _lead_with(NIGHTLY_GATED_NOTE)
        dossier = contextvars.copy_context().run(probe)
        self.assertEqual(dossier.verdict.decision, Decision.abstain)
        self.assertNotIn("skeptic_build_flag_unbound", dossier.corroborations)


if __name__ == "__main__":
    unittest.main()
