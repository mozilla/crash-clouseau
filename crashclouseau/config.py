# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import os


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


def get_ndays():
    return _get_global()["backward_lookup_ndays"]


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
_SPIKE_DEFAULTS = {"floor": 5, "ratio": 3}


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
    prod until then. A path is re-read only when its mtime changes."""
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
