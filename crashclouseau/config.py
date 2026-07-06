# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import json


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


def get_agent_version():
    return get_agent().get("agent_version", 1)


def get_patch_extraction_cfg():
    return get_agent().get("patch_extraction", {})


def get_agent_ui():
    """UI/apply knobs for the evidence panel + apply/replay step (#12).

    Normalized so callers never re-derive defaults: ``show_abstain`` (show the
    panel for ABSTAIN verdicts), ``high_confidence_label`` (badge text),
    ``apply_min_confidence`` (numeric 0-100 gate — ``Verdict.confidence`` is stored
    as an int, high==85 via CONFIDENCE_SCORE), and ``enabled_types`` (the ONLY
    recorded action types the human-confirmed apply route is allowed to execute).
    """
    agent = get_agent()
    ui = agent.get("ui", {})
    return {
        "show_abstain": ui.get("show_abstain", False),
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


def get_eval():
    return _get_global().get("eval", {})
