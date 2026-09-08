# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Analyse one crash on request: the back end of ``POST /api/tasks/trigger``.

The pipeline chooses what to analyse (the selector, the rate path, the spike sweep). This is
the operator's way past that choice for ONE crash -- or a short list -- with the two decisions
the pipeline otherwise makes for them: whether the run may write to Bugzilla (``file_bug``) and
whether it shows on tasks.html (``show_in_tasks``). Both ride in the dossier payload under
``run_options`` (``Dossier.set_run_options``), STICKY across the run's own settle write, the
reaper and a later retrigger click, so the run itself needs no new argument: ``autofile_bug``
reads the first, ``Dossier.list_tasks`` the second.

A uuid the pipeline never ingested is fetched from Socorro and put through the same scoring as
a selected crash (``update.put_report``), forced past the proto-signature and stack dedups --
this is one explicit crash somebody asked for. Its build has to be in the ``builds`` table:
outside the ingested window there is no pushlog to score against and the run would have nothing
to say, so that is refused with the reason rather than run blind.

Born 2026-09-08, when "can you trigger an analysis of SplitSingleCharHelper out of curiosity"
needed a one-off dyno with ``AUTOFILE_BUGS=0`` in its environment and a hand-rolled ingest.
"""
import re
from datetime import datetime, timezone

from . import config, inspector, models, tools, update, utils
from .logger import logger

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class TriggerError(Exception):
    """A reason this crash cannot be analysed, for the caller: not a bug in the caller."""


def _search_names(label):
    names = utils.get_search_channel(label)
    return list(names) if isinstance(names, (list, tuple)) else [names]


def _major(version):
    m = re.match(r"\s*(\d+)", str(version or ""))
    return int(m.group(1)) if m else None


def channel_label(socorro_channel, version=None):
    """Our channel LABEL for a Socorro ``release_channel``, or ``None`` when no configured
    channel covers it. ``aurora`` is beta's second name; Socorro's one ``esr`` is shared by
    every ESR line, and the line is told from the version's major (``153.1.0esr`` -> ``esr153``)."""
    ch = (socorro_channel or "").lower()
    if not ch:
        return None
    major = _major(version)
    for lab in config.get_channels():
        if ch not in _search_names(lab):
            continue
        line = config.esr_major(lab)
        # An ESR LINE label covers one major only -- also when it is the only line configured:
        # a 115.x crash is not an esr153 crash because esr153 is the one line we run.
        if line is None or line == major:
            return lab
    return None


def ingest(uuid):
    """Put one crash the pipeline never selected into the database, scored, ready for a run.
    Returns ``{channel, signature, buildid}``; raises ``TriggerError`` with the reason it
    cannot be done."""
    try:
        data = inspector.get_crash_data(uuid)
    except Exception as exc:  # noqa: BLE001 - Socorro's failure IS the reason
        raise TriggerError("Socorro has no processed crash for {}: {}".format(uuid, exc))
    if not isinstance(data, dict) or not data.get("build") or not data.get("signature"):
        raise TriggerError("Socorro's processed crash for {} has no build or no signature".format(uuid))
    product = data.get("product")
    if product not in config.get_products():
        raise TriggerError("product {!r} is not configured".format(product))
    channel = channel_label(data.get("release_channel"), data.get("version"))
    if channel is None:
        raise TriggerError("channel {!r} (version {!r}) is not one of the configured channels".format(
            data.get("release_channel"), data.get("version")))
    buildid = str(data["build"])
    bid = utils.get_build_date(buildid)
    bidid = models.Build.get_id(bid, channel, product)
    if bidid is None:
        raise TriggerError(
            "build {} is not in the builds table for {}/{}: only a build inside the ingested "
            "window has a pushlog to score against".format(buildid, product, channel))
    signature = data["signature"]
    sgnid = models.Signature.get_id(signature)
    proto = data.get("proto_signature") or signature
    # `force`: this uuid gets its own row even when a same-proto sibling exists on the build.
    models.UUID.add(uuid, sgnid, proto, bidid, force=True)
    chgset = tools.get_changeset(bid, channel, product)
    # `enqueue=False`: the run is enqueued by the caller, FORCED and with its options recorded
    # first; an ordinary enqueue here would race it with a run that has neither.
    scored = update.put_report(uuid, bid, channel, product, chgset, signature,
                               enqueue=False, force=True)
    if not scored:
        raise TriggerError("no usable stack for {} (Socorro has no json_dump for it)".format(uuid))
    return {"channel": channel, "signature": signature, "buildid": buildid}


def trigger_one(uuid, file_bug=False, show_in_tasks=True):
    """Analyse *uuid*: ingest it if the pipeline never did, record the run's options, and
    enqueue a forced run (``retrigger_agent``: stops a running one first). Never raises; the
    returned dict says what happened."""
    from crashclouseau.agent import orchestrator  # lazy: pulls the SDK into the web process

    out = {"uuid": uuid, "ok": False, "ingested": False}
    if not isinstance(uuid, str) or not _UUID_RE.match(uuid):
        out["error"] = "not a crash uuid"
        return out
    try:
        if models.UUID.exists(uuid):
            out["channel"] = models.UUID.get_channel(uuid)
            out["signature"] = models.UUID.get_signature(uuid)
        else:
            info = ingest(uuid)
            out.update(ingested=True, channel=info["channel"], signature=info["signature"])
        options = {
            "autofile": bool(file_bug),
            "show_in_tasks": bool(show_in_tasks),
            "requested": datetime.now(timezone.utc).isoformat(),
            "source": "api",
        }
        models.Dossier.set_run_options(uuid, options)
        res = orchestrator.retrigger_agent(uuid, channel=out.get("channel"))
        out.update(ok=True, action="queued", run_options=options,
                   cancelled_running=bool(res.get("cancelled")),
                   already_filed=res.get("already_filed"))
        logger.info("trigger: %s queued (file_bug=%s, show_in_tasks=%s, ingested=%s)",
                    uuid, file_bug, show_in_tasks, out["ingested"])
    except TriggerError as exc:
        out["error"] = str(exc)
        _rollback()
    except Exception as exc:  # noqa: BLE001 - one bad uuid must not fail the batch
        logger.error("trigger: %s failed", uuid, exc_info=True)
        out["error"] = "{}: {}".format(type(exc).__name__, exc)
        _rollback()
    return out


def trigger_many(uuids, file_bug=False, show_in_tasks=True):
    return [trigger_one(u, file_bug=file_bug, show_in_tasks=show_in_tasks) for u in uuids]


def _rollback():
    try:
        models.db.session.rollback()
    except Exception:  # pragma: no cover - best-effort
        pass
