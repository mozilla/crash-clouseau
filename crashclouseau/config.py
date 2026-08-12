# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import os

import libmozdata.config


__GLOBAL = None
__EXTS = None
__LOCAL = None


def _get_global():
    global __GLOBAL
    if not __GLOBAL:
        with open("./config/global.json", "r") as In:
            __GLOBAL = json.load(In)
    return __GLOBAL


def _get_exts():
    global __EXTS
    if not __EXTS:
        with open("./config/interesting_extensions.json", "r") as In:
            data = json.load(In)
            __EXTS = set(x for v in data.values() for x in v)
    return __EXTS


def _get_local():
    global __LOCAL
    if not __LOCAL:
        try:
            with open("./config/local.json", "r") as In:
                __LOCAL = json.load(In)
        except Exception:
            __LOCAL = {}
    return __LOCAL


def get_channels():
    return _get_global()["channels"]


def get_products():
    return _get_global()["products"]


def get_limit_facets():
    return _get_global()["facets_limit"]


def get_build_facets_limit():
    """Facet size for the ``build_id`` facet that enumerates a window's builds
    (``datacollector.get_buildids_from_socorro``). Socorro's terms facets are
    COUNT-ordered, so a window with more builds than this silently loses the
    quietest ones -- and a build we never list is a build we never query."""
    return _get_global().get("build_facets_limit", 500)


def get_ndays():
    """Baseline length for the spike detector, and (deliberately, elsewhere) the
    buildhub backfill and the regressor pushlog window. Widening the *build* window
    is ``get_nightly_window_ndays``; this one must not be used for it."""
    return _get_global()["backward_lookup_ndays"]


def get_nightly_window_ndays():
    """How far back ``datacollector.get_builds`` looks for nightly builds.

    Separate from ``get_ndays`` on purpose: that value is also the baseline length, the
    buildhub backfill and the regressor pushlog window, so widening the build window
    through it would also widen what the agent may blame. Kill switch for the wider
    window: set this back to 8 (the old ``ndays + 5``)."""
    return _get_global().get("nightly_window_ndays", 21)


def get_ndays_of_data():
    return _get_global()["max_ndays"]


def get_extensions():
    return _get_exts()


def get_max_score():
    return _get_global()["score"]["max"]


def get_num_lines():
    return _get_global()["score"]["number_of_lines"]


def get_database():
    return _get_local().get("database", "")


def get_redis():
    return _get_local().get("redis", "")


def get_socorro():
    return _get_local().get("socorro", "")


def get_bugzilla_token():
    """The Bugzilla API key the write path authenticates with — environment first.

    Environment first because **libmozdata cannot read it**. ``libmozdata.config.get``
    looks like it honours ``LIBMOZDATA_CFG_<SECTION>_<OPTION>`` (that is what its
    ``ConfigEnv`` provider is for), but the module installs ``ConfigIni`` as the global
    provider and nothing calls ``set_config``, so the env var is never consulted. On
    Heroku there is no ``~/.mozdata.ini`` and the deployed ``mozdata.ini`` has no token,
    so the lookup returned "" and ``autofile_bug`` skipped every single crash with "no
    Bugzilla API token configured" — silently, with the key sitting in the config vars.

    Swapping the global provider to ``ConfigEnv`` is NOT the fix, on two counts:

    * it does not read the ini at all, and ``[User-Agent] name`` is fetched with
      ``required=True`` by every libmozdata connection — losing it asserts, and losing
      the allowlisted ``crash-clouseau`` UA gets us 406-throttled by hg.mozilla.org;
    * it would also put a token on ``libmozdata.bugzilla.Bugzilla``, whose reads we
      deliberately leave anonymous: ``buginfo.get_bugs`` infers "security bug" from a
      bug Socorro knows about that a Bugzilla search does not return, so authenticating
      it would render a restricted bug's summary on a public canary instead.

    ``BUGZILLA_TOKEN`` mirrors ``SOCORRO_TOKEN``, the convention this app already uses.
    ``LIBMOZDATA_CFG_BUGZILLA_TOKEN`` is accepted too: it is the name libmozdata *would*
    read if it read the environment, so anyone who sets it has every reason to expect it
    to work, and finding out otherwise costs a night of unfiled bugs.
    """
    for name in ("BUGZILLA_TOKEN", "LIBMOZDATA_CFG_BUGZILLA_TOKEN"):
        token = os.getenv(name)
        if token:
            return token
    # Never None: the caller tests ``if not token`` to skip, and the apply path puts it
    # straight into a header, where None is a TypeError inside requests.
    return libmozdata.config.get("Bugzilla", "token", "") or ""


