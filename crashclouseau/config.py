# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import os

import libmozdata.config

from .logger import logger


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


def get_buildhub_lookback_ndays():
    """How far back ``update.update_builds`` asks Buildhub for builds.

    SEPARATE FROM ``get_ndays`` (3), which is what it used to be, and the difference is
    whether a non-nightly channel can be switched on at all. ``update()`` runs
    ``put_filelog`` first, which sets ``LastDate.maxdate = now``; ``update_builds`` reads that
    back and subtracted 3 days, so a fresh channel was offered only the builds of the last 3
    days — and beta ships every ~2.00 days (median of 58 consecutive gaps, 2026-04-01..08-24).
    Over Buildhub's 196 days of beta history a rolling 3-day window holds **0 builds on 23
    days (12%) and 1 build on 117 (60%)**, so **71% of possible switch-on moments** gave
    ``Build.get_last_versions(n=3)`` fewer than two rows, and the table then grew one build at
    a time — roughly five days before the selection window was three deep. The hazard is
    specific to flipping ``INGEST_CHANNELS`` on a live database, which is the documented
    canary mechanism, so it would have been the first thing to go wrong and the hardest to
    see (one warning per 20-minute tick).

    Defaults to ``max_ndays`` (30), which is not a tuned number: it is the same window
    ``Node.clean`` retains changesets for, so we ask for builds exactly as far back as we
    keep the pushlog they would be scored against. ``Build.put_data`` is
    ``on_conflict_do_nothing``, so a wider fetch is idempotent; the cost is one larger
    Buildhub POST per tick."""
    return _get_global().get("buildhub_lookback_ndays", get_ndays_of_data())


def get_extensions():
    """The source extensions that get ``Changeset`` rows. See ``utils.is_interesting_file``
    for what this does and does not gate — measured, it moves no on-stack candidate."""
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


# BMO products belonging to an application OTHER than the one whose crashes we triage, keyed
# by that application. Every one of them is built on mozilla-central, so every one of them
# shares our crash SIGNATURES — and nothing on a Bugzilla bug says which application crashed:
# not ``cf_crash_signature``, not the summary, not a regressor bug's own component.
#
# Defined by exclusion (the other applications) rather than by inclusion (ours) deliberately.
# The Firefox side is twenty-odd products — Core, Firefox, Toolkit, DevTools, WebExtensions,
# GeckoView, Fenix, NSS, External Software Affecting Firefox, the graveyards — and an inclusion
# list that fell one product behind BMO would quietly start filing duplicates.
#
# IT IS NOT COMPLETE. The claim that stood here until 2026-08-21 — "these are the only
# non-Firefox products whose application reports crashes to Socorro at all" — is false in both
# directions, and ``bin/audit_products.py`` is that claim turned into a check, which FAILS today
# on purpose. Socorro's product facet, 30d to 2026-08-21, no product filter: Firefox 1,129,749 /
# Fenix 458,043 / Thunderbird 223,861 / Focus 5,804 / ReferenceBrowser 125 — three reporting
# applications with no entry here — while SeaMonkey, which HAS one, reported 0 crashes over the
# entire retention (180d: 11,383,017 reports; the 365d facet is identical, so 180d is all there
# is). MozillaVPN shows up at 180d with 9.
#
# THEY STAY OUT because measured 2026-08-21 they cost nothing in the only scope this map is used
# in, which is desktop Firefox. Of the 96 open ``Firefox for Android`` + 38 ``GeckoView`` + 2
# ``Focus`` bugs carrying a ``cf_crash_signature``, exactly FOUR carry a signature that also
# occurs in the 180d desktop-Firefox population: 1245570, 1644486, 1855806, 1812544. Two of the
# four (1245570, 1644486) collide ONLY on ``EMPTY: no frame data available; *`` signatures —
# 1245570's field carries 20 entries, but its ``Abort | …``, ``EMPTY: no crashing thread
# identified; *`` and ``OOM | large | …`` ones have 0 desktop reports each — and the reports
# behind the colliding ones have 0 threads and 0 frames, so ``inspector.thread_for_analysis``
# returns None and there is no analysis to file anywhere. Adding
# Fenix/Focus/ReferenceBrowser/MozillaVPN moves the chosen venue on 0 of the 51 filings
# (2026-08-05..21, live signatures re-run through ``_split_by_application``; 27 had a venue,
# exactly 1 had a foreign candidate) and on 0 of the 300 loudest desktop-nightly
# signatures (2026-08-07..21, 15,131 nightly reports; 298 lookups
# clean and the 2 that answered 502 retried by hand; 4 nominal hits, every one ``EMPTY: no
# frame data available; *``, so 0 analysable). It LOSES bug 1855806
# (``arena_run_reg_dalloc | arena_t::DallocSmall | arena_dalloc | idalloc``, NEW, 1
# desktop-nightly crash in 180d) as a comment venue. Panel and scripts:
# ``spike/other_app_products/``.
#
# AND THE MAP CANNOT BE DERIVED FROM BMO. ``/rest/product`` carries name, classification,
# description, components and versions — no application, no family. ``classification`` is the
# trap: ``MailNews Core`` and ``GeckoView`` are ``Components`` while ``Firefox`` and ``Focus``
# are ``Client Software``, so a classification-keyed "is this an application's product" map
# hands crash 05381864-aa6e-402f-a1fd-56a3e0260816 straight back to bug 2057980
# ``MailNews Core`` — measured on the panel, filing 2064066's venue flips from None to 2057980,
# so the derived map eats the one case this map exists for — and it strips BMO ``Firefox`` as
# well, 14 of whose 17 open signature-bugs collide with the desktop population. Hand-maintained
# is the answer; the audit is what keeps it honest.
#
# FENIX DAY (plans/16) NEEDS A SHAPE CHANGE, NOT ENTRIES. ``get_other_app_products`` drops the
# single key equal to the crash product, so with Fenix and Focus both keyed to the shared
# Android products a Fenix crash would still see ``Firefox for Android`` as foreign via the
# ``Focus`` key. It has to become ``Socorro product -> family`` plus ``family -> the BMO
# products only that family files in``, and the ``desktop: ["Firefox"]`` entry must NOT be
# applied when the crash product is unknown, or ``_split_by_application(bugs, None)`` starts
# stripping BMO ``Firefox`` venues and contradicts bugzilla_apply's "may only drop what it can
# positively identify as somebody else's". Leave ``GeckoView`` shared in that design; the
# measured price is bug 1812544 staying a desktop candidate. Two cases pin the shape: today's
# map calls bug 1681745 ``Firefox :: Installer`` a venue for a Fenix crash (wrong), and 40 of
# the 96 open ``Firefox for Android`` signature-bugs collide with the FOCUS population — one
# triage family, two Socorro products, which no per-Socorro-product map can express.
_OTHER_APP_PRODUCTS = {
    "Thunderbird": ["Thunderbird", "MailNews Core", "Calendar", "Chat Core"],
    "SeaMonkey": ["SeaMonkey"],
}


