# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# WHICH CHANNELS PRODUCTION TRIAGES, FILES ON AND INGESTS — the shipped values, in one place.
#
# THE FORCING FUNCTION THE SUITE LACKS (plan #18 §6.3 T10, and §6 measured it in a symlinked
# scratch tree): flipping `agent.channels` to `["nightly", "beta"]` left the WHOLE suite GREEN
# (1,713 tests, OK); so did retuning beta's `installs` / `ratio` / `protos` /
# `mature_after_days`; so did inventing brand-new config keys — there is no config-schema
# validation anywhere. EXACTLY ONE test in the tree trips on a beta retune
# (`tests/test_selection_log.py`'s `floor(beta) > installs(beta)` tripwire), and because no test
# in the repo uses `autospec=True`, every mocked `config.get_*` accepts any call signature, so
# the per-channel arguments this plan adds are invisible to all of them. So a beta extension can
# land with no test in the diff, and nothing tells a reader what prod actually spends money and
# Bugzilla writes on.
#
# This module pins the SHIPPED values of `config/global.json` — not the code defaults, which are
# a different (deliberately quieter) set — the way
# `tests/test_phase2_calibration.py:test_the_shipped_table_is_the_full_arm` pins the calibration
# table and `tests/test_selection_log.py:test_shipped_values` pins the selector's knobs. Every
# number carries the measurement and the DENOMINATOR from plan #18 §2/§4 that chose it, because
# a number nobody can re-derive is a number the next person will "clean up".
#
# No test here reaches the network or the DB: the one call into the filer returns at its first
# gate, which is the point of that test.
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_shipped_channels
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import bugzilla_apply, config, datacollector as dc  # noqa: E402
from crashclouseau import report_bug, sigage, update  # noqa: E402
from crashclouseau.agent import orchestrator as orch, triage  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    Candidate,
    Claim,
    Confidence,
    Decision,
    Dossier,
    SearchfoxCitation,
    Verdict,
)

_SF = SearchfoxCitation(
    permalink="https://searchfox.org/x#1", symbol_id="_Z1", repo="mozilla-central"
)


def _lead_dossier():
    """A reported lead at the `medium` rung (50) — the rung the shipped table maps to 0.5714.

    The candidate is not decoration: an anchorless lead is demoted to `abstain` by
    `Dossier._has_lead_anchor`, and an abstain has no worth-investigating probability at all,
    so without it this fixture would pass the beta half for the wrong reason."""
    return Dossier(
        candidate=Candidate(node="regnode00001"),
        verdict=Verdict(
            decision=Decision.lead, confidence=Confidence.medium,
            mechanism=Claim(statement="a use-after-free of Foo", citations=[_SF]),
        ),
    )