def get_threshold(typ, product, channel):
    return (
        _get_global()
        .get("thresholds", {})
        .get(typ, {})
        .get(product, {})
        .get(channel, 1)
    )


# Fallback when the ``spike`` block (or a product/channel within it) is absent: bias to
# FEWER detections so a stripped config stays quiet rather than flooding the pipeline.
_SPIKE_DEFAULTS = {
    "floor": 5,
    "ratio": 3,
    # A build older than this many days is "mature": most of its crashes have already
    # arrived, so a spike on it is judged against a stricter bar (see utils.evaluate_days).
    "mature_after_days": 5,
    # ...that bar: a mature build-day must clear `floor` outright (the from-zero rule
    # alone is not enough) and its buildid must carry at least this many distinct
    # installations. 1 would be inert -- `cardinality_install_time` is never 0 here
    # (datacollector coerces it to 1). Measured on the 2026-08-11 nightly window, this
    # is the dial that prices the wider window: against the old 8-day window it lets
    # through 59 extra signatures at 2, 37 at 3, 15 at 4 and 5 at 6, and everything it
    # drops between 2 and 4 was a two-installation third-party driver signature
    # (igdusc64.dll, mfx_mft_h264ve_64.dll, ...). This half of the bar is inert on
    # beta/release, whose install thresholds (6/50) are already higher -- see `needed` in
    # utils.evaluate_days -- but the `floor` half is NOT, so datacollector applies the
    # whole bar to nightly only.
    "mature_installs": 4,
}


def get_spike(typ, product, channel):
    """Spike-detection knob ``typ`` (``"floor"`` | ``"ratio"``) for a product/channel.
    ``floor`` = minimum crashes on the spike day; ``ratio`` = minimum multiple over the
    loudest of the preceding days. See ``utils.is_spike``."""
    return (
        _get_global()
        .get("spike", {})
        .get(typ, {})
        .get(product, {})
        .get(channel, _SPIKE_DEFAULTS[typ])
    )


def get_agent():
    return _get_global().get("agent", {})


def get_searchfox():
    return get_agent().get("searchfox", {})


def get_agent_schema_version():
    return get_agent().get("schema_version", 1)


def get_min_citations_per_claim():
    return get_agent().get("min_citations_per_claim", 1)


def get_abstain_below_confidence():
    return get_agent().get("abstain_below_confidence", 0.5)


def get_llm():
    return get_agent().get("llm", {})


def get_llm_role(role):
    return get_llm().get("roles", {}).get(role, {})


def get_agent_enabled():
    return get_agent().get("enabled", True)


def get_agent_channels():
    """Channels the evidence agent runs on. Defaults to nightly only — the product's
    target population (small volume, high per-crash significance); beta/release would
    multiply cost with less value. Empty list means "no channel filter" (all)."""
    return get_agent().get("channels", ["nightly"])


def get_agent_queue():
    return get_agent().get("queue", "agent")


def get_agent_job_timeout():
    return get_agent().get("job_timeout", 1800)


def get_agent_skip_if_existing():
    return get_agent().get("skip_if_existing", True)


def get_agent_max_seed_frames():
    return get_agent().get("max_seed_frames", 40)


def get_agent_reap_max_attempts():
    """How many times the stale-job reaper may re-enqueue one orphaned dossier before
    GIVING UP (marking it ``error``). Bounds the OOM re-enqueue loop: a crash that keeps
    orphaning (e.g. OOMs on every run) fails visibly instead of burning tokens forever.
    Default 2 (one transient blip is covered; a persistent failure gives up)."""
    return get_agent().get("reap_max_attempts", 2)


