# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Recorded-action apply/replay step + evidence-view policy (#12).

The evidence agent only *records* Bugzilla intents through the vendored ``actions``
MCP server (hackbot ``ActionsRecorder`` shape: ``{type, params, reasoning}``) — it
never touches Bugzilla. hackbot ships **no apply step**; Clouseau builds it here.

This module has two jobs:

* ``build_evidence(uuid)`` composes the persisted verdict/dossier/actions with the
  UI/apply policy (``can_apply`` gate + which recorded-action indices are
  apply-eligible) for the read-only panel and ``/api/evidence``. It writes nothing.

* ``apply_recorded_actions(uuid, indices)`` executes the human-confirmed subset of
  recorded actions via Bugzilla REST. It is the ONLY place in the product that
  writes to Bugzilla, and only ever from the human-confirmed POST route. It
  re-reads the persisted actions (never trusts a client-supplied action body — the
  client sends indices only), refuses any ``type`` outside ``apply.enabled_types``,
  skips already-applied actions (idempotent), and records the outcome so a partial
  failure leaves landed writes marked applied.
"""
from __future__ import annotations

import libmozdata.config
import requests

from crashclouseau import config, models
from crashclouseau.logger import logger

# Recorded actions the apply step knows how to execute directly. ``create_bug`` is
# intentionally NOT here: new bugs stay human-filed through the report_bug draft.
_EXECUTABLE = {"bugzilla.add_comment", "bugzilla.update_bug"}

_BZ_REST = "https://bugzilla.mozilla.org/rest/bug"
_HTTP_TIMEOUT = 60


# --------------------------------------------------------------------------- #
# Read-only evidence view + apply policy
# --------------------------------------------------------------------------- #
def applicable_indices(actions, ui):
    """Indices of recorded actions the apply route is *allowed* to execute now:
    type in ``enabled_types`` and not already applied."""
    enabled = set(ui.get("enabled_types") or [])
    return [
        i
        for i, a in enumerate(actions or [])
        if (a or {}).get("type") in enabled and not (a or {}).get("applied_at")
    ]


def _confidence_ok(confidence, ui):
    return confidence is not None and confidence >= ui.get("apply_min_confidence", 85)


def _apply_eligible(verdict, confidence, ui):
    """Apply is allowed for a high-confidence culprit (>= apply_min) OR a lead at/above
    the lower lead threshold (>= lead_apply_min, #15 phase 4). Abstain is never
    eligible. The human still has to confirm — this only gates whether the control is
    offered / a POST is accepted."""
    if verdict == "culprit":
        return _confidence_ok(confidence, ui)
    if verdict == "lead":
        return confidence is not None and confidence >= ui.get(
            "lead_apply_min_confidence", 50
        )
    return False


def build_evidence(uuid):
    """Verdict/dossier/actions + UI/apply policy for one UUID, or ``None`` when no
    verdict row exists (panel hidden). Read-only."""
    ev = models.Verdict.get_evidence(uuid)
    if ev is None:
        return None
    ui = config.get_agent_ui()
    idxs = applicable_indices(ev.get("actions"), ui)
    ev["ui"] = ui
    ev["apply_indices"] = idxs
    ev["can_apply"] = bool(
        _apply_eligible(ev.get("verdict"), ev.get("confidence"), ui) and idxs
    )
    return ev


# --------------------------------------------------------------------------- #
# Bugzilla REST writes (the ONLY place the product writes to Bugzilla)
# --------------------------------------------------------------------------- #
def _post_comment(bug_id, text, is_private, token):
    """POST /rest/bug/<id>/comment -> new comment id."""
    r = requests.post(
        "{}/{}/comment".format(_BZ_REST, bug_id),
        headers={"X-Bugzilla-API-Key": token},
        json={"comment": text, "is_private": bool(is_private)},
        timeout=_HTTP_TIMEOUT,
    )
    r.raise_for_status()
    return (r.json() or {}).get("id")


def _put_bug(bug_id, changes, token):
    """PUT /rest/bug/<id> with the recorded ``changes`` (this is how the recorded
    needinfo flag gets set, from ``changes.flags``) -> the bug id on success."""
    if not changes:
        raise ValueError("update_bug action has no changes to apply")
    r = requests.put(
        "{}/{}".format(_BZ_REST, bug_id),
        headers={"X-Bugzilla-API-Key": token},
        json=changes,
        timeout=_HTTP_TIMEOUT,
    )
    r.raise_for_status()
    return bug_id


def _execute(action, token):
    atype = action.get("type")
    params = action.get("params") or {}
    bug_id = params.get("bug_id")
    if not bug_id:
        raise ValueError("action missing bug_id")
    if atype == "bugzilla.add_comment":
        return _post_comment(
            bug_id, params.get("text", ""), params.get("is_private", False), token
        )
    if atype == "bugzilla.update_bug":
        return _put_bug(bug_id, params.get("changes") or {}, token)
    raise ValueError("unsupported action type: {}".format(atype))


def apply_recorded_actions(uuid, indices):
    """Execute the human-confirmed subset of recorded actions for ``uuid``.

    ``indices`` are positions into the persisted ``payload["actions"]`` list. The
    persisted action body is re-read here — a client only supplies indices. Returns
    a per-action result list ``[{index, type, ok, result_id|error|skipped}]``;
    already-applied actions are skipped (idempotent). Never raises for a single bad
    action; the whole call raises only when the UUID has no persisted dossier.
    """
    ui = config.get_agent_ui()
    enabled = set(ui.get("enabled_types") or [])
    token = libmozdata.config.get("Bugzilla", "token", "")

    # Re-read the persisted verdict + actions (never trust the client — it sends only
    # indices). ``get_evidence`` sources the actions from ``Dossier.payload["actions"]``.
    ev = models.Verdict.get_evidence(uuid)
    if ev is None:
        raise LookupError("no verdict for uuid {}".format(uuid))
    actions = ev.get("actions") or []

    # De-duplicate indices (order-preserving): the local ``actions`` snapshot is not
    # refreshed after mark_action_applied writes to the DB, so a repeated index in the
    # same request would otherwise re-execute (double-post) the same action.
    seen = set()
    indices = [i for i in indices if not (i in seen or seen.add(i))]

    # Server-side authorization gate (defense-in-depth — the UI only *hides* the apply
    # control; a hand-crafted POST must not bypass it). The apply path executes ONLY for
    # a high-confidence culprit or a lead at/above the lead threshold; an abstain or
    # below-threshold UUID is refused outright, before any write.
    if not _apply_eligible(ev.get("verdict"), ev.get("confidence"), ui):
        return [
            {
                "index": i,
                "ok": False,
                "error": "verdict not eligible for apply "
                         "(requires a high-confidence culprit or a lead)",
            }
            for i in indices
        ]

    results = []
    for index in indices:
        if not isinstance(index, int) or index < 0 or index >= len(actions):
            results.append({"index": index, "ok": False, "error": "no such action"})
            continue
        action = actions[index] or {}
        atype = action.get("type")

        if action.get("applied_at"):
            results.append(
                {
                    "index": index,
                    "type": atype,
                    "ok": True,
                    "result_id": action.get("result_id"),
                    "skipped": "already applied",
                }
            )
            continue

        if atype == "bugzilla.create_bug":
            # New bugs stay human-filed: route back to the report_bug draft.
            results.append(
                {
                    "index": index,
                    "type": atype,
                    "ok": True,
                    "draft_url": "/bug.html?uuid={}".format(uuid),
                    "note": "new bug — open the draft and submit it yourself",
                }
            )
            continue

        if atype not in enabled or atype not in _EXECUTABLE:
            results.append(
                {
                    "index": index,
                    "type": atype,
                    "ok": False,
                    "error": "action type not enabled for apply: {}".format(atype),
                }
            )
            continue

        if not token:
            results.append(
                {
                    "index": index,
                    "type": atype,
                    "ok": False,
                    "error": "no Bugzilla API token configured (set [Bugzilla] token)",
                }
            )
            continue

        try:
            result_id = _execute(action, token)
        except Exception as exc:
            logger.error("apply: action #%s (%s) failed: %s", index, atype, exc)
            results.append(
                {"index": index, "type": atype, "ok": False, "error": str(exc)}
            )
            continue

        # Mark applied even on a later action's failure, so a retry never re-posts.
        models.Dossier.mark_action_applied(uuid, index, result_id)
        results.append(
            {"index": index, "type": atype, "ok": True, "result_id": result_id}
        )

    return results
