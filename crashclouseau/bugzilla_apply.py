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
  recorded actions via Bugzilla REST. It re-reads the persisted actions (never trusts a
  client-supplied action body — the client sends indices only), refuses any ``type``
  outside ``apply.enabled_types``, skips already-applied actions (idempotent), and
  records the outcome so a partial failure leaves landed writes marked applied.

* ``autofile_bug(...)`` files a bug for a reported crash with NO human in the loop —
  the one unattended write in the product. Every gate lives in that function and each
  fails closed; ``AUTOFILE_BUGS`` is its kill-switch.

This module remains the ONLY place in the product that writes to Bugzilla.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

from . import net

from crashclouseau import config, models
from crashclouseau.agent import schema
from crashclouseau.logger import logger

# Recorded actions the human-confirmed APPLY step executes directly. ``create_bug`` is
# still intentionally NOT here: a bug the MODEL asked for, replayed from a recorded action
# body, stays human-filed through the report_bug draft. Automatic filing goes through
# ``autofile_bug`` instead, which builds its own payload from the persisted dossier and
# applies its own gates — the model never chooses what gets filed.
_EXECUTABLE = {"bugzilla.add_comment", "bugzilla.update_bug"}

_BZ_REST = "https://bugzilla.mozilla.org/rest/bug"
_HTTP_TIMEOUT = 60