def get_agent_proto_max_unusable():
    """How many BROKEN runs (``models._UNUSABLE_VERDICT_PREFIXES``: no readable handoff, or a
    dossier that failed validation) one proto-signature cluster may pay for before
    ``UUID.proto_already_analyzed`` treats the cluster as triaged anyway.

    A broken run examined nothing, so it must not close its cluster — but the failure is not
    guaranteed to be independent of the crash either: a stack that reliably makes the model
    omit a cited field would re-break on every new uuid in the cluster, at ~$3 a time,
    forever. Default 2, matching ``reap_max_attempts``: retry once, then give up loudly
    rather than pay indefinitely. Set to 0 to retry without a bound."""
    return get_agent().get("proto_max_unusable", 2)


def get_agent_version():
    return get_agent().get("agent_version", 1)


def get_patch_extraction_cfg():
    return get_agent().get("patch_extraction", {})


def _env_bool(name, default):
    """A boolean config override from the environment (canary knob, like INGEST_CHANNELS
    / QUEUES): unset -> default; 1/true/yes/on -> True; anything else -> False."""
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def get_agent_autofile():
    """Automatic bug FILING knobs (the only unattended write to Bugzilla).

    ``enabled`` is a genuine kill-switch, not dead config: this posts to production BMO
    without a human in the loop, so it has to be stoppable from ``heroku config:set``
    without waiting on a deploy. It defaults OFF and is armed with ``AUTOFILE_BUGS=1``.

    ``min_confidence`` 70 is the ``probable`` rung — a lead the model rated strongly or that
    a deterministic check corroborated — measured at ~3 crashes/day, versus ~7.6/day if it
    were lowered to the ``medium`` rung of 50. ``daily_cap`` bounds the damage a bad gate
    can do in one night; the pipeline itself has no such bound."""
    a = get_agent().get("autofile", {})
    return {
        "enabled": _env_bool("AUTOFILE_BUGS", a.get("enabled", False)),
        "min_confidence": a.get("min_confidence", 70),
        "verdicts": a.get("verdicts", ["lead", "culprit"]),
        "needinfo": _env_bool("AUTOFILE_NEEDINFO", a.get("needinfo", True)),
        "daily_cap": a.get("daily_cap", 10),
        # An open bug already referencing the signature: comment there instead of filing a
        # duplicate. Turning this off does NOT file anyway — it skips.
        "comment_on_existing": a.get("comment_on_existing", True),
        # ...but only if that bug can be ABOUT this regression. How many days the suspected
        # regressor may land AFTER an open bug was filed and still count as that bug's cause;
        # past it, the bug describes crashes the candidate cannot have caused and we file a
        # new one (``bugzilla_apply._bug_for_this_regression``).
        #
        # 30 days, deliberately LOOSER than the stale-signature gate's 7 even though it is the
        # same argument, because the cost of being wrong is asymmetric. A bigger number admits
        # more bugs as the venue, so it errs toward commenting — the pre-existing behaviour,
        # whose failure is a report buried in an unrelated bug. A smaller one errs toward
        # filing, whose failure is a near-duplicate on BMO for a human to close, and this
        # module's standing rule is that a missed filing is recoverable where a duplicate is
        # not. Nothing in the evidence asks for a tight threshold either: the two real cases
        # separate by three orders of magnitude — the correct comment landed on a bug filed 9
        # days AFTER its regressor, the wrong one on a bug filed 1375 days BEFORE.
        "comment_max_bug_age_days": a.get("comment_max_bug_age_days", 30),
    }


