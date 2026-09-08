# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""A REAL spike files a bug, culprit or not -- and Claude Fable 5.1 gets one shot at the culprit.

THE RULE (Calixte, 2026-09-07). Whatever the channel, a real spike of crashes -- not 0 -> 1, but
a volume a human would call a spike (``spikes``) -- is a fact by itself and MUST reach Bugzilla,
with a culprit when we have one and without when we do not. The ordinary pipeline files only
what it can defend at rung 70, so on a spike where it abstained, was refuted, or never ran, this
module takes over: it hands everything we know to a single strong investigator (``spike_agent``,
Claude Fable 5.1 at effort xhigh) and files the volume plus whatever the investigator could
GROUND. Fable is never spent on noise: the predicate runs first, and it is strict on purpose.

THE LOOP, on the clock (``bin/schedule.py``), every few minutes:

1. ``sweep_real_spikes`` reads the selection log for the pairs the selector ANALYSED in the last
   ``lookback_days`` and judges each (``spikes.judge_selection``; a lambda's two demanglings are
   judged as one). A real spike is escalated once per ``once_per_days`` per signature family,
   only after ``grace_s`` since it was first selected and only once no ordinary run on its
   build is still pending or running -- "the classic pushlog stuff failed" has to be true before
   it is acted on. A spike the ordinary path DID file is recorded and left alone.
2. ``run_spike_escalation`` (an RQ job on the agent queue, its own timeout) builds the brief
   (``build_spike_brief``: the numbers, the signature's history, up to ``max_stacks`` distinct
   stacks, the ordinary runs' conclusions, the on-stack candidates and the spiking build's whole
   pushlog window), runs the investigator, validates what it said against what it was given
   (``_validate_findings``: a culprit must be a real changeset that landed before the build; a
   claim with no source is dropped; a run that consulted no tool grounded nothing), then files
   (``file_spike_bug``).
3. Filing: the global ``AUTOFILE_BUGS`` switch and a per-channel daily cap are the only gates --
   the per-channel culprit-filing hold does NOT apply, a spike is filed on every triaged channel.
   An open same-application non-meta bug on the signature gets the spike as a COMMENT (the volume
   is news to whoever owns that bug; a bug filed FOR this spike that already names a regressor
   gets nothing); otherwise a new bug is created, with the investigator's product::component
   validated against Bugzilla and falling back to the signature's existing bugs' component, then
   ``Core :: General`` -- a spike is filed into a component that can move it rather than not
   filed. Memory-safety crashes follow the ordinary filer's security branch.

WHAT IS DELIBERATELY NOT HERE. No skeptic, no second opinion: the investigator's output is
validated mechanically and published as an offer under the volume facts, which stand on their own.
No stamping of ``Dossier.payload['filed_bug']``: that key is the ordinary filer's idempotence and
daily-cap key, and a spike bug is a different kind of bug with its own table
(``models.SpikeEscalation``). The two filers meet on Bugzilla, through
``_open_bugs_for_signature``, the way each meets a human's bug.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from crashclouseau import (
    app, bugzilla_apply, config, db, models, net, report_bug, sensitive, sigage, sigtrend,
    spike_report, spikes, utils, worker,
)
from crashclouseau.logger import logger

# A run that died (a SIGKILLed worker, an RQ timeout) is retried once; a run that failed twice
# is a bug in the tooling, not bad luck, and the row stays `error` for a human.
_MAX_ATTEMPTS = 2
# Slack on top of the job timeout before a `running` row is presumed dead.
_STALE_BUFFER_S = 300
# A filing that could not be made for a reason that may clear (a BMO lookup failed, the daily
# cap was reached) is retried from the sweep without re-running the investigator.
_FILING_RETRIES = 3
_FILING_RETRY_AFTER_S = 900
_RAW_HEAD, _RAW_TAIL = 2000, 8000
_HEX = frozenset("0123456789abcdef")
_MIN_NODE = 7

# product -> {component name} of the ACTIVE components, memoised for the process. A missing
# product reads as unknown (None), never as "no components".
_COMPONENTS_CACHE: dict = {}


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #
def sweep_real_spikes():
    """The clock job: find the real spikes the ordinary pipeline did not file, and escalate them.
    Best-effort (never raises out); returns how many investigations were enqueued."""
    try:
        with app.app_context():
            cfg = config.get_agent_spike_escalation()
            if not cfg["enabled"]:
                return 0
            _reap_stale(cfg)
            _retry_filings(cfg)
            channels = config.get_agent_channels()
            room = cfg["max_per_tick"]
            enqueued = 0
            for product in config.get_products():
                for channel in channels or []:
                    if room <= 0:
                        break
                    n = _sweep_channel(product, channel, cfg, room)
                    room -= n
                    enqueued += n
            return enqueued
    except Exception:  # pragma: no cover - defensive; the clock must survive
        logger.error("spike: sweep failed", exc_info=True)
        return 0