def get_other_app_products(product=None):
    """The BMO products a bug about *product*'s crashes cannot belong to.

    *product* is the crash's own Socorro product (``uuid_info["product"]``). A product the map
    does not name — Firefox, Fenix — gets every entry, and so does ``None``: an unknown product
    exempts nothing, so a missing one can never silently switch the check off.
    """
    return frozenset(
        p for app, products in _OTHER_APP_PRODUCTS.items() if app != product for p in products
    )


def describe_other_applications(product=None):
    """The other applications as one prose clause, for an agent prompt to interpolate.

    Today it renders the application name, then its BMO products in RST literal markup, then
    "and SeaMonkey". It exists so ``agent/tools/bugzilla.py``'s ``signature_bugs`` description
    is RENDERED from this map rather than restating it: that description was a second
    hand-written copy of the map, and the THIRD copy
    (``eval/study_corpus._NON_DESKTOP_PRODUCTS``) had already drifted to the opposite answer —
    it called ``Firefox for Android`` and ``GeckoView`` somebody else's, which this map
    deliberately does not, and omitted ``Calendar``, ``Chat Core`` and ``SeaMonkey``, which it
    does. Measured 2026-08-21 on the 287 blind study fixtures, the two lists classified 26 of
    them (9.1%) differently and no test compared them.

    Same ``product`` semantics as ``get_other_app_products``: the crash's own Socorro product is
    left out, ``None`` leaves out nobody. Map order rather than sorted, so the sentence leads
    with the application that actually shares signatures with us. An empty map renders an empty
    string — this is a source constant, so that is a source edit, not a runtime state."""
    bits = [
        app if products == [app] else "{} (``{}``)".format(app, "``, ``".join(products))
        for app, products in _OTHER_APP_PRODUCTS.items() if app != product
    ]
    if len(bits) > 1:
        return "{} and {}".format(", ".join(bits[:-1]), bits[-1])
    return bits[0] if bits else ""


_POPULATION_DEFAULTS = {
    "enabled": True,
    # The window reaches back to the build's date, clamped to [min, max] — see
    # ``population._window_start`` for why both ends are needed.
    "min_lookback_days": 7,
    "max_lookback_days": 30,
    "facets_size": 1000,
    # Thresholds MEASURED on the 59 loudest Firefox nightly signatures of 2026-08-05..12; see
    # the ``population`` module docstring for the sample.
    #  - top share: median 0.18, p75 0.47 -> 0.5 sits just above the third quartile (fired on
    #    13/59), so the flag means "unusually concentrated", not "more than a couple".
    #    THIS 0.5 BELONGS TO `install_time` ONLY. The same statistic over the `cpu_info` facet
    #    runs median 0.32 / p75 0.78 (200 Firefox-nightly signatures, 2026-08-21), where 0.5
    #    fires on 35% of the population and, swept as a suppressor, eats five of the canary's 19
    #    FIXED/DUPLICATE/ASSIGNED filings. See `sigage.POPULATION_TOP_CPU_SHARE_MEDIAN` and
    #    `orchestrator._signature_is_mostly_hardware`; do not inherit this number for that facet.
    #  - median gap between consecutive installs: p10 4430s (74 min), median 8h -> 300s is far
    #    below the tenth percentile (fired on 4/59). Those four are 20s, 62s, 89s and 142s:
    #    populations that cannot be independent users.
    "concentrated_share": 0.5,
    "clustered_gap_s": 300,
    # Minimum n for a flag. The shape of two reports is not a shape: top_share on 2 crashes is
    # 0.5 or 1.0 regardless, and the "median" of a single gap is that gap.
    "min_crashes": 5,
    "min_installs_for_gap": 3,
}


def get_population():
    """Crash-population knobs for the crashstack.html stats block (``population.for_crash``).

    Reporting only — nothing here can gate, score or suppress anything, which is why the
    thresholds are looser than ``get_agent_bad_machine``'s (that one suppresses a verdict, so it
    also demands the single-CPU mechanism test). Returned as one normalized dict so callers never
    re-derive a default."""
    cfg = dict(_POPULATION_DEFAULTS)
    cfg.update(_get_global().get("population", {}) or {})
    cfg["enabled"] = _env_bool("POPULATION_STATS", cfg["enabled"])
    return cfg