def get_agent_ui():
    """UI/apply knobs for the evidence panel + apply/replay step (#12).

    Normalized so callers never re-derive defaults: ``show_abstain`` (show the
    panel for ABSTAIN verdicts; env override ``SHOW_ABSTAIN`` so a canary can surface
    every triaged crash's rationale while evaluating), ``high_confidence_label`` (badge
    text), ``apply_min_confidence`` (numeric 0-100 gate — ``Verdict.confidence`` is
    stored as an int, high==85 via CONFIDENCE_SCORE), and ``enabled_types`` (the ONLY
    recorded action types the human-confirmed apply route is allowed to execute).
    """
    agent = get_agent()
    ui = agent.get("ui", {})
    return {
        "show_abstain": _env_bool("SHOW_ABSTAIN", ui.get("show_abstain", False)),
        "show_lead": ui.get("show_lead", True),
        "show_experts": ui.get("show_experts", True),
        "high_confidence_label": ui.get("high_confidence_label", "STRONG EVIDENCE"),
        "lead_label": ui.get("lead_label", "LEAD"),
        "apply_min_confidence": agent.get("confidence", {}).get("apply_min", 85),
        "lead_apply_min_confidence": agent.get("confidence", {}).get("lead_apply_min", 50),
        "enabled_types": agent.get("apply", {}).get(
            "enabled_types", ["bugzilla.add_comment", "bugzilla.update_bug"]
        ),
    }


def get_agent_filters():
    """Noise-filter knobs (#15 phase 3): down-rank — never drop — candidates that are
    obviously unrelated. ``ubiquitous_paths``/``ubiquitous_symbols`` are the
    everything-uses-it primitives (a break there would crash all of Firefox, not one
    signature) — matched against both frame filenames (paths) and frame functions
    (symbols); ``anchor_frame_patterns`` are universal bottom-of-stack frames (the
    'main()' problem); ``penalty`` is the seed-score multiplier applied to a candidate
    whose only support is such noise."""
    f = get_agent().get("filters", {})
    return {
        "ubiquitous_paths": f.get("ubiquitous_paths", []),
        "ubiquitous_symbols": f.get("ubiquitous_symbols", []),
        "anchor_frame_patterns": f.get("anchor_frame_patterns", []),
        "penalty": f.get("penalty", 0.1),
    }


def get_agent_offstack():
    """P1 off-stack seeding knobs. ~29% of regressors are *off-stack* (touch no file on
    the crash stack), so no changeset scores onto a frame and ``build_seed`` skips the
    agent entirely. When ``enabled``, seed the agent with the FULL first-bad-build pushlog
    window instead of skipping. Gated OFF by default; the two precision guards
    (``require_callpath_for_strong``, ``exposer_classifier``) and ``observe_only`` default
    ON, so turning off-stack ON can never produce a low-precision, action-emitting run
    without an explicit second edit. Returned as one normalized dict so callers never
    re-derive defaults and a future config edit can't silently flip a guard off.
    ``OFFSTACK_ENABLED`` / ``OFFSTACK_PINNED`` / ``OFFSTACK_OBSERVE_ONLY`` are env canary
    levers (like ``SHOW_ABSTAIN``) so the worker dyno flips them without editing tracked
    JSON. Still layered UNDER ``get_agent_enabled`` and bounded by ``get_agent_channels``
    (nightly-only) — this does NOT widen either."""
    o = get_agent().get("offstack", {})
    return {
        "enabled": _env_bool("OFFSTACK_ENABLED", o.get("enabled", False)),
        "max_candidates": o.get("max_candidates", 150),
        "pinned": _env_bool("OFFSTACK_PINNED", o.get("pinned", True)),
        "require_callpath_for_strong": o.get("require_callpath_for_strong", True),
        "exposer_classifier": o.get("exposer_classifier", True),
        "observe_only": _env_bool("OFFSTACK_OBSERVE_ONLY", o.get("observe_only", True)),
        # Prior-signature (P4) corroboration: seed the agent with, and confidence-corroborate
        # on, the regressor a prior FIXED sibling of this signature already names. ~10%
        # off-stack reach (spike/PRIOR_SIGNATURE_REPORT). Adds one Socorro+Bugzilla lookup
        # per off-stack seed; off by setting this false.
        "prior_signature": o.get("prior_signature", True),
    }


