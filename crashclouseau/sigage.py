# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""How OLD was a crash signature when the build we are triaging was produced?

The triage pipeline's premise is "the regressor is somewhere in this build's pushlog window".
That premise only holds if the signature is actually NEW as of that build. When the signature
has existed for months, nothing in the window introduced it, and naming a window changeset
produces a confident-looking lead that cannot be right.

This is not hypothetical. Over the canary's first three prod days, 10 of 10 HIGH-confidence
blind-second-opinion refutations rested on exactly this argument, and all 10 verified
deterministically with a median gap of 178 days between the signature first appearing and the
named candidate landing (`spike/verify_so_timing_claims.py`). 24 of 32 reported leads came from
signatures already more than a week old.

One Socorro lookup answers it. Four gotchas, all learned the hard way and all encoded below:

  * The `build_id` FACET is ordered by COUNT, so truncating it silently drops the oldest build —
    the one value we need. Sort ASCENDING by `build_id` and take one row instead.
  * `_facets_size=10000` is rejected outright (HTTP 400).
  * A date range of exactly 365 days is rejected too ("Date range is bigger than 365 days"),
    because the implicit upper bound is *now*. Clamp to 364.
  * `date` filters the crash REPORT date, not the build date, so even a short window surfaces
    very old buildids — which is exactly what makes this cheap.

