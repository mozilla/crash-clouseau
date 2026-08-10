# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""What became of the bugs we filed, pulled back from Bugzilla.

The pipeline has always recorded what it DID (``Dossier.payload["filed_bug"]``) and never what
came of it, so "how often are we right?" has only ever been answerable by hand — and the answer
that exists (~28% of leads name the true regressor, CI 0.13-0.50) came from a one-off spike, not
from the product's own output.

Two things make this cheap rather than a labelling project:

* the bug ids are already ours, in ``filed_bug``;
* the correction is STRUCTURED. A reviewer who rejects our attribution sets BMO's own
  ``regressed_by``. Bug 2062119 carries ``regressed_by: [1412726]`` against the ``1768581`` we
  named, so "were we right?" is a comparison, not an inference from prose.

Read-only and unauthenticated: everything needed is public on a bug we filed publicly.

The reason this module exists at all is ``models.Archetype``. A learned rule is a guess until
its firings can be scored against outcomes, and ``Feedback.archetypes`` is what makes that join
possible.
"""
from datetime import datetime, timezone

from crashclouseau import models, net
from crashclouseau.logger import logger

_BZ_REST = "https://bugzilla.mozilla.org/rest/bug"
_HTTP_TIMEOUT = 60
# BMO takes many `id=` params happily, but a URL has limits and a partial failure should cost
# one batch rather than the sweep.
_BATCH = 50
_FIELDS = "id,status,resolution,dupe_of,regressed_by"


def _filed_bugs():
    """``[{bug_id, uuid, named_bug, named_node, archetypes, filed_at}]`` for every bug the
    pipeline has filed, newest last. Sourced from the dossiers, which are the only record."""
    out = []
    for row in models.Dossier.filed_bug_rows():
        info = row.get("filed_bug") or {}
        bug_id = info.get("bug")
        if not bug_id:
            continue
        dossier = row.get("dossier") or {}
        candidate = dossier.get("candidate") or {}
        corrob = dossier.get("corroborations") or {}
        out.append({
            "bug_id": int(bug_id),
            "uuid": info.get("uuid") or row.get("uuid"),
            "named_bug": candidate.get("bug"),
            "named_node": candidate.get("node"),
            "archetypes": corrob.get("archetypes") or [],
            "filed_at": info.get("at"),
        })
    return out


def _fetch(bug_ids):
    """``{bug_id -> bug dict}`` from BMO for these ids. Missing ids simply do not appear — a
    restricted bug is ABSENT from the query form rather than an error."""
    out = {}
    for start in range(0, len(bug_ids), _BATCH):
        chunk = bug_ids[start:start + _BATCH]
        try:
            r = net.get(_BZ_REST,
                        params={"include_fields": _FIELDS, "id": [str(b) for b in chunk]},
                        timeout=_HTTP_TIMEOUT)
            r.raise_for_status()
            for bug in (r.json() or {}).get("bugs") or []:
                out[int(bug["id"])] = bug
        except Exception as exc:                            # pragma: no cover - network
            logger.warning("feedback: lookup failed for %s: %s", chunk, exc)
    return out


def _as_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def refresh():
    """Pull the current state of every filed bug into ``models.Feedback``. Returns a summary.

    Idempotent, and safe to run on a schedule: a row is keyed by bug id and simply re-stamped.
    A bug BMO did not return is left exactly as it was rather than being blanked — an
    unreadable bug (restricted, or a lookup that failed) must not erase a verdict we already
    recorded about it."""
    filed = _filed_bugs()
    fetched = _fetch([f["bug_id"] for f in filed])
    updated = 0
    for entry in filed:
        bug = fetched.get(entry["bug_id"])
        if bug is None:
            continue
        models.Feedback.record(
            entry["bug_id"],
            uuid=entry["uuid"],
            named_bug=entry["named_bug"],
            named_node=entry["named_node"],
            archetypes=entry["archetypes"],
            filed_at=_as_datetime(entry["filed_at"]),
            status=bug.get("status"),
            resolution=bug.get("resolution") or None,
            dupe_of=bug.get("dupe_of"),
            regressed_by=bug.get("regressed_by") or [],
        )
        updated += 1
    summary = {"filed": len(filed), "fetched": len(fetched), "updated": updated,
               **models.Feedback.scoreboard()}
    logger.info("feedback: refreshed %s of %s filed bugs -> %s",
                updated, len(filed), summary["by_attribution"])
    return summary