def get_agent_offstack_cost_cap():
    """Per-crash cost cap for an OFF-STACK run (a ~112-candidate window is pricier than a
    handful of scored candidates). Falls back to the on-stack cap, then 4.0. Log-only,
    like ``max_cost_usd_per_crash`` (orchestrator warns; it does not abort mid-run)."""
    llm = get_llm()
    return llm.get(
        "max_cost_usd_per_crash_offstack", llm.get("max_cost_usd_per_crash", 4.0)
    )


def get_agent_second_opinion():
    """Blind second-opinion pass knobs (#SO). For a REPORTED lead whose confidence rung is
    at/above ``min_confidence`` (0-100), a fresh independent agent re-analyses the crash with
    NO context from the first pipeline (verifier if we have a candidate, mechanism-generator
    if not). Returned as one normalized dict so callers never re-derive defaults. Gated OFF
    by default; ``SECOND_OPINION_ENABLED`` is the env canary lever (like ``OFFSTACK_ENABLED``).
    A strong model (opus/effort=max) is deliberate: this is a rare, single-shot, no-context
    call — the blanket effort=max OOM/no-gain finding was about the full multi-agent pipeline,
    not one blind call."""
    o = get_agent().get("second_opinion", {})
    return {
        "enabled": _env_bool("SECOND_OPINION_ENABLED", o.get("enabled", False)),
        "model": o.get("model", "opus"),
        # `high`, NOT `max`. Measured head-to-head on 51 corpus cases with known ground truth
        # (spike/so_instrument_calibration.py, both arms, identical cases): `high` matched or beat
        # `max` on every axis — clean-label sensitivity 15/15 vs 14/15, specificity 26/26 for
        # both, at HALF the cost ($19.89 vs $40.62) and 2.6x the speed (101s vs 258s mean). The
        # sensitivity edge is one case and well within noise; the cost and latency wins are not.
        # So the "SO is the allowed single-shot exception to the no-effort=max rule" carve-out is
        # retired: max was simply worse here.
        "effort": o.get("effort", "high"),
        "max_turns": o.get("max_turns", 20),
        # Report threshold. There is NO separate report gate: ANY ``lead`` is shown (only
        # abstains are hidden, modulo ``show_abstain``), so this must sit at the LOWEST rung a
        # lead can hold — ``Confidence.low`` (0.25) — for "every reported lead gets a second
        # opinion" to actually hold. It was 50, which silently left the WEAKEST shown leads (the
        # ones an independent check helps most) with no second opinion at all: 4 of 31 reported
        # leads over the first three prod days.
        "min_confidence": o.get("min_confidence", 25),
        # Separate, HIGHER bar for letting a corroboration MOVE the band (vs merely measuring).
        # Measuring every reported lead is not a licence to re-rank the weakest ones: at `low` a
        # boost would jump TWO rungs (low -> probable, p_worth 0.50 -> 0.97).
        #
        # NOTE this floor originally existed to stop the fold being one-directional at the bottom
        # rung, back when a refutation there was a no-op. That is no longer why it is here: a
        # refutation now ABSTAINS a lead at/below `medium` (see `_fold_second_opinion`), so the
        # bottom rung moves in both directions — just not symmetrically, and deliberately. What
        # justifies the floor NOW is that the two signals are not equally trustworthy: the
        # corroborate side was never part of the calibration fit, and in the first prod days 2 of
        # 6 corroborated leads still had the candidate landing AFTER the signature's first-seen
        # buildid, whereas measured SO specificity is 1.00 (when it refutes, it is right). So:
        # promote conservatively, suppress readily.
        "min_boost_confidence": o.get("min_boost_confidence", 50),
    }