class TestShippedAgentChannels(unittest.TestCase):
    """What the pipeline SPENDS on, and what it merely ingests. Two different switches."""

    def test_the_shipped_agent_channels(self):
        """`agent.channels` is the money switch: ~$1-3 and ~20 minutes per crash.

        Nightly plus beta, and beta is the whole of plan #18. Beta's projected load at the
        shipped selector knobs is 1.33 distinct (signature, buildid) pairs per run-day = 7.6
        UUIDs/day = 4.2-5.8 dossiers/day = $4-17/day (30 as-of run-days replayed at the
        DEPLOYED cadence), against nightly's existing spend — so this is a real budget line,
        not a flag flip.

        RELEASE IS ABSENT ON PURPOSE and its absence is load-bearing in three other places
        pinned below: it is 10%-sampled by Socorro, it has no measured population rates
        (`sigage._POPULATION_RATES`), and nobody has made a filing decision about it
        (`config.autofile_channel_declared`). It is nonetheless one `INGEST_CHANNELS` typo
        away from being ingested — see `test_ingest_channels_must_always_be_set_explicitly`."""
        self.assertEqual(config.get_agent_channels(), ["nightly", "beta"])
        # ...and the config's third channel is a channel we ingest-only at most.
        self.assertEqual(config.get_channels(), ["nightly", "beta", "release"])

    def test_agent_channels_can_be_stopped_without_a_deploy(self):
        """`AGENT_CHANNELS` is a REAL kill switch, and the reason it had to exist.

        `agent.channels` was the one canary lever of ~14 with no environment override, so
        turning a channel's triage OFF needed a DEPLOY — and a deploy kills every in-flight
        ~20-minute run at ~$3 each. `AUTOFILE_BUGS=0` was no substitute: it is global, so the
        only way to stop beta was to stop nightly too.

        The empty-string case is the one to read twice: an explicitly EMPTY value means "no
        channel filter" (i.e. EVERY channel, release included — `enqueue_agent`'s gate is
        `if channel is not None and channels and channel not in channels`), matching the
        config's own empty-list semantics. That is the same shape as `INGEST_CHANNELS`'s
        foot-gun below: clearing the variable is not disabling the feature."""
        for value, expected in (("nightly", ["nightly"]),
                                ("nightly beta", ["nightly", "beta"]),
                                ("  beta ", ["beta"]),
                                ("", [])):
            with self.subTest(AGENT_CHANNELS=value):
                with mock.patch.dict(os.environ, {"AGENT_CHANNELS": value}):
                    self.assertEqual(config.get_agent_channels(), expected)
        # Unset (the prod state as of 2026-08-25) falls back to the config file, so the value
        # pinned above is the one that actually runs.
        env = {k: v for k, v in os.environ.items() if k != "AGENT_CHANNELS"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(config.get_agent_channels(), ["nightly", "beta"])

    def test_ingest_channels_must_always_be_set_explicitly(self):
        """CLEARING `INGEST_CHANNELS` TURNS RELEASE ON. `update_all`'s default is not "nothing".

        `os.getenv("INGEST_CHANNELS", "").split() or config.get_channels()` — the `or` makes an
        empty value fall through to ALL configured channels, so `heroku config:unset
        INGEST_CHANNELS` (the instinctive way to undo `INGEST_CHANNELS="nightly beta"`) starts
        ingesting release on the next 20-minute tick, with no deploy and no log line saying so.
        Prod has `INGEST_CHANNELS=nightly` today (plan #18 preamble, `heroku config`
        re-verified 2026-08-25); this test exists so that the day it becomes `"nightly beta"`
        the reader knows the variable must never be cleared, only rewritten.

        Ingestion itself is free — the containment is that the money and the Bugzilla writes are
        gated elsewhere, which is asserted at the end and is the only reason this is a foot-gun
        and not an incident."""
        for value in ("", "   "):
            with self.subTest(INGEST_CHANNELS=value), \
                    mock.patch.dict(os.environ, {"INGEST_CHANNELS": value}), \
                    mock.patch.object(update, "update_in_queue") as enq:
                update.update_all(products=["Firefox"])
                self.assertEqual([c.args[0] for c in enq.call_args_list],
                                 ["nightly", "beta", "release"])
        env = {k: v for k, v in os.environ.items() if k != "INGEST_CHANNELS"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(update, "update_in_queue") as enq:
            update.update_all(products=["Firefox"])
        self.assertIn("release", [c.args[0] for c in enq.call_args_list])
        # Set explicitly, it is exactly what it says — and nothing else.
        with mock.patch.dict(os.environ, {"INGEST_CHANNELS": "nightly beta"}), \
                mock.patch.object(update, "update_in_queue") as enq:
            update.update_all(products=["Firefox"])
        self.assertEqual([c.args[0] for c in enq.call_args_list], ["nightly", "beta"])
        # The trap has to be documented at the function that reads the variable, because that
        # is where somebody stands when they decide to unset it.
        doc = (update.update_all.__doc__ or "").lower()
        self.assertIn("ingest_channels", doc)
        self.assertIn("all configured channels", doc)
        # The containment, and the reason an accidental release ingest is not an incident: it is
        # not triaged (no LLM spend) and not declared for filing (no Bugzilla write).
        self.assertNotIn("release", config.get_agent_channels())
        self.assertFalse(config.autofile_channel_declared("release"))


class TestShippedAutofilePolicyPerChannel(unittest.TestCase):
    """The only unattended Bugzilla write, now per channel. `config/global.json`
    `agent.autofile`."""

    def test_the_shipped_autofile_policy_per_channel(self):
        """nightly = comment / 10 a day; beta = skip / 3 a day. Both numbers are decisions.

        `daily_cap` 3 IS NOT A THROUGHPUT CONSTRAINT. Beta's projected filing rate is
        0.008-0.048 filings/day — one bug every 21 to 125 days — so 3 can only ever bind on a
        burst, and that is what it is for: 48% of beta's selections arrive in the 4 days after
        a central->beta merge (4 of 30 replayed run-days carried 108 of 179 selections), which
        is exactly when a freshly uplifted regression first reaches users AND the only way beta
        could eat nightly's budget. It bounds the blast radius, nothing else. Nightly keeps 10
        against a measured ~3 filings/day (60 bugs in 21 days).

        `skip` is the LITERAL reading of "file only crashes that have no bug in Bugzilla": an
        open bug on the signature means we write nothing at all. It is measurably STRICTER than
        "file only new bugs" — 58-59% of beta signatures carry an open same-application
        non-meta bug (three instruments: 58/98 = 59.2%, Wilson 49.3-68.4%; 45/77 = 58%; 43/67 =
        64% at selection level) against a 23% nightly control (28/120), and
        `_split_by_application` rescues 0 of 58 — so `skip` suppresses ~58-59% of beta
        filings. `file_new` is the measured alternative at ~2.4x the volume and is a separate,
        later decision (plan #18 Phase 6), so if this value changes to `file_new` the volume
        claim above is what has to be re-read."""
        nightly = config.get_agent_autofile("nightly")
        beta = config.get_agent_autofile("beta")
        self.assertEqual(nightly["comment_on_existing"], "comment")
        self.assertEqual(nightly["daily_cap"], 10)
        self.assertEqual(beta["comment_on_existing"], "skip")
        self.assertEqual(beta["daily_cap"], 3)
        # Those two keys are the WHOLE beta overlay: the rung, the fileable verdicts and the
        # needinfo behaviour are shared, so a change to any of them moves both channels at once.
        # `enabled` cannot appear in this diff even if the overlay sets it, because the
        # `AUTOFILE_BUGS` env read overwrites it for BOTH channels — see
        # `test_a_global_arm_must_not_arm_a_channel_nobody_armed`.
        self.assertEqual(
            {k: v for k, v in beta.items() if nightly.get(k) != v},
            {"comment_on_existing": "skip", "daily_cap": 3},
        )
        self.assertEqual((beta["min_confidence"], beta["verdicts"], beta["needinfo"]),
                         (70, ["lead", "culprit"], True))
        # No argument == nightly, byte for byte. This is what keeps the four existing
        # `return_value=` mocks of `get_agent_autofile` (test_autofile.py:86,125,1004,1457;
        # test_bit_flip_gate.py:235; test_bad_machine_gate.py:180,185) honest after the
        # signature grew a parameter.
        self.assertEqual(config.get_agent_autofile(), nightly)

    def test_every_triaged_channel_has_a_filing_decision(self):
        """A channel we spend money analysing must be a channel somebody DECIDED about filing.

        `autofile_bug` fails closed on an undeclared channel, so the failure mode of forgetting
        one is silence — the shape of the four silent no-ops this codebase has already been
        bitten by (`done-is-not-triaged`). "Undeclared" and "declared and switched off" must
        stay different states: the first is a gap, the second is a decision, and plan #18's
        Phase 4 (beta triaged, beta filing held) needs the second to exist."""
        for channel in config.get_agent_channels():
            with self.subTest(channel=channel):
                self.assertTrue(config.autofile_channel_declared(channel))
        self.assertFalse(config.autofile_channel_declared("release"))
        # Fails CLOSED, at the filer, before any BMO request: this is the hole that a
        # tasks.html retrigger (`enqueue_agent(..., force=True)`, which bypasses the channel
        # gate by design) would otherwise walk straight through with `AUTOFILE_BUGS=1` live.
        for channel in ("release", None, "esr", "nightly-asan"):
            with self.subTest(channel=channel):
                res = bugzilla_apply.autofile_bug(
                    "u-1", {"channel": channel, "signature": "Foo::Bar"},
                    [], {}, "lead", 90)
                self.assertFalse(res["filed"])
                self.assertIn("no autofile configuration", res["skipped"])

    def test_a_global_arm_must_not_arm_a_channel_nobody_armed(self):
        """DEFECT: `AUTOFILE_BUGS=1` overrides a per-channel `enabled: false`, so plan #18's
        Phase 4 — "beta triage on, beta filing still held" — cannot be expressed in prod.

        `_env_bool("AUTOFILE_BUGS", a.get("enabled", False))` is applied AFTER the channel
        overlay and is SYMMETRIC, so it wins in both directions. The kill direction is correct
        and deliberate ("a kill switch a JSON overlay can defeat is not a kill switch"); the ARM
        direction is the defect: prod has `AUTOFILE_BUGS=1`, so `channels.beta.enabled: false`
        — the exact value plan #18 item 17 prescribes for `config/global.json` and Phase 4/5
        gate the rollout on — is unreachable. The only lever left is `AGENT_CHANNELS=nightly`,
        which also stops beta TRIAGE, i.e. the thing Phase 4 exists to measure.

        The shipped config makes it live rather than theoretical: the beta overlay carries no
        `enabled` key at all, so beta inherits the global one and THE FIRST BETA FILING — the
        one irreversible step in the whole plan, expected to be a single bug every 50-125 days
        and explicitly "an event to inspect by hand" — happens on deploy, not on a deliberate
        arm.

        CONTESTED, AND COUPLED: tests/test_beta_autofile.py::
        test_the_env_kill_switch_beats_the_overlay asserts the SAME behaviour as correct and
        deliberate (the switch is global on purpose). Fixing this defect fails that assertion,
        so the two move in one diff — either an asymmetric `AUTOFILE_BUGS` (arm cannot override
        an explicit per-channel `false`) or a separate per-channel env switch."""
        agent = dict(config.get_agent())
        autofile = dict(agent["autofile"])
        autofile["channels"] = {"beta": {"enabled": False, "comment_on_existing": "skip",
                                         "daily_cap": 3}}
        agent["autofile"] = autofile
        with mock.patch.dict(os.environ, {"AUTOFILE_BUGS": "1"}), \
                mock.patch.object(config, "get_agent", return_value=agent):
            self.assertTrue(config.get_agent_autofile("nightly")["enabled"])
            self.assertFalse(config.get_agent_autofile("beta")["enabled"])

    def test_the_kill_direction_of_the_global_switch_still_wins(self):
        """The half of the same asymmetry that IS right: `AUTOFILE_BUGS=0` beats an overlay
        that enables a channel. A kill switch a JSON edit can defeat is not a kill switch."""
        agent = dict(config.get_agent())
        autofile = dict(agent["autofile"])
        autofile["channels"] = {"beta": {"enabled": True}}
        agent["autofile"] = autofile
        with mock.patch.dict(os.environ, {"AUTOFILE_BUGS": "0"}), \
                mock.patch.object(config, "get_agent", return_value=agent):
            self.assertFalse(config.get_agent_autofile("beta")["enabled"])
            self.assertFalse(config.get_agent_autofile("nightly")["enabled"])


class TestBetaPublishesNoCalibratedProbability(unittest.TestCase):
    """`agent.calibration.channels.beta = {}` — the empty table is the measurement."""

    def test_beta_publishes_no_calibrated_probability(self):
        """Beta gets NOTHING, and nightly keeps all four rungs.

        The shipped fit is all 90 rows of `corpus_ship` (25 -> 1/2, 50 -> 4/7, 70 -> 21/27,
        85 -> 13/20), every one of them Firefox NIGHTLY, and there is no beta arm of the corpus
        (`eval/corpus.py`, `eval/study_corpus.py` — plan #18 §5 item 15), so there is nothing
        to fit. This number is not internal: it reaches a human twice over
        (`report_bug._worth_phrase` puts "N% worth investigating" in the FILED BUG, and the
        crashstack badge shows it), and this repo's rule is that a number a Bugzilla reviewer
        reads cannot be fit on the wrong arm — the same rule that retired the 0.9714 table when
        it turned out to be a positives-only fit.

        An ABSENT channel key falls back to the top-level fit; a channel present with `{}` gets
        nothing. The asymmetry is deliberate — nightly must keep its table without naming
        itself — but it means the DEFAULT for a new channel is "publish nightly's number", so
        the loop below is the guard: every triaged channel other than nightly must name itself
        in `calibration.channels`."""
        self.assertEqual(config.get_agent_calibration("beta"), {})
        shipped = {25: 0.5, 50: 0.5714, 70: 0.7234, 85: 0.7234}
        self.assertEqual(config.get_agent_calibration("nightly"), shipped)
        # No argument == nightly: every caller that passes nothing is a nightly path, because
        # nightly is the only channel the pipeline has ever run on.
        self.assertEqual(config.get_agent_calibration(), shipped)
        declared = set((config.get_agent().get("calibration", {}).get("channels") or {}))
        for channel in config.get_agent_channels():
            if channel != "nightly":
                with self.subTest(channel=channel):
                    self.assertIn(channel, declared,
                                  "a triaged channel with no calibration entry silently "
                                  "publishes nightly's fit to Bugzilla")

    def test_a_beta_bug_comment_carries_no_worth_investigating_number(self):
        """End to end, because the config value alone is not the claim: the seed's channel has
        to reach the table read, and the missing table has to reach the PROSE.

        `_worth_phrase` deliberately offers no fallback ("with no calibrated number the honest
        thing is to claim nothing"), so the beta comment simply omits the sentence, exactly as
        every filed bug did before the calibration landed."""
        beta = _lead_dossier()
        orch._apply_worth_investigating(beta, {"channel": "beta"})
        self.assertIsNone(beta.verdict.p_worth_investigating)
        nightly = _lead_dossier()
        orch._apply_worth_investigating(nightly, {"channel": "nightly"})
        self.assertEqual(nightly.verdict.p_worth_investigating, 0.5714)

        def _comment(dossier):
            return report_bug._explanation_comment(
                {"mechanism": {"statement": "a use-after-free of Foo"},
                 "p_worth_investigating": dossier.verdict.p_worth_investigating},
                {}, "beta")

        self.assertNotIn("worth investigating", _comment(beta))
        self.assertIn("57% worth investigating", _comment(nightly))


class TestShippedBetaSelectionKnobs(unittest.TestCase):
    """What beta's spike selector can even see. `config/global.json` `thresholds` / `spike`."""

    def test_the_shipped_beta_selection_knobs(self):
        """The six values that decide beta's volume, each with the measurement that chose it.

        `installs` 6 is THE GATE — 78-89% of beta selections come through `is_spike`'s
        from-zero branch, which neither `floor` nor `ratio` touches. It lands at 40 distinct
        (signature, buildid) pairs per 30 as-of run-days = 1.33/day = 7.6 UUIDs/day = $4-17/day.
        Priced alternatives: 3 -> 4.60 pairs/day ($9-38); 2 -> 13.3/day ($21-89); 1 -> 96.6/day
        ($71-299); 10 -> 0.57/day; 20 -> ONE pair in 30 days. And the non-fitted argument for
        it: 6 sits at the 90.01st percentile of 5,527 beta (signature, buildid) pairs — the same
        place nightly's only non-degenerate install bar (`mature_installs` = 4) sits in
        nightly's own distribution (91.97th of 6,915).

        `floor` 10 gates only the NONZERO-baseline branch, i.e. "a live beta signature suddenly
        getting worse". Branch split over the same 30 run-days: floor 3 or 5 -> 61 of 122
        selections (50%); 10 -> 17 of 78 (22%); 15 -> 10%; 20 -> 6%; 25 and 50 -> 0%, which
        makes that whole class structurally undetectable. Note this refutes the tempting
        population-rate transfer: a beta build carries 8.49x a nightly build's reports
        (lifetime medians 2,674 vs 315 over 18 and 84 builds), so nightly's 3 would "scale" to
        25.5 — and at 25 the branch is dead.

        `ratio` 3 is nightly's value UNCHANGED, and that is an argument, not laziness: a ratio
        is dimensionless, so the 8.49x per-build scale factor does not apply to it. Measured
        cost of moving it: 2 -> 56 pairs/30 d (+40%); 3 -> 40; 5 -> 29 (-27%).

        `protos` 5 — beta is the FIRST channel where this cap binds, and it is the DOMINANT
        cost term there rather than a refinement. §4 priced 20 (truncating 1 pair of 40) and
        deferred 5 as an open question; the live end-to-end ingest then measured **4 selected
        pairs carrying 37 distinct proto-signatures** — 37 paid LLM runs from 4 selections
        (19 crashes -> 12 protos, 10 -> 10, 10 -> 10, 6 -> 5) — because beta crash stacks are
        nearly all distinct, so the dedup that makes nightly cheap (mean 1.07 protos/pair, max
        6, cap 50 NEVER binds) does almost nothing. Sweep on that live selection: cap 1 -> 4
        runs, 3 -> 12, 5 -> 20, 10 -> 35, 20 -> 37, 50 -> 37, i.e. ~$20-60 rather than ~$37-111
        for a single tick, and the facet is count-ordered so the five kept are the five loudest
        clusters. Cap 3 (12 runs) is the priced fallback if beta's dossier yield lands above the
        nightly-calibrated 0.55-0.77 the arithmetic assumes."""
        self.assertEqual(config.get_threshold("installs", "Firefox", "beta"), 6)
        self.assertEqual(config.get_spike("floor", "Firefox", "beta"), 10)
        self.assertEqual(config.get_spike("ratio", "Firefox", "beta"), 3)
        self.assertEqual(config.get_spike("ratio", "Firefox", "nightly"), 3)
        self.assertEqual(config.get_threshold("protos", "Firefox", "beta"), 5)
        # The cap binds on beta and does not on nightly — the shape of the claim above, not
        # just its literals.
        self.assertLess(config.get_threshold("protos", "Firefox", "beta"),
                        config.get_threshold("protos", "Firefox", "nightly"))
        # For contrast, and because it is why beta needed its own number at all: nightly
        # selects at ONE installation.
        self.assertEqual(config.get_threshold("installs", "Firefox", "nightly"), 1)

    def test_the_no_user_build_floor_is_beta_only(self):
        """`min_build_reports` 100 — the merge-day `N.0b1` build that has never had a user.

        Every cycle ships two builds tagged `N.0b1`: the merge-day one (revision "Update
        configs after merge day operations") and, days later, the one that reaches users.

        DISTINCT INSTALLATIONS, not reports, and that is the corrected version of this
        threshold. Measured lifetime figures for v151-v155's merge-day builds, all channels, no
        date bound: 8/4, 13/6, 7/7, 17/4 and 1/1 reports over installations, against
        435-9,124 reports and 268-5,084 installations for all 54 other builds since 2026-04-01.
        The report gap looks bigger, but it is a gap between LIFETIME totals and the code can
        only see what has arrived by now: a 1.25-day-old build holds ~11% of its eventual
        crashes, so the quietest real build shows ~48 reports while it sits at window index 1 —
        under a report floor of 100, and index 1 is where 135 of 135 replayed selections landed.
        A build nobody runs never acquires an installation, so 4-7 is where the merge-day builds
        stay forever while the quietest real build shows ~29 at that same index. Every floor in
        [8, 24] is therefore the same decision, and the assertion below is the interval as well
        as the literal.

        Read through `datacollector.get_no_user_build_floor`, not the raw config, because the
        `channel != "nightly"` guard is the load-bearing half: nightly's builds do not come
        from the `builds` table, there is no merge-day build, and a quiet nightly build-day is
        ordinary (median 315 lifetime reports/build against beta's 2,674) — dropping one there
        would REMOVE a real baseline and make the from-zero branch fire MORE."""
        self.assertEqual(dc.get_no_user_build_floor("Firefox", "beta"), 15)
        self.assertLessEqual(8, dc.get_no_user_build_floor("Firefox", "beta"))
        self.assertLessEqual(dc.get_no_user_build_floor("Firefox", "beta"), 24)
        self.assertEqual(dc.get_no_user_build_floor("Firefox", "nightly"), 0)

    def test_the_buildhub_lookback_is_the_retention_window(self):
        """`buildhub_lookback_ndays` 30, and 30 is not a tuned number — it is `max_ndays`.

        It used to be `get_ndays()` (3), and three days is shorter than beta's build interval
        often enough that a rolling 3-day window over Buildhub's 196 days of beta history held
        0 builds on 23 days (12%) and 1 build on 117 (60%): 71% of possible switch-on moments
        gave `Build.get_last_versions(n=3)` fewer than two rows, so the table grew one build at
        a time and the selection window took ~5 days to become three deep. The hazard is
        specific to flipping `INGEST_CHANNELS` on a live database, which is the documented
        canary mechanism.

        The two relations, rather than the literal: (1) it equals the window `Node.clean`
        retains changesets for, so we ask for builds exactly as far back as we keep the pushlog
        they would be scored against, and never further; (2) it has to cover
        `get_last_versions(n=3)` at beta's measured cadence of 2.00 days (median of 58
        consecutive gaps, 2026-04-01..08-24) on the FIRST tick, or the canary starts blind."""
        self.assertEqual(config.get_buildhub_lookback_ndays(), 30)
        self.assertEqual(config.get_buildhub_lookback_ndays(), config.get_ndays_of_data())
        self.assertGreaterEqual(config.get_buildhub_lookback_ndays(), 3 * 2.0)


class TestBetaPopulationRatesAreBetas(unittest.TestCase):
    """`sigage._POPULATION_RATES` — the denominator is the whole rule
    (`hardware-noise-denominator`)."""

    def test_the_beta_population_rates_are_beta_s(self):
        """Beta 6.75% / 5.82% against nightly 2.55% / 4.15%. Same instrument, same 364 days.

        Measured 2026-08-25 with `hardware_noise`'s shape (Firefox, `get_search_channel`,
        364 days): nightly n=692,770 — which REPRODUCES the two shipped module constants, so
        the instrument agrees with the values it is replacing — and beta n=269,501, i.e. 2.6x
        and 1.4x. Beta is 41.9% 32-bit x86 against nightly's 1.6%, which is the kind of
        difference that moves a hardware-annotation rate.

        These numbers are printed to the model immediately before "the higher these are, the
        likelier it is that this signature is a failing-hardware artefact ... any mechanism you
        can construct for it will be fiction that fits". Telling a beta run that its 6% flip
        rate is 2.6x the population, when 6.75% IS the beta population, is an instruction to
        disbelieve an ordinary beta signature.

        `aurora` carries beta's numbers because aurora IS beta in Socorro's data
        (`get_search_channel("beta")` returns `["beta", "aurora"]`, and omitting it costs -36%
        of the rows), so a rate keyed on the raw channel string must answer for both."""
        self.assertEqual(sigage.population_bit_flip_rate("beta"), 0.0675)
        self.assertEqual(sigage.population_broken_cpu_rate("beta"), 0.0582)
        self.assertEqual(sigage.population_bit_flip_rate("aurora"), 0.0675)
        self.assertEqual(sigage.population_broken_cpu_rate("aurora"), 0.0582)
        self.assertEqual(sigage.population_bit_flip_rate("nightly"), 0.025)
        self.assertEqual(sigage.population_broken_cpu_rate("nightly"), 0.041)
        # The old channel-blind constants are still exported and are still nightly's, so the
        # two cannot drift apart while both exist.
        self.assertEqual(sigage.POPULATION_BIT_FLIP_RATE,
                         sigage.population_bit_flip_rate("nightly"))
        self.assertEqual(sigage.POPULATION_BROKEN_CPU_RATE,
                         sigage.population_broken_cpu_rate("nightly"))
        # An ORDINARY beta signature must not sit at the suppression threshold: the gate's
        # `max_bit_flip_rate` is channel-blind at 0.2 and beta's population is 0.0675, which is
        # why 0.2 survives beta unchanged (`bit_flip_rate` clears it on 10 of 77 beta
        # selections; median 0.000, p75 0.038).
        self.assertLess(sigage.population_bit_flip_rate("beta"),
                        config.get_agent_bit_flip()["max_bit_flip_rate"])

    def test_every_triaged_channel_has_its_own_two_rates(self):
        """The prerequisite, stated as a rule: measure the two RATES before triaging a channel.

        Item 24 measured beta's flip and broken-CPU rates before beta was switched on and
        deliberately did NOT measure its top-`cpu_info` median, so the median is optional (the
        prose drops the comparison) while the two rates are not: they are printed as "crash
        population: N%" right next to the signature's own share, and a `None` there leaves the
        model reading a bare percentage with nothing to read it against. That is the
        `hardware-noise-denominator` lesson — the denominator is the whole rule — so a third
        triaged channel must arrive with its own two numbers."""
        for channel in config.get_agent_channels():
            with self.subTest(channel=channel):
                self.assertIsNotNone(sigage.population_bit_flip_rate(channel))
                self.assertIsNotNone(sigage.population_broken_cpu_rate(channel))

    def test_the_top_cpu_share_median_says_nothing_on_beta(self):
        """`population_top_cpu_share_median("beta")` is None, and None is the honest answer.

        0.32 was measured over 200 Firefox-NIGHTLY signatures drawn the way the spike selector
        draws them, and nobody has run that sample on beta. An unmeasured population must say
        NOTHING rather than borrow nightly's — quoting nightly's median at a beta run is the
        `hardware-noise-denominator` mistake in the direction that reads as evidence.

        Also pinned: an ABSENT channel falls back to nightly (every caller that passes none is
        a nightly path, since nightly is the only channel the pipeline has run on) while a
        channel that is NAMED but unmeasured (release, which is 10%-sampled) does not. Those are
        two different questions — "I was not told" and "we have no measurement" — and they must
        not share an answer."""
        self.assertIsNone(sigage.population_top_cpu_share_median("beta"))
        self.assertIsNone(sigage.population_top_cpu_share_median("aurora"))
        self.assertEqual(sigage.population_top_cpu_share_median("nightly"), 0.32)
        self.assertEqual(sigage.population_top_cpu_share_median(), 0.32)
        self.assertEqual(sigage.POPULATION_TOP_CPU_SHARE_MEDIAN,
                         sigage.population_top_cpu_share_median("nightly"))
        # Absent -> nightly's, for all three rates and the label.
        self.assertEqual(sigage.population_bit_flip_rate(), 0.025)
        self.assertEqual(sigage.population_broken_cpu_rate(), 0.041)
        self.assertEqual(sigage.population_label(), "Firefox-nightly")
        self.assertEqual(sigage.population_label("beta"), "Firefox-beta")
        self.assertEqual(sigage.population_label("aurora"), "Firefox-beta")
        # Named but unmeasured: nothing, including the label, which must not say "nightly" next
        # to a number that is not nightly's.
        for rate in (sigage.population_bit_flip_rate, sigage.population_broken_cpu_rate,
                     sigage.population_top_cpu_share_median):
            with self.subTest(rate=rate.__name__):
                self.assertIsNone(rate("release"))
        self.assertEqual(sigage.population_label("release"), "Firefox")

    def test_a_beta_prompt_never_quotes_the_nightly_median(self):
        """The None has to reach the PROSE, not just the accessor — that is where it can lie.

        `triage._cpu_spread_line` is the one consumer that states a background figure in words,
        and the line goes to the principal AND to the blind second opinion. The fixture is a
        signature whose reports are 90% on one CPU model, above
        `POPULATION_TOP_CPU_SHARE_MIN_REPORTS`."""
        noise = {"top_cpu_share": 0.9, "cpu_reports": 40, "cpu_terms": 3,
                 "top_cpu_term": "family 6 model 183 stepping 1"}
        nightly = triage._cpu_spread_line(noise, {"channel": "nightly"})
        self.assertIn("median Firefox-nightly signature", nightly)
        self.assertIn("32%", nightly)
        beta = triage._cpu_spread_line(noise, {"channel": "beta"})
        self.assertIn("90%", beta)                       # the signature's own share still runs
        self.assertIn("has NOT been measured", beta)
        # The nightly median must not be stated AS beta's background. The word
        # "Firefox-nightly" does still appear, disowning the 32% figure ("the 32% figure this
        # note quotes elsewhere is Firefox-nightly's") — that is the honest form of the
        # sentence, so the assertion is on the CLAIM, not on the string.
        self.assertNotIn("median Firefox-nightly signature", beta)
        self.assertNotIn("sits at 32%", beta)


if __name__ == "__main__":
    unittest.main()