def get_threshold(typ, product, channel):
    """``thresholds.<typ>.<product>.<channel>``, default 1.

    ``installs`` is the minimum distinct installations a build-day needs before a spike counts.
    ``protos`` is the cap on how many distinct PROTO-SIGNATURE clusters one selected
    (signature, buildid) pair may contribute — and off nightly it is the dominant cost term,
    which is the opposite of what nightly's value suggests.

    MEASURED LIVE, 2026-08-25, by running the real selector against real beta data (one run,
    Firefox beta, window 155.0b2/b3/b4): **4 selected pairs carried 37 distinct protos**, i.e.
    37 paid LLM runs from 4 selections. Per pair: 19 crashes -> 12 protos, 10 -> 10, 10 -> 10,
    6 -> 5. Beta crash stacks are nearly all DISTINCT, so the proto-signature dedup that makes
    nightly cheap (mean 1.07 protos per pair, max 6, so its cap of 50 has NEVER bound) does
    almost nothing here. Sweep on that same live selection: cap 1 -> 4 runs, 3 -> 12, **5 -> 20**,
    10 -> 35, 20 -> 37, 50 -> 37.

    5, not 20: at ~$1-3 a run the cap is the difference between ~$20-60 and ~$37-111 for one
    tick's selections, and the facet is COUNT-ORDERED, so the five kept are the five loudest
    clusters rather than an arbitrary five. 3 is the priced fallback (12 runs) if beta's real
    dossier yield comes in above the nightly-calibrated 0.55-0.77 this arithmetic assumes.
    Nightly's 50 is untouched — it does not bind, and lowering it would change a channel this
    measurement says nothing about."""
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
    # Minimum distinct INSTALLATIONS a build-day needs before it may act as a spike BASELINE.
    # Read only off nightly (see `datacollector.get_no_user_build_floor`, which returns 0 for
    # nightly): it exists for the merge-day `N.0b1` build, which ships to 4-7 installations
    # FOREVER and would otherwise be a zero baseline for the build after it. Any value in
    # [8, 24] is equivalent on the measured data (5 merge-day builds at 1-7 installs, the
    # quietest real build showing ~29 at the one window index that ever selects). NOT reports:
    # that number is age-dependent and the floor then fires on a real build seen early.
    "min_build_installs": 15,
    # THE RATE PATH'S DAILY BUDGET, per channel: how many EXISTING signatures whose
    # exposure-normalised rate is rising (`sigtrend.rising_candidates`) the selector may add per
    # day on top of the spike test, best statistic first. 0 = off (the default: a channel with no
    # daily rollup has nothing to rank). Replayed over the 30 days to 2026-09-03 on the real
    # rollup with the deployed constants (ratio >= 3, >= 5 installs, 7 vs 56 days, one pick per
    # signature per week): ~6.7 new rising episodes a day on nightly, of which ~1.9 the spike
    # test had NOT selected within a week; ~6.6 on beta, of which ~6.5. Uncapped that is most of
    # beta's triage spend again, so this is a BUDGET ranked by the statistic -- the use the
    # trend study validated (top-5/day reaches 15-19 of 57 human cases at 7% of the spend) --
    # not a threshold fitted to the case that motivated it.
    "rising_per_day": 0,
    # Proto-signature clusters one rate-path pick may spawn. Nightly's spike cap is 50 because it
    # never binds there (mean 1.07 protos per pair); a rising EXISTING signature is a different
    # population -- hangs and shutdown timeouts whose stacks are all distinct -- and 3 is the
    # priced fallback from the beta measurement in `get_threshold`.
    "rising_protos": 3,
}


def get_spike(typ, product, channel):
    """Spike-detection knob ``typ`` (``"floor"`` | ``"ratio"``) for a product/channel.
    ``floor`` = minimum crashes on the spike day; ``ratio`` = minimum multiple over the
    loudest of the preceding days. See ``utils.is_spike``.

    A ``floor`` AT OR BELOW THE CHANNEL'S INSTALL THRESHOLD CANNOT BIND, and release ships in
    exactly that state (``floor`` 50 == ``thresholds.installs`` 50). ``utils.evaluate_days``
    computes ``spiked`` and does ``if not spiked: continue`` BEFORE the install test, so the
    floor looks like the first gate — but a day's ``count`` is the SUM over that day's buildids
    of each buildid's report count, and for a given buildid reports >= distinct installations by
    construction (``datacollector`` coerces a 0 cardinality to 1). So a day that fails
    ``floor <= installs`` could never have passed ``installs >= threshold`` either. Verified
    empirically on release: floor 1, 3, 10, 20 and 50 all give exactly 329 selected pairs over
    133 replayed run-days, and the first value that changes anything is 100.
    Nobody has measured release's floor and nobody needs to: it is not a lever. If a future
    change makes the floor bind, it will be because the install threshold moved, not the floor.
    ``tests/test_selection_log`` pins the beta side of the same relation (there the floor, 10,
    genuinely sits ABOVE the threshold, 6, and does bind)."""
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


def get_ingest_channels():
    """Channels to INGEST (free), from ``INGEST_CHANNELS`` (space-separated).

    THE SINGLE READER. It used to be parsed inline in ``update.update_all`` with an ``or
    config.get_channels()`` fallback, and that fallback fired: an unset variable meant every
    configured channel, so one tick ingested release and left 7,267 ``nodes`` rows, 20,320
    ``changesets`` rows and 2,628 wasted hg fetches behind it. ``config.get_channels()`` is the
    list that defines the ``CHANNEL_TYPE`` enum — every channel ever contemplated — and is
    therefore the wrong list to default an ACTION to.

    Absent or empty returns ``[]``, and every caller must read that as "nothing", not "all".
    Distinct from ``get_agent_channels``, which decides what gets ANALYSED (~$1-3 a crash)."""
    return os.getenv("INGEST_CHANNELS", "").split()