def _bz_rest():
    """The Bugzilla REST base these writes target. Overridable with ``BUGZILLA_REST_URL``
    so the write path can be exercised against staging (bugzilla.allizom.org) with no way
    for a test run to reach production BMO."""
    return os.getenv("BUGZILLA_REST_URL", _BZ_REST)


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
    # `rationale` is rendered verbatim on crashstack.html, and for a validation-failure
    # abstain it used to be a raw pydantic dump — input_value reprs, errors.pydantic.dev
    # links, the lot. Rewriting it HERE rather than only at the point it is produced also
    # repairs the ~118 dossiers that already have one persisted.
    ev["rationale"] = schema.humanize_validation_reason(ev.get("rationale"))
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
    r = net.post(
        "{}/{}/comment".format(_bz_rest(), bug_id),
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
    r = net.put(
        "{}/{}".format(_bz_rest(), bug_id),
        headers={"X-Bugzilla-API-Key": token},
        json=changes,
        timeout=_HTTP_TIMEOUT,
    )
    r.raise_for_status()
    return bug_id


class BugzillaRejected(RuntimeError):
    """BMO answered and REFUSED a write (HTTP >= 400).

    Deliberately distinct from a transport failure: when the connection times out we do not
    know whether the write landed, so nothing may be retried.

    ``is_client_error`` narrows that further, and the difference is not academic. A 4xx means
    BMO understood the request and rejected the PAYLOAD — nothing was created, and changing
    the payload is a sensible response. A 5xx (or a gateway page from in front of BMO) means
    the request may have been understood, may have half-run, and had nothing to do with what
    we sent; treating it as "the flags were the problem" would drop a perfectly good needinfo
    during a BMO deploy, and re-posting could file the bug twice."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status

    @property
    def is_client_error(self):
        return self.status is not None and 400 <= self.status < 500


def _create_bug(payload, token):
    """POST /rest/bug -> the new bug id.

    libmozdata has no bug-creation call (``Bugzilla`` exposes ``put`` for existing bugs
    only), so this posts directly, but through the same ``_bz_rest()`` base everything else
    here uses — which is what makes ``BUGZILLA_REST_URL`` able to divert the whole write
    path to bugzilla.allizom.org."""
    r = net.post(
        _bz_rest(),
        headers={"X-Bugzilla-API-Key": token},
        json=payload,
        timeout=_HTTP_TIMEOUT,
    )
    if r.status_code >= 400:
        # Bugzilla explains a rejection in the BODY; ``raise_for_status`` shows only
        # "400 Client Error", which is useless in a worker log for a payload we cannot
        # reproduce after the fact. Surface the reason.
        raise BugzillaRejected("bugzilla create failed ({}): {}".format(
            r.status_code, (r.text or "")[:400]), status=r.status_code)
    return (r.json() or {}).get("id")


def _link_blockers(bug_id, blockers, token):
    """Set ``blocks`` on a freshly-created bug. Returns what actually got linked.

    Has to be a SECOND call: BMO's create endpoint accepts ``blocks`` (and ``blocked``)
    without complaint and silently discards both — verified on allizom, where three test
    filings came back 200 with an empty blocks list. Only ``PUT {"blocks": {"add": [...]}}``
    works.

    The PUT is atomic and strict: one unknown id rejects the WHOLE list with code 101, so a
    regressor bug that is restricted, wrong, or simply not visible to this account would
    otherwise also cost us the ``clouseau`` meta-bug link. Hence the retry with the meta-bug
    alone. Best-effort throughout — a bug that is filed but unlinked is a small loss; an
    exception here would strand a filing we have already made."""
    if not blockers:
        return []
    for attempt in (list(blockers), [b for b in blockers if isinstance(b, str)]):
        if not attempt:
            continue
        try:
            _put_bug(bug_id, {"blocks": {"add": attempt}}, token)
            return attempt
        except Exception as exc:
            logger.warning("autofile: linking bug %s to %s failed: %s", bug_id, attempt, exc)
    return []


def _create_bug_keeping_the_bug(payload, token):
    """``(bug_id, needinfo_dropped)`` — create the bug, and never let the needinfo cost it.

    BMO validates the ``flags`` requestee while CREATING the bug and rejects the WHOLE post
    if it cannot resolve them: an hg commit address that is not a Bugzilla account came back
    ``code 51, "There is no user named 'farre@mozilla.com'"`` and crash f6fe186b got no bug
    at all. ``report_bug`` now resolves a verified account, so this should never fire — but
    "should never fire" is exactly what the last unattended write said, and the failure mode
    is silent: it surfaces as ``skipped: bugzilla write failed``, indistinguishable from a
    transient blip.

    The retry is deliberately NOT keyed on code 51. A disabled account, a requestee who
    cannot be needinfo'd, a flag renamed on BMO's side — each would reject the create the
    same way, and a narrow match would let those keep costing us bugs. Instead: if we sent
    flags and the create failed, try once without them, and let the ORIGINAL error surface
    if it fails again (the second failure means the flags were never the problem).

    Safe against double-filing, and this is the whole reason ``BugzillaRejected`` carries a
    status: ONLY a 4xx is retried. A timeout or a reset arrives as a plain ``requests``
    exception and a 5xx arrives as a non-client rejection; in both cases we cannot tell
    whether the POST landed, and re-posting could file the same bug twice — exactly what the
    rest of this module (``already_filed``, ``record_filed_bug``) exists to prevent. A 5xx
    also has nothing to do with the flags, so "retry without them" would throw away a good
    needinfo every time BMO is mid-deploy."""
    try:
        return _create_bug(payload, token), False
    except BugzillaRejected as exc:
        if not payload.get("flags") or not exc.is_client_error:
            raise
        logger.warning("autofile: create rejected with a needinfo flag (%s); "
                       "retrying without it rather than losing the bug", exc)
        retry = {k: v for k, v in payload.items() if k != "flags"}
        try:
            return _create_bug(retry, token), True
        except BugzillaRejected as second:
            # Refused again: the flags were never the problem. Surface the FIRST rejection —
            # it describes what BMO objected to about the bug itself — but LOG the second,
            # because if it differs it is the one naming a systemic failure (a lost
            # permission, a newly mandatory field) that would block every filing, and the
            # caller only logs ``str(exc)`` with no ``__context__`` chain.
            if str(second) != str(exc):
                logger.error("autofile: create refused again without the flag: %s", second)
            raise exc


def _set_needinfo(bug_id, email, token):
    """Set the needinfo flag on an existing bug. Returns the exception on failure, ``None``
    on success — it never raises, because every caller has already made a write it must not
    lose."""
    try:
        _put_bug(bug_id, _needinfo_changes(email), token)
        return None
    except Exception as exc:
        logger.warning("autofile: needinfo for %s on bug %s failed: %s", email, bug_id, exc)
        return exc


def _is_specific_signature(signature):
    """Is this signature distinctive enough to match against free-text bug SUMMARIES?

    A qualified symbol (``mozilla::MediaDecoder::SetCDMProxy``) identifies one crash. A bare
    token does not: searching summaries for ``memcpy`` returns 32 open bugs, and commenting
    on the wrong one is worse than filing a duplicate. ``cf_crash_signature`` needs no such
    guard — that field only ever holds crash signatures."""
    sig = (signature or "").strip()
    return len(sig) >= 16 and ("::" in sig or "|" in sig)


def _open_bugs_for_signature(signature):
    """Ids of OPEN bugs referencing *signature*, oldest first.

    Read-only and unauthenticated (public bugs only, which is the right scope: we must not
    reason about a security bug we can only see because the filing account can).

    Matches the BARE signature, not the ``[@ signature]`` form. Bug 1990812 carries
    ``[@ mozilla::MediaDecoder::SetCDMProxy ]`` — with a trailing space — so the bracketed
    form missed it and we filed 2060922 as a near-duplicate of a REOPENED bug for the exact
    same crash. Summaries are searched too, gated on ``_is_specific_signature``, because
    that is where 1990812 carried it.

    Oldest first: with several open bugs for one signature the earliest is the canonical
    one, carrying whatever discussion already exists. Newest-first would prefer a recent
    duplicate — including one we filed ourselves."""
    if not signature:
        return []
    sig = signature.strip()
    params = {
        "include_fields": "id,summary,status,resolution",
        "f1": "cf_crash_signature", "o1": "substring", "v1": sig,
        "resolution": "---",
    }
    if _is_specific_signature(sig):
        params.update({"j_top": "OR", "f2": "short_desc", "o2": "substring", "v2": sig})
    try:
        r = net.get(_bz_rest(), params=params, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        bugs = (r.json() or {}).get("bugs") or []
    except Exception as exc:                                   # pragma: no cover - network
        # Fail CLOSED: if we cannot tell whether a bug exists, do not file a possible
        # duplicate. A missed filing is recoverable; a duplicate on BMO is not.
        logger.warning("autofile: signature bug lookup failed for %r: %s", signature, exc)
        return None
    return [b["id"] for b in sorted(bugs, key=lambda b: b.get("id", 0))]


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


_BARE_ADDR = re.compile(r"^@?0x[0-9a-fA-F]+$")


def _is_unsymbolicated(signature):
    """True when NO component of the signature resolved to a symbol.

    Requires every ``|``-separated part to be a bare address, so a partly-symbolicated
    signature still files: ``OOM | unknown | memcpy_repmovs_Intel | …`` is perfectly
    actionable and must not be caught here."""
    parts = [p.strip() for p in (signature or "").split("|") if p.strip()]
    return bool(parts) and all(_BARE_ADDR.match(p) for p in parts)


def _needinfo_changes(email):
    """The PUT body that sets a needinfo flag on an existing bug."""
    return {"flags": [{"name": "needinfo", "status": "?", "requestee": email}]}


def autofile_bug(uuid, uuid_info, stack, dossier, verdict, confidence):
    """File a Bugzilla bug for a reported crash, unattended. Returns a result dict; NEVER
    raises — a filing failure must not lose an analysis that is already persisted.

    This is the only write to Bugzilla with no human in the loop, so every gate is here
    rather than at the call site, and each one fails CLOSED:

    * disabled unless ``AUTOFILE_BUGS`` is on (a real kill-switch: it writes to production
      BMO on a schedule, so it has to be stoppable without a deploy);
    * verdict must be reported and at/above ``min_confidence`` (70 = the ``probable`` rung);
    * never twice for one crash (``Dossier.already_filed``), which matters because the
      orphan reaper re-runs a crashed run and would otherwise re-file on recovery;
    * a ``daily_cap`` bound, because the pipeline itself has none and a bad gate at 3/day
      is a nuisance while a bad gate at 300/day is an incident;
    * if an OPEN bug already references the signature, comment there instead of filing a
      duplicate — and if that lookup FAILS we skip entirely rather than risk the duplicate.

    ``regressed_by`` is still not set (see ``report_bug.build_bug_preview``): the suspected
    regressor is named in the comment prose, where a human can weigh it, and the needinfo
    asks that human directly. The blocker link records the association without asserting
    cause."""
    cfg = config.get_agent_autofile()
    if not cfg["enabled"]:
        return {"filed": False, "skipped": "autofile disabled"}
    if verdict not in cfg["verdicts"]:
        return {"filed": False, "skipped": "verdict {} not fileable".format(verdict)}
    if confidence is None or confidence < cfg["min_confidence"]:
        return {"filed": False, "skipped": "confidence {} below {}".format(
            confidence, cfg["min_confidence"])}

    # OFF-STACK OBSERVE-ONLY. `_apply_offstack_observe_only` empties `result.actions`
    # precisely to "SUPPRESS any outward action" while the off-stack canary's calibration is
    # being watched. This filer does not read `result.actions` — it builds its own payload —
    # so without this check it walks straight through that suppression. 14 of the 66 rung-70
    # verdicts in the last 30 days carry the flag, i.e. ~1 filed bug in 5.
    if (dossier or {}).get("corroborations", {}).get("offstack_observe_only"):
        return {"filed": False, "skipped": "off-stack run is observe-only"}

    # An unsymbolicated signature is a bare address: "@0xe2ba40f948". Filing it produces a
    # bug titled "Crash in [@ @0xe2ba40f948]" whose `cf_crash_signature` matches nothing and
    # dedupes against nothing, because the address differs per crash — and if no frame
    # resolves to code, nothing ties the crash to the candidate anyway.
    if _is_unsymbolicated(uuid_info.get("signature")):
        return {"filed": False, "skipped": "signature is unsymbolicated ({})".format(
            uuid_info.get("signature"))}

    prior = models.Dossier.already_filed(uuid)
    if prior:
        return {"filed": False, "skipped": "already filed", "prior": prior}

    since = datetime.now(timezone.utc) - timedelta(days=1)
    try:
        recent = models.Dossier.filed_bugs_since(since)
    except Exception as exc:                                # pragma: no cover - defensive
        return {"filed": False, "skipped": "cap check failed: {}".format(exc)}
    if recent >= cfg["daily_cap"]:
        logger.warning("autofile: daily cap %s reached (%s in 24h) — not filing for %s",
                       cfg["daily_cap"], recent, uuid)
        return {"filed": False, "skipped": "daily cap {} reached".format(cfg["daily_cap"])}

    token = config.get_bugzilla_token()
    if not token:
        return {"filed": False, "skipped": "no Bugzilla API token configured"}

    from crashclouseau import report_bug
    try:
        preview = report_bug.build_bug_preview(uuid_info, stack, dossier)
    except Exception as exc:
        logger.error("autofile: preview build failed for %s", uuid, exc_info=True)
        return {"filed": False, "skipped": "preview failed: {}".format(exc)}
    if not preview:
        return {"filed": False, "skipped": "no candidate regressor to file against"}
    # ``resolve_product_component`` is best-effort and returns empty on a Bugzilla read
    # failure or an unreadable regressor bug. Filing then gets rejected outright
    # ("Bad argument param sent to Bugzilla::Product::new") — but the real reason to check
    # here is that a HALF-resolved pair would file the bug into the wrong component, which
    # is worse than not filing: it lands on a team that has no idea why they got it.
    if not (preview.get("product") and preview.get("component")):
        return {"filed": False,
                "skipped": "product/component unresolved — refusing to file into the wrong "
                           "component"}

    signature = (uuid_info.get("signature") or "").strip()
    existing = _open_bugs_for_signature(signature)
    if existing is None:
        return {"filed": False, "skipped": "signature lookup failed; not risking a duplicate"}

    email = preview.get("needinfo_email") if cfg["needinfo"] else ""
    result = {"filed": False, "uuid": uuid, "signature": signature,
              "at": datetime.now(timezone.utc).isoformat()}
    try:
        if existing:
            if not cfg["comment_on_existing"]:
                return {"filed": False, "skipped": "open bug {} exists".format(existing[0])}
            bug_id = existing[0]
            _post_comment(bug_id, preview["comment"], False, token)
            # The comment is already posted, so a failing needinfo must not escape: it would
            # skip ``record_filed_bug`` below and the next run would comment a second time on
            # the same bug. Lose the flag, keep the filing.
            failed = _set_needinfo(bug_id, email, token) if email else None
            result.update({"filed": True, "bug": bug_id, "mode": "comment_on_existing",
                           "needinfo": None if failed else (email or None)})
            if failed:
                result["needinfo_failed"] = email
        else:
            payload = {k: v for k, v in preview.items()
                       if k in ("product", "component", "version", "type", "keywords",
                                "cf_crash_signature")}
            payload["summary"] = preview["title"]
            payload["description"] = preview["comment"]
            if email:
                payload["flags"] = [{"name": "needinfo", "status": "?", "requestee": email}]
            bug_id, dropped = _create_bug_keeping_the_bug(payload, token)
            if dropped:
                result["needinfo_dropped"] = email
                email = ""
            # Blockers need a second call — create discards them silently (see
            # ``_link_blockers``). After the bug exists, so a link failure can't lose it.
            wanted = preview.get("blocked") or []
            linked = _link_blockers(bug_id, wanted, token)
            result.update({"filed": True, "bug": bug_id, "mode": "new_bug",
                           "needinfo": email or None, "blocks": linked})
            # Record what did NOT link. Two of the first three real filings lost their
            # regressor link because that bug is access-restricted (BMO answers 102 for
            # 2043188), and the atomic PUT then rejects the whole list. The bug is still
            # correct — the changeset is named in the comment prose — but a silent gap in
            # the structured data is exactly the kind of thing nobody notices for a month.
            missing = [b for b in wanted if b not in linked]
            if missing:
                result["blocks_unlinked"] = missing
                logger.warning("autofile: bug %s could not link %s (restricted or unknown)",
                               bug_id, missing)
    except Exception as exc:
        logger.error("autofile: Bugzilla write failed for %s: %s", uuid, exc)
        return {"filed": False, "skipped": "bugzilla write failed: {}".format(exc)}

    models.Dossier.record_filed_bug(uuid, result)
    logger.info("autofile: %s -> bug %s (%s, needinfo=%s)",
                uuid, result["bug"], result["mode"], result.get("needinfo"))
    return result


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
    token = config.get_bugzilla_token()

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
