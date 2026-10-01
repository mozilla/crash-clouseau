# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Record links across signatures using candidate nodes and native stack sites.

Links require a shared 12-hex candidate prefix and matching function/file/line keys.
``site`` uses the first specific frame; ``site_skip1`` may skip one without a file or
positive line. Watchdog and ``IPCError-*`` signatures are excluded.

Store up to 20 links and their recorded filings in ``payload['same_site']``.
These records do not write to Bugzilla or establish that crashes share a defect.
"""
import re
from datetime import datetime, timezone

from crashclouseau import config, db, hang, models, sigfamily, utils
from crashclouseau.logger import logger

NODE_LEN = 12
KEYS = ("site", "site_skip1")
_MAX_LINKS = 20
_NODE_RE = re.compile(r"[0-9a-f]{%d}" % NODE_LEN)


def eligible(signature):
    """Exclude empty, watchdog and ``IPCError-*`` signatures."""
    fr = sigfamily.frames(signature)
    if not fr:
        return False
    return not utils.is_watchdog_crash(signature=signature) and not fr[0].startswith("IPCError-")


def crash_site(frames, max_skip=0):
    """Return a specific frame's function, file and positive line, or ``None``.

    Skip at most *max_skip* specific frames lacking a file or positive line.
    Native ingestion defaults missing lines to -1.
    """
    skipped = 0
    for f in frames:
        name = hang.clean_symbol(f.get("function"))
        if not name or not sigfamily.specific_frame(name):
            continue
        if f.get("filename") and (f.get("line") or 0) > 0:
            return {"function": sigfamily.normalize_frame(name), "file": f["filename"],
                    "line": int(f["line"])}
        skipped += 1
        if skipped > max_skip:
            return None
    return None


def sites(uuid, signature):
    """Return both site keys for *uuid*, using ``None`` when unavailable."""
    if not eligible(signature):
        return dict.fromkeys(KEYS)
    frames = models.CrashStack.native_frames(uuid)
    return {"site": crash_site(frames), "site_skip1": crash_site(frames, max_skip=1)}


def find_links(uuid, signature, node):
    """Build a record of matching runs under other signatures, capped at ``_MAX_LINKS``.

    Return ``None`` for an invalid node prefix, an excluded signature or a failed run query.
    """
    node = str(node or "").strip().lower()[:NODE_LEN]
    if not _NODE_RE.fullmatch(node) or not eligible(signature):
        return None
    mine = sites(uuid, signature)
    record = {"node": node, **mine, "links": []}
    if not any(mine.values()):
        return record
    runs = models.Dossier.runs_for_candidate(node)
    spikes = models.SpikeEscalation.runs_for_culprit(node)
    if runs is None or spikes is None:
        return None
    cache = {}
    links = []
    for run in runs + spikes:
        other = run["uuid"]
        if not other or other == uuid or run["signature"] == signature:
            continue
        if other not in cache:
            cache[other] = sites(other, run["signature"])
        via = [k for k in KEYS if mine[k] and mine[k] == cache[other][k]]
        if via:
            links.append(dict(run, via=via))
    record["links"] = links[:_MAX_LINKS]
    if len(links) > _MAX_LINKS:
        record["links_dropped"] = len(links) - _MAX_LINKS
    return record


def _build(uuid, signature, node):
    if not config.get_agent_same_site()["enabled"]:
        return None
    try:
        record = find_links(uuid, signature, node)
    except Exception:
        logger.error("same_site: lookup failed for %s", uuid, exc_info=True)
        db.session.rollback()
        return None
    if record is not None:
        record["at"] = datetime.now(timezone.utc).isoformat()
    return record


def record_for_dossier(uuid, signature, node):
    """Attempt to store the dossier's links; return the record or ``None``."""
    record = _build(uuid, signature, node)
    if record is None:
        return None
    try:
        models.Dossier.merge_payload(uuid, {"same_site": record})
    except Exception:
        logger.error("same_site: cannot store the record for %s", uuid, exc_info=True)
        db.session.rollback()
        return None
    return record


def record_for_spike(esc, node):
    """Attempt to store the escalation's links; return the record or ``None``."""
    if not esc.uuid:
        return None
    record = _build(esc.uuid, esc.signature, node)
    if record is None:
        return None
    try:
        esc.merge_payload({"same_site": record})
    except Exception:
        logger.error("same_site: cannot store the record for escalation %s", esc.id,
                     exc_info=True)
        db.session.rollback()
        return None
    return record