def get_agent_signature_age():
    """Stale-signature downweight knobs. When a crash's signature was first seen more than
    ``min_age_days`` BEFORE the named candidate landed, that candidate cannot be the crash's
    ORIGIN. Measured on the canary's first three prod days: 10 of 10 high-confidence
    second-opinion refutations rested on this argument and all 10 verified deterministically
    (median gap 178 days).

    ``min_age_days`` = 7 was chosen by back-testing thresholds against those 23 real leads, with
    the blind second opinion as an independent yardstick: at 7 days the rule fires on 10/10
    high-confidence refutations while sparing 5 of 6 CORROBORATED leads. Tighter (>0d) drags in a
    second corroborated lead for no extra recall; looser (>90d) drops to 6/10.

    A DOWNWEIGHT, deliberately not a drop: signature REUSE is real (an old signature can acquire
    a new cause, and a rare pre-existing crash can be made frequent by a new change), and 1 of 6
    independently-confirmed leads still trips it — a hard rule would kill real leads."""
    a = get_agent().get("signature_age", {})
    return {
        "enabled": _env_bool("SIGNATURE_AGE_ENABLED", a.get("enabled", True)),
        "min_age_days": a.get("min_age_days", 7),
    }


def get_agent_bit_flip():
    """Hardware bit-flip suppression knobs.

    Socorro's stackwalker checks, for the faulting address and each register the crashing
    instruction names, whether flipping ONE bit yields a plausible value (NULL, or mapped
    memory), and publishes the best score as ``possible_bit_flips_max_confidence``. When it says
    yes and the signature has never crashed anyone else, the likeliest explanation is a bad
    machine — there is no software bug for anybody to fix. Bug 2061961 was filed and needinfo'd
    at a developer on exactly such a crash (confidence 62, one report) and closed INVALID two
    days later, by two people citing this field.

    ``min_confidence`` 50 is a structural line, not a tuned one. rust-minidump combines
    hand-picked weights with a noisy-OR over a 0.25 baseline, so 25 means only "some single-bit
    variant happens to be mapped" — near noise on a 64-bit heap — and a poison register (which
    argues for a use-after-free, i.e. SOFTWARE) multiplies the result by 0.5. Above 50 sits
    exactly the un-detracted evidence: non-canonical, or NULL-and-not-low, or a nearby register.
    Production values cluster with a gap between 43 and 62, so the threshold is not on a knife
    edge.

    ``max_reports`` 1 is the other half, and it is load-bearing rather than belt-and-braces: the
    same score is common on high-volume signatures (one flaky machine can contribute hundreds of
    reports), so confidence ALONE would suppress busy, real crashes. Both must hold.

    An env kill-switch rather than a plain constant, matching ``SIGNATURE_AGE_ENABLED``: this one
    can silence a verdict outright, so it has to be stoppable without a deploy."""
    a = get_agent().get("bit_flip", {})
    return {
        "enabled": _env_bool("BIT_FLIP_GATE_ENABLED", a.get("enabled", True)),
        "min_confidence": a.get("min_confidence", 50),
        "max_reports": a.get("max_reports", 1),
    }