def _parse_ts(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_day(value):
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _group_by_family(rows):
    """``{(family, build_day): [rows]}`` -- a lambda's two demanglings on one build-day are one
    spike (``utils.lambda_family``), as the selector already decides them."""
    groups = {}
    for row in rows:
        key = (utils.lambda_family(row.get("signature") or ""), row.get("build_day"))
        groups.setdefault(key, []).append(row)
    return groups


def merge_rows(rows):
    """One selection-shaped dict for a family's rows on one build-day: counts and installs are
    summed, the baseline elementwise (the selector merged the series the same way), the loudest
    member lends its signature and its picked build. A single row comes back as itself plus the
    ``signatures`` list."""
    rows = sorted(rows, key=lambda r: -(r.get("number") or 0))
    primary = dict(rows[0])
    primary["signatures"] = [r.get("signature") for r in rows]
    if len(rows) == 1:
        return primary
    primary["number"] = sum(int(r.get("number") or 0) for r in rows)
    bids = {}
    for r in rows:
        for bid, info in (r.get("bids") or {}).items():
            cur = bids.setdefault(bid, {"count": 0, "installs": 0})
            cur["count"] += int((info or {}).get("count") or 0)
            cur["installs"] += int((info or {}).get("installs") or 0)
    primary["bids"] = bids
    baselines = [r.get("baseline") or [] for r in rows]
    if baselines and all(len(b) == len(baselines[0]) for b in baselines):
        primary["baseline"] = [sum(int(b[i] or 0) for b in baselines)
                               for i in range(len(baselines[0]))]
    primary["picked"] = next((r.get("picked") for r in rows if r.get("picked")), None)
    firsts = [_parse_ts(r.get("first_run_date")) for r in rows]
    firsts = [f for f in firsts if f]
    if firsts:
        primary["first_run_date"] = min(firsts).isoformat()
    if any(r.get("outcome") == utils.RISING_RATE for r in rows) and all(
            r.get("outcome") == utils.RISING_RATE for r in rows):
        primary["outcome"] = utils.RISING_RATE
    return primary


def _trend(product, channel, signature):
    try:
        return sigtrend.trend_facts(product, channel, signature)
    except Exception:
        logger.warning("spike: cannot read the rate of %s", signature, exc_info=True)
        return {}


def _retryable(row, cfg):
    return row.status == "error" and (row.attempts or 0) < _MAX_ATTEMPTS


def _sweep_channel(product, channel, cfg, room):
    rows = models.Selection.escalation_candidates(product, channel, cfg["lookback_days"])
    if not rows:
        return 0
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    budget = cfg["max_runs_per_day"] - models.SpikeEscalation.count_since(
        product, channel, day_start)
    enqueued = 0
    groups = _group_by_family(rows)
    for (family, build_day), members in sorted(
            groups.items(), key=lambda kv: str(kv[0][1] or ""), reverse=True):
        if room <= 0:
            break
        merged = merge_rows(members)
        signature = merged.get("signature") or family
        # A deliberate test crash is never a spike worth a run, and its OLD `selected` rows
        # (three for CrashChannel::OpenContentStream on 2026-09-08) sit inside the lookback.
        if any(config.is_ignored_signature(s)
               for s in [signature, family] + list(merged.get("signatures") or [])):
            continue
        trend = None
        if merged.get("outcome") == utils.RISING_RATE:
            trend = _trend(product, channel, signature)
        spike = spikes.judge_selection(merged, product, channel, trend_facts=trend)
        if not spike:
            continue
        day = _parse_day(build_day)
        if day is None:
            continue
        siblings = sorted(set(utils.lambda_siblings(signature)) | set(
            s for s in merged.get("signatures") or [] if s))
        existing = models.SpikeEscalation.for_pair(signature, product, channel, day)
        if existing is not None:
            if _retryable(existing, cfg) and budget > 0:
                logger.info("spike: retrying escalation %s (%s on %s, attempt %d failed)",
                            existing.id, signature, day, existing.attempts)
                _enqueue(existing.id, cfg)
                room -= 1
                budget -= 1
                enqueued += 1
            continue
        since = now - timedelta(days=cfg["once_per_days"])
        prior = models.SpikeEscalation.latest_for_signature(siblings, product, channel, since)
        if prior is not None:
            continue
        first = _parse_ts(merged.get("first_run_date"))
        if first is not None and (now - first).total_seconds() < cfg["grace_s"]:
            continue
        picked = merged.get("picked")
        if not picked:
            continue
        runs = classic_runs(siblings, picked, channel)
        if any(r.get("status") in ("pending", "running") for r in runs):
            continue
        filed = [r for r in runs if r.get("filed_bug")]
        payload = {"spike": spike, "siblings": siblings,
                   "spike_sentence": spikes.describe(spike, channel, day.isoformat(), picked)}
        if filed:
            bug = (filed[0].get("filed_bug") or {}).get("bug")
            row = models.SpikeEscalation.create(
                signature, product, channel, day, buildid=picked, uuid=filed[0].get("uuid"),
                kind=spike["kind"], payload=dict(
                    payload, skipped="the ordinary triage filed bug {} for this spike".format(bug)))
            row.set_status("done")
            logger.info("spike: %s on %s is a real spike and the ordinary triage filed bug %s; "
                        "nothing to escalate", signature, day, bug)
            continue
        if budget <= 0:
            logger.info("spike: %s on %s is a real spike but %s-%s has spent today's %d runs",
                        signature, day, product, channel, cfg["max_runs_per_day"])
            break
        uuid = representative_uuid(siblings, picked, channel, product, runs)
        if uuid is None:
            row = models.SpikeEscalation.create(
                signature, product, channel, day, buildid=picked, kind=spike["kind"],
                payload=dict(payload, skipped="no ingested report with a stack on the build"))
            row.set_status("done")
            logger.warning("spike: %s on %s is a real spike but no report with a stack was "
                           "ingested for build %s", signature, day, picked)
            continue
        payload["classic_runs"] = len(runs)
        row = models.SpikeEscalation.create(
            signature, product, channel, day, buildid=picked, uuid=uuid, kind=spike["kind"],
            payload=payload)
        _enqueue(row.id, cfg)
        room -= 1
        budget -= 1
        enqueued += 1
        logger.info("spike: escalating %s on %s-%s build %s (%s; %d ordinary run(s), none "
                    "filed): escalation %s on %s", signature, product, channel, picked,
                    payload["spike_sentence"], len(runs), row.id, uuid)
    return enqueued


def _enqueue(escalation_id, cfg):
    queue = worker.get_queue(config.get_agent_queue())
    queue.enqueue_call(
        func=run_spike_escalation, args=(escalation_id,), result_ttl=0,
        # Its own timeout: a Fable run at xhigh over a whole pushlog window can outlast the
        # ordinary run's 1800s, and RQ's default would kill it at 180s.
        timeout=cfg["job_timeout"],
    )


def _reap_stale(cfg):
    for row in models.SpikeEscalation.stale_running(cfg["job_timeout"] + _STALE_BUFFER_S):
        logger.warning("spike: escalation %s (%s) was running for too long; marking it failed",
                       row.id, row.signature)
        row.set_status("error", error="stale: the worker died or the job timed out")


# --------------------------------------------------------------------------- #
# What the ordinary pipeline did on this build
# --------------------------------------------------------------------------- #
def classic_runs(signatures, buildid, channel):
    """``[{uuid, status, verdict, confidence, abstain_kind, abstain_reason, candidate,
    mechanism, second_opinion, declined, filed_bug}]`` for every ordinary run on any of
    ``signatures`` on ``buildid``. Never raises: a log we cannot read is an empty list, and the
    caller then treats the pipeline as not having run."""
    if not signatures or not buildid:
        return []
    try:
        build = utils.get_build_date(buildid)
        rows = (
            db.session.query(models.UUID.uuid, models.Dossier.status, models.Dossier.payload)
            .select_from(models.Dossier)
            .join(models.UUID, models.Dossier.uuidid == models.UUID.id)
            .join(models.Build, models.Build.id == models.UUID.buildid)
            .join(models.Signature, models.Signature.id == models.UUID.signatureid)
            .filter(
                models.Signature.signature.in_(sorted(signatures)),
                models.Build.buildid == build,
                models.Build.channel == channel,
            )
            .order_by(models.Dossier.id)
            .all()
        )
    except Exception:
        logger.error("spike: cannot read the ordinary runs for %s", buildid, exc_info=True)
        try:
            db.session.rollback()
        except Exception:  # pragma: no cover - defensive
            pass
        return []
    return [summarize_run(uuid, status, payload or {}) for uuid, status, payload in rows]


def summarize_run(uuid, status, payload):
    dossier = payload.get("dossier") or {}
    verdict = dossier.get("verdict") or {}
    candidate = dossier.get("candidate") or {}
    so = dossier.get("second_opinion") or {}
    filed = payload.get("filed_bug") or {}
    declined = payload.get("filing_declined") or {}
    out = {
        "uuid": uuid,
        "status": status,
        "verdict": verdict.get("decision"),
        "confidence": verdict.get("confidence"),
        "abstain_kind": verdict.get("abstain_kind"),
        "abstain_reason": _clip(verdict.get("abstain_reason"), 600),
        "candidate": {"node": candidate.get("node"), "bug": candidate.get("bug")}
        if candidate.get("node") else None,
        "mechanism": _clip(((verdict.get("mechanism") or {}).get("statement")), 600),
        "second_opinion": ({"corroborates": so.get("corroborates"),
                            "refutation": _clip(so.get("refutation"), 400)} if so else None),
        "declined": _clip(declined.get("skipped"), 200),
        "filed_bug": filed if filed.get("filed") else None,
    }
    if status == "error" and payload.get("error"):
        out["abstain_reason"] = _clip("the run failed: {}".format(payload["error"]), 300)
    return out


def _clip(text, n):
    text = (text or "").strip() if isinstance(text, str) else text
    if not text:
        return None
    return text if len(text) <= n else text[:n] + "..."


def reports_for(signatures, buildid, channel, product):
    """``[(uuid, protohash)]`` of the ingested, stack-bearing reports of these signatures on the
    build -- the spike's own crashes, one proto-signature cluster per distinct stack."""
    if not signatures or not buildid:
        return []
    try:
        build = utils.get_build_date(buildid)
        rows = (
            db.session.query(models.UUID.uuid, models.UUID.protohash)
            .select_from(models.UUID)
            .join(models.Build, models.Build.id == models.UUID.buildid)
            .join(models.Signature, models.Signature.id == models.UUID.signatureid)
            .filter(
                models.Signature.signature.in_(sorted(signatures)),
                models.Build.buildid == build,
                models.Build.channel == channel,
                models.Build.product == product,
                models.UUID.useless.is_(False),
                models.UUID.analyzed.is_(True),
            )
            .order_by(models.UUID.id)
            .all()
        )
        return [(u, p) for u, p in rows]
    except Exception:
        logger.error("spike: cannot list the reports of %s", buildid, exc_info=True)
        try:
            db.session.rollback()
        except Exception:  # pragma: no cover - defensive
            pass
        return []


def _has_frames(uuid):
    try:
        res, _info = models.CrashStack.get_by_uuid(uuid)
    except Exception:
        return False
    return bool((res or {}).get("frames"))


def representative_uuid(signatures, buildid, channel, product, runs):
    """The report the brief is built around: one with a stored stack, preferring one the ordinary
    pipeline finished a run on (its dossier carries the most context), then the earliest."""
    reports = reports_for(signatures, buildid, channel, product)
    if not reports:
        return None
    done = {r["uuid"] for r in runs if r.get("status") == "done"}
    ordered = [u for u, _p in reports if u in done] + [u for u, _p in reports if u not in done]
    for uuid in ordered:
        if _has_frames(uuid):
            return uuid
    return None


# --------------------------------------------------------------------------- #
# The brief
# --------------------------------------------------------------------------- #
def _frames_from_dump(raw):
    """Frames of the analysed thread straight from the processed crash, WITHOUT the build-node
    check ``inspector.inspect_stacktrace`` applies: this is for the prompt, and a stack whose
    frames were built at a slightly different revision is still what the model needs to read."""
    from crashclouseau import inspector

    dump = (raw or {}).get("json_dump") or {}
    threads = dump.get("threads") or []
    n = inspector.thread_for_analysis(raw or {})
    if not isinstance(n, int) or not 0 <= n < len(threads):
        return []
    out = []
    for i, frame in enumerate((threads[n].get("frames") or [])[:50]):
        filename, node = inspector.get_path_node(frame.get("file"))
        out.append({"stackpos": i, "filename": filename or (frame.get("file") or ""),
                    "function": frame.get("function") or "", "line": frame.get("line") or -1,
                    "module": frame.get("module") or "", "node": node, "changesets": {}})
    return out


def _minimal_seed(uuid, uuid_info, esc):
    """What ``build_seed`` would have handed the ordinary agent, when it refused (no frames, or
    an off-stack crash with the off-stack path switched off): the report, its stack and the
    signature-level facts, with no candidates. Never lets one lookup failure lose the brief."""
    from crashclouseau import inspector
    from crashclouseau.agent import orchestrator

    raw = None
    try:
        raw = inspector.get_crash_data(uuid)
    except Exception as exc:
        logger.warning("spike: could not fetch the processed crash %s: %s", uuid, exc)
    stack, _info = models.CrashStack.get_by_uuid(uuid)
    frames = (stack or {}).get("frames") or _frames_from_dump(raw)
    frames = frames[: config.get_agent_max_seed_frames()]
    info = {"signature": esc.signature, "product": esc.product, "channel": esc.channel}
    seed = {
        "uuid": uuid, "signature": esc.signature, "channel": esc.channel,
        "product": esc.product, "buildid": esc.buildid,
        "version": (raw or {}).get("version"), "frames": frames,
        "stack": orchestrator._stack_text(frames) if frames else "", "candidates": [],
        "experts": [], "raw_crash": raw, "is_offstack": True, "offstack_reason": None,
        "build_node": (uuid_info or {}).get("node", ""), "pin_rev": (uuid_info or {}).get("node", ""),
        "prior_regressor_bugs": [], "prior_hints": [], "candidate_pushdates": {},
        "archetypes": [], "install_history": {}, "candidate_window": None,
    }
    seed["signature_trend"] = orchestrator._signature_trend(info, uuid_info or {}, esc.channel)
    seed["version_rates"] = orchestrator._version_rates(info, esc.channel)
    seed["hardware_noise"] = orchestrator._hardware_noise(info, esc.channel)
    try:
        history = sigage.signature_history(esc.signature, esc.product, esc.channel)
        seed["signature_first_seen_buildid"] = history.get("first_seen")
        seed["signature_first_seen_any"] = history.get("first_seen_any")
        seed["signature_first_seen_channel"] = history.get("first_seen_channel")
        seed["signature_report_count"] = history.get("total")
        seed["signature_first_seen_ever"] = sigage.first_seen_ever(
            [esc.signature]).get(esc.signature)
    except Exception as exc:
        logger.warning("spike: signature history lookup failed for %s: %s", esc.signature, exc)
    return seed


def _window_for(uuid_info, seed):
    """The spiking build's pushlog window as candidate dicts, and a sentence saying how wide it
    was. Reuses the off-stack enumeration; empty on any failure (the investigator then works from
    blame and history)."""
    from crashclouseau.agent import orchestrator

    if seed.get("is_offstack") and seed.get("candidates"):
        window = [c for c in seed["candidates"] if c.get("score") is None]
        win = seed.get("candidate_window") or {}
        if window:
            return window, _extent(win.get("hours"), win.get("widened"))
    if not uuid_info or not uuid_info.get("buildid"):
        return [], None
    try:
        rising = sigtrend.is_rising(seed.get("signature_trend") or {})
        bounds = orchestrator._offstack_window(uuid_info, rising=rising)
        window = orchestrator._offstack_candidates(
            uuid_info, config.get_agent_offstack(), window=bounds)
        return window, _extent(bounds.get("hours"), bounds.get("widened"))
    except Exception as exc:
        logger.warning("spike: could not enumerate the pushlog window for %s: %s",
                       uuid_info.get("uuid"), exc)
        return [], None


def _extent(hours, widened):
    if hours:
        return "the {:.0f} hours before this build{}".format(
            hours, " (WIDENED past the previous build because the rate was already rising)"
            if widened else "")
    return "from the previous build to this one"


def build_spike_brief(esc, light=False):
    """Everything the investigator (and the filer) gets about one escalation. ``light`` skips
    the parts only the investigator needs -- the window, the other stacks, the ordinary runs --
    for a filing retry."""
    from crashclouseau.agent import orchestrator, triage

    uuid = esc.uuid
    payload = esc.payload or {}
    spike = payload.get("spike") or {}
    siblings = payload.get("siblings") or [esc.signature]
    _stack, uuid_info = models.CrashStack.get_by_uuid(uuid)
    seed = None
    try:
        seed = orchestrator.build_seed(uuid)
    except Exception as exc:
        logger.warning("spike: build_seed failed for %s: %s", uuid, exc)
    if seed is None:
        seed = _minimal_seed(uuid, uuid_info, esc)
    build_day = esc.build_day.isoformat() if esc.build_day else None
    brief = {
        "escalation_id": esc.id,
        "uuid": uuid,
        "signature": esc.signature,
        "siblings": siblings,
        "product": esc.product,
        "channel": esc.channel,
        "version": seed.get("version"),
        "buildid": esc.buildid,
        "build_day": build_day,
        "spike": spike,
        "spike_sentence": payload.get("spike_sentence") or spikes.describe(
            spike, esc.channel, build_day, esc.buildid),
        "trend_sentence": spike_report.trend_sentence(seed.get("signature_trend")),
        "version_step": (seed.get("version_rates") or {}).get("step"),
        "first_seen_ever": seed.get("signature_first_seen_ever"),
        "first_seen": seed.get("signature_first_seen_buildid"),
        # The CHANNEL's own first-seen: what decides whether the spike is an appearance on this
        # channel and earns its `summary_prefix` (`spike_report.is_new_signature`).
        "first_seen_channel": seed.get("signature_first_seen_channel"),
        "is_hang": spike_report.is_hang(esc.signature, seed.get("raw_crash")),
        "pin_rev": seed.get("pin_rev") or (uuid_info or {}).get("node") or "",
        "raw_crash": seed.get("raw_crash"),
        "stack_frames": {"frames": seed.get("frames") or []},
        "facts": triage._crash_facts(seed),
        "stacks": [{"uuid": uuid, "stack": seed.get("stack") or ""}],
        "classic_runs": [],
        "candidates": {"onstack": [], "window": [], "window_extent": None},
        "candidate_nodes": {},
    }
    if light:
        return brief
    cands = seed.get("candidates") or []
    onstack = [c for c in cands if c.get("score") is not None]
    window, extent = _window_for(uuid_info, seed)
    brief["candidates"] = {"onstack": onstack, "window": window, "window_extent": extent}
    brief["candidate_nodes"] = {
        str(c.get("node") or "").lower(): c for c in onstack + window if c.get("node")}
    brief["classic_runs"] = classic_runs(siblings, esc.buildid, esc.channel)
    brief["stacks"] = _stacks(uuid, siblings, esc, seed)
    return brief


def _stacks(uuid, siblings, esc, seed):
    """The representative stack plus up to ``max_stacks - 1`` other proto-signature clusters of
    the same spike, each with its report-level facts -- a spike that is one defect under three
    stacks reads very differently from three unrelated crashes sharing a signature."""
    from crashclouseau import inspector
    from crashclouseau.agent import orchestrator, triage

    reports = reports_for(siblings, esc.buildid, esc.channel, esc.product)
    by_proto = {}
    for u, proto in reports:
        by_proto.setdefault(proto or u, []).append(u)
    total = len(reports)
    own_proto = next((p for p, us in by_proto.items() if uuid in us), None)
    stacks = [{
        "uuid": uuid, "protohash": own_proto, "stack": seed.get("stack") or "",
        "share": "{} of the {} ingested report{} on this build share this stack".format(
            len(by_proto.get(own_proto, [uuid])), total, "" if total == 1 else "s")
        if total else None,
    }]
    others = sorted(((p, us) for p, us in by_proto.items() if p != own_proto),
                    key=lambda kv: -len(kv[1]))
    limit = max(0, config.get_agent_spike_escalation()["max_stacks"] - 1)
    for proto, uuids in others[:limit]:
        other = next((u for u in uuids if _has_frames(u)), None)
        if other is None:
            continue
        res, _info = models.CrashStack.get_by_uuid(other)
        frames = ((res or {}).get("frames") or [])[: config.get_agent_max_seed_frames()]
        facts = []
        try:
            raw = inspector.get_crash_data(other)
            facts = triage._crash_facts({
                "raw_crash": raw, "product": esc.product, "version": (raw or {}).get("version"),
                "buildid": esc.buildid})
        except Exception as exc:
            logger.warning("spike: could not fetch %s for the brief: %s", other, exc)
        stacks.append({
            "uuid": other, "protohash": proto, "facts": facts,
            "stack": orchestrator._stack_text(frames) if frames else "",
            "share": "{} of the {} ingested reports share this stack".format(len(uuids), total),
        })
    return stacks


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #
def run_spike_escalation(escalation_id):
    """RQ entrypoint: investigate one escalation and file. Never raises; the row records what
    happened (``done`` with a ``filing``, or ``error`` with the reason)."""
    esc = models.SpikeEscalation.get(escalation_id)
    if esc is None:
        logger.warning("spike: escalation %s does not exist", escalation_id)
        return
    if esc.status not in ("pending", "error"):
        logger.info("spike: escalation %s is %s; not running it again", escalation_id, esc.status)
        return
    from crashclouseau.agent import spike_agent  # lazy: pulls the SDK

    esc.attempts = (esc.attempts or 0) + 1
    esc.set_status("running")
    try:
        brief = build_spike_brief(esc)
        run = asyncio.run(spike_agent.run_spike_agent(brief))
        esc.add_usage(cost_usd=run.total_cost_usd, input_tokens=run.input_tokens,
                      output_tokens=run.output_tokens, cache_read_tokens=run.cache_read_tokens,
                      commit=False)
        findings, dropped = validate_findings(run.findings, brief)
        esc.merge_payload({
            "usage": run.usage(),
            "result": _elide(run.result),
            "run_error": run.error,
            "grounded": run.grounded,
            "findings": findings.model_dump() if findings is not None else None,
            "dropped": dropped,
            "brief": _public_brief(brief),
        })
        logger.info("spike: escalation %s investigated (%s turns, $%.2f, %d tool calls, "
                    "findings=%s)", esc.id, run.num_turns, run.total_cost_usd or 0.0,
                    run.tool_calls, "yes" if findings is not None else "none")
        filing = file_spike_bug(esc, brief, findings, grounded=run.grounded)
        esc.merge_payload({"filing": filing})
        esc.set_status("done")
        if filing.get("filed"):
            logger.info("spike: escalation %s -> bug %s (%s)", esc.id, filing.get("bug"),
                        filing.get("mode"))
        else:
            logger.info("spike: escalation %s not filed -- %s", esc.id, filing.get("skipped"))
    except Exception as exc:
        logger.error("spike: escalation %s failed", escalation_id, exc_info=True)
        try:
            esc.set_status("error", error="{}: {}".format(type(exc).__name__, exc))
        except Exception:  # pragma: no cover - best-effort
            pass


def _public_brief(brief):
    """The parts of the brief worth persisting: what the model was shown, minus the payloads
    that are reproducible from Socorro."""
    cands = brief.get("candidates") or {}
    return {
        "uuid": brief.get("uuid"),
        "stacks": [{"uuid": s.get("uuid"), "share": s.get("share")} for s in brief.get("stacks") or []],
        "classic_runs": brief.get("classic_runs"),
        "onstack": len(cands.get("onstack") or []),
        "window": len(cands.get("window") or []),
        "window_extent": cands.get("window_extent"),
        "is_hang": brief.get("is_hang"),
        "trend_sentence": brief.get("trend_sentence"),
        "version_step": brief.get("version_step"),
        "first_seen_ever": brief.get("first_seen_ever"),
        "first_seen_channel": brief.get("first_seen_channel"),
        "new_signature": spike_report.is_new_signature(brief),
    }


def _elide(text):
    text = text or ""
    if len(text) <= _RAW_HEAD + _RAW_TAIL:
        return text
    return "{}\n\n[... {} chars elided ...]\n\n{}".format(
        text[:_RAW_HEAD], len(text) - _RAW_HEAD - _RAW_TAIL, text[-_RAW_TAIL:])


def _is_node(text):
    return len(text) >= _MIN_NODE and set(text) <= _HEX


def _known_candidate(node, candidate_nodes):
    for known, cand in (candidate_nodes or {}).items():
        if known.startswith(node) or node.startswith(known):
            return cand
    return None


def validate_findings(findings, brief):
    """What the investigator said, checked against what it was given. Returns
    ``(findings, dropped)``: the findings with anything unverifiable removed, and a list of what
    was removed and why (persisted, so a systematic model habit is measurable).

    * a culprit must name a real changeset: one of the brief's candidates (prefix match either
      way), or one hg resolves whose push predates the spiking build -- otherwise it is dropped,
      and the prose keeps whatever the summary said about it;
    * an evidence item with no source is not evidence; it is dropped."""
    if findings is None:
        return None, []
    dropped = []
    culprit = findings.culprit
    in_window = False
    if culprit is not None:
        node = culprit.node
        cand = _known_candidate(node, brief.get("candidate_nodes")) if _is_node(node) else None
        if cand is not None:
            culprit.node = str(cand.get("node") or node)
            if culprit.bug is None and cand.get("bug"):
                culprit.bug = cand["bug"]
            in_window = True
        elif _is_node(node):
            pushdate = _pushdate(node, brief.get("channel"))
            build = utils.get_build_date(brief.get("buildid")) if brief.get("buildid") else None
            if pushdate is None:
                dropped.append("culprit {}: not a changeset hg knows".format(node))
                findings.culprit = None
            elif build is not None and _aware(pushdate) > _aware(build):
                dropped.append("culprit {}: landed after the spiking build".format(node))
                findings.culprit = None
        else:
            dropped.append("culprit {!r}: not a changeset hash".format(node))
            findings.culprit = None
    kept = []
    for item in findings.evidence:
        if item.claim and item.source:
            kept.append(item)
        elif item.claim:
            dropped.append("evidence without a source: {}".format(item.claim[:120]))
    findings.evidence = kept
    brief["culprit_in_window"] = in_window
    return findings, dropped


def _aware(dt):
    if isinstance(dt, datetime):
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return dt


def _pushdate(node, channel):
    """When ``node`` landed, as a tz-aware datetime, or ``None``.

    ``sigage.pushdate_for_node`` answers in hg's own shape, ``[epoch, tzoffset]``, and
    ``validate_findings`` compares the answer to the build's datetime. Handed the list as-is, that
    comparison raised ``TypeError: '>' not supported between instances of 'list' and
    'datetime.datetime'`` -- AFTER Claude Fable 5.1 had run and been paid for -- and the beta
    cookie-WAL escalation of 2026-09-07 (row 3) burned both its attempts on it. The unit tests
    had mocked THIS function with a datetime, so the shape mismatch was never exercised; the
    test now mocks the sigage call underneath."""
    try:
        return sigage.to_datetime(sigage.pushdate_for_node(node, channel or "nightly"))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Filing
# --------------------------------------------------------------------------- #
def _components_of(product):
    """Bugzilla's ACTIVE component names for ``product``, or ``None`` when BMO could not say."""
    if not product:
        return None
    if product in _COMPONENTS_CACHE:
        return _COMPONENTS_CACHE[product]
    names = None
    try:
        import re
        base = re.sub(r"/bug/?$", "", bugzilla_apply._bz_rest())
        r = net.get("{}/product".format(base),
                    params={"names": product,
                            "include_fields": "name,components.name,components.is_active"},
                    timeout=30)
        r.raise_for_status()
        for p in (r.json().get("products") or []):
            if (p.get("name") or "") == product:
                names = {c.get("name") for c in (p.get("components") or [])
                         if c.get("name") and c.get("is_active", True)}
    except Exception:  # noqa: BLE001
        logger.warning("spike: could not read the components of %r", product, exc_info=True)
        names = None
    if names is not None:
        _COMPONENTS_CACHE[product] = names
    return names


def _component_from_signature_bugs(signature, crash_product):
    """The most common product::component among the bugs whose crash-signature field lists this
    signature, open bugs preferred, other applications excluded. ``(None, None)`` when none."""
    foreign = config.get_other_app_products(crash_product)
    try:
        params = {
            "include_fields": "id,product,component,status,resolution,cf_crash_signature",
            "f1": "cf_crash_signature", "o1": "substring", "v1": signature,
        }
        r = net.get(bugzilla_apply._bz_rest(), params=params, timeout=30)
        r.raise_for_status()
        bugs = (r.json() or {}).get("bugs") or []
    except Exception:  # noqa: BLE001
        logger.warning("spike: signature bug lookup failed for %r", signature, exc_info=True)
        return None, None

    def usable(b):
        if not bugzilla_apply._row_is_about(b, signature):
            return False
        return bool(b.get("product") and b.get("component")) and b.get("product") not in foreign

    rows = [b for b in bugs if usable(b)]
    if not rows:
        return None, None
    is_open = [b for b in rows if (b.get("resolution") or "") == ""]
    pool = is_open or rows
    pair, _n = Counter((b["product"], b["component"]) for b in pool).most_common(1)[0]
    return pair


def resolve_component(findings, signature, crash_product):
    """``(product, component, how)``: the investigator's pair when Bugzilla knows it, else the
    signature's existing bugs' pair, else ``Core :: General``. A spike is filed somewhere a human
    can move it from, never left unfiled over a component."""
    foreign = config.get_other_app_products(crash_product)
    if findings is not None and findings.product and findings.component:
        if findings.product in foreign:
            logger.info("spike: the investigator's %s is another application's product",
                        findings.product)
        else:
            components = _components_of(findings.product)
            if components is None:
                return findings.product, findings.component, "investigator (unverified: BMO unreachable)"
            if findings.component in components:
                return findings.product, findings.component, "investigator"
            lowered = {c.lower(): c for c in components}
            if findings.component.lower() in lowered:
                return findings.product, lowered[findings.component.lower()], "investigator"
            logger.info("spike: %s :: %s is not a Bugzilla component", findings.product,
                        findings.component)
    product, component = _component_from_signature_bugs(signature, crash_product)
    if product and component:
        return product, component, "the signature's existing bugs"
    return "Core", "General", "fallback"


def _pick_venue(existing, build_day):
    """Which open bug gets the comment: the newest one filed for this spike (created from the day
    before the spike day on) if any -- somebody is already on it -- else the oldest."""
    if not existing:
        return None
    start = None
    if build_day:
        start = datetime.combine(build_day, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=1)
    recent = []
    for b in existing:
        created = _parse_ts(b.get("creation_time"))
        if start is not None and created is not None and created >= start:
            recent.append((created, b))
    if recent:
        return max(recent, key=lambda t: t[0])[1], True
    return sorted(existing, key=lambda b: b.get("id", 0))[0], False


def _bug_state(bug_id, token):
    """One bug's status, read WITH the filing token: ``{status, resolution, resolved,
    assigned_to}`` or ``None``. Authenticated because the whole point is a bug the public lookup
    could not see -- one a human restricted -- and BMO hides those from anonymous readers."""
    try:
        r = net.get("{}/{}".format(bugzilla_apply._bz_rest(), bug_id),
                    headers={"X-Bugzilla-API-Key": token},
                    params={"include_fields": "id,status,resolution,cf_last_resolved,assigned_to"},
                    timeout=30)
        r.raise_for_status()
        bugs = (r.json() or {}).get("bugs") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("spike: could not read bug %s: %s", bug_id, exc)
        return None
    if not bugs:
        return None
    b = bugs[0]
    return {"id": b.get("id"), "status": b.get("status") or "",
            "resolution": (b.get("resolution") or "").upper(),
            "resolved": sigage.to_datetime(b.get("cf_last_resolved")),
            "assigned_to": b.get("assigned_to") or ""}


def _own_prior_bugs(signatures):
    """The bugs WE filed on these signatures, from the database -- the ordinary filer's
    ``filed_bug`` records and the spike table -- newest spike filing first. Neither lookup may
    raise into the filer; the ordinary one hands back a fail-closed sentinel with no ``bug``."""
    out = []
    try:
        prior = models.SpikeEscalation.prior_bug_for(signatures)
        if prior:
            out.append(prior)
    except Exception:  # pragma: no cover - defensive
        pass
    for sig in signatures:
        try:
            filed = models.Dossier.already_filed_for_signature(sig) or {}
        except Exception:  # pragma: no cover - defensive
            filed = {}
        bug = filed.get("bug") if isinstance(filed, dict) else None
        if bug:
            try:
                out.append(int(bug))
            except (TypeError, ValueError):
                continue
    seen = []
    for b in out:
        if b not in seen:
            seen.append(b)
    return seen


def resolve_venue_below_public(signatures, product, buildid, token):
    """Where a spike goes when no OPEN public bug on the signature exists, in order:

    1. a bug WE filed on the signature (any channel) that is still open but invisible to the
       public lookup -- a human restricted it -- gets the comment (``kind`` ``own_restricted``);
    2. a bug we filed that was RESOLVED FIXED after the spiking build was produced gets it: the
       spike is on builds without the fix (``kind`` ``fixed``);
    3. any public same-application bug RESOLVED FIXED after the build, the ordinary filer's
       ``_fixed_after_build_bug`` question, gets it the same way.

    ``None`` means a new bug: no bug, a bug resolved before the build (its fix is in the build,
    so this is a new defect or a fix that did not hold) or one closed INVALID / WORKSFORME /
    DUPLICATE, which say nothing about whether the crash is still happening."""
    build_dt = sigage.to_datetime(str(buildid)) if buildid else None
    for bug in _own_prior_bugs(signatures):
        state = _bug_state(bug, token)
        if not state:
            continue
        if not state["resolution"]:
            return {"id": bug, "kind": "own_restricted", "assigned_to": state["assigned_to"]}
        if state["resolution"] == "FIXED" and build_dt is not None and state["resolved"] is not None \
                and _aware(state["resolved"]) > _aware(build_dt):
            return {"id": bug, "kind": "fixed", "resolved": state["resolved"],
                    "assigned_to": state["assigned_to"]}
    if build_dt is None:
        return None
    try:
        fixed = bugzilla_apply._fixed_bugs_about(signatures[0], product)
    except Exception:  # pragma: no cover - the lookup already swallows
        fixed = []
    for bug, resolved in fixed:
        if resolved is not None and _aware(resolved) > _aware(build_dt):
            return {"id": bug["id"], "kind": "fixed", "resolved": resolved,
                    "assigned_to": bug.get("assigned_to") or ""}
    return None


def _needinfo_person_for(findings, brief):
    culprit = findings.culprit if findings is not None else None
    if culprit is None or not culprit.node:
        return {}
    channel = brief.get("channel")
    candidate = {"node": culprit.node, "bug": culprit.bug}
    try:
        person = report_bug._needinfo_person(candidate, channel)
        if not person:
            info = sigage.json_rev(culprit.node, channel or "nightly") or {}
            if info.get("user"):
                person = report_bug._needinfo_person(
                    dict(candidate, author=info["user"]), channel)
        return person or {}
    except Exception:
        logger.warning("spike: could not resolve the culprit's author", exc_info=True)
        return {}


def file_spike_bug(esc, brief, findings, grounded=True):
    """File the spike -- a comment on the open bug about this signature, or a new bug -- and
    return what happened. NEVER raises. ``retry: True`` marks a decline that may clear later."""
    channel = esc.channel
    product = esc.product
    signature = (esc.signature or "").strip()
    cfg = config.get_agent_spike_escalation()
    now = datetime.now(timezone.utc)
    result = {"filed": False, "at": now.isoformat(), "signature": signature, "channel": channel,
              "buildid": esc.buildid, "uuid": esc.uuid}
    if not config.autofile_globally_enabled():
        return dict(result, skipped="autofile disabled")
    token = config.get_bugzilla_token()
    if not token:
        return dict(result, skipped="no Bugzilla API token configured")
    if bugzilla_apply._is_unsymbolicated(signature):
        return dict(result, skipped="signature is unsymbolicated ({})".format(signature))
    filed_today = models.SpikeEscalation.count_since(
        product, channel, now - timedelta(days=1), filed_only=True)
    if filed_today >= cfg["daily_cap"]:
        return dict(result, retry=True,
                    skipped="daily cap {} reached on {}".format(cfg["daily_cap"], channel))
    raw = brief.get("raw_crash") or {}
    try:
        signals = sensitive.memory_unsafe_signals(raw) if raw else []
    except Exception:
        signals = []
    withheld = bool(signals)
    existing = bugzilla_apply._open_bugs_for_signature(signature)
    if existing is None:
        return dict(result, retry=True, skipped="signature lookup failed; not risking a duplicate")
    existing, other_app = bugzilla_apply._split_by_application(existing, product)
    existing, meta_bugs = bugzilla_apply._split_out_metas(existing)
    mode = cfg["comment_on_existing"]
    venue = for_spike = None
    related = []
    if existing:
        venue, for_spike = _pick_venue(existing, esc.build_day)
    if venue is not None and mode == "skip" and not withheld:
        return dict(result, skipped="open bug {} exists".format(venue["id"]))
    if venue is not None and mode == "file_new":
        related = sorted(b["id"] for b in existing)
        venue = None
    public_venue_declined = None
    if venue is not None and withheld:
        public_venue_declined, venue = venue["id"], None
    if venue is not None and for_spike and venue.get("regressed_by"):
        return dict(result, bug=venue["id"], skipped=(
            "bug {} was filed for this spike and already names its regressor ({})".format(
                venue["id"], ", ".join("bug {}".format(b) for b in venue["regressed_by"]))))
    # BELOW THE PUBLIC OPEN BUGS: a bug we filed ourselves that a human restricted or resolved,
    # or anybody's bug fixed AFTER this build -- see `resolve_venue_below_public`. Not in `skip`
    # mode (nothing is written on an existing bug there), and a memory-safety crash declines a
    # public fixed bug the way it declines a public open one.
    venue_kind = "open" if venue is not None else None
    preface = None
    if venue is None and mode != "skip":
        siblings = brief.get("siblings") or [signature]
        below = resolve_venue_below_public(siblings, product, esc.buildid, token)
        if below is not None and withheld and below["kind"] != "own_restricted":
            public_venue_declined = public_venue_declined or below["id"]
        elif below is not None:
            venue = {"id": below["id"], "assigned_to": below.get("assigned_to") or ""}
            venue_kind = below["kind"]
            for_spike = False
            if below["kind"] == "fixed":
                preface = spike_report.fixed_venue_note(
                    below["id"], below.get("resolved"), esc.buildid, channel)
    person = _needinfo_person_for(findings, brief) if grounded else {}
    if not person and venue is not None and venue.get("assigned_to"):
        # Nobody to ask about a culprit: the bug's own assignee is the human who knows the fix.
        try:
            person = report_bug._person_for_account(venue["assigned_to"]) or {}
        except Exception:  # pragma: no cover - a BMO read inside the ladder
            person = {}
    link_regressor = bool(brief.get("culprit_in_window")) and grounded
    if venue is None:
        bz_product, component, how = resolve_component(findings, signature, product)
    else:
        bz_product, component, how = None, None, "existing bug"
    try:
        details = report_bug.fetch_crash_reason(esc.uuid) if esc.uuid else {}
    except Exception:
        details = {}
    stack = brief.get("stack_frames") or {}
    email = ""
    try:
        if venue is not None:
            text = spike_report.build_spike_comment(
                brief, findings, details=details, stack=stack, person=person,
                author_display=report_bug._person_display(person) if person else None,
                link_regressor=link_regressor, grounded=grounded, as_comment=True,
                preface=preface)
            bugzilla_apply._post_comment(venue["id"], text, False, token)
            email = (person or {}).get("account") or ""
            outcome = bugzilla_apply._set_needinfo(venue["id"], email, token) if email else None
            result.update({"filed": True, "bug": venue["id"], "mode": "spike_comment",
                           "venue_kind": venue_kind, "venue_for_spike": bool(for_spike),
                           "needinfo": None if isinstance(outcome, Exception) else (email or None)})
        else:
            preview = spike_report.build_spike_preview(
                brief, findings, product=bz_product, component=component, person=person,
                details=details, stack=stack, link_regressor=link_regressor, grounded=grounded,
                related_bugs=related or None, other_app_bugs=other_app or None,
                meta_bugs=meta_bugs or None, withhold=withheld)
            if withheld and not preview.get("groups"):
                return dict(result, skipped=(
                    "memory-safety crash and no security group for product {!r}".format(bz_product)))
            if public_venue_declined is not None:
                preview["comment"] = (
                    "{}\n\n_Probably a duplicate of bug {}, which is on this same signature. "
                    "This bug was filed separately, and restricted, because the crash report shows "
                    "a memory-safety fault and that bug is public._".format(
                        preview["comment"], public_venue_declined))
            email = preview.get("needinfo_email") if config.get_agent_autofile(channel)["needinfo"] else ""
            payload = {k: v for k, v in preview.items()
                       if k in ("product", "component", "version", "type", "keywords",
                                "cf_crash_signature", "groups", "cc")}
            for k in ("groups", "cc"):
                if not payload.get(k):
                    payload.pop(k, None)
            payload["summary"] = preview["title"]
            payload["description"] = preview["comment"]
            if email:
                payload["flags"] = [{"name": "needinfo", "status": "?", "requestee": email}]
            bug_id, dropped = bugzilla_apply._create_bug_keeping_the_bug(payload, token)
            if dropped:
                result["needinfo_dropped"] = email
                email = ""
            linked = bugzilla_apply._link_blockers(bug_id, preview.get("blocked") or [], token)
            result.update({"filed": True, "bug": bug_id, "mode": "spike_new_bug",
                           "product": bz_product, "component": component, "component_from": how,
                           "needinfo": email or None, "blocks": linked,
                           "keywords": preview.get("keywords")})
            regressors = preview.get("regressed_by") or []
            if regressors:
                result["regressed_by"] = bugzilla_apply._link_regressed_by(bug_id, regressors, token)
            flag = preview.get("tracking_flag")
            if flag:
                result["tracking_nominated" if bugzilla_apply._nominate_tracking(bug_id, flag, token)
                       else "tracking_failed"] = flag
            if public_venue_declined is not None:
                result["public_venue_declined"] = public_venue_declined
            if related:
                result["related_bugs"] = related
            if withheld:
                result["security_groups"] = preview.get("groups") or []
                result["memory_unsafe_signals"] = signals
    except Exception as exc:
        logger.error("spike: Bugzilla write failed for escalation %s: %s", esc.id, exc)
        return dict(result, skipped="bugzilla write failed: {}".format(exc), error=str(exc)[:500])
    logger.info("spike: escalation %s -> bug %s (%s, needinfo=%s)", esc.id, result.get("bug"),
                result.get("mode"), result.get("needinfo"))
    return result


def _retry_filings(cfg):
    """Re-attempt the filings the sweep's earlier ticks could not make for a reason that may have
    cleared (a failed venue lookup, the daily cap), without paying for the investigator again."""
    from crashclouseau.agent.spike_agent import SpikeFindings

    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg["lookback_days"] + 2)
    try:
        rows = (
            db.session.query(models.SpikeEscalation)
            .filter(models.SpikeEscalation.status == "done",
                    models.SpikeEscalation.created >= cutoff)
            .all()
        )
    except Exception:
        logger.error("spike: cannot list escalations for filing retries", exc_info=True)
        return 0
    retried = 0
    for esc in rows:
        payload = esc.payload or {}
        filing = payload.get("filing") or {}
        if not filing.get("retry") or filing.get("filed"):
            continue
        attempts = int(filing.get("attempts") or 1)
        if attempts >= _FILING_RETRIES or not esc.uuid:
            continue
        if esc.updated and (datetime.now(timezone.utc) - _aware(esc.updated)).total_seconds() < _FILING_RETRY_AFTER_S:
            continue
        try:
            findings = (SpikeFindings.model_validate(payload["findings"])
                        if payload.get("findings") else None)
            brief = build_spike_brief(esc, light=True)
            brief["culprit_in_window"] = bool(payload.get("culprit_in_window"))
            result = file_spike_bug(esc, brief, findings, grounded=bool(payload.get("grounded")))
        except Exception as exc:
            logger.error("spike: filing retry failed for escalation %s", esc.id, exc_info=True)
            result = {"filed": False, "skipped": "retry failed: {}".format(exc)}
        result["attempts"] = attempts + 1
        esc.merge_payload({"filing": result})
        retried += 1
        logger.info("spike: filing retry %d for escalation %s -> %s", attempts + 1, esc.id,
                    "bug {}".format(result.get("bug")) if result.get("filed") else result.get("skipped"))
    return retried