Window truncation can only make first-seen look NEWER than it really is, so an age computed here
is a LOWER bound and the resulting downweight is conservative.
"""
import re
from datetime import datetime, timedelta, timezone

from libmozdata import socorro
from libmozdata.connection import Query

from crashclouseau import config, utils
from crashclouseau.logger import logger

# Socorro hard-rejects more than 365 days; the implicit "to now" upper bound pushes an exact
# 365 over the line. Public because the second-opinion agent's crash-stats tool states the
# window in its agent-facing text, and the figure must not drift between the two files.
MAX_WINDOW_DAYS = 364


def _buildid_to_dt(buildid):
    """``YYYYMMDDHHMMSS`` (UTC) -> datetime, or None when it is not a buildid."""
    try:
        return datetime.strptime(str(buildid)[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _channel_total(result, channel):
    """How many reports *channel* has, off the response's ``release_channel`` facet.

    The query is no longer channel-filtered (see ``signature_history``), so the count the
    bit-flip gate needs has to be recovered from the facet rather than read off ``total``.

    ``beta`` is summed over BOTH terms ``utils.get_search_channel`` maps it to: Socorro still
    stores part of beta under ``aurora``, and dropping it loses about a third of the channel."""
    if not channel:
        total = result.get("total")
        return int(total) if isinstance(total, int) else None
    wanted = utils.get_search_channel(channel)
    wanted = {wanted} if isinstance(wanted, str) else set(wanted)
    rows = (result.get("facets") or {}).get("release_channel") or []
    if not rows:
        # An empty RESULT SET is a real zero; a missing facet on a non-empty one is not.
        return 0 if result.get("total") == 0 else None
    return sum(int(r.get("count") or 0) for r in rows if r.get("term") in wanted)


def _oldest_build(result):
    """The oldest buildid in an ascending-sorted, one-row result. ``None`` when there is none."""
    for hit in (result or {}).get("hits") or []:
        if hit.get("build_id"):
            return str(hit["build_id"])
    return None


def signature_history(signature, product="Firefox", channel="nightly", days=MAX_WINDOW_DAYS,
                      other_channel_floor=None):
    """How old a signature is and how much it crashes:
    ``{"first_seen", "first_seen_channel", "total", "total_other_channels"}``.

    ``first_seen`` is the ANSWER — the buildid the stale-signature gate and the agent should
    both reason from. The other three are the parts it is built out of, returned so the choice
    is inspectable rather than hidden.

    WHY THE ANSWER IS NOT SIMPLY THE CHANNEL'S OWN FIRST-SEEN. "Could this candidate be the
    crash's ORIGIN?" is not a per-channel question. Scoped to nightly it read exactly backwards
    for ``nsStyleContent::NonAltContentItems``: esr back to build 20251009121631 and release to
    20251106194447, 32 reports across the two — but its first NIGHTLY report is build
    20260811085340, so the gate saw a nine-day-old signature, stayed silent, and bug 2062934 was
    filed at 97% naming a four-week-old changeset into a bug where a human had already written
    "there's no recent regressor/regression-range to cast blame on here".

    WHY IT IS NOT SIMPLY THE ALL-CHANNEL FIRST-SEEN EITHER. Signature reuse is real, and an old
    report on another channel can be a different defect wearing the same name. Replayed over the
    35 filings the canary had made by 2026-08-20, the unfloored all-channel rule fired on 12 that
    the nightly one spared — and those 12 split roughly evenly into filings a human refuted and
    filings a human FIXED. It is not usable as-is.

    SO: the other channels' history is admitted only once there is enough of it to be evidence —
    ``other_channel_floor`` reports outside ``channel``, else the channel's own first-seen. On
    those same 12 the separation is clean: every filing a human refuted had 21 or more
    off-channel reports (2062934: 26, 2064137: 21, 2062335: 26, 2063003: 43, 2063902: 80 — the
    last two refuted in almost these words, "RESOLVED DUPLICATE" and "crash stats shows this is
    an existing crash signature"), and all three a human acted on had 9 or fewer (2063892: 9,
    2062286: 3, 2063864: 1). The default sits at the conservative end of that gap. Release is
    10%-sampled, so 20 reports there is nearer 200 real crashes — an established crash, not a
    stray. CALIBRATED ON TEN POINTS, though: re-measure it before trusting it far.

    The fallback is what makes this purely ADDITIVE. Below the floor the answer is byte-for-byte
    the value this returned before, so the change can only ever add a firing, never remove one —
    all six leads the gate already caught still trip it.

    ``total`` stays on ``channel``, because its consumer needs the opposite scope. It answers
    "has this signature ever been anything but a one-off?" for the bit-flip gate, whose
    population rates (``POPULATION_BIT_FLIP_RATE``) are nightly-measured precisely because
    release accumulates hardware noise that says nothing about a nightly crash — the all-channel
    flip rate is three times higher. Widening it would silently move that gate's denominator.
    ``report_bug.fetch_signature_stats`` computes yet another quantity (from this buildid on)
    for the bug comment; all three are deliberately different questions.

    TWO requests, issued together so they cost one round-trip: the oldest build on ``channel``
    cannot be read off the all-channel response, because the only ordering Socorro will give is
    over the whole result set and a count-ordered facet drops the oldest row.

    ``None`` for any field means we could not find out — never zero. A failed lookup must not
    read as "brand new" to the stale-signature gate nor as "a singleton" to the bit-flip gate."""
    empty = {"first_seen": None, "first_seen_channel": None,
             "total": None, "total_other_channels": None}
    if not signature:
        return empty
    if other_channel_floor is None:
        other_channel_floor = config.get_agent_signature_age()["other_channel_floor"]
    days = max(1, min(int(days or MAX_WINDOW_DAYS), MAX_WINDOW_DAYS))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    base = {
        "signature": "=" + signature,
        "product": product or "Firefox",
        "date": ">=" + since,
        # Sort ascending and take ONE row: the build_id facet is count-ordered, so paging it
        # can drop the oldest build, and an untruncated facet size is a 400.
        "_columns": ["build_id"],
        "_sort": "build_id",
        "_results_number": 1,
    }
    # Unfiltered, plus the facet that recovers the per-channel count the filter used to give.
    any_params = {**base, "_facets": "release_channel"}
    got = {}

    def make_handler(key):
        def handler(json_, data):
            data[key] = json_
        return handler

    queries = [Query(socorro.SuperSearch.URL, params=any_params,
                     handler=make_handler("any"), handlerdata=got)]
    if channel:
        queries.append(Query(socorro.SuperSearch.URL,
                             params={**base, "release_channel": utils.get_search_channel(channel)},
                             handler=make_handler("channel"), handlerdata=got))
    try:
        socorro.SuperSearch(queries=queries).wait()
    except Exception as exc:  # pragma: no cover - network; never break a seed
        logger.warning("sigage: signature history lookup failed for %r: %s", signature, exc)
        return empty

    any_result = got.get("any") or {}
    first_seen_any = _oldest_build(any_result)
    total = _channel_total(any_result, channel)
    overall = any_result.get("total")
    overall = int(overall) if isinstance(overall, int) else None
    other = None if (overall is None or total is None) else max(0, overall - total)

    if not channel:
        first_seen_channel = first_seen_any
    else:
        first_seen_channel = _oldest_build(got.get("channel") or {})

    # The floor. An unknown off-channel count is not a cleared floor: an unresolved lookup must
    # not be what widens the gate.
    if other is not None and other >= other_channel_floor and first_seen_any:
        first_seen = first_seen_any
    else:
        first_seen = first_seen_channel
    return {"first_seen": first_seen, "first_seen_channel": first_seen_channel,
            "total": total, "total_other_channels": other}


def first_seen_buildid(signature, product="Firefox", channel="nightly",
                       days=MAX_WINDOW_DAYS):
    """The oldest buildid this signature appears in, within ``days`` — across EVERY channel
    once the other channels carry enough reports to be evidence, else within ``channel``. See
    ``signature_history``, whose choice this returns unchanged so the agent and the
    deterministic gate can never disagree about how old a signature is. ``None`` when the
    lookup finds nothing or fails. Raises nothing."""
    return signature_history(signature, product, channel, days)["first_seen"]


# The CPU models whose OWN defects make a crash report untrustworthy. One entry, and it is not a
# guess: Intel Raptor Lake desktop parts (13th/14th gen) have a documented instability that
# corrupts computation on perfectly healthy software, and Mozilla tracks the fallout in meta bug
# 1975808, "[meta] Raptor Lake (family 6 model 183 stepping 1) bugs" -- 48 dependent bugs when
# this was written, several of them layout/display-list crashes indistinguishable from a real one.
#
# Matched EXACTLY, against the same string mozilla/bugbot matches in `bugbot/crash/analyzer.py`,
# so the two filers cannot disagree about which hardware to distrust. Socorro renders `cpu_info`
# as a stable "family F model M stepping S", so an exact comparison is the right one.
BROKEN_CPUS = ("family 6 model 183 stepping 1",)

# What the same two measurements read across the NIGHTLY population, so a signature's share can
# be judged rather than merely quoted. Measured 2026-08-19 over 696,901 Firefox nightly reports
# in a 364-day window: 2.5% carry a bit-flip annotation, 4.1% come from a `BROKEN_CPUS` machine.
# Nightly-specific on purpose, to match the denominator `hardware_noise` uses -- the all-channel
# flip rate is 7.6%, three times higher, because release accumulates hardware noise that says
# nothing about a nightly crash. Constants rather than a second query: they move slowly, and the
# alternative is a 700k-document aggregation per run to refine a number used only to say "this is
# high".
POPULATION_BIT_FLIP_RATE = 0.025
POPULATION_BROKEN_CPU_RATE = 0.041


def hardware_noise(signature, product="Firefox", channel="nightly", days=MAX_WINDOW_DAYS):
    """How much of this SIGNATURE is hardware error rather than a bug anyone can fix?

    ``{"reports", "bit_flip_reports", "broken_cpu_reports", "bit_flip_rate", "broken_cpu_rate"}``,
    every value ``None`` when we could not find out.

    WRITTEN FOR BUG 2064600. Clouseau filed a display-list crash at 97% worth-investigating and
    Timothy Nikkel replied within twenty minutes: "About 50% of the crashes with this signature
    have non-zero bit flip probability. That might be something you want to include in your llm
    prompt to consider. And there is also several of the known buggy family 6 model 183 stepping 1
    without a bit flip annotation. ... I always look for these two things in crash reports." Both
    numbers check out -- 3 of the signature's 6 nightly reports carry a flip annotation, and 139
    of its 142 Raptor Lake reports carry none -- and Clouseau could see neither, because it read
    the flip field only for the ONE report it was triaging and never read ``cpu_info`` at all.

    THE TWO SIGNALS ARE NEARLY DISJOINT, which is why both are measured rather than one. Across
    all channels that signature has 107 reports with a flip annotation and 142 on a Raptor Lake,
    and just 3 that are BOTH: the stackwalker's heuristic wants a faulting address one bit away
    from something plausible, and a Raptor Lake miscomputation rarely looks like that. Each covers
    a different third of the signature. A per-report flip check -- all Clouseau had -- sees
    neither.

    THE DENOMINATOR IS THE WHOLE RULE, and getting it wrong is not a detail. Computed across all
    products and channels over 180 days this fires on 6 of the 47 bugs the canary had filed, and
    one of them is bug 2062219 (``nsAtom::IsStatic``), RESOLVED FIXED -- a real defect, killed,
    because that signature runs 49% bit flips over a release population and only 13% in nightly.
    Restricted to the crash's OWN product and channel it fires on 6 different signatures and kills
    ZERO of the 18 filings that are FIXED, DUPLICATE or ASSIGNED, while still catching the INVALID
    one (bug 2062173) and bug 2064600 itself. Release accumulates years of failing consumer
    hardware on a hot signature; that says nothing about whether a nightly crash is real, and
    averaging the two populations together lets the larger one decide.

    THE FULL 364-DAY WINDOW, unlike bugbot's few weeks, for the same reason in reverse: a nightly
    slice is SMALL. Bug 2064600's signature has 6 nightly reports in a year and 1 in the last 28
    days, so bugbot's window would see no sample at all here. bugbot can afford a short one
    because it is scanning for busy signatures worth filing; we are handed one crash and have to
    judge it. Window truncation can only shrink the sample, never inflate a rate.

    Reports, not machines -- and checked. A single failing machine filing hundreds of reports
    would fake any share computed this way, so the confound was measured on the signature that
    prompted this: its flip-annotated reports span more than 100 distinct ``install_time`` values,
    the largest contributing 3. Read ``distinct_signatures`` in ``machine.py`` for the
    complementary rule that catches the one-broken-machine case directly.

    ONE SuperSearch, ~300ms, on a run that already takes ~20 minutes. ``None`` rather than 0 on
    every failure path, because this feeds a suppression and "we could not find out" must never
    be able to satisfy a threshold."""
    empty = {"reports": None, "bit_flip_reports": None, "broken_cpu_reports": None,
             "bit_flip_rate": None, "broken_cpu_rate": None}
    if not signature:
        return empty
    days = max(1, min(int(days or MAX_WINDOW_DAYS), MAX_WINDOW_DAYS))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    params = {
        "signature": "=" + signature,
        "product": product or "Firefox",
        "date": ">=" + since,
        "_facets": ["possible_bit_flips_max_confidence", "cpu_info"],
        # `possible_bit_flips_max_confidence` takes 17 distinct values across the entire corpus,
        # so summing its facet is exact. `cpu_info` has a long tail, but truncation is SAFE here
        # for a reason worth stating: facets come back COUNT-ORDERED, so a model holding a
        # material share of a signature is always near the top, and anything the cut hides is by
        # construction too small to clear a threshold. That is the exact inverse of the `build_id`
        # trap documented at the top of this module, where the row we needed was the rarest.
        "_facets_size": 200,
        "_results_number": 0,
    }
    if channel:
        params["release_channel"] = channel
    got = {}

    def handler(json_, data):
        data["result"] = json_

    try:
        socorro.SuperSearch(params=params, handler=handler, handlerdata=got).wait()
    except Exception as exc:  # pragma: no cover - network; never break a seed
        logger.warning("sigage: hardware-noise lookup failed for %r: %s", signature, exc)
        return empty
    result = got.get("result") or {}
    total = result.get("total")
    if not isinstance(total, int) or total <= 0:
        # Zero rows is NOT "a clean signature": an empty response is equally what a malformed or
        # throttled query returns, and this feeds a suppression. Say we do not know.
        return empty
    facets = result.get("facets") or {}

    def _sum(field, keep=None):
        rows = facets.get(field)
        if not isinstance(rows, list):
            return None
        return sum(r.get("count") or 0 for r in rows
                   if isinstance(r, dict) and (keep is None or r.get("term") in keep))

    flips = _sum("possible_bit_flips_max_confidence")
    broken = _sum("cpu_info", keep=set(BROKEN_CPUS))
    return {
        "reports": total,
        "bit_flip_reports": flips,
        "broken_cpu_reports": broken,
        "bit_flip_rate": None if flips is None else flips / total,
        "broken_cpu_rate": None if broken is None else broken / total,
    }


_JSON_REV_CACHE: dict = {}


def json_rev(node, channel="nightly"):
    """hg's ``json-rev`` for a changeset: ``{node, pushdate, git_commit, ...}``.

    ONE request serves both things we want about a changeset — when it landed
    (``pushdate_for_node``) and its git counterpart (``git_commit_for_node``) — so they share
    this cache rather than each paying for the same lookup. The endpoint is SLOW (measured
    8-13s on mozilla-central, whichever of hg.mozilla.org / hg-edge you ask), which is why it
    belongs in the worker and never on a page render.

    Takes a SHORT rev, unlike lando's ``hg2git`` which needs the full 40 chars. ``{}`` when
    unresolvable; raises nothing. Cached per ``(node, channel)`` — both fields are immutable."""
    if not node:
        return {}
    key = (node, channel or "")
    if key in _JSON_REV_CACHE:
        return _JSON_REV_CACHE[key]
    from libmozdata.hgmozilla import Mercurial

    from crashclouseau import net

    out = {}
    repo_url = Mercurial.get_repo_url(channel) if channel else ""
    if repo_url:
        try:
            r = net.get("{}/json-rev/{}".format(repo_url, node), allow_redirects=True)
            r.raise_for_status()
            out = r.json() or {}
        except Exception as exc:  # pragma: no cover - network; never break a gate
            logger.warning("sigage: json-rev lookup failed for %s: %s", node, exc)
    _JSON_REV_CACHE[key] = out
    return out


def pushdate_for_node(node, channel="nightly"):
    """When a changeset landed (``[epoch, tzoffset]``), or ``None``.

    This is the FALLBACK for the stale-signature gate. The seed pre-computes a pushdate for
    every candidate in the build's pushlog window, but the agent can choose a candidate that
    was never in that window (it found it via blame), and such a candidate has no pre-computed
    landing date -- so the gate used to silently no-op on precisely the crashes it exists to
    catch. Seen in prod on ``0cf2a052-2eae-4228-824f-6284d0260728``: the candidate landed 126
    days after the signature first appeared, the gate skipped, and only the (paid) blind second
    opinion noticed."""
    return json_rev(node, channel).get("pushdate") or None


def git_commit_for_node(node, channel="nightly"):
    """The git sha for an hg changeset, or ``""``. Firefox lives in both forges since the
    hg->git migration, so the filed bug links the changeset on each; resolved HERE, in the
    worker, and persisted on the candidate so no page render ever pays for it."""
    return json_rev(node, channel).get("git_commit") or ""


def backedout_by_for_node(node, channel="nightly"):
    """The sha that BACKED OUT this changeset: ``""`` when hg says it was not backed out, and
    ``None`` when we could not find out.

    TRI-STATE on purpose. A backed-out candidate is SUPPRESSED outright, not downweighted, so
    "we don't know" must never collapse into "it's clean" — a failed lookup has to leave the
    verdict alone. ``json_rev`` returns (and caches) ``{}`` for every no-answer case: an empty
    channel makes no request at all, and a 404/timeout is swallowed. Hence the ``node`` sentinel
    rather than testing ``backedoutby`` directly, which is simply ABSENT on a clean changeset
    and would be indistinguishable from a failure.

    Free in practice: this is the same cached ``json-rev`` request ``pushdate_for_node`` and
    ``git_commit_for_node`` already make for this same node on every online run.

    Note it says nothing about WHEN the backout landed, and it stays set forever — a change
    that was backed out and later RE-LANDED (as a new node) still reports the old backout."""
    rev = json_rev(node, channel)
    if not rev.get("node"):
        return None
    return rev.get("backedoutby") or ""


def desc_for_node(node, channel="nightly"):
    """A changeset's commit message, or ``""`` when we could not find out.

    Free: the same cached ``json-rev`` request already serves ``pushdate_for_node`` /
    ``git_commit_for_node`` / ``backedout_by_for_node`` for this node on every online run.
    Wanted for the MIRROR of ``backedout_by_for_node``'s predicate — hg tells us straight out
    whether a changeset WAS backed out, but whether it IS ITSELF a backout only shows up in
    its description (``pushlog.is_backed_out``)."""
    return json_rev(node, channel).get("desc") or ""


_PUSH_CACHE: dict = {}

# What a backout says it undoes, in the two shapes mozilla-central actually uses. The GIT one
# dominates since the hg->git migration: Lando writes `Revert "<title>"` + `This reverts commit
# <40-char GIT sha>`, and NOT ONE of 909 backout descriptions sampled across pushes 44620-45020
# names an hg short hash. The hg one is kept for `hg backout`-style descriptions (one line per
# backed-out changeset, so ``findall``).
_REVERTS_GIT_RE = re.compile(r"^This reverts commit ([0-9a-f]{40})", re.M)
_BACKED_OUT_RE = re.compile(r"[Bb]acked out changeset ([0-9a-f]{12,40})")


def push_for_node(node, channel="nightly"):
    """The ``json-pushes`` record of the push that landed ``node``, ``full=1`` so every member
    changeset carries its own ``node`` + ``desc``. ``{}`` when unresolvable; raises nothing.

    Cached per ``(node, channel)`` like ``json_rev`` — a push is immutable once landed. Its
    ONE caller (``same_push_backout_target``) only runs for a candidate that is itself a
    backout, which is ~0.5% of runs, so this never costs a normal triage anything."""
    if not node:
        return {}
    key = (node, channel or "")
    if key in _PUSH_CACHE:
        return _PUSH_CACHE[key]
    from libmozdata.hgmozilla import Mercurial

    from crashclouseau import net

    out = {}
    repo_url = Mercurial.get_repo_url(channel) if channel else ""
    if repo_url:
        try:
            r = net.get(
                "{}/json-pushes".format(repo_url),
                params={"changeset": node, "version": "2", "full": "1"},
                allow_redirects=True,
            )
            r.raise_for_status()
            for pushid, push in ((r.json() or {}).get("pushes") or {}).items():
                out = {**push, "pushid": pushid}
                break
        except Exception as exc:  # pragma: no cover - network; never break a gate
            logger.warning("sigage: json-pushes lookup failed for %s: %s", node, exc)
    _PUSH_CACHE[key] = out
    return out


def revert_targets(desc):
    """Every changeset ``desc`` says it reverts, as 12-char hg hashes — or ``None`` when we
    cannot enumerate them exactly.

    ``None`` covers three cases that must NOT be told apart, because all three mean "we do not
    know what this undoes": the description names nothing, it names a git commit lando could
    not map, or lando was unreachable (``inspector.git2hg`` returns ``""`` for a genuine
    non-Firefox commit AND for a transient failure). Every one of them has to reach the caller
    as unknown, since the only thing a caller does with a complete answer is SUPPRESS.

    Deliberately reads the description rather than matching against the push's members: a
    sheriff routinely reverts and RELANDS in one push, and a reland carries the reverted
    patch's title verbatim, so any title-similarity match happily "proves" that a backout of a
    days-old changeset is same-push. Measured on live mozilla-central, that mistake makes 6.4%
    of matches point at a node the backout does not revert at all."""
    if not desc:
        return None
    targets = {h[:12] for h in _BACKED_OUT_RE.findall(desc)}
    git_shas = _REVERTS_GIT_RE.findall(desc)
    if not targets and not git_shas:
        return None
    from crashclouseau import inspector

    for git_sha in git_shas:
        hg_hash = inspector.git2hg(git_sha)
        if not hg_hash:
            return None
        targets.add(hg_hash[:12])
    return targets or None


def same_push_backout_target(node, channel="nightly"):
    """Does ``node`` back out changesets that ALL landed in ``node``'s own push?

    TRI-STATE like ``backedout_by_for_node``: the first such changeset, ``""`` when the answer
    is no, and ``None`` when we could not find out. The distinction matters because a hit
    SUPPRESSES the verdict outright, so an unresolvable lookup must never read as a hit — nor
    as a clean "no".

    WHY THIS IS THE PRECISE DISCRIMINATOR. A backout is only interesting as a "regressor" when
    it restores a crash that some build had stopped shipping. If everything it reverts landed
    in its own push, no build ever contained any of it: the tree's content is identical before
    the push and after it, so the changeset provably changed nothing. Seen in prod on
    ``00b44d2a-4343-4caa-9e12-907550260802``, where a fix and its same-day revert both reached
    mozilla-central in autoland merge push 44977 (``dom/onnx/InferenceSession.cpp`` is
    byte-identical at the push parent, at the revert and at the push head) and the pipeline
    still reported the revert as the culprit at 97%.

    ALL of them, not any: proving one of three reverted patches is same-push says nothing about
    the other two, and a target that landed in an EARLIER push is exactly the case where the
    tree did differ and the backout is a real regressor worth reporting."""
    targets = revert_targets(desc_for_node(node, channel))
    if targets is None:
        return None
    members = push_for_node(node, channel).get("changesets") or []
    if not members:
        return None
    by_short = {(m.get("node") or "")[:12]: (m.get("node") or "")
                for m in members if m.get("node")}
    if not targets <= set(by_short):
        return ""
    # Push order, so a multi-changeset backout names the first thing it undid rather than
    # whichever hash happens to sort first.
    for member in members:
        short = (member.get("node") or "")[:12]
        if short in targets:
            return member.get("node")
    return ""


def to_datetime(value):
    """Best-effort UTC datetime from any of the pushdate shapes the candidate builders produce:
    a tz-aware ``datetime`` (on-stack, straight from the DB column), hg's ``[epoch, tzoffset]``
    pair or a bare epoch number (off-stack, from ``json-pushes``), an ISO string, or a
    ``YYYYMMDDHHMMSS`` buildid. ``None`` when it is none of those."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (list, tuple)):
        return to_datetime(value[0]) if value else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.isdigit() and len(text) in (8, 14):
        return _buildid_to_dt(text)
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def days_landed_after_first_seen(first_seen, pushdate):
    """Days the candidate landed AFTER the signature was first seen. Positive means the crash
    already existed before the candidate landed — so that candidate cannot be its ORIGIN.
    ``None`` when either side is unknown/unparseable.

    This is the comparison that actually discriminates. Comparing first-seen against the crash's
    BUILD date instead does NOT: two thirds of all triaged signatures are old, so a build-based
    rule fires on ~83% of independently-CONFIRMED leads as well as on the wrong ones. Measured
    on 23 real prod leads with the blind second opinion as the yardstick, this comparison at a
    7-day threshold fires on 10/10 high-confidence refutations while sparing 5 of 6
    corroborated leads."""
    seen_dt = _buildid_to_dt(first_seen)
    push_dt = to_datetime(pushdate)
    if seen_dt is None or push_dt is None:
        return None
    return round((push_dt - seen_dt).total_seconds() / 86400.0, 1)