def get_agent_channels():
    """Channels the evidence agent runs on. Empty list means "no channel filter" (all).

    ``AGENT_CHANNELS`` (space-separated, same shape as ``INGEST_CHANNELS``) overrides the config.
    IT IS A REAL KILL SWITCH, not dead config, and it is the one this file was missing: this is
    the flag that decides whether the pipeline SPENDS MONEY on a channel, and it was the only one
    of the canary levers with no environment override — so turning a channel's triage off needed
    a DEPLOY, and a deploy kills every in-flight ~20-minute run at ~$3 each. It also has to be
    per channel: ``AUTOFILE_BUGS=0`` is global, so without this the only way to stop beta was to
    stop nightly too.

    Distinct from ``INGEST_CHANNELS``, which decides what gets INGESTED (free); this decides what
    gets ANALYSED (~$1-3 a crash). Ingest-only is the cheap canary, and it is the combination
    ``INGEST_CHANNELS="nightly beta"`` + ``AGENT_CHANNELS=nightly`` expresses.

    AN EMPTY VALUE NOW MEANS "NO CHANNEL", NOT "EVERY CHANNEL". It used to mean the latter: the
    readers are ``if channel is not None and channels and channel not in channels``
    (``orchestrator.enqueue_agent``) and ``if channels:`` (``models.UUID.untriaged``), both of
    which treat ``[]`` as *no filter*. So ``AGENT_CHANNELS=""`` — set, but empty — armed triage on
    every channel at $1-3 a crash, on the one variable an operator reaches for to turn spending
    OFF. Nothing in the repo has ever set ``agent.channels: []``, so the "no filter" capability had
    no user, while the state it enabled was the expensive one.

    Inverted here rather than at the two readers, deliberately: their ``channels and`` / ``if
    channels`` shape is also what keeps every no-channel legacy caller working, and changing it
    would have needed both sites to move in lockstep or the SWEEP would have stayed wide open.
    One reader of the variable, one place to get it wrong.

    Note the asymmetry with ``INGEST_CHANNELS``, which is now also closed: that one had TWO
    dangerous states (unset AND empty, both meaning "every configured channel", and it fired —
    see ``update.update_all``), this one had one and it takes a deliberate ``=""`` to reach."""
    env = os.getenv("AGENT_CHANNELS")
    if env is not None:
        channels = env.split()
        if not channels:
            logger.warning(
                "AGENT_CHANNELS is set but empty, so NO channel will be triaged. It no "
                "longer means 'every channel' -- unset the variable to fall back to "
                "agent.channels in the config."
            )
        return channels
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


_SWEEP_DEFAULTS = {
    # A GENUINE kill-switch, not dead config: this is the one periodic job that spends money with
    # nobody watching, so there has to be a way to stop it without a deploy.
    "enabled": True,
    # 3 per tick. At the clock's 6-hourly interval that is 12/day — comfortably above the ~3.4/day
    # these arrive at, so the steady-state cost is ~$10/day and the measured 86-crash backlog
    # drains in about a week rather than in one bill.
    "max_per_run": 3,
    # A merely-QUEUED crash has no dossier row either (claim_running is what inserts one), and the
    # queue runs hours deep: three workers at ~16 minutes drain ~11/hour, so the tail of one
    # ingestion batch waits well over an hour by design. Six hours is comfortably past any real
    # queue delay; being conservative here only costs latency.
    "min_age_s": 21600,
    # A crash on a build from a month ago is not worth ~$3 — the build is long gone and the same
    # money buys a triage of today's.
    "max_age_s": 1209600,
    # Per CHANNEL, so one channel's backlog cannot eat another's tick. `max_per_run` is the
    # per-tick total; this is the most any single channel may take of it. With one channel it is
    # inert (3 of 3). With two, `tests/test_sweep_untriaged.py`'s own comment describes the
    # hazard — "three beta candidates would otherwise fill a tick ... and starve the nightly
    # ones" — and today the only thing preventing it is the channel FILTER, which is exactly what
    # a beta rollout removes. 2 of 3 leaves at least one slot for anything else that is waiting.
    "max_per_channel": 2,
}


def get_agent_sweep():
    """Untriaged-crash sweep knobs (``agent.orchestrator.sweep_untriaged_crashes``). Returned as
    one normalized dict so callers never re-derive a default. ``AGENT_SWEEP`` is the env kill
    switch, like ``OFFSTACK_ENABLED``."""
    cfg = dict(_SWEEP_DEFAULTS)
    cfg.update(get_agent().get("sweep", {}) or {})
    cfg["enabled"] = _env_bool("AGENT_SWEEP", cfg["enabled"])
    return cfg


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


# The three values ``agent.autofile.comment_on_existing`` may take, and the two legacy booleans
# they replace. NOT a boolean any more because the requirement "file only crashes that have no bug
# in Bugzilla" and the requirement "never write on somebody else's bug" are different rules, and
# the old ``False`` implemented the SECOND one: ``autofile_bug`` returned
# ``{"filed": False, "skipped": "open bug N exists"}`` -- no comment AND no new bug -- before it
# had even asked whether that bug could be about this regression.
#
#   comment    an open same-application bug that can be about this regression is the venue.
#   skip       an open bug on the signature means we write NOTHING. `False` maps here, and two
#              tests pin that meaning by name.
#   file_new   never comment; file a new bug, naming the open bugs we chose not to comment on.
#
# `skip` is STRICTER than "file only new bugs", measurably so -- but two of the three figures this
# comment used to give were wrong, in ways worth naming because the conclusion survives both.
#
# CORRECTED 2026-08-31. Measured with THIS function's own chain (`_open_bugs_for_signature` ->
# `_split_by_application` -> `_split_out_metas`, which is all `bugzilla_apply`'s `skip` branch
# ever sees) on the signatures BETA'S OWN SELECTOR picks: 39/77 = 50.6% (Wilson 39.7-61.5)
# carry an open same-application non-meta bug, 36/67 = 53.7% per selection, against 26/120 =
# 21.7% (15.2-29.9) on a matched nightly selector sample -- z = 4.22, p = 2.4e-5. So beta really
# is ~2.3x nightly on the population its own selector produces, `skip` suppresses about HALF of
# beta's candidate filings, and `file_new` restores them at ~2.0-2.2x (not the ~2.4x quoted
# elsewhere).
#
# The two errors: (1) the "58/98 = 59.2%" that used to lead is a TOP-100-BY-INSTALLS PANEL, and
# that instrument does not discriminate channels at all -- on it nightly is 63%, release 64% and
# beta 59% (z = -0.58, p = 0.56 nightly vs beta). Never quote a volume panel here; it measures
# signature volume, not channel. (2) the 58% and 64% figures were raw
# `_open_bugs_for_signature` counts that never went through `_split_out_metas`, even though the
# sentence says "non-meta". And it credited the wrong split: `_split_out_metas` is what moves
# beta's number (6 of 45, whose only open bugs are trackers 1472062 and 1588498), while
# `_split_by_application` moves 0.
#
# A VENUE RATE DOES NOT ACTUALLY DECIDE THE MODE, and this is the number to get next: of the
# venues the FILER sees, how many are the RIGHT venue. On production's 83 real nightly filings
# it is 15/22 = 68% (Wilson 47-84); on beta's selection population only 6/39 = 15.4% would be
# accepted by `_bug_for_this_regression` (median venue-bug age 995 days). A 4.4x swing across
# populations, and on the filer-visible one it argues FOR `skip`, since `file_new` would file a
# near-duplicate of a bug genuinely about this crash about two thirds of the time.
COMMENT_ON_EXISTING = ("comment", "skip", "file_new")