def get_agent_bad_machine():
    """Bad-machine suppression knobs.

    A machine with failing memory scatters: one installation produced 21 crashes across 20
    distinct signatures in two days and we filed TWO bugs from it (2062168, 2062173). Jan de
    Mooij closed the first with exactly this rule -- "It's just one crash report and that
    installation has multiple crashes with distinct signatures" -- and the same reviewer had
    already written, on bug 2061124, that "crashes with very few reports in common code paths
    are often hardware related".

    ``min_signatures`` 10 is where the effect is both largest and stable. Measured over 141k
    nightly crashes (11,735 single-machine signatures, outcome = later reproduced on DIFFERENT
    hardware, base rate 17.96%): at 10 with the CPU guard the recurrence rate drops to 11.58%
    (-7.0pp, z=-4.4), the largest effect anywhere in the study, holding across a split-half
    (-8.6pp and -5.8pp on consecutive months). Lower thresholds are weak (5 gives -1.8pp before
    the guard) and higher ones overfit (15 flips sign across lookbacks). Crash COUNT is not a
    predicate at any threshold -- every value from 3 to 50 lands between -1.5pp and +1.8pp with
    no significance. Diversity, not volume.

    ``max_cpu_infos`` 1 is the MECHANISM TEST, not a refinement. ``install_time`` collides: 11%
    of ids with 3+ signatures span several CPU models (VM/distro images sharing one install
    second). Bug 2061961 looks like a scattergun and carries 4 CPUs and 3 operating systems. The
    scatter effect is strong where the id resolves to one CPU and vanishes where it does not
    (+1.0pp, p=0.77) -- it appears exactly where "one bad machine" predicts and nowhere else.

    ``min_span_seconds`` 1800 separates a failing machine from one cascading session. Bug
    2047016 (RESOLVED FIXED, a real regression that grew to 682 crashes across 23 installs) had
    its FIRST crash on a machine that emitted 5 distinct signatures in 22 minutes -- one broken
    Wayland/video stack unwinding, not bad hardware. Signature count cannot tell a cascade from a
    scattergun; elapsed time can, and this guard is what keeps that bug out of the false
    negatives.

    NOT scoped to JS, though the request came from the JS team: a bad machine poisons every
    component. Of the filings this reasoning covers, one is Servo/style and one is WebRTC.

    UPTIME IS DELIBERATELY ABSENT. The JS team's rule of thumb paired "no recent crashes from the
    same machine" with "high uptime"; the first half holds and the second does not. Uptime looks
    predictive only because the previous crash resets the clock (median uptime by crash ordinal
    on one machine: 1281s, 585s, 278s, 171s), so any machine that crashes often looks
    low-uptime. Matched on crashes-per-machine the signal is AUC 0.497 -- a coin flip -- and its
    sign flips between adjacent fortnights. It is measured and recorded, never gated on."""
    a = get_agent().get("bad_machine", {})
    return {
        "enabled": _env_bool("BAD_MACHINE_GATE_ENABLED", a.get("enabled", True)),
        "min_signatures": a.get("min_signatures", 10),
        "max_cpu_infos": a.get("max_cpu_infos", 1),
        "min_span_seconds": a.get("min_span_seconds", 1800),
        "lookback_days": a.get("lookback_days", 14),
    }


def _normalize_calibration_table(raw):
    """Coerce a rung -> P map to ``{int rung score: float P}``; drop non-numeric entries.
    Accepts either the flat map or ``eval.calibrate``'s wrapper (a ``calibration_table`` key)."""
    if isinstance(raw, dict) and "calibration_table" in raw:
        raw = raw["calibration_table"]
    table = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            try:
                table[int(key)] = float(value)
            except (TypeError, ValueError):
                continue
    return table


__CALIBRATION_CACHE = {}


def get_agent_calibration():
    """The fitted worth-investigating calibration table (Phase-2): ``{rung score (int) ->
    P(worth-investigating)}`` mapping a verdict's confidence rung (``CONFIDENCE_SCORE`` * 100)
    to its empirical calibrated probability. Sourced from ``agent.calibration.table`` (an inline
    map) or ``agent.calibration.path`` (a ``calibration_table.json`` written by
    ``eval.calibrate`` — its ``calibration_table`` sub-key is used). Empty ``{}`` until a paid
    calibration run has been fit + wired, so ``Verdict.p_worth_investigating`` stays ``None`` in
    prod until then. A path is re-read only when its mtime changes.

    The shipped table gives rungs 70 and 85 the SAME value (0.9714) on purpose: it is the pooled
    70+85 bin (34/35), because rung 85 measured worse than rung 70 on the study corpus. It is a
    result, not an unfinished fit — do not "separate" them, and expect no badge movement from any
    gate that drops a verdict from 85 to 70. Full numbers in ``eval.calibrate``'s module
    docstring."""
    cal = get_agent().get("calibration", {})
    if cal.get("table") is not None:
        return _normalize_calibration_table(cal["table"])
    path = cal.get("path")
    if not path:
        return {}
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    cached = __CALIBRATION_CACHE.get(path)
    if cached is None or cached[0] != mtime:
        try:
            with open(path, "r") as handle:
                table = _normalize_calibration_table(json.load(handle))
        except (OSError, ValueError):
            return {}
        __CALIBRATION_CACHE[path] = (mtime, table)
        return table
    return cached[1]


def get_eval():
    return _get_global().get("eval", {})