def comment_mode(value):
    """Coerce ``comment_on_existing`` to one of :data:`COMMENT_ON_EXISTING`.

    PUBLIC, and called AGAIN at the read site (``bugzilla_apply.autofile_bug``) even though this
    function already normalises what ``get_agent_autofile`` returns. Not belt-and-braces: every
    test in the suite that exercises the filer mocks ``get_agent_autofile`` with a plain dict
    (``return_value=``), so the value the filer actually sees is whatever the test wrote --
    ``True`` -- and a raw ``True`` compared against ``"comment"`` is False. Coercing at both ends
    means a legacy boolean from a mock, an old config or a stored payload behaves the way it
    always did instead of silently selecting the strictest mode.

    Legacy booleans are accepted forever: ``True`` -> ``comment`` (the shipped default),
    ``False`` -> ``skip`` (what it has always DONE). An unrecognised string falls back to
    ``comment``, i.e. today's behaviour, rather than silently turning writing off -- a typo must
    not be able to stop the filer, which is the ``_ENUM_ADDITIONS`` failure mode."""
    if isinstance(value, bool):
        return "comment" if value else "skip"
    text = str(value or "").strip().lower()
    return text if text in COMMENT_ON_EXISTING else "comment"


def autofile_channel_declared(channel):
    """Is *channel* a channel somebody has DECIDED about filing on?

    True for a channel with an ``agent.autofile.channels.<ch>`` entry, and for the one channel
    the top-level block itself describes (``agent.autofile.default_channel``, nightly). Anything
    else — a channel that appears in ``INGEST_CHANNELS`` and nowhere in the filing config, or an
    unknown one — is undeclared, and ``bugzilla_apply.autofile_bug`` refuses to file on it.

    A SEPARATE PREDICATE FROM ``enabled``, because "somebody set this to false" and "nobody has
    thought about this channel" must not look the same from the filer. An overlay of
    ``{"enabled": false}`` is a decision; a missing overlay is a gap.

    RELEASE IS NOW DECLARED AND HELD (``enabled: false``, ``skip``, ``daily_cap: 2``), which
    closes the gap this docstring used to describe. The only DANGEROUS overlay shape is one with
    no explicit ``enabled`` key — a bare ``{}``, or beta's shape minus its ``false`` — because
    ``get_agent_autofile``'s veto is ``over.get("enabled") is False`` and prod runs
    ``AUTOFILE_BUGS=1``, so an overlay that merely names a channel ARMS it at the top-level
    policy. Verified across five overlay shapes.

    ``skip`` for release is the CONSERVATIVE default and it is not the measured answer, because
    there is no measured answer. Release's venue rate at the SELECTION level (the population the
    filer sees, not a top-N-by-volume panel) is 14/30 = 46.7%, Wilson 30.2-63.9% — an interval
    that overlaps both nightly's 21.7% and beta's 50.6%. And a venue RATE does not decide the
    mode: what decides it is how many of those venues are the RIGHT venue, which is 68% (15/22)
    on production's real nightly filings but 15.4% (6/39) on beta's selection population, a 4.4x
    swing across populations and unmeasured on release. ``skip`` writes on nobody's bug and
    files no near-duplicate; ``file_new`` is roughly 2.0-2.2x the volume. Open question, and it
    cannot be answered while filing is held."""
    a = get_agent().get("autofile", {})
    ch = (channel or "").lower()
    if not ch:
        return False
    if ch == (a.get("default_channel") or "nightly").lower():
        return True
    return ch in {k.lower() for k in (a.get("channels") or {})}


def autofile_channel_held(channel):
    """Is *channel*'s filing held by an EXPLICIT per-channel ``enabled: false``?

    Distinct from ``not get_agent_autofile(channel)["enabled"]``, which is also true when the
    global ``AUTOFILE_BUGS`` switch is off or the top-level default is false. The three states
    have to stay apart because only this one is a per-channel DECISION, and only this one is
    worth a log line: with the global switch off, every run on every channel would say "not
    filed", which is noise, so ``orchestrator._maybe_autofile`` suppresses that string — and a
    channel held on purpose would have been suppressed with it.

    THIS IS PLAN #18's PHASE 4 BEING MEASURABLE AT ALL. The point of triaging beta with filing
    held is to find out how much it WOULD file; a hold that leaves no trace answers nothing, and
    is the silent-no-op shape this codebase keeps being bitten by. Note the log line is not the
    durable record — Heroku keeps ~2h and there is no drain — so the number to count over a week
    is beta dossiers whose verdict reached the filing rung. That is an UPPER bound: the gates
    after this one (an open bug on the signature, which is ~51% of beta's selected signatures; the daily
    cap; product/component resolution) never run, so they cannot subtract."""
    over = (get_agent().get("autofile", {}).get("channels") or {}).get((channel or "").lower())
    return bool(over) and over.get("enabled") is False


def get_agent_autofile(channel=None):
    """Automatic bug FILING knobs (the only unattended write to Bugzilla).

    ``enabled`` is a genuine kill-switch, not dead config: this posts to production BMO
    without a human in the loop, so it has to be stoppable from ``heroku config:set``
    without waiting on a deploy. It defaults OFF and is armed with ``AUTOFILE_BUGS=1``.

    ``min_confidence`` 70 is the ``probable`` rung — a lead the model rated strongly or that
    a deterministic check corroborated — measured at ~3 crashes/day, versus ~7.6/day if it
    were lowered to the ``medium`` rung of 50. ``daily_cap`` bounds the damage a bad gate
    can do in one night; the pipeline itself has no such bound."""
    a = get_agent().get("autofile", {})
    # THE PER-CHANNEL OVERLAY, merged BEFORE the per-key reads below so every one of the twelve
    # gates in ``autofile_bug`` is covered by one argument. ``channel=None`` returns today's dict
    # byte-identically, which is what keeps every existing caller and mock honest.
    #
    # Nothing in the filing half knew about channels: ``autofile_bug``'s documented gates contain
    # none, and the ONLY thing that kept filing nightly-only was ``get_agent_channels()`` inside
    # ``enqueue_agent`` -- which ``enqueue_agent(..., force=True)`` bypasses by design, and that
    # is exactly what a tasks.html retrigger calls. With ``AUTOFILE_BUGS=1`` live, the day
    # ``INGEST_CHANNELS`` gained ``beta`` one retrigger click would have filed a beta bug under
    # the nightly rules.
    over = (a.get("channels") or {}).get((channel or "").lower()) or {}
    a = {**a, **{k: v for k, v in over.items() if k != "channels"}}
    # THE STRICTEST OF THE TWO WINS, IN BOTH DIRECTIONS, and that needs saying because they are
    # different kinds of statement. `AUTOFILE_BUGS=0` is a KILL SWITCH and must beat any JSON --
    # a switch a config file can defeat is not one. But an explicit `channels.<ch>.enabled:
    # false` is a DECISION about one channel, and a global arm must not undo it either, or
    # "triage this channel but do not file from it yet" cannot be expressed at all: the env var
    # is global, so `AUTOFILE_BUGS=1` would silently arm every declared channel. Only an
    # EXPLICIT per-channel `false` is honoured this way -- an absent key still inherits the
    # top-level default and the global arm, which is how beta is configured.
    channel_veto = over.get("enabled") is False
    return {
        "enabled": _env_bool("AUTOFILE_BUGS", a.get("enabled", False)) and not channel_veto,
        "min_confidence": a.get("min_confidence", 70),
        "verdicts": a.get("verdicts", ["lead", "culprit"]),
        "needinfo": _env_bool("AUTOFILE_NEEDINFO", a.get("needinfo", True)),
        "daily_cap": a.get("daily_cap", 10),
        # An open bug already referencing the signature: comment there instead of filing a
        # duplicate. Turning this off does NOT file anyway — it skips.
        "comment_on_existing": comment_mode(a.get("comment_on_existing", True)),
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
        # boost would jump TWO rungs (low -> probable, p_worth 0.50 -> 0.72).
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
    independently-confirmed leads still trips it — a hard rule would kill real leads.

    ``other_channel_floor`` is how many reports a signature needs OUTSIDE the crash's own channel
    before that history counts as evidence of the crash's age. First-seen was scoped to nightly,
    which reads backwards for a defect that is longstanding on release and merely new to nightly;
    admitting the other channels unconditionally is worse still, splitting evenly between filings
    a human refuted and filings a human fixed. See ``sigage.signature_history`` for the
    measurement that put the boundary here, and for why the rule is purely additive."""
    a = get_agent().get("signature_age", {})
    return {
        "enabled": _env_bool("SIGNATURE_AGE_ENABLED", a.get("enabled", True)),
        "min_age_days": a.get("min_age_days", 7),
        "other_channel_floor": a.get("other_channel_floor", 20),
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
    can silence a verdict outright, so it has to be stoppable without a deploy.

    THE SIGNATURE-LEVEL HALF (bug 2064600) asks a different question with different knobs. The
    two above read the ONE report being triaged; these read the signature, because Timothy Nikkel
    pointed out that a signature can be mostly hardware while the particular report in hand looks
    fine -- as his did. See ``sigage.hardware_noise`` for both measurements.

    ``max_bit_flip_rate`` 0.2 and ``max_broken_cpu_rate`` 0.7 ARE NOT TUNED HERE, and that is the
    point of choosing them: they are the two thresholds mozilla/bugbot already applies in
    ``bugbot/crash/analyzer.py`` to decide it will not file on a signature. Clouseau filed bug
    2064600 on a signature 50% of whose nightly reports carry a bit-flip annotation; bugbot, over
    its own line at 0.2, would not have. Matching the numbers ends a disagreement between two filers
    rather than inventing a third opinion, and the asymmetry between them is real -- a bit-flip
    annotation is already a positive finding by the stackwalker, whereas merely owning a Raptor
    Lake is common enough (4.1% of nightly reports) that it takes a landslide to mean anything.

    ``max_broken_cpu_rate`` IS FITTED ON ONE CASE, and the panel cannot do better than that. On
    the canary's 52 filings the highest control is bug 2062219 (FIXED) at 0.302 and the only
    INVALID this arm catches is bug 2063364 at 0.789, with nothing in between, so every value in
    (0.30, 0.79] scores identically and 0.7 is defensible by provenance rather than by fit;
    lowering it to 0.5 catches no extra bad filing and newly suppresses three background
    signatures that all carry FIXED bugs. A THIRD ARM on how concentrated the signature's
    ``cpu_info`` facet is -- the obvious repair for bug 2065373, whose 58 reports are all one CPU
    model at ``broken_cpu_rate`` 0.0 -- was measured on that panel on 2026-08-21 and KILLED:
    every threshold from 0.40 to 0.95 eats at least one FIXED/DUPLICATE/ASSIGNED filing (0.50
    eats five), AUC is 0.333 at this sample floor, and the shape is present in 13-35% of the
    triaged population. Do not rebuild it, and do not borrow
    ``_POPULATION_DEFAULTS["concentrated_share"]`` 0.5 for it -- that one was fit on
    ``install_time``. The spread is REPORTED instead; see
    ``orchestrator._signature_is_mostly_hardware`` and ``sigage.hardware_noise``.

    ``min_signature_reports`` 5 is the sample floor, and it is MEASURED rather than picked.
    Re-measured 2026-08-21 on the 52 bugs the canary has filed, floors of 3 and 5 give identical
    answers -- 6 filings suppressed, 0 of the 19 FIXED/DUPLICATE/ASSIGNED ones among them, the
    INVALID bugs 2062173 and 2063364 caught -- while a floor of 6 or more now loses bug 2064600
    itself, whose signature held 6 nightly reports in a year when this was written and 5 two days
    later. The margin here is ONE report, not three: a floor is cheap to raise and this one is
    already at the edge of the case it was written for.
    It is far below bugbot's own low-volume line of 20 because ``sigage.hardware_noise`` counts
    only the crash's OWN product and channel, where volumes are an order of magnitude smaller;
    see that function for why the wider denominator is not usable (it kills bug 2062219, FIXED).
    A floor there must be, though: below it any percentage is noise, 1 of 3 being "33%", and a
    brand-new nightly regression is exactly the small-sample case this pipeline exists for."""
    a = get_agent().get("bit_flip", {})
    return {
        "enabled": _env_bool("BIT_FLIP_GATE_ENABLED", a.get("enabled", True)),
        "min_confidence": a.get("min_confidence", 50),
        "max_reports": a.get("max_reports", 1),
        "min_signature_reports": a.get("min_signature_reports", 5),
        "max_bit_flip_rate": a.get("max_bit_flip_rate", 0.2),
        "max_broken_cpu_rate": a.get("max_broken_cpu_rate", 0.7),
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


def get_agent_calibration(channel=None):
    """The fitted worth-investigating calibration table (Phase-2): ``{rung score (int) ->
    P(worth-investigating)}`` mapping a verdict's confidence rung (``CONFIDENCE_SCORE`` * 100)
    to its empirical calibrated probability. Sourced from ``agent.calibration.table`` (an inline
    map) or ``agent.calibration.path`` (a ``calibration_table.json`` written by
    ``eval.calibrate`` — its ``calibration_table`` sub-key is used). Empty ``{}`` until a paid
    calibration run has been fit + wired, so ``Verdict.p_worth_investigating`` stays ``None`` in
    prod until then. A path is re-read only when its mtime changes.

    SHIPPED 2026-08-21: ``{25: 0.5, 50: 0.5714, 70: 0.7234, 85: 0.7234}``, the fit over ALL 90
    rows of ``corpus_ship`` (``eval.calibrate --corpus-dir corpus_ship --holdout-folds 0``).
    n per rung, reported rows only: 25 -> 1/2, 50 -> 4/7, 70 -> 21/27 = 0.778, 85 -> 13/20 =
    0.650, isotonic-pooled at the top into 34/47 = 0.7234 (Wilson95 0.5824-0.8306). Read rung 50
    with care: 0.8 -> 0.5714 is a 23-point move off a SEVEN-row bin whose only change is its two
    culprit-absent rows (4/5 -> 4/7). It is below ``autofile.min_confidence`` so nothing is filed
    on it and it reaches only the crashstack badge, but it is the one value here that a handful
    of new rows could move again.

    IT REPLACES ``{25: 0.5, 50: 0.8, 70: 0.9714, 85: 0.9714}``, WHICH WAS A POSITIVES-ONLY FIT.
    ``corpus_ship/calibration_table_positives.json`` is byte-identical to that table and records
    ``n_negative`` 0 and ``n_test`` 0: no negative arm and no held-out split at all. The 26
    culprit-absent rows it drops are not noise — 14 of them were REPORTED, 12 at rung 70+, and
    every one of the 12 was a miss. 0.9714 = 34/35 is therefore the precision left after deleting
    only losses, and its own Wilson95 lower bound (0.8547) sits ABOVE the upper bound of the
    corpus fit (0.8306) and above every production reading below except the loosest one, which
    counts self-duplicates (22/30, upper 0.8582). A number a Bugzilla reviewer reads cannot be
    fit that way.

    HELD OUT, NOT MERELY FIT. With ``--holdout-folds 3`` (the stable uuid-hash split, 62 cal /
    28 test) the calibration split fits rung 70+ at 26/34 = 0.7647 and the 28 rows it never saw
    read 8/13 = 0.615 (Wilson95 0.3552-0.8229) — an interval that covers the shipped 0.7234. The
    all-rows fit is what ships because it uses every labelled row; the split is what says the fit
    is not an artefact of the rows it was fit on. The retired table has no such evidence:
    refit that arm with ``--positives-only --holdout-folds 3`` and every rung is a zero-failure
    bin (1.000 across the board, floored to 0.8865 by ``eval.calibrate._deceil``) — deleting the
    losses is what the arm IS, so a split has nothing left to disagree with.

    AND IT AGREES WITH PRODUCTION, which is the real argument for the number. Over the 52 bugs
    filed since 2026-08-05 (BMO ``creator=cdenizet@mozilla.com``, ``short_desc`` "Crash in [@"),
    the adjudicated ones are 10 FIXED + 2 ASSIGNED + 3 still open with a ``regressed_by`` the
    filer did not set (2061691, 2061969, 2061975) + 2 duplicates of a pre-existing bug, against
    8 closed INVALID/WORKSFORME: 17/25 = 0.680 (Wilson95 0.4841-0.8280), or 22/30 = 0.733
    (0.5555-0.8582) counting the 5 self-duplicates of our own filings as positive.

    That numerator is deliberately the LOOSEST honest one, and this patch itself argues against
    two of its rows: 2061691 and 2061969 are the two ``Feedback.classify`` now scores
    ``unconfirmed`` (dmeehan set the field answering a BugBot nag; nobody but us ever wrote on
    either bug). Drop them and production reads 15/23 = 0.652 (0.4489-0.8119), or 20/28 = 0.714
    (0.5294-0.8475). So the real production statement is a RANGE, 0.65-0.73 across every way of
    counting an endorsement, and every one of those intervals contains the shipped 0.7234 while
    none of them contains 0.9714. That, not an exact coincidence of two decimals, is why this
    number is publishable and the old one was not.

    WHAT THE SHIPPED FIT ITSELF RESTS ON, said out loud because the null result below refuses
    to read the same rows as informative: 12 of the 47 reported rung-70+ rows are the corpus's
    culprit-DELETED negatives, whose ``worth`` is False BY CONSTRUCTION. Pooling them in is
    defensible — a lead named in a window with no culprit IS a false investigate, which is the
    quantity the badge claims — but it is not free. On the 3 of those 12 whose positive twin is
    still in corpus_ship, TWO cited the exact changeset the negative build had deleted
    (5a444e22.../``c11e7594c36d``, d069e9f8.../``13d65a94e4cf``): the agent found the real
    regressor off-window and was scored 0 for it. Relabelling only those two gives 36/47 = 0.766.
    0.7234 is therefore a LOWER bound on the full arm, and the production readings above are what
    keep it honest — the corpus alone cannot settle its own label.
    ``spike/window_arm_null.py`` prints this check.

    A TWO-ARM TABLE WAS TRIED AND MEASURED DEAD (2026-08-21) — recorded so the next session does
    not rebuild it. The idea was to key the table on ``corroborations.candidate_in_pushlog_window``
    (the observable fact ``report_bug.is_suspected_regression`` already reads) and publish a high
    number in-window, a low one outside. Backfilling that flag onto corpus_ship from each case's
    frozen candidate set (``spike/window_arm_null.py``) gives reported rung 70+ = 26/26 in-window
    against 8/21 = 0.381 out of it, which looks decisive and is not:

    * 12 of those 21 out-of-window rows are the corpus's culprit-DELETED negatives, and a
      negative row has no ``regressor_nodes``, so ``worth`` is False whatever the agent said
      (0 of 26 negatives score ``worth`` at ANY rung). 0.381 is 57% unconditional zeros.
    * On the rows whose label can carry information, out-of-window reads 8/9 = 0.889 against
      in-window's 26/26. Fisher exact p = 0.257 — THE PREDICATE DOES NOT SEPARATE.
    * All 12 reported rung-70+ negatives are out-of-window (12/12), so the "observable" predicate
      is a strict SUPERSET of the unobservable ``is_negative`` label it claimed to replace, not
      an independent axis — which makes the in-window arm the positives-only fit under a new
      name, i.e. exactly the defect being removed.
    * And the corpus structurally CANNOT contain a bug-2062806 shape: an out-of-window candidate
      that was the true cause. Publishing 0.381 for that population puts the cleanest confirmed
      fix of all 52 filings in the lowest bin.

    Settling it needs the 16 out-of-window filings labelled one by one, not a refit. The flag is
    still recorded on every dossier and ``eval.calibrate --arm`` still fits either side of it.

    WHAT THIS MUST NOT BE READ AS: a reason to file less. Nothing gates on this number —
    ``autofile.min_confidence`` and every other threshold key on the 0-100 RUNG; ``p_worth``
    reaches only the bug comment (``report_bug._worth_phrase``) and the crashstack badge. The two
    highest-value outputs of the 52 filings are both cases a lower number would have discouraged.
    Bug 2062806: the filed comment itself said the changeset "did not land in this build's
    pushlog window ... named only as the closest thing found on the crash path", and hzhao
    confirmed the mechanism, backed that exact changeset out, RESOLVED FIXED. Bug 2062119: filed
    at rung 85 with a flatly wrong attribution (jstutte, "I do not think bug 1768581 is the
    regressor"), and it produced two patches uplifted to beta AND esr153 while scoring
    hit=0/worth=0 in the corpus vocabulary. 0.72 is what those look like from outside, and 0.72
    is worth someone's time.

    THE TOP TWO RUNGS SHARE ONE VALUE, unchanged and still on purpose: rung 85 measures WORSE
    than rung 70 on every cut (full arm 13/20 = 0.650 vs 21/27 = 0.778; positives-only 13/14 vs
    21/21), so isotonic pools them. A result, not an unfinished fit — do not "separate" them, and
    expect no badge movement from any gate that drops a verdict from 85 to 70. The fit LABEL is
    also not changeset-exactness: ``eval.metrics`` scores ``worth`` = hit OR person_hit, which is
    what the badge claims. The defect was the ARM, not the label and not the pooling. Full
    numbers in ``eval.calibrate``'s module docstring."""
    cal = get_agent().get("calibration", {})
    # PER CHANNEL, and beta deliberately gets NOTHING. The shipped table is the fit over all 90
    # rows of `corpus_ship`, every one of them Firefox NIGHTLY -- and this number is not
    # internal: `report_bug._worth_phrase` puts "N% worth investigating" in the FILED BUG and the
    # crashstack badge shows it. There is no beta arm of the corpus, so there is nothing to fit
    # and nothing honest to publish; an empty table leaves `p_worth_investigating` at `None` and
    # the comment simply omits the sentence, exactly as it did before any calibration existed.
    # This module's own rule: a number a Bugzilla reviewer reads cannot be fit on the wrong arm.
    #
    # `channels: {"<ch>": {...}}` overrides per channel, INCLUDING with an explicit empty table.
    #
    # AN ABSENT CHANNEL KEY USED TO INHERIT THE FIT, AND THAT WAS THE BUG. Falling back to the
    # top-level table for ANY channel with no entry meant `get_agent_calibration("release")` --
    # or "esr", or any channel added later -- returned nightly's fitted `{25: 0.5, 50: 0.5714,
    # 70: 0.7234, 85: 0.7234}` and published "72% worth investigating" from a fit on 90
    # Firefox-nightly rows. That is exactly the rule three paragraphs up forbids, and it is a
    # CLASS of defect rather than one instance: the next channel inherits it again.
    #
    # So the fallback is now EXPLICIT, following `sigage._population`'s shape verbatim: the
    # top-level fit is returned only for a falsy channel (every no-argument caller is a nightly
    # path) or for the channel the fit was actually measured on, named in
    # `agent.calibration.fit_channel`. A NAMED channel with no entry gets `{}` and publishes no
    # sentence at all. Nightly still keeps its table without naming itself in `channels`, which
    # is the property the asymmetry existed to protect.
    channels = cal.get("channels") or {}
    ch = (channel or "").lower()
    over = channels.get(ch)
    if over is not None:
        cal = over
    elif ch and ch != (cal.get("fit_channel") or "nightly").lower():
        return {}
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
