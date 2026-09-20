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

* ``autofile_bug(...)`` files a bug for a reported crash with NO human in the loop.
  Every gate lives in that function and each fails closed; ``AUTOFILE_BUGS`` is its
  kill-switch.

Every Bugzilla write in the product goes through this module's REST helpers
(``_post_comment``, ``_create_bug_keeping_the_bug``, ``_put_bug`` and their wrappers) -- but
it is no longer the only FILER: ``agent.spike_escalation.file_spike_bug`` is a second
unattended one (a real spike is filed culprit or not) and calls the helpers here. The two
share the global switch (``config.autofile_globally_enabled``) and the per-PRODUCT hold
(``config.autofile_product_held``); only the per-channel hold is the ordinary filer's alone.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

from . import net

from crashclouseau import config, corroborations, models, sensitive, utils
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


def build_evidence(uuid, public=True):
    """Verdict/dossier/actions + UI/apply policy for one UUID, or ``None`` when no
    verdict row exists (panel hidden). Read-only.

    THE ONE CHOKEPOINT FOR PUBLISHING AN ANALYSIS. Three of the four surfaces that can expose a
    dossier come through here -- ``html.crashstack``, ``html.diff`` and ``api.evidence`` -- and
    the autofiler does NOT (it composes its own text via ``report_bug.build_bug_preview``), so
    withholding here cannot suppress a filing. The fourth surface, ``html.bug``, renders the
    drafted comment through ``report_bug.get_info`` and carries its own check.

    ``public`` defaults to True, i.e. to withholding, so a NEW caller is safe by default and has
    to ask for the unredacted dossier on purpose. That default is the whole guard: the failure
    mode this is built against is a fifth surface added later by someone who has never read
    ``sensitive.py``."""
    ev = models.Verdict.get_evidence(uuid)
    if ev is None:
        return None
    if public and sensitive.is_withheld((ev.get("dossier") or {}).get("corroborations")):
        # Everything the analysis produced goes, not selected fields. The mechanism sentence IS
        # the disclosure -- "releases a stale, already-freed RefPtr" names the defect outright --
        # and `diff.html` would still highlight exactly the lines the analysis flagged. `status`
        # stays so the tasks view can still say the run finished.
        return {"uuid": ev.get("uuid") or uuid,
                "status": ev.get("status"),
                "verdict": None,
                "withheld": True,
                "withheld_reasons": ((ev.get("dossier") or {})
                                     .get("corroborations") or {}).get(
                                         "memory_unsafe_signals") or []}
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


# The web page's budget for one BMO request about a signature. The filer's ``_HTTP_TIMEOUT`` is
# 60 s, which a worker can afford and a page view cannot: the web dyno runs ONE gunicorn worker
# and Heroku's router gives up at 30 s, so a single slow BMO answer would take the whole site
# down with it. The venue search is at most two requests (the open bugs, then the open targets
# of the signature's duplicates), and at 8 s each both fit under the router's clock beside the
# preview's own lookups.
_PAGE_HTTP_TIMEOUT = 8


def open_venues(signature, product, timeout=_PAGE_HTTP_TIMEOUT):
    """The open Bugzilla bugs on *signature* EXACTLY AS THE FILER SEES THEM, for display:
    ``{"venues", "other_app", "metas"}`` (each a list of ``_venue_row`` dicts, oldest id
    first), or ``None`` when BMO could not be asked.

    The same search, the same exact-entry test, the same duplicate-target follow-up and the
    same two splits as ``autofile_bug``, so that what crashstack.html calls "a bug already
    exists for this signature" is the bug the filer would have declined for or commented on --
    not a looser lookup naming bugs the filer ignores, nor a tighter one missing the venue it
    chose. Read-only, unauthenticated, public bugs only, like every venue lookup here.

    ASKED AT PAGE-VIEW TIME rather than read from the run's record, because the record is only
    as informative as the gate that wrote it. The filer's gates run cheapest first and the venue
    search is one of the last, so a run the daily cap declined never asked BMO at all: 8ab28d1a
    (2026-09-17, a culprit at 85 on release) carries ``daily cap 2 reached on release`` and
    nothing else, while dmeehan's bug 2072627 had been open on the signature since the day
    before -- "did you file a bug for this?" had to be answered by hand. And a bug a human files
    AFTER our run is invisible to any record, however complete. ``timeout`` is per request."""
    existing = _open_bugs_for_signature(signature, timeout=timeout)
    if existing is None:
        return None
    venues, other_app = _split_by_application(existing, product)
    venues, metas = _split_out_metas(venues)
    return {"venues": venues, "other_app": other_app, "metas": metas}


# --------------------------------------------------------------------------- #
# Bugzilla REST writes (every write in the product, the spike filer's included, is one of
# these)
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
    """Add ``blocks`` after it was removed from the create request.

    A PUT containing an invalid bug rejects the whole list, so retry with aliases alone when the
    first attempt mixes aliases and ids. Linking is best-effort because the bug already exists."""
    if not blockers:
        return []
    attempts = [list(blockers)]
    aliases = [b for b in blockers if isinstance(b, str)]
    if aliases and aliases != attempts[0]:
        attempts.append(aliases)
    for attempt in attempts:
        try:
            _put_bug(bug_id, {"blocks": {"add": attempt}}, token)
            return attempt
        except Exception as exc:
            logger.warning("autofile: linking bug %s to %s failed: %s", bug_id, attempt, exc)
    return []


def _link_regressed_by(bug_id, regressors, token):
    """Add ``regressed_by`` after it was removed from the create request.

    Keep this separate from ``blocks`` because one rejected field makes the combined PUT fail.
    The update is best-effort because the bug already exists."""
    if not regressors:
        return []
    try:
        _put_bug(bug_id, {"regressed_by": {"add": list(regressors)}}, token)
        return list(regressors)
    except Exception as exc:
        logger.warning("autofile: setting regressed_by %s on bug %s failed: %s",
                       regressors, bug_id, exc)
    return []


# The per-train flags a bug we file carries, by field-name prefix: `cf_tracking_firefox<N>` (an
# ask) and `cf_status_firefox<N>` (a statement), `_esr<N>` for the ESR family of either.
_TRAIN_FLAG_PREFIXES = ("cf_status_firefox", "cf_tracking_firefox")


def _is_train_flag(key):
    return str(key).startswith(_TRAIN_FLAG_PREFIXES)


def _status_flags(preview, signature, product):
    """Return ``affected`` status fields for the crash's own train and every other live train
    on which Socorro reports the signature. If discovery fails, keep the preview's own-train
    field. Bucket signatures are catch-alls, so do not use them to discover other trains.

    This runs only when filing a new bug. BugBot may infer other statuses from ``regressed_by``;
    its ``regression_set_status_flags`` rule leaves values other than ``---`` unchanged."""
    preview = preview or {}
    flags = dict(preview.get("status_flags") or {})
    if signature and not preview.get("bucket"):
        try:
            from crashclouseau import report_bug, sigage
            trains = sigage.affected_trains(signature, product)
            for flag, value in report_bug.status_flags_for_trains(trains or ()).items():
                flags.setdefault(flag, value)
        except Exception as exc:
            logger.warning("autofile: discovering affected trains for %r failed: %s",
                           signature, exc)
    return flags


def _train_flags(preview, signature, product):
    """Return the tracking nomination and ``affected`` status fields for a new bug's create
    body. BMO's TrackingFlags create hooks remove known tracking fields before inserting the
    bug, then write only active fields visible for its product and component. An unknown field
    name instead rejects the create before insertion; the caller can retry without these fields
    and submit them separately. See ``extensions/TrackingFlags/Extension.pm`` in BMO."""
    preview = preview or {}
    flags = {}
    if preview.get("tracking_flag"):
        flags[preview["tracking_flag"]] = "?"
    flags.update(_status_flags(preview, signature, product))
    return flags


def _put_train_flags(bug_id, flags, token):
    """Submit each train flag separately after a create rejected them.

    Return fields whose PUT returned successfully and names whose PUT raised. This is not a
    readback: an ambiguous network failure may have applied a change."""
    done, refused = {}, []
    for flag in sorted(flags or {}):
        try:
            _put_bug(bug_id, {flag: flags[flag]}, token)
            done[flag] = flags[flag]
        except Exception as exc:
            logger.warning("autofile: setting %s = %s on bug %s failed: %s",
                           flag, flags[flag], bug_id, exc)
            refused.append(flag)
    return done, refused


def _record_train_flags(result, flags, refused=()):
    """Record submitted train fields and fields whose fallback PUT raised.

    This records request outcomes, not a BMO readback. Omit empty result keys."""
    for flag, value in (flags or {}).items():
        if flag.startswith("cf_tracking_"):
            result["tracking_nominated"] = flag
        else:
            result.setdefault("status_flags", {})[flag] = value
    for flag in refused or ():
        if flag.startswith("cf_tracking_"):
            result["tracking_failed"] = flag
        else:
            result.setdefault("status_flags_failed", []).append(flag)


# Preview keys copied unchanged into the create request. `_create_payload` maps relations and
# train flags separately because their API field names differ from the preview's.
_CREATE_KEYS = ("product", "component", "version", "type", "keywords",
                "cf_crash_signature", "groups", "cc")


def _create_payload(preview, email, train_flags=None):
    """Build the create request shared by the ordinary and spike filers.

    Empty relations are omitted. Preview key ``blocked`` becomes API field ``blocks``; train
    flags already use their API ``cf_*`` names."""
    payload = {k: v for k, v in preview.items() if k in _CREATE_KEYS}
    # Empty ones would be sent as `[]`; drop them so an ordinary filing's payload is
    # byte-identical to what it was before this existed. `cf_crash_signature` is empty on a
    # BUCKET bug (the signature stays on the [meta] tracker) and is dropped for the same reason.
    for k in ("groups", "cc", "cf_crash_signature"):
        if not payload.get(k):
            payload.pop(k, None)
    payload["summary"] = preview["title"]
    payload["description"] = preview["comment"]
    # Created CONFIRMED. `clouseau-bot` has been in `canconfirm` since 2026-09-17; before that
    # every filing sat UNCONFIRMED until BugBot's crash-signature rule confirmed it, 5 h to 3
    # days later (2069800, 2067456 -- bug 2071727 comment 1 is what that looks like). Bugzilla
    # derives `is_confirmed` ("Ever confirmed") from the status, so this one key sets both. It
    # can never cost a bug: an account outside the group is not refused, it silently gets
    # UNCONFIRMED (proved on allizom, 2026-09-07), and BugBot confirms it later as before.
    payload["status"] = "NEW"
    if preview.get("blocked"):
        payload["blocks"] = list(preview["blocked"])
    if preview.get("regressed_by"):
        payload["regressed_by"] = list(preview["regressed_by"])
    payload.update(train_flags or {})
    if email:
        payload["flags"] = [{"name": "needinfo", "status": "?", "requestee": email}]
    return payload


_EDITBUGS_BY_TOKEN = {}


def _can_create_relationships(token):
    """Return whether the account has ``editbugs``, cached per token.

    BMO silently ignores create-time relationships without that group. A failed lookup returns
    ``False`` without being cached, so this filing uses the PUT fallback."""
    if token in _EDITBUGS_BY_TOKEN:
        return _EDITBUGS_BY_TOKEN[token]
    root = _bz_rest().rsplit("/bug", 1)[0]
    headers = {"X-Bugzilla-API-Key": token}
    try:
        r = net.get("{}/whoami".format(root), headers=headers, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        groups = set((r.json() or {}).get("groups") or [])
    except Exception as exc:
        logger.warning("autofile: could not read the account's groups (%s); relationships go "
                       "by PUT this time", exc)
        return False
    _EDITBUGS_BY_TOKEN[token] = "editbugs" in groups
    if not _EDITBUGS_BY_TOKEN[token]:
        logger.warning("autofile: the account is not in editbugs; relationships go by PUT")
    return _EDITBUGS_BY_TOKEN[token]


def _create_bug_keeping_the_bug(payload, token):
    """Create a bug, removing optional fields after a 4xx refusal.

    The retry order is ``regressed_by``, ``blocks``, train fields, then needinfo. Return the bug
    id and removed categories so callers can retry them by PUT. Relationships are removed before
    the first attempt when the account lacks ``editbugs``. Never retry transport errors or 5xx
    responses because the POST may have succeeded. If all retries fail, raise the first refusal."""
    body, dropped = payload, frozenset()
    relations = [k for k in ("regressed_by", "blocks") if payload.get(k)]
    if relations and not _can_create_relationships(token):
        body = {k: v for k, v in payload.items() if k not in relations}
        dropped = frozenset(relations)
    rungs = [(body, dropped)]
    for name, keys in (("regressed_by", ("regressed_by",)),
                       ("blocks", ("blocks",)),
                       ("train_flags", tuple(k for k in body if _is_train_flag(k))),
                       ("needinfo", ("flags",))):
        if not any(body.get(k) for k in keys):
            continue
        body = {k: v for k, v in body.items() if k not in keys}
        dropped = dropped | {name}
        rungs.append((body, dropped))
    first = None
    for i, (body, dropped) in enumerate(rungs):
        try:
            return _create_bug(body, token), set(dropped)
        except BugzillaRejected as exc:
            if first is None:
                first = exc
            elif str(exc) != str(first):
                logger.error("autofile: create refused again without %s: %s (first: %s)",
                             " and ".join(sorted(dropped)), exc, first)
            if not exc.is_client_error:
                raise exc               # This POST may have succeeded; preserve its ambiguity.
            if i == len(rungs) - 1:
                raise first
            logger.warning("autofile: create rejected (%s); retrying without %s rather than "
                           "losing the bug", exc, " and ".join(sorted(rungs[i + 1][1])))
    raise first  # pragma: no cover - the loop returns or raises


def _existing_needinfos(bug_id, token):
    """The logins that already have a pending ``needinfo?`` on *bug_id*, lowercased, or
    ``None`` when BMO could not be read.

    Authenticated, unlike the other reads here: to a logged-out client Bugzilla hides the
    requestee behind a masked login, and a masked address matches nobody, so an anonymous
    read would report every needinfo as somebody else's."""
    try:
        r = net.get(
            "{}/{}".format(_bz_rest(), bug_id),
            headers={"X-Bugzilla-API-Key": token},
            params={"include_fields": "flags"},
            timeout=_HTTP_TIMEOUT,
        )
        r.raise_for_status()
        bugs = (r.json() or {}).get("bugs") or []
    except Exception as exc:                                   # pragma: no cover - network
        logger.warning("autofile: flag lookup failed for bug %s: %s", bug_id, exc)
        return None
    return {
        f["requestee"].lower()
        for f in ((bugs[0] if bugs else {}).get("flags") or [])
        if f.get("name") == "needinfo" and f.get("status") == "?" and f.get("requestee")
    }


_NEEDINFO_ALREADY = "already_set"


def _set_needinfo(bug_id, email, token):
    """Ask *email* for information on an existing bug, unless somebody already has.

    Returns ``None`` when we set the flag, ``_NEEDINFO_ALREADY`` when *email* was already on
    the hook, and the exception on failure — it never raises, because every caller has
    already made a write it must not lose.

    Two people needinfo'd on one bug is normal and ``_needinfo_changes`` now adds rather than
    replaces, so the only thing left to avoid is asking the SAME person twice: our comment
    would put a second ``needinfo?(x)`` under a question x has not answered yet, which reads
    as a nag and tells them nothing new. The pending flag they already have covers our ask.

    Fails CLOSED when the flags cannot be read. ``new: true`` would keep the write safe for
    everyone else, but the point of the read is the duplicate, and there is no evidence for
    "not asked yet" if BMO would not say. A skipped needinfo is recorded and recoverable; the
    comment naming the regressor is posted either way."""
    existing = _existing_needinfos(bug_id, token)
    if existing is None:
        return RuntimeError("could not read the flags on bug {}".format(bug_id))
    if email.lower() in existing:
        logger.info("autofile: bug %s already has a needinfo on %s; not asking twice",
                    bug_id, email)
        return _NEEDINFO_ALREADY
    try:
        _put_bug(bug_id, _needinfo_changes(email), token)
        return None
    except Exception as exc:
        logger.warning("autofile: needinfo for %s on bug %s failed: %s", email, bug_id, exc)
        return exc


_SUMMARY_CRASH_FORMS = ("[@ {}]", "[@ {} ]")


def _summary_is_about(summary, signature):
    """Does this bug SUMMARY carry *signature* the way a crash bug carries it — ``[@ sig]``?

    Replaces a length-and-token gate (``len(sig) >= 16 and ("::" in sig or "|" in sig)``) read
    off two points, ``memcpy`` on one side and ``mozilla::MediaDecoder::SetCDMProxy`` on the
    other. On the top 200 Firefox-nightly signatures (SuperSearch facet 2026-08-07..08-21,
    15131 crashes) the LENGTH half decided only 2 of the 200 — ``OOM | small`` and
    ``js::IsProxy`` — the other 198 being settled by the ``::``/``|`` token alone; and
    ``nsAtom::IsStatic``, the signature this whole venue rule was written about, is exactly 16
    characters, so the panel's most consequential case sat ON the boundary. No test ever
    exercised the threshold: every negative in tests/test_autofile.py was rejected by the token
    test too.

    What the summary search needed was the FORM, not a size. Same 200 signatures: cf-plus-
    bracket adds a bug the ``cf_crash_signature`` search misses for 8/200 and moves the chosen
    venue for 2/200 — the same counts as the gated bare-substring rule — while swapping two
    false venues for two true ones. It DROPS bug 1891138 ("Crash in js::gc::HeaderWord::get
    when doing native allocation profiling of www.itemkey.co.uk/…", Core::Gecko Profiler) and
    bug 2009859 ("3% of doxbee-promise is nsCycleCollectingAutoRefCnt::incr from CallSetup and
    xpc::NativeGlobal", a performance bug) — prose mentions, exactly what the old gate existed
    to avoid. It GAINS bug 2016952 ("Crash in [@ OOM | small]") and bug 1960108 ("A Firefox 137
    tab crashed on YouTube [@ EMPTY: no frame data available; EmptyMinidump ]"), real crash bugs
    on generic signatures the length test refused to search for at all. On ``memcpy`` the bare
    summary substring returns 26 open bugs today (the retired docstring said 32) and the
    bracketed form returns 1, bug 1819825 — the precision the 16 was reaching for is delivered
    by the form.

    MUST NOT EAT bug 1990812, ``[Intermittent] Crash on canalplus.com … [@
    mozilla::MediaDecoder::SetCDMProxy ]`` — hence the TRAILING-SPACE variant, which is how that
    bug writes it in both its summary and its ``cf_crash_signature``. On the 51 filed-panel
    signatures plus the four counter-example ones this rule differs from the retired one for
    exactly ONE, ``mozilla::detail::MutexImpl::mutexLock``, where it removes both bad venues
    that filing 2064274 would otherwise have landed on (1695119 "Crash @ …mutexLock() |
    …WebProgressListener::OnStateChange" and 1777373 "Frequent Hit
    MOZ_CRASH(mozilla::detail::MutexImpl::mutexLock: pthread_mutex_lock failed)").

    Exact-form rather than the prefix BMO is asked for, and INSURANCE rather than a measured
    save. ``[@ sig`` over-matched a LONGER signature for 3 of the 200 —
    ``mozilla::widget::WlLogHandler`` against bug 1996736 ``Crash in [@
    mozilla::widget::WlLogHandler_UnknownObject]``, plus ``amdxx64.dll`` and
    ``IPCError-browser | ShutDownKill`` — but every one of those bugs ALSO carries the longer
    signature in its ``cf_crash_signature``, which the cf side accepted as a bare substring at
    the time, so the union the caller kept was identical: the exact form changed the keep-set
    for 0 of the 200 and moved the venue for 0. The cf side is an exact entry too now
    (``_row_is_about``), so those three rows drop on both sides -- a longer signature is a
    different signature. What this half guards is a shape the panel does not contain — a crash
    bug with an EMPTY ``cf_crash_signature`` (1891138, 2009859 and 1960108 each have one)
    whose summary carries a longer signature. Case-insensitive because BMO's ``substring``
    operator is, and anything this sees was already returned by that search."""
    low = (summary or "").lower()
    sig = (signature or "").strip().lower()
    return bool(sig) and any(form.format(sig) in low for form in _SUMMARY_CRASH_FORMS)


def _signature_field_entries(field):
    """The signatures a ``cf_crash_signature`` field carries, one per ``[@ ...]`` entry, bare.

    Split on the OPENING ``[@`` and take ONE closing bracket off each piece; never scan to the
    first ``]``, because a signature can end in one -- bug 1996583 carries two entries of the
    shape ``[@ mozilla::detail::InvalidArrayIndex_CRASH | mozilla::Array<T>::operator[] | ...]``
    and a regex to the first ``]`` reads them as ``... operator[``. Entries may be separated by
    newlines (BMO's own layout), CRLF or a space; the trailing-space form ``[@ sig ]`` (bug
    1990812) strips to the bare signature like any other. Text before the first ``[@`` -- a
    field somebody typed without brackets -- is kept as an entry too, so it can still match
    exactly."""
    return utils.bugzilla_signature_entries(field)


def _row_is_about(bug, signature):
    """Does this BMO row really carry *signature*, or did the OR over-match?

    One request asks ``cf_crash_signature`` for the bare signature as a SUBSTRING, OR
    ``short_desc`` for the prefix ``[@ sig``, and the response does not say which clause
    matched, so both are re-checked here -- and both re-checks are EXACT. The cf side is an
    exact ``[@ sig]`` entry (``_signature_field_entries``); the summary side demands the exact
    crash-bug form (``_summary_is_about``). The substring is the fetch, not the test.

    The cf side used to be a bare substring, on the argument that the field "only ever holds
    signatures". It does -- but one signature can be a PREFIX of another, and for
    ``AsyncShutdownTimeout`` it routinely is: the blockers are comma-joined, so the one-blocker
    ``AsyncShutdownTimeout | profile-before-change | CookiePersistentStorage: cookies.sqlite
    closing`` is contained in the two-blocker ``...cookies.sqlite closing,ServiceWorkerRegistrar:
    Flushing data`` (bug 2067456, our own nightly filing), and the substring took that bug as the
    venue for a release crash on the shorter signature (0027161c, 2026-09-06). Measured over
    every comment-on-existing filing ever made (17): 16 venues carried the exact signature and 1
    was substring-only -- bug 2068006 took the one-blocker ``ServiceWorkerRegistrar: Flushing
    data`` while carrying only multi-blocker supersets. Same shape both times, and only that
    direction exists: a longer search never substring-matches a shorter entry. On a ``skip``
    channel an over-matched venue is a FULL STOP ("open bug N exists"), so this decided release's
    only rung-70 finding of its first held week; on the fixed-after-build side, which shares this
    re-check, a FIXED bug about a longer signature would suppress a filing on the shorter one.

    Case-insensitive on both sides, because BMO's ``substring`` operator is and anything this
    sees was already returned by that search. Lambda demanglings are the CALLER's business:
    ``_open_bugs_for_signature`` asks once per ``utils.lambda_siblings`` spelling."""
    sig = (signature or "").strip().lower()
    if not sig:
        return False
    if any(entry.lower() == sig
           for entry in _signature_field_entries(bug.get("cf_crash_signature"))):
        return True
    return _summary_is_about(bug.get("summary"), sig)


# How many other names of the crash the venue search may add to its OR. Each costs two clauses
# on the BMO query; the family of one signature is rarely more than four names (2070554's four
# `WaitOnAddress` spellings are the widest case measured).
_MAX_FAMILY_SPELLINGS = 8


def _family_spelling_map(signature, family):
    """``{spelling: {"via", "relation", "since"}}`` -- every OTHER name of this crash the venue
    search should ask for (``sigfamily``: handoff predecessors first, then the live siblings,
    each in every lambda demangling), minus the signature's own spellings. ``since`` is the
    ISO instant the crash took the new name (S's first build on the channel), set on handoff
    predecessors only: it is the ``venue_since`` clock a bug on the OLD name is dated by."""
    from crashclouseau import sigage, sigfamily

    own = {s.lower() for s in utils.lambda_siblings(signature)}
    fam = family or {}
    since = None
    build = fam.get("s_first_build")
    if build and fam.get("predecessors"):
        dt_ = sigage._buildid_to_dt(build)
        since = dt_.isoformat() if dt_ else None
    out = {}
    names = [(p, "handoff") for p in sigfamily.spellings({"predecessors": fam.get("predecessors")})]
    names += [(s, "sibling") for s in sigfamily.spellings({"siblings": fam.get("siblings")})]
    for name, relation in names[:_MAX_FAMILY_SPELLINGS]:
        for spelling in utils.lambda_siblings(name):
            if spelling.lower() in own or spelling in out:
                continue
            out[spelling] = {"via": name, "relation": relation,
                             "since": since if relation == "handoff" else None}
    return out


def _open_bugs_for_signature(signature, timeout=_HTTP_TIMEOUT, family=None):
    """OPEN bugs referencing *signature* as
    ``[{"id", "creation_time", "product", "keywords"}, ...]``, oldest first.

    Every field beyond the id is there because the oldest open bug is not automatically the
    right place to comment: ``creation_time`` for ``_bug_for_this_regression``, ``product``
    for ``_split_by_application``, ``keywords`` for ``_split_out_metas``.

    Read-only and unauthenticated (public bugs only, which is the right scope: we must not
    reason about a security bug we can only see because the filing account can).

    ``cf_crash_signature`` is QUERIED on the BARE signature, not the ``[@ signature]`` form:
    bug 1990812 carries ``[@ mozilla::MediaDecoder::SetCDMProxy ]`` — with a trailing space —
    so the bracketed form missed it and we filed 2060922 as a near-duplicate of a REOPENED bug
    for the exact same crash. The rows that come back are then held to an EXACT ``[@ sig]``
    entry by ``_row_is_about``, because a one-blocker ``AsyncShutdownTimeout`` signature is a
    substring of every longer blocker list that starts with it (bug 2067456 against the cookie
    crash 0027161c). The SUMMARY half is the other way round and ungated: it asks for
    the crash-bug form ``[@ sig`` and then keeps only an exact ``[@ sig]``/``[@ sig ]``
    (``_summary_is_about``), which is what the retired ``_is_specific_signature`` length test
    was really reaching for. One request, so the rows are re-checked here rather than in a
    second query — BMO's OR does not say which clause matched.

    Oldest first, because among the bugs that could be about this crash the earliest is the
    canonical one, carrying whatever discussion already exists; newest-first would prefer a
    recent duplicate, including one we filed ourselves. Only a tie-break, though —
    ``_bug_for_this_regression`` decides which of them qualify at all.

    PLUS THE OPEN TARGETS OF THE SIGNATURE'S DUPLICATES (``_duplicate_targets_for_signature``).
    A bug on this signature that a human resolved DUPLICATE of bug N is that human saying "this
    signature's crashes are bug N", and N is a venue whether or not they also copied the
    signature onto it — they usually should and often do not. Those rows carry two extra keys,
    ``venue_since`` (when the dup was resolved: the moment the signature was tied to N) and
    ``via_duplicates`` (the dup ids on the chain), and a target that the direct search already
    returned keeps its row and gains the two keys. Bug 2070711 (2026-09-09) is what this costs
    without it: our own 2069647 on ``wgpu_server_buffer_get_mapped_range`` had been duped into
    1976766 two days earlier, the DUPLICATE was invisible to ``resolution="---"``, and 1976766
    was rejected as predating the regressor by fourteen months — so we filed the same analysis
    a second time, past both, and :teoxoy's comment 3 asked why we track neither.

    PLUS THE CRASH'S OTHER NAMES (*family*, a ``sigfamily.lookup`` answer or the facts the run
    recorded): the older name a rename handed off from and the spellings still live beside this
    one. One filing in eight was named for a crash that already had another name, and 17 of the
    18 clearest had an open bug on the OLD name at filing time (plans/24): 1737467 on
    ``PatchNtdll`` for our 2073210, 1976766 on ``WebGPUParent::MapCallback`` for our 2069647. A
    row reached only through another name carries ``via_signature`` (that name) and
    ``via_relation`` (``handoff`` / ``sibling``); a handoff row also carries ``venue_since`` --
    the instant the crash took this name -- because that is when the crash under THIS signature
    became that bug's, whatever year the bug was filed in (the same clock as a dup or an
    attached signature, see ``_bug_for_this_regression``). A sibling row keeps its creation
    clock: another spelling of the same crash is exactly as old as the crash."""
    if not signature:
        return []
    sig = signature.strip()
    # EVERY SPELLING OF THE LAMBDA, not just the one this crash came in under: the `<T>` and `$`
    # demanglings of one lambda are one defect (`utils.lambda_siblings`), and a bug filed on the
    # Windows spelling is the venue for the Linux crash too. Two clauses per spelling, same
    # `j_top: OR`.
    own = sorted(utils.lambda_siblings(sig))
    others = _family_spelling_map(sig, family)
    spellings = own + sorted(others)
    params = {
        "include_fields": "id,summary,status,resolution,creation_time,product,keywords,"
                          "cf_crash_signature,regressed_by",
        "j_top": "OR",
        "resolution": "---",
    }
    for i, spelling in enumerate(spellings):
        params["f{}".format(2 * i + 1)] = "cf_crash_signature"
        params["o{}".format(2 * i + 1)] = "substring"
        params["v{}".format(2 * i + 1)] = spelling
        params["f{}".format(2 * i + 2)] = "short_desc"
        params["o{}".format(2 * i + 2)] = "substring"
        params["v{}".format(2 * i + 2)] = "[@ " + spelling
    try:
        r = net.get(_bz_rest(), params=params, timeout=timeout)
        r.raise_for_status()
        bugs = (r.json() or {}).get("bugs") or []
    except Exception as exc:                                   # pragma: no cover - network
        # Fail CLOSED: if we cannot tell whether a bug exists, do not file a possible
        # duplicate. A missed filing is recoverable; a duplicate on BMO is not.
        logger.warning("autofile: signature bug lookup failed for %r: %s", signature, exc)
        return None
    # ``regressed_by`` rides along because it decides whether a venue wants our comment at
    # all — see the gate in ``autofile_bug``. Free: same request, one more field.
    rows = []
    for b in sorted(bugs, key=lambda b: b.get("id", 0)):
        if not b.get("id"):
            continue
        if any(_row_is_about(b, s) for s in own):
            rows.append(_venue_row(b))
            continue
        via = next((s for s in sorted(others) if _row_is_about(b, s)), None)
        if via is None:
            continue
        row = _venue_row(b)
        row["via_signature"] = others[via]["via"]
        row["via_relation"] = others[via]["relation"]
        if others[via]["since"]:
            row["venue_since"] = others[via]["since"]
        rows.append(row)
    return _merge_duplicate_targets(
        rows, _duplicate_targets_for_signature(sig, timeout=timeout, spellings=spellings))


def _venue_row(bug):
    """The row shape every venue consumer reads, from one BMO bug dict."""
    return {"id": bug["id"], "creation_time": bug.get("creation_time"),
            "product": bug.get("product"), "keywords": bug.get("keywords") or [],
            "regressed_by": bug.get("regressed_by") or []}


def _merge_duplicate_targets(rows, targets):
    """*rows* (the direct open-bug search) plus *targets* (the open bugs the signature's
    DUPLICATEs resolve into), one row per bug id, oldest id first.

    A target the direct search already found keeps its row and gains ``venue_since`` (the later
    of the two, if both know one) and the union of ``via_duplicates``: bug 1976766 carried the
    signature itself by the time 2070711 was filed AND was the target of our duped 2069647, and
    it is the second fact that dates the tie."""
    from crashclouseau import sigage

    by_id = {r["id"]: r for r in rows or []}
    for t in targets or []:
        row = by_id.get(t["id"])
        if row is None:
            by_id[t["id"]] = dict(t)
            continue
        dated = [(sigage.to_datetime(s), s) for s in (row.get("venue_since"), t.get("venue_since"))
                 if s]
        dated = [(d, s) for d, s in dated if d is not None]
        if dated:
            row["venue_since"] = max(dated)[1]
        via = {*(row.get("via_duplicates") or []), *(t.get("via_duplicates") or [])}
        if via:
            row["via_duplicates"] = sorted(via)
    return [by_id[k] for k in sorted(by_id)]


# How many ``dupe_of`` hops to follow from a DUPLICATE on the signature to an open bug. BMO
# itself redirects a dup-of-a-dup at resolution time, so real chains are one or two long; the
# bound is against a cycle, not a budget.
_DUP_CHAIN_MAX_HOPS = 5


def _duplicate_targets_for_signature(signature, timeout=_HTTP_TIMEOUT, spellings=None):
    """The OPEN bugs that the RESOLVED DUPLICATE bugs on *signature* resolve into, as venue rows
    (``_venue_row`` plus ``venue_since`` and ``via_duplicates``), oldest id first; ``[]`` when
    there are none or BMO could not be asked.

    THE DUP IS THE HUMAN'S VERDICT ON THE SIGNATURE, and it is the one verdict the open-only
    venue search cannot see. When :teoxoy resolved our 2069647 as a duplicate of 1976766
    (2026-09-07 11:37) they were saying that ``wgpu_server_buffer_get_mapped_range`` crashes are
    bug 1976766 — a bug filed in July 2025 for the destroy-while-mapping race, which the
    September regressor re-signatured rather than created. Two minutes later they also copied the
    signature onto 1976766, which is the hygiene Calixte asks for on every dup; this function
    exists so the filer does not DEPEND on it, because the duplicate list of a crash bug is full
    of dups whose signature was never copied (plan 17: 5 of 7 duplicate targets of our filings
    were our own earlier bugs, and the target carried the variant signature in 2 of them).

    ``venue_since`` IS THE DUP'S RESOLUTION TIME (``cf_last_resolved``), the later one when
    several dups point at one target, and the latest hop when a chain is followed: it is the
    moment the signature was tied to the target, and ``_bug_for_this_regression`` reads it as
    the bug's clock in place of its creation time. So an old dup rescues nothing — a 2022
    signature duped into a 2022 bug still predates a 2026 regressor — and a dup resolved after
    the regressor landed is a human who looked at crashes that include this regression's and
    filed them under that bug. That is the same reasoning as the reopen rescue, on the field
    that actually records the decision.

    MEASURED over the 106 bugs Clouseau had filed by 2026-09-10, each rewound to its own filing
    instant against today's BMO (landing approximated by the filing time, which is generous):
    five filings had a DUPLICATE on their signature pointing at a then-open bug, and the clock
    rejects four of them — ties 120, 466, 967 days old (two of those targets are ``[meta]``
    trackers the meta split would drop anyway) — and accepts exactly one, 2070711 into 1976766
    at 2.4 days. The attachment rescue (``_signature_attached``) saw two and fired on the same
    one. So neither rule moves a filing a human later kept; both catch the one they were built
    on, by the clock rather than by a fitted number.

    CHAINS: a target that is itself RESOLVED DUPLICATE is followed (``_DUP_CHAIN_MAX_HOPS``);
    any other closed target is dropped, because a closed bug is not a comment venue and the
    FIXED-after-this-build question stays with ``_fixed_after_build_bug``, which does not follow
    dups. Public, unauthenticated, read-only like every venue lookup, so a restricted target
    comes back as a ``faults`` entry and is simply not a venue. FAILS OPEN — no rows on any
    failure — for the reason ``_fixed_after_build_bug`` gives: the direct search already fails
    closed for the whole path, and a second fail-closed BMO request would make one flaky read
    a silent filing stop."""
    sig = (signature or "").strip()
    if not sig:
        return []
    from crashclouseau import sigage

    # The caller's spellings when it widened them over the crash's other names (a dup on the
    # OLD name into an open bug is that human saying "this crash is bug N" too).
    spellings = sorted(set(spellings or ()) | utils.lambda_siblings(sig))
    params = {
        "include_fields": "id,summary,cf_crash_signature,dupe_of,cf_last_resolved",
        "j_top": "OR",
        "resolution": "DUPLICATE",
    }
    for i, spelling in enumerate(spellings):
        params["f{}".format(2 * i + 1)] = "cf_crash_signature"
        params["o{}".format(2 * i + 1)] = "substring"
        params["v{}".format(2 * i + 1)] = spelling
        params["f{}".format(2 * i + 2)] = "short_desc"
        params["o{}".format(2 * i + 2)] = "substring"
        params["v{}".format(2 * i + 2)] = "[@ " + spelling
    try:
        r = net.get(_bz_rest(), params=params, timeout=timeout)
        r.raise_for_status()
        dups = (r.json() or {}).get("bugs") or []
    except Exception as exc:                                   # pragma: no cover - network
        logger.warning("autofile: duplicate lookup failed for %r: %s", signature, exc)
        return []

    def tie(into, target, since, via):
        cur = into.setdefault(target, {"since": None, "via": []})
        if since is not None and (cur["since"] is None or since > cur["since"]):
            cur["since"] = since
        cur["via"] = sorted({*cur["via"], *via})

    pending = {}
    for d in dups:
        if not d.get("id") or not d.get("dupe_of"):
            continue
        if not any(_row_is_about(d, s) for s in spellings):
            continue
        tie(pending, d["dupe_of"], sigage.to_datetime(d.get("cf_last_resolved")), [d["id"]])
    out = {}
    seen = set()
    for _hop in range(_DUP_CHAIN_MAX_HOPS):
        # A target reached a second way (2070711 -> 2069647 -> 1976766 beside 2069647 ->
        # 1976766) merges its tie into the row it already has; anything else already visited
        # is a cycle and is dropped.
        for k in [k for k in pending if k in out]:
            t = pending.pop(k)
            row = out[k]
            since = sigage.to_datetime(row.get("venue_since"))
            if t["since"] is not None and (since is None or t["since"] > since):
                row["venue_since"] = t["since"].isoformat()
            row["via_duplicates"] = sorted({*row["via_duplicates"], *t["via"]})
        pending = {k: v for k, v in pending.items() if k not in seen}
        if not pending:
            break
        seen.update(pending)
        fetched = _bugs_by_id(sorted(pending), timeout=timeout)
        if fetched is None:
            return []
        following = {}
        for b in fetched:
            t = pending.get(b.get("id"))
            if t is None:
                continue
            resolution = (b.get("resolution") or "").upper()
            if not resolution:
                row = _venue_row(b)
                if t["since"] is not None:
                    row["venue_since"] = t["since"].isoformat()
                row["via_duplicates"] = t["via"]
                out[b["id"]] = row
            elif resolution == "DUPLICATE" and b.get("dupe_of"):
                resolved = sigage.to_datetime(b.get("cf_last_resolved"))
                since = max(x for x in (t["since"], resolved) if x is not None) \
                    if (t["since"] is not None or resolved is not None) else None
                tie(following, b["dupe_of"], since, [*t["via"], b["id"]])
        pending = following
    return [out[k] for k in sorted(out)]


def _bugs_by_id(ids, timeout=_HTTP_TIMEOUT):
    """The public BMO rows for *ids*, with what a venue row and a dup hop need; ``None`` when
    BMO could not be asked. A restricted id comes back in ``faults`` rather than failing the
    request, so it is simply absent from the result."""
    if not ids:
        return []
    params = {
        "id": ",".join(str(i) for i in ids),
        "include_fields": "id,status,resolution,dupe_of,cf_last_resolved,creation_time,"
                          "product,keywords,regressed_by",
    }
    try:
        r = net.get(_bz_rest(), params=params, timeout=timeout)
        r.raise_for_status()
        return (r.json() or {}).get("bugs") or []
    except Exception as exc:                                   # pragma: no cover - network
        logger.warning("autofile: bug lookup failed for %s: %s", ids, exc)
        return None


# The resolutions of a bug of ours on a signature that leave room for a NEW bug on it. FIXED:
# the defect was real and is gone, so a fresh crash on the signature is a fresh regression --
# `_fixed_after_build_bug` and `_known_on_train_bug` still decide whether it is one. DUPLICATE:
# the human's verdict moved to the TARGET, which `_duplicate_targets_for_signature` folds into
# the venue rows while it is open. Everything else -- INVALID, WORKSFORME, INCOMPLETE, WONTFIX,
# MOVED -- is a human saying the filing was not wanted, and the `skip` channels have treated
# every one of those as a full stop since the guard shipped.
_REFILEABLE_RESOLUTIONS = ("FIXED", "DUPLICATE")


def _own_bug_out_of_sight(prior, existing):
    """A ``{"filed": False, ...}`` decline when the bug WE already filed on this signature
    (*prior*, from ``Dossier.already_filed_for_signature``) is not among the *existing* venue
    rows and its state says a second bug must not be filed; ``None`` to carry on.

    THE ``comment``-MODE HALF OF THE PRIOR-FILING GUARD. On a ``skip``/``file_new`` channel a
    prior filing is a full stop before the venue search is made. On a ``comment`` channel our
    open bug is the ordinary venue -- the search returns it and ``already_commented`` declines
    the second analysis -- so there is only work to do when the search CANNOT see the bug, and
    why it cannot decides:

    * RESTRICTED. ``_bugs_by_id`` returns rows for the ids anonymous BMO may read and simply
      OMITS the rest (live probe 2026-09-16: ``id=2072488,1976766`` came back as one row and an
      empty ``faults``), so "absent" is the signal. Bug 2072488, 2026-09-16 03:02Z: a
      poison-address crash on ``core::ptr::drop_in_place | ... | style_traits::owned_slice::
      impl$1::drop`` filed restricted to core-security; 2072492, 2072493, 2072502 and 2072521
      followed by 08:01Z, one per proto-signature cluster of the SAME nightly build, each
      needinfo'ing the same developer, because the unauthenticated venue search sees none of
      them. ``already_filed_for_signature``'s docstring had named this exact case ("IT ALSO
      CLOSES A DISCLOSURE CASE") and nightly never called it. Skip, naming the bug -- and the
      same skip stops a run that is NOT withheld from filing a PUBLIC bug on the signature.
    * RESOLVED, neither FIXED nor DUPLICATE: a human closed our bug as not wanted. Skip. Never
      observed on nightly (the only other repeat, 2069647/2070711, was a DUPLICATE and is now
      followed), so this is the ``skip`` channels' rule applied for consistency, not a measured
      fix.
    * OPEN but not returned. Our bug carried the exact ``[@ sig]`` entry and title the day it
      was filed (``report_bug.bug_title``), so a human has edited both off it. Skip: this is
      about which way to be wrong, and a duplicate is the worse noise.
    * BMO unreadable: skip, like the venue search itself. Fails closed.
    * FIXED or DUPLICATE (``_REFILEABLE_RESOLUTIONS``): carry on; the existing gates own it.

    Not asked when our bug IS a venue row, or is the duplicate a venue row was reached through
    (``via_duplicates``) -- ``already_commented`` covers both, and asking BMO there would cost a
    request on every re-crash of every signature we ever filed."""
    bug = (prior or {}).get("bug")
    try:
        bug = int(bug)
    except (TypeError, ValueError):
        # The fail-closed sentinel (`{"skipped": ...}`) or a record with no id: silence, not a
        # possible duplicate, like every sibling guard.
        return {"filed": False, "skipped": "prior-filing lookup failed; not risking a duplicate",
                "prior_signature_filing": prior}
    for row in existing or []:
        if row.get("id") == bug or bug in (row.get("via_duplicates") or []):
            return None
    rows = _bugs_by_id([bug])
    if rows is None:
        state, why = "unreadable", "could not be read from Bugzilla"
    else:
        row = next((r for r in rows if r.get("id") == bug), None)
        if row is None:
            state, why = "restricted", "is restricted, so the venue search cannot see it"
        else:
            resolution = (row.get("resolution") or "").upper()
            if resolution in _REFILEABLE_RESOLUTIONS:
                return None
            if not resolution:
                state, why = "open", "is open but no longer carries the signature"
            else:
                state, why = resolution, "was resolved {}".format(resolution)
    return {"filed": False, "bug": bug,
            "skipped": "already filed bug {} for this signature; it {} — not filing "
                       "again".format(bug, why),
            "prior_signature_filing": prior, "own_bug_state": state}


def _via_clause(venue):
    """`` (on this crash's earlier name `P`)`` / `` (on its sibling spelling `P`)``, for a venue
    row reached through another name of the crash; ``""`` otherwise."""
    via = (venue or {}).get("via_signature")
    if not via:
        return ""
    kind = "earlier name" if venue.get("via_relation") == "handoff" else "sibling spelling"
    return " (on this crash's {} `{}`)".format(kind, via)


def _via_fields(venue):
    """The audit keys of a decline about a venue reached through another name."""
    via = (venue or {}).get("via_signature")
    if not via:
        return {}
    return {"venue_via_signature": via, "venue_via_relation": venue.get("via_relation")}


def _bucket_of(dossier):
    """The awaited-work bucket key of this crash (``hang.bucket_key`` via the orchestrator's
    ``hang_awaited_work`` record), or ``""`` when the crash is not a hang whose awaited thread
    was found."""
    work = ((dossier or {}).get("corroborations") or {}).get("hang_awaited_work") or {}
    return str(work.get("bucket") or "")


def _different_bucket(prior, bucket):
    """Was our earlier filing on this signature about a DIFFERENT bucket than *bucket*? Only
    when both KEYS are known: an unknown on either side reads as the same bucket, so the one-
    bug-per-signature stop keeps applying wherever the cohort cannot be told apart.

    THE KEY ALONE, NEVER THE TITLE. A bucket title is the model's sentence (or the mechanism's
    first sentence) and two runs on one cause write two of them, so "the titles differ" is not
    "the causes differ"; read as a difference it would file the same cause twice. A non-hang
    bucket bug has no key and is therefore one bug per signature (the lookup in
    ``models.Dossier.already_filed_for_signature`` matches any prior filing for it)."""
    previous = str((prior or {}).get("bucket") or "")
    bucket = str(bucket or "")
    return bool(previous and bucket and previous != bucket)


def _split_by_application(bugs, product):
    """``(venues, other_app)`` — the open bugs that can be about a *product* crash, and the
    ones that belong to a different application built on Gecko.

    Gecko is shared, so the signature is shared; the crash is not. Crash
    ``05381864-aa6e-402f-a1fd-56a3e0260816`` (Firefox nightly 155) had exactly ONE open bug
    anywhere on BMO for its signature — 2057980, ``MailNews Core :: Networking: Exchange``, a
    Thunderbird 153 crash triggered by a proprietary Exchange add-on and already understood
    there — and the age test happily accepted it, so the regression was reported into a
    Thunderbird bug, needinfo included. Same Gecko assertion, different application, different
    cause, and a team with no reason to read it.

    Not a duplicate risk either, which is what makes this a clean drop rather than a trade: a
    Firefox crash bug and a Thunderbird crash bug on one shared signature are two bugs by
    construction. The new bug cross-references what it filed past
    (``report_bug.build_other_app_bugs_note``).

    A bug with NO product counts as a venue. The caller's every default is to comment, so this
    may only drop what it can positively identify as somebody else's."""
    foreign = config.get_other_app_products(product)
    ours = [b for b in bugs or [] if (b.get("product") or "") not in foreign]
    theirs = [b for b in bugs or [] if (b.get("product") or "") in foreign]
    return ours, theirs


def _split_out_metas(bugs):
    """``(venues, metas)`` — the open bugs that can hold a crash report, and the ``[meta]``
    trackers that cannot.

    A meta bug is a list of other bugs. Posting a stack, a needinfo and a regressor claim into
    one buries the analysis among dozens of unrelated dependencies and asks the question of
    whoever happens to own the tracker. On the top 200 Firefox-nightly signatures (SuperSearch
    facet 2026-08-07..08-21) the oldest open same-application bug is a meta for 9/200 = 4.5%
    (95% CI 2.4–8.3), four distinct trackers: 1279293 ``[meta] Crash in [@ IPCError-browser |
    ShutDownKill]``, 858032 ``[meta] crashes in EnterBaseline / EnterJit``, 1472062 and 1588498.

    THE OBVIOUS DETECTOR IS THE WRONG ONE, and it is worth saying because it is the diagnosis
    this rule arrived with: tightening the summary search fixes NOTHING here. All nine arrive
    through ``cf_crash_signature`` — bug 1279293's own ``cf_crash_signature`` IS ``[@
    IPCError-browser | ShutDownKill]`` — and the count is 9/200 either way with the summary
    clause removed. The ``[meta]`` summary prefix is a convention, not a field; the ``meta``
    KEYWORD is set on all four, so that is what this reads.

    COST MEASURED ZERO: no meta appears among the 10 top-200 signatures where a two-day-old
    regressor SHOULD comment on an open bug, nor among the 6 venues the filer has ever
    accepted. It cannot: all four metas were filed 2013–2019, so ``_bug_for_this_regression``
    already rejects them for any recent candidate. The hole is only reachable through a path
    that SKIPS that test — which is why this ships with the unresolved-landing-date rule, where
    metas were 9 of the 96 wrong venues. Cross-referenced in the new bug
    (``report_bug.build_meta_bugs_note``) rather than silently dropped, for the same reason
    ``_split_by_application``'s bucket is, and because a new meta filed inside the 30-day window
    is the case this would otherwise get wrong in the other direction."""
    ours = [b for b in bugs or [] if "meta" not in (b.get("keywords") or [])]
    metas = [b for b in bugs or [] if "meta" in (b.get("keywords") or [])]
    return ours, metas


def _candidate_landed(dossier, channel):
    """When the suspected regressor landed, as a UTC datetime, or ``None``.

    Not read off the persisted candidate: ``Candidate.pushdate`` is ``null`` on every dossier
    in prod (nothing fills it once the seed's per-node map is gone) and ``Candidate.channel``
    is likewise always ``""`` — hence ``uuid_info``'s channel, the same one
    ``resolve_product_component`` is given.

    Free online, which is why this can sit on the filing path at all: the orchestrator already
    resolved this node's hg ``json-rev`` during the run (the backout gate and the git-commit
    link both go through it) and ``sigage`` caches per ``(node, channel)``, so this is a dict
    hit rather than hg's measured 8-13s.

    NOT best-effort any more. ``None`` now costs every open bug its venue
    (``_bug_for_this_regression``), so it is worth knowing how reachable ``None`` is. Replayed
    offline over the 52 candidate nodes of the 52 bugs filed since 2026-08-05, this function
    answered 52/52 (median 11.6 s cold). But the cache it rides on is a NEGATIVE cache —
    ``sigage.json_rev`` stores ``{}`` on failure (``_JSON_REV_CACHE[key] = out``, sigage.py:491)
    — so ONE hg 406 anywhere earlier in the run makes this return ``None`` in 0.00 s with hg
    healthy again. The exposure is "did any json-rev read for this node fail earlier in the
    run", not "did this read fail". Prod-time witness: the ``(gh)`` link comes from the SAME
    ``json_rev`` dict (``orchestrator._resolve_candidate_git_commit``), so a filed comment with
    an hg link and no ``[gh]`` is a run where it returned nothing. 5 of the 52 filings have no
    ``[gh]``; 4 are pre-git-migration nodes with no ``git_commit`` in hg at all (2020-01,
    2022-04, 2022-12, 2024-12), and the fifth — bug 2060924, node 74675cc139d9 — has
    ``git_commit=9d7faea5127c…`` today. Witnessed 1/48 = 2.1% (95% CI 0.4–10.9)."""
    node = ((dossier or {}).get("candidate") or {}).get("node")
    if not node:
        return None
    from crashclouseau import sigage

    try:
        return sigage.to_datetime(sigage.pushdate_for_node(node, channel))
    except Exception as exc:                                   # pragma: no cover - network
        logger.warning("autofile: landing date for %s unresolved: %s", node, exc)
        return None


_CLOSED_STATUSES = {"RESOLVED", "VERIFIED", "CLOSED"}


def _last_reopened(bug_id):
    """When *bug_id* was last REOPENED, or ``None`` if it never was (or we could not tell).

    A crash bug's creation time stops describing it the moment somebody reopens it: bug 1990812
    was filed in September, fixed in October and reopened in November because the crash came
    back, and it is the November date that says whether it is the venue for a November cause.
    BMO exposes this nowhere in a search — only in ``/rest/bug/<id>/history`` — so this is a
    second request, and it is only ever made for a bug the cheap creation-time test has already
    rejected (2 of the canary's first 20 filings saw ANY open bug at all).

    Matches on the status LEAVING a closed state rather than on the string ``REOPENED``: bugs
    are routinely reopened straight to NEW or ASSIGNED.

    Unauthenticated, like the search that produced ``bug_id``. Raises nothing — this is a rescue
    for a bug we have already decided against, so a failure simply leaves that decision standing
    rather than flipping it."""
    history = _bug_history(bug_id)
    if history is None:
        return None
    from crashclouseau import sigage

    last = None
    for entry in history:
        for change in entry.get("changes") or []:
            reopen = change.get("field_name") == "status" \
                and change.get("removed") in _CLOSED_STATUSES
            if not reopen:
                continue
            when = sigage.to_datetime(entry.get("when"))
            if when is not None and (last is None or when > last):
                last = when
    return last


def _bug_history(bug_id):
    """``/rest/bug/<id>/history`` as BMO returns it, or ``None`` when it could not be read.
    Shared by the two history rescues so each stays a one-line question."""
    try:
        r = net.get("{}/{}/history".format(_bz_rest(), bug_id), timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        return ((r.json() or {}).get("bugs") or [{}])[0].get("history") or []
    except Exception as exc:                                   # pragma: no cover - network
        logger.warning("autofile: history lookup failed for bug %s: %s", bug_id, exc)
        return None


def _signature_attached(bug_id, signature):
    """When *signature* was last ADDED to *bug_id*'s ``cf_crash_signature`` after the bug already
    existed, or ``None`` if it was there from the start, never there, or we could not tell.

    THE SECOND HISTORY RESCUE, and the same argument as the reopen: a bug's creation time stops
    dating its relationship to a signature the moment somebody attaches that signature to it.
    Bug 1976766 was filed 2025-07-10 for ``WebGPUParent::MapCallback``; on 2026-09-07 11:39
    :teoxoy added ``[@ wgpu_bindings::server::wgpu_server_buffer_get_mapped_range]`` to it,
    three days AFTER the regressor that produced that signature landed — a human who had looked
    at the new crashes and filed them under the old bug. Two days later the age test rejected
    1976766 by fourteen months and we filed 2070711 past it. The signature was attached after
    the cause landed, so the bug is live for the cause, whatever year it was opened in.

    Read off ``cf_crash_signature`` changes whose ``added`` value carries an EXACT entry for the
    signature (any lambda spelling, ``_signature_field_entries``) and whose ``removed`` value
    does not — BMO records the whole old and new field, so that is "this change attached it",
    and a change that merely reshuffles a field already carrying it is not. Latest such change
    wins, as with reopens. Only ever asked about a bug the creation-time test has already
    rejected, and only from the filer (``_bug_for_this_regression`` needs the signature passed
    to ask it), so the extra request rides on the rare path. Raises nothing, for the same
    rescue-not-gate reason as ``_last_reopened``."""
    sig = (signature or "").strip()
    if not sig:
        return None
    history = _bug_history(bug_id)
    if history is None:
        return None
    from crashclouseau import sigage

    spellings = {s.lower() for s in utils.lambda_siblings(sig)}
    last = None
    for entry in history:
        for change in entry.get("changes") or []:
            if change.get("field_name") != "cf_crash_signature":
                continue
            added = {e.lower() for e in _signature_field_entries(change.get("added"))}
            removed = {e.lower() for e in _signature_field_entries(change.get("removed"))}
            if not (spellings & added) or (spellings & removed):
                continue
            when = sigage.to_datetime(entry.get("when"))
            if when is not None and (last is None or when > last):
                last = when
    return last


def _bug_for_this_regression(bugs, landed, max_age_days, candidate_bug=None, signature=None):
    """Which open bug this crash belongs in: ``(bug_id or None, ids that predate the cause)``.

    The oldest open bug for a signature is the canonical one only when it can be about the same
    crash, and it often cannot. ``nsAtom::IsStatic`` has had bug 1798397 open since 2022 — a
    bug whose own comments propose adding ``nsAtom`` to the irrelevant-signature list — while
    the regressor named for ``ddeac1a4-64d1-4413-b03b-f79540260809`` landed 1375 days later.
    Commenting there filed a fresh Nightly regression under four years of unrelated discussion,
    where nobody watching that bug had any reason to read it as new.

    THE TEST IS THE SAME ONE THE STALE-SIGNATURE GATE MAKES, against a different clock. A bug
    that already existed before the candidate landed describes crashes the candidate cannot
    have caused, so it is not this crash's venue. Signature reuse is exactly why: an old
    signature acquiring a new cause is a real and common thing, and the new cause deserves a
    bug someone will actually look at. ``max_age_days`` of slack keeps a bug filed at around
    the same time as the regressor — plausibly about it — as the venue.

    THE CLOCK IS THE LAST TIME A HUMAN TIED THIS SIGNATURE TO THE BUG, when that is later than
    the bug's creation. Three things record such a tie, and each is the reopen argument again on
    a different field: a REOPEN (below); a DUPLICATE on the signature resolved into this bug
    (``venue_since``, set by ``_duplicate_targets_for_signature``); and the signature being ADDED
    to the bug's ``cf_crash_signature`` (``_signature_attached``, asked when *signature* is
    given). Bug 2070711 is the case all three of the missing ones would have caught: our 2069647
    duped into 1976766 on 2026-09-07 and the signature attached there two minutes later, both
    after the regressor landed on 09-04, both invisible to a creation-time test that read
    1976766 as fourteen months too old and filed the same analysis a second time.

    TWO THINGS OUTRANK THE AGE TEST, and both were found by replaying it over every filing the
    canary had already made:

    * ``candidate_bug`` — the bug the suspected regressor was written FOR. If that bug is one of
      the open ones, it is the venue whatever the dates say: the crash is that work coming back.
      Crash b66819b5's candidate ``e6335c6fffd3`` is literally "Bug 1990812 - handle the case
      where switching the decoder state machine fails due to shutdown", and 1990812 was open.
    * a REOPEN after the candidate landed (``_last_reopened``). A bug's creation stops
      describing it once someone reopens it, and crash bugs get reopened all the time when a
      signature comes back. 1990812 again: filed September, fixed by the candidate in October,
      reopened that November. On creation time alone it missed the 30-day window by ONE day.

    Creation time is otherwise a deliberately CONSERVATIVE proxy for "crashes were already
    happening": a bug is always filed at or after the crash it reports, so it can only
    understate the gap, and understating it means commenting rather than filing.

    ONE-SIDED ON PURPOSE; THE TWO-SIDED VERSION IS MEASURED DEAD. A bug created long AFTER the
    candidate landed is accepted unconditionally, which reads like a hole. It is not reachable
    from the direction it looks reachable from — BMO cannot return a future ``creation_time`` —
    and the direction it really exercises is a candidate that landed long BEFORE the bug, which
    is the mechanism working. The one real venue acceptance that decides it is bug 1830323,
    ``Crash in [@ mozilla::EbmlComposer::WriteSimpleBlock]``, ASSIGNED, created 2023-04-27,
    where we commented on 2026-08-20 with candidate 7dfc286be921 ("Bug 1577198 - Don't write
    cluster sizes…") that landed 2021-02-11 — 805 days before the bug existed, same signature,
    still open and owned, and the right venue. Every bound tried (±30, ±90, ±180, ±365) eats it
    and files a duplicate of an assigned bug, and with n=1 there is no panel to fit one on.

    AN UNKNOWN LANDING DATE IS NOT A LICENCE TO COMMENT — the one unknown that does NOT fail
    toward commenting, and the change that stopped it. Replaying the chooser over the top 200
    Firefox-nightly signatures (SuperSearch facet 2026-08-07..08-21, 15131 crashes), 102 have at
    least one open same-application bug; with a two-day-old regressor it files new for 92 of
    them and comments on 10, and with the date withheld it comments on 102/102 and picks a
    DIFFERENT venue for 96/102 = 94.1% (95% CI 87.8–97.3) — median age 1022 days (p25 327, p75
    2500, max 6264), 74% older than a year, 48% older than three, 9% of them ``[meta]``. On the
    52 real filings that is 5 hg-blind comments instead of 1, and 4 of the 5 are wrong:
    2062219→1798397 (+1377 d, the nsAtom::IsStatic bug this docstring opens on), 2063003→1863599
    (+1005 d, a JS Engine bug that merely lists ``nsCharTraits<T>::copy``), 2063364→1874575
    (+836 d) and 2064274→1695119 (+1964 d). Failing CLOSED is wrong for the other 10/102 = 9.8%
    (CI 5.4–17.1) — 9.6x less often — and its wrong outcome is a duplicate that NAMES what it
    filed past (``report_bug.build_related_bugs_note``) instead of a needinfo'd analysis buried
    in a median-2.8-year-old stranger's bug.

    IT MUST NOT EAT the three legitimate comment venues, and it does not: all three resolve a
    landing date, so they never reach this branch — bug 1898399 (gap −9 d), 1999518 (−4 d) and
    1830323 (−805 d) get the same venue as before. Nor bug 1990812, which the ``candidate_bug``
    shortcut above answers before any date logic, with or without a date.

    The other unknowns still fail toward COMMENTING, because there a duplicate on BMO is the
    worse noise: a creation time BMO did not return or that will not parse keeps the bug, and so
    does an unreachable reopen history — that one is a rescue and not a gate, so a BMO blip
    leaves the age verdict standing rather than flipping it.

    Scans oldest-first and takes the first plausible bug rather than testing only the oldest:
    with a 2022 bug and one we filed last week both open, the right answer is last week's, not
    a third bug."""
    from crashclouseau import sigage

    ids = [b["id"] for b in bugs or []]
    if candidate_bug and candidate_bug in ids:
        return candidate_bug, []
    if landed is None:
        # No clock, no verdict. Returned as `predating` so the caller files a new bug that
        # cross-references these and says WHY (`report_bug.build_related_bugs_note`), rather
        # than skipping: a silent skip loses the analysis, and this is a hg blip, not evidence.
        return None, ids

    def within(clock):
        return (landed - clock).total_seconds() / 86400.0 <= max_age_days

    predating = []
    for bug in bugs or []:
        created = sigage.to_datetime(bug.get("creation_time"))
        if created is None:
            return bug["id"], []
        # A dup resolved into this bug dates the tie, not the bug's own filing.
        tied = sigage.to_datetime(bug.get("venue_since"))
        if within(max(created, tied) if tied is not None else created):
            return bug["id"], predating
        reopened = _last_reopened(bug["id"])
        if reopened is not None and within(reopened):
            return bug["id"], predating
        attached = _signature_attached(bug["id"], signature) if signature else None
        if attached is not None and within(attached):
            return bug["id"], predating
        predating.append(bug["id"])
    return None, predating


def _fixed_after_build_bug(signature, buildid, product, spellings=None):
    """The id of a bug on *signature* that was RESOLVED FIXED **after** *buildid* was produced,
    or ``None``. In one line: is this crash a pre-fix report of a defect somebody has already
    fixed?

    Asked only when we are about to file a NEW bug. ``_open_bugs_for_signature`` filters
    ``resolution="---"`` and must keep doing so — a closed bug is not a comment venue — so this
    is a SIBLING asking the other question, not a widening of that one, and the two param dicts
    are deliberately duplicated (30+ tests mock that function by name and one pins the exact
    three-key row it returns). Keep them in step by hand. (The one closed resolution that search
    does follow is DUPLICATE, and only to reach the OPEN bug it points at —
    ``_duplicate_targets_for_signature``; a dup into a FIXED bug is still this function's
    question, and it does not follow dups.)

    (a) THE OBVIOUS PREDICATE is "a closed bug on this signature means the crash was already
    reported", i.e. just drop the filter. (b) IT IS DEAD, measured over the 52 bugs the canary
    has filed (BMO ``creator=cdenizet@mozilla.com``, ``creation_time>=2026-08-05``, summary
    ``Crash in [@``), each one rewound through ``/rest/bug/<id>/history`` to its own filing
    instant. Dropping the filter moves 4 of the 52 VENUES and 3 of the 4 are wrong: filing
    2062119 would have commented into bug 1861423, open since 2023-10-26 and closed WORKSFORME
    on 2025-03-24, instead of filing the bug a human then FIXED; 2063234 (still open today) into
    1816975, FIXED in 2023; 2064066 into 2054485. Only 2064537 -> 2063862 is right. Using the
    closed bugs to SUPPRESS rather than to comment is no better while it is ungated: "any closed
    bug on the signature" suppresses 17 of the 52 and destroys 13 good filings (8 still open, 5
    FIXED) to catch 3 duplicates, and "any FIXED bug" suppresses 14 and destroys 10.

    (c) THE BUILD DATE IS THE WHOLE RULE, the same shape as the bad-machine denominator. A fix
    that landed before this build existed is not this crash's fix, whatever the signature says.
    Requiring ``cf_last_resolved`` to POSTDATE the build suppresses 1 of the 52, and that one is
    bug 2064537, which a human closed as a duplicate of 2063862 — RESOLVED FIXED
    2026-08-17T08:07:20, crash build 20260816083833, filed 2026-08-18T21:15. No threshold was
    fitted: across the 33 FIXED bugs the unfiltered query surfaces over those 52 filings the one
    firing margin is +1.0 d and the closest non-firing one is -22.4 d, so the test is the SIGN of
    a 23-day gap, not a number read off the motivating case.

    ``cf_last_resolved`` is the bug's RESOLUTION clock, not its patch's LANDING clock. They
    agree to the second on the one firing case (2063862) and no plausible clock error is 22
    days wide, which is the gap to the nearest competing margin — but a bug resolved long after
    its patch merged reads as a fix postdating a build that already contains it, and that is
    the one way this can eat a good filing with no signature reuse involved.

    (d) WHAT IT MUST NOT EAT is a post-fix crash on a REUSED signature. Our 2064066 (build
    20260812202037) carries bug 2054485 RESOLVED FIXED 22.4 days BEFORE that build, plus 2048851
    at -43.9 d, 1823765 at -1238 d and 1809003 at -1310 d; 2063234, still open today, carries
    1897201 at -808 d; 2060924 (FIXED) carries 1983101 at -334.7 d. An old signature acquiring a
    new cause is the normal case — it is why ``_bug_for_this_regression`` exists — and all three
    of those filings survive because the margin is negative.

    ONLY ``FIXED`` COUNTS. 16 of the 49 closed bugs the unfiltered query adds across those 52
    filings are INCOMPLETE (9), DUPLICATE (4) or WORKSFORME (3). "Nobody could reproduce it" and
    "it was filed twice" say nothing about whether this crash still happens, and WORKSFORME is
    exactly what bug 1861423 above is.

    ``_split_by_application`` FIRST, and it is not ceremonial. 3 of those 49 are Thunderbird
    bugs — 2011814 on our 2061960's signature, 2001729 and 1954381 on our 2063003's — and none
    of the three fires the gate; but on the control sample below the split removes a firing
    outright. ``shutdownhang | mozilla::SpinEventLoopUntil | nsThread::WaitForAll…`` on build
    20260408160318 is suppressed by exactly one bug — 1524247, product ``MailNews Core``.
    Gecko's signatures are shared; the crash is not.

    FAILS OPEN, deliberately unlike its sibling: a lookup failure logs and files. The venue
    lookup in ``autofile_bug`` already fails closed for the whole path, so a second fail-closed
    network dependency would let one flaky BMO request become a silent global filing stop — for
    a rule that fires on 1 filing in 52 — and a stalled pipeline in this product has no alarm.

    DOMAIN: builds no more than about two weeks old. All 52 filings sit on builds 0.2-9.3 days
    old (median 1.7) and ``config._SWEEP_DEFAULTS["max_age_s"]`` is 14 days. On a 14-day nightly
    control sample (60 reports/day from 2026-08-07; 599 distinct (signature, build) pairs over
    287 signatures) the rule fires on 47/599 = 7.8% overall — 48 before the application split
    above — but that rate is a pure function of build age (measured as of 2026-08-21): 6.0%
    for the 414 pairs at most 14 days old (25 fires, unchanged by the split) against 25.9% for
    the 54 pairs older than 90 days — 27.8% before the split, whose single removal lands in
    that bucket — where signature reuse dominates. Re-measure before the sweep window grows or
    beta/Fenix is enabled. 21 of the 25 in-domain firings are suppressed by a bug Clouseau
    itself filed and a human then fixed (8 of the 11 distinct suppressing bugs; the other 3 are
    aryx's), which is exactly the shape this is for.

    KILLED ALTERNATIVE, recorded so nobody rebuilds it: keying the dedup on the SUSPECTED
    REGRESSOR NODE instead of the signature. It looks strictly better — it would catch 4 of the
    panel's 7 duplicates against the signature key's ceiling of 2, and costs no BMO request at
    all — and it is dead. 13 pairs among the 52 filings share a candidate node and only 4 of
    those pairs are a true duplicate relation. The killer is 2061973 vs 2061975: same node
    ``dfbb73240fbf``, same build 20260806095421, two different zlib-rs signatures
    (``zlib_rs::inflate::inflate_fast_help_impl`` and
    ``zlib_rs::inflate::writer::Writer::copy_match_help``), both still open and both worked by
    humans (gsvelto on 2061973; ryanvm and glob on 2061975). Adding the build to the key does not
    rescue it — 2064436 and 2065075 also share node ``e7ad1bf72931`` and build 20260818092026 and
    are two different bugs.

    CEILING, so the next reader expects the right amount: a signature-keyed dedup can see at most
    2 of the panel's 7 duplicates at filing time, and this gate catches 1. 5 of the 7 are
    cross-signature and 5 of the 7 targets are our OWN earlier filings — plan 17's defect A, not
    this one. The headline case is NOT caught: bug 2063003 was filed 2026-08-12T15:19 against bug
    2062219, whose ``cf_crash_signature`` carried only ``[@ nsAtom::IsStatic]`` until a human
    added the variants 2h25m later. What this does buy is that human triage hygiene starts
    paying: replaying those 7 with the target's signature present, the gate fires on 3 at the
    real filing instant and on 6 of 7 against BMO as of today, and the resolution filter was the
    only thing standing in the way.

    Public, unauthenticated, read-only — like the venue lookup, and for the same reason: we must
    not reason about a security bug only the filing account can see. Lowest bug id wins when
    several qualify, a tie-break only (0 of the 52 filings had more than one)."""
    sig = (signature or "").strip()
    if not sig or buildid is None or buildid == "":
        return None
    from crashclouseau import sigage

    # ``uuid_info["buildid"]`` is a tz-aware datetime in prod (``UUID.get_bid_chan_by_uuid``
    # converts the column), a ``YYYYMMDDHHMMSS`` string everywhere a crash is described by hand.
    build_dt = sigage.to_datetime(buildid if isinstance(buildid, datetime) else str(buildid))
    if build_dt is None:
        return None
    # The SAME query shape and the SAME re-check as `_open_bugs_for_signature`, minus the
    # `resolution` filter — that one difference is the whole point of this function, and
    # sharing everything else is what keeps the two questions answerable about one bug set.
    # (It used to run its own `_is_specific_signature` gate on the summary clause; that length
    # test was retired for `_summary_is_about`, which decides the same 200-signature panel on
    # FORM instead of on a 16-character threshold read off two points.)
    for bug, resolved in _fixed_bugs_about(sig, product, spellings=spellings):
        if resolved is not None and resolved > build_dt:
            return bug["id"]
    return None


_FIXED_BUGS_CACHE: dict = {}


def _fixed_bugs_about(signature, product, use_cache=False, major=None,
                      resolutions=("FIXED",), spellings=None):
    """``[(bug_row, resolved_datetime), ...]`` for the bugs on *signature* that are RESOLVED
    FIXED and belong to this crash's own application, lowest id first.

    The query, the ``_split_by_application`` drop, the ``_row_is_about`` re-check and the
    FIXED-only rule are shared by the TWO questions the resolution date can answer — was this
    crash reported before somebody fixed it (``_fixed_after_build_bug``), and is this crash
    still happening on a build that already contains the fix (``_incomplete_fix_bug``). They are
    mirror images across the same inequality, so they must see the same bug set or the pair
    stops being exhaustive. Every argument for the filters is in ``_fixed_after_build_bug``.

    Deliberately NOT merged with ``_open_bugs_for_signature``: that one filters
    ``resolution="---"`` and must keep doing so (a closed bug is not a comment venue), and 30+
    tests mock it by name. Keep the two param dicts in step by hand.

    Public, unauthenticated, read-only. ``[]`` on any failure, which both callers read as "no
    information" — see each one for which way that makes it fail.

    ``spellings`` widens the question over the crash's other names (``sigfamily``): a bug FIXED
    on the old name after this build is this crash's fix as much as one on the new name."""
    sig = (signature or "").strip()
    if not sig:
        return []
    from crashclouseau import sigage

    names = [sig] + sorted(s for s in set(spellings or ()) if s and s != sig)

    # OPT-IN caching, per (signature, product), for the worker's lifetime. `_incomplete_fix_bug`
    # asks this on every run that would otherwise file nothing -- ~90% of them -- and the same
    # signature recurs across proto-clusters and build days, so uncached it is a BMO request per
    # dossier on a service that rate-limits for ~45 minutes.
    #
    # OFF for `_fixed_after_build_bug`, deliberately: that one SUPPRESSES a filing, it runs at
    # most once per filing attempt so it costs nothing to keep live, and a stale "no FIXED bug
    # yet" would let through exactly the duplicate it exists to stop.
    key = (sig, product or "", major, tuple(resolutions), tuple(names[1:]))
    if use_cache and key in _FIXED_BUGS_CACHE:
        return _FIXED_BUGS_CACHE[key]

    fields = ("id,summary,status,resolution,product,component,"
              "cf_crash_signature,cf_last_resolved,creation_time,"
              "assigned_to,assigned_to_detail")
    if major:
        # The crash's own train's status flag (`cf_status_firefox155`): the one field that says
        # whether a FIXED bug's fix is on THIS train. See `_known_on_train_bug`.
        fields += ",{}".format(_train_flag_field(major))
    params = {
        # `assigned_to` AND `assigned_to_detail`: BMO returns a `*_detail` field only when the
        # BASE field is also requested, and silently omits it otherwise -- which read as
        # "unassigned" here rather than as an error.
        "include_fields": fields,
        "j_top": "OR",
    }
    for i, name in enumerate(names):
        params["f{}".format(2 * i + 1)] = "cf_crash_signature"
        params["o{}".format(2 * i + 1)] = "substring"
        params["v{}".format(2 * i + 1)] = name
        params["f{}".format(2 * i + 2)] = "short_desc"
        params["o{}".format(2 * i + 2)] = "substring"
        params["v{}".format(2 * i + 2)] = "[@ " + name
    try:
        r = net.get(_bz_rest(), params=params, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        bugs = (r.json() or {}).get("bugs") or []
    except Exception as exc:                                   # pragma: no cover - network
        logger.warning("autofile: fixed-bug lookup failed for %r: %s", signature, exc)
        return []
    ours, _theirs = _split_by_application(bugs, product)
    out = []
    wanted = {r.upper() for r in resolutions}
    for bug in sorted((b for b in ours if b.get("id")), key=lambda b: b["id"]):
        if (bug.get("resolution") or "").upper() not in wanted:
            continue
        if not any(_row_is_about(bug, name) for name in names):
            continue
        out.append((bug, sigage.to_datetime(bug.get("cf_last_resolved"))))
    if use_cache:
        _FIXED_BUGS_CACHE[key] = out
    return out


# The `cf_status_firefox<N>` values that say "this train has the bug and not the fix". `fixed`,
# `verified` and `unaffected` say the opposite; `---` says nothing.
_TRAIN_HAS_THE_BUG = frozenset({"affected", "wontfix", "fix-optional", "disabled"})


def _train_flag_field(major):
    return "cf_status_firefox{}".format(major)


def _major_version(version):
    """``"155.0.1"`` -> ``155``; ``None`` when there is no leading number to read."""
    m = re.match(r"\s*(\d+)", str(version or ""))
    return int(m.group(1)) if m else None


def _known_on_train_bug(signature, product, major, spellings=None):
    """The RESOLVED bug that already tracks this signature ON THIS TRAIN, or ``None``:
    ``{"id", "field", "flag", "resolution"}``.

    In one line: somebody fixed (or declined to fix) this, the fix is NOT on the train this crash
    came from, and their bug says so in the crash's own status flag -- so this crash is that bug.

    THE GAP IT CLOSES. Bug 2070489 (2026-09-09): a release 155.0.1 crash on `AsyncShutdownTimeout
    | quit-application | newtabTrainhopAddon scheduleUpdateTrainhopAddonState shutting down`,
    filed as a NEW bug naming a regressor, while bug 2016440 -- the exact signature, RESOLVED
    FIXED on 09-01 with `cf_status_firefox155 = wontfix` and 156/157 = fixed -- sat there with
    Ryan's April comment "we're still seeing this spike every time a new trainhop addon ships".
    All three existing gates let it through, each by design: the venue lookup is open-only
    (``_open_bugs_for_signature``), ``_fixed_after_build_bug`` compares the RESOLUTION date
    (09-01) with the build date (09-03) and reads the fix as older than the build, and
    ``_incomplete_fix_bug`` asks whether the bug OWNS the signature (filed within a week of it),
    which a 206-day-later bug does not. None of them reads the per-train flag, which is the one
    field Mozilla maintains for exactly this question.

    A STATUS-FLAG RULE, NOT A THRESHOLD. The flag is set by the people who decided where the fix
    ships; `wontfix` on the crash's train means "known here, deliberately not fixed here", and
    `affected` means "known here, fix pending". Either way the crash belongs to that bug and a
    new bug is a duplicate. ``fixed``/``verified`` on this train is the incomplete-fix question
    (``_incomplete_fix_bug``) and ``---``/``unaffected``/absent says nothing, so both fall
    through to the ordinary rules.

    FIXED and WONTFIX resolutions only: an INVALID/WORKSFORME/INCOMPLETE bug's flags describe a
    bug nobody confirmed. Same application split and exact-signature re-check as its siblings.
    FAILS OPEN like ``_fixed_after_build_bug``, and for the same reason: the venue lookup
    already fails closed for the whole path."""
    sig = (signature or "").strip()
    if not sig or not major:
        return None
    field = _train_flag_field(major)
    for bug, _resolved in _fixed_bugs_about(sig, product, major=major,
                                            resolutions=("FIXED", "WONTFIX"),
                                            spellings=spellings):
        flag = str(bug.get(field) or "").strip().lower()
        if flag in _TRAIN_HAS_THE_BUG:
            return {"id": bug["id"], "field": field, "flag": flag,
                    "resolution": bug.get("resolution")}
    return None


# How far apart a signature's first appearance and its bug's filing may be and still count as
# THAT BUG'S OWN signature. Not a tuned number and not read off the motivating case: it is the
# same contemporaneity unit the stale-signature gate already uses for "did this land near when
# the signature appeared". The prod panel's firing case is 0 days and its nearest miss is 28.
_OWN_SIGNATURE_GRACE = timedelta(days=7)


def _incomplete_fix_bug(signature, buildid, product, channel, first_seen=None):
    """The bug whose FIX IS ALREADY IN THIS BUILD and whose crash is still happening — the
    mirror of ``_fixed_after_build_bug`` across the same inequality. ``None``, or
    ``{"id", "resolved", "node", "pushdate", "component", "assigned_to", "predates_days"}``.

    In one line: somebody fixed this, the fix shipped, and it is still crashing — so there is
    something to investigate whether or not we can name a changeset.

    THREE CONDITIONS, and the third is what makes it a rule rather than a nuisance.

    1. A bug on this signature is RESOLVED FIXED and its resolution predates this build
       (``_fixed_bugs_about``).
    2. Its fix actually LANDED on this channel before this build, from our own pushlog
       (``models.Node.landing_for_bug``) — not inferred from ``cf_last_resolved``, which is the
       resolution clock. Requiring the node is why this can say "the fix is in this build"
       instead of "the bug was closed before it". It also bounds the rule to
       ``Node.clean``'s 30-day retention, so an older fix answers ``None``: a conservative miss,
       logged, never a false claim.
    3. THE BUG OWNS THE SIGNATURE: the bug was filed within ``_OWN_SIGNATURE_GRACE`` of the
       signature first appearing, so that bug exists BECAUSE this signature appeared, rather
       than being one of several defects that happen to share a generic frame.

       SYMMETRIC, and the second direction is not hypothetical. A signature predating its bug
       is the reuse case below. A BUG predating its SIGNATURE by years is a triager adding a
       new signature to an old bug — the same "these crash alike" judgement, and just as far
       from "this bug is why that signature exists". One condition covers both.

    Condition 3 is the whole rule, and dropping it is measurably wrong rather than merely
    noisy. Over 21 days of prod, 26 (signature, FIXED bug) pairs pass conditions 1-2 across 22
    signatures — about 1.2 a day. The counter-example that decides it is ``nsAtom::IsStatic``
    (bug 2062219, fixed 2026-08-12): still crashing on post-fix builds, and the fix plainly
    WORKED — 50 reports / 8 installations per build before, 1-2 / 1-2 after. Filing that is
    filing a bug about a fixed crash. Its signature predates its bug by 3,073 days, so
    condition 3 drops it. The rest of what condition 3 drops is the same shape:
    ``mozilla::ipc::FatalError | IProtocol`` (1,030 d, 4-6 bugs), ``<unknown in ntdll.pdb>``
    (5 bugs across 3 products), ``nsHttpChannel::OnStartRequest`` (26 bugs across 5 products).

    It is also exactly the population ``_fixed_after_build_bug``'s docstring already warns must
    not be eaten — "a post-fix crash on a REUSED signature": our 2064066 carries bug 2054485
    FIXED 22.4 days BEFORE its build, plus 2048851 at -43.9 d, 1823765 at -1238 d and 1809003 at
    -1310 d; 2063234 carries 1897201 at -808 d; 2060924 carries 1983101 at -334.7 d. All six
    fail condition 3, so this gate cannot resurrect what that one is careful to allow through.

    NO DAY COUNT WAS FITTED. The 12 pairs in that panel with a measurable first-seen predate
    their bug by 0, 28, 162, 190, 1030, 1054, 1055, 1069, 3073, 4599, 4892 and 4906 days, so
    condition 3 is deciding the SIGN of a gap with a 28-day nearest miss, not a threshold read
    off the one case that motivated it (``libc.so.6 | cuEGLApiInit`` / bug 2063678, gap 0 — the
    bug was filed the day the signature first appeared).

    ``first_seen`` is a buildid or datetime the caller already has (the dossier records both
    signature clocks); looked up only when absent. An UNKNOWN first-seen answers ``None`` —
    ``SignatureFirstDate``'s cron has not minted a row for the newest signatures, which is this
    rule's own target class, so a missing clock must never read as "brand new".

    FAILS OPEN like its mirror: no bugs, no lookup, no node — no claim, and the ordinary
    filing rules decide."""
    sig = (signature or "").strip()
    if not sig or buildid is None or buildid == "":
        return None
    from crashclouseau import sigage

    build_dt = sigage.to_datetime(buildid if isinstance(buildid, datetime) else str(buildid))
    if build_dt is None:
        return None
    seen_dt = sigage.to_datetime(first_seen) if first_seen else None
    if seen_dt is None:
        return None

    for bug, resolved in _fixed_bugs_about(sig, product, use_cache=True):
        if resolved is None or resolved >= build_dt:
            continue
        filed = sigage.to_datetime(bug.get("creation_time"))
        if filed is None or abs(filed - seen_dt) > _OWN_SIGNATURE_GRACE:
            continue                      # not contemporaneous: not this bug's own signature
        landing = models.Node.landing_for_bug(bug["id"], channel)
        if not landing or landing["pushdate"] >= build_dt:
            logger.info("autofile: bug %s fixed %s owns %r but no landing of it is in our "
                        "pushlog before build %s — not claiming the fix is in this build",
                        bug["id"], (bug.get("cf_last_resolved") or "")[:10], sig, build_dt)
            continue
        return {
            "id": bug["id"],
            "resolved": bug.get("cf_last_resolved"),
            "node": landing["node"],
            "pushdate": landing["pushdate"],
            "component": bug.get("component"),
            "product": bug.get("product"),
            "assigned_to": ((bug.get("assigned_to_detail") or {}).get("email") or ""),
            "predates_days": max(0, (filed - seen_dt).days),
        }
    return None


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
    """True when NO component of the signature names code: every part is a bare address, a
    module name, a generic crash word or an OS lock/heap primitive (``sigfamily.
    is_unsymbolicated``).

    Bare addresses alone were the rule until 2026-09-19, and it let three module-only names
    file: ``libxul.so (deleted) | ... | libnspr4.so (deleted)`` (2069648, a DUP of our own
    2065373 -- :jld, "Duplicate of 2065373, but with missing symbols"), ``xul.dll |
    _PR_MD_UNLOCK | PR_Unlock | xul.dll`` (2061962, a DUP of our own 2061960) and
    ``libvulkan_radeon.so``. Such a name cannot be searched, blamed or deduplicated; the
    symbolicated sibling files. A partly-symbolicated signature still files: ``OOM | unknown |
    memcpy_repmovs_Intel | mozilla::dom::RTCEncodedFrameBase::...`` is perfectly actionable,
    and a prefix-listed frame standing alone (``mozilla::detail::MutexImpl::mutexLock``) is
    still a symbol."""
    from crashclouseau import sigfamily

    parts = [p.strip() for p in (signature or "").split("|") if p.strip()]
    if parts and all(_BARE_ADDR.match(p) for p in parts):
        return True
    return sigfamily.is_unsymbolicated(signature)


def _signature_family(dossier):
    """The crash's other names, as the run RECORDED them: the persisted corroborations first
    (`sigfamily.family_from_corroborations`), else the seed the dossier carries. Never a fresh
    lookup from the filer: a run that recorded no lookup is a run made before the instrument
    existed or with it switched off, and the filer must not spend Socorro requests -- or reach
    the network from a test -- to second-guess it. ``{}`` when there is nothing."""
    from crashclouseau import sigfamily

    d = dossier or {}
    fam = sigfamily.family_from_corroborations(d.get("corroborations"))
    if fam is None and (d.get("crash") or {}).get("signature_family_lookup"):
        fam = sigfamily.family_from_seed(d.get("crash"))
    return fam or {}


def _family_spellings(signature, family):
    """Every name the crash goes by, this signature's own spellings first -- what the FIXED and
    known-on-train lookups are asked with."""
    from crashclouseau import sigfamily

    out = sorted(utils.lambda_siblings((signature or "").strip()))
    for name in sigfamily.spellings(family):
        for spelling in sorted(utils.lambda_siblings(name)):
            if spelling not in out:
                out.append(spelling)
    return out


def _signature_field(bug_id, timeout=_HTTP_TIMEOUT):
    """A bug's current ``cf_crash_signature``, or ``None`` when it could not be read."""
    try:
        r = net.get(_bz_rest(), params={"id": str(bug_id),
                                        "include_fields": "id,cf_crash_signature"},
                    timeout=timeout)
        r.raise_for_status()
        bugs = (r.json() or {}).get("bugs") or []
    except Exception as exc:                                   # pragma: no cover - network
        logger.warning("autofile: cf_crash_signature read failed for bug %s: %s", bug_id, exc)
        return None
    row = next((b for b in bugs if b.get("id") == bug_id), None)
    return None if row is None else (row.get("cf_crash_signature") or "")


def _attach_signature(bug_id, signature, token):
    """APPEND ``[@ signature]`` to *bug_id*'s ``cf_crash_signature`` in its own PUT, and say what
    happened: ``"attached"``, ``"already"`` (an entry for it, in any lambda spelling, is there),
    or ``"failed"``. Never raises.

    Calixte's rule from bug 2063003: a crash filed under a bug that carries another name MUST add
    its own name to that bug, or the next Socorro click and the next triager start from zero.
    Append, never rewrite -- the field is read back and the old entries are kept verbatim -- and
    a PUT of its own, because BMO's PUT is atomic across fields and a refused link elsewhere
    must not cost the signature (`_link_regressed_by`)."""
    sig = (signature or "").strip()
    if not sig or not bug_id:
        return "failed"
    current = _signature_field(bug_id)
    if current is None:
        return "failed"
    present = {e.lower() for e in _signature_field_entries(current)}
    if present & {s.lower() for s in utils.lambda_siblings(sig)}:
        return "already"
    entry = "[@ {}]".format(sig)
    new = (current.rstrip() + "\n" + entry) if current.strip() else entry
    try:
        _put_bug(bug_id, {"cf_crash_signature": new}, token)
    except Exception as exc:
        logger.warning("autofile: could not attach %r to bug %s: %s", sig, bug_id, exc)
        return "failed"
    return "attached"


def _venue_via_signature_note(signature, venue, family):
    """The one paragraph a comment posted through the crash's OTHER name carries: which name
    this crash reports under now, since when, what changed, and that the name has been added
    to the bug. ``""`` for a venue reached through the signature itself."""
    from crashclouseau import sigage, sigfamily

    via = (venue or {}).get("via_signature")
    if not via:
        return ""
    fam = family or {}
    change = ""
    for p in fam.get("predecessors") or []:
        if p.get("signature") == via and p.get("change"):
            change = p["change"]
    if not change:
        change = sigfamily.describe_change(signature, via)
    build = fam.get("s_first_build")
    if (venue or {}).get("via_relation") == "handoff":
        since = (" since build {} ({})".format(build, sigage.buildid_day(build)) if build else "")
        return ("_Posted here because this crash reports under the signature `{}`{}: {}. This "
                "bug carried the earlier name `{}`; `[@ {}]` has been added to its crash "
                "signatures._".format(signature, since, change, via, signature))
    return ("_Posted here because `{}` is another spelling of this bug's signature `{}` ({}); "
            "`[@ {}]` has been added to its crash signatures._".format(
                signature, via, change, signature))


def _needinfo_changes(email):
    """The PUT body that ADDS a needinfo flag to an existing bug.

    ``new`` is the whole point, and it cost a bug. A flag change identified only by ``name``
    is an UPDATE: BMO looks for a flag of that type already on the bug and rewrites it, so
    ``{"name": "needinfo", "requestee": <us>}`` silently reassigns somebody else's pending
    question instead of asking our own. On bug 2068006 (2026-09-01) release-mgmt-account-bot
    needinfo'd jjalkanen at 09:43 to review the regressor and set severity; our comment's PUT
    at 10:27 moved that flag to jstutte, who cleared it 50 minutes later — the release ask was
    gone, and nothing in our result dict knew it had ever existed. ``new: true`` makes it an
    addition, which is what we always meant.

    Not used on the create path: a bug that does not exist yet has no flag to collide with,
    and create validates its ``flags`` differently."""
    return {"flags": [{"name": "needinfo", "status": "?", "requestee": email, "new": True}]}


def autofile_bug(uuid, uuid_info, stack, dossier, verdict, confidence):
    """File a Bugzilla bug for a reported crash, unattended. Returns a result dict; NEVER
    raises — a filing failure must not lose an analysis that is already persisted.

    This is a write to Bugzilla with no human in the loop (the spike filer is the other), so
    every gate is here rather than at the call site, and each one fails CLOSED except where
    marked otherwise:

    * held for a PRODUCT whose filing is held (``agent.autofile.products.<p>.enabled: false``
      -- Fenix, plans/16 D4) before any other gate, the operator's per-run instruction
      included, and refused for a product nobody has decided about (fails closed, like the
      channel gate);
    * disabled unless ``AUTOFILE_BUGS`` is on (a real kill-switch: it writes to production
      BMO on a schedule, so it has to be stoppable without a deploy);
    * verdict must be reported and at/above ``min_confidence`` (70 = the ``probable`` rung);
    * never twice for one crash (``Dossier.already_filed``), which matters because the
      orphan reaper re-runs a crashed run and would otherwise re-file on recovery;
    * never a SECOND bug for one SIGNATURE (``Dossier.already_filed_for_signature``): a full
      stop on a ``skip``/``file_new`` channel, and on a ``comment`` channel a stop whenever the
      venue search cannot see the bug we filed -- restricted, resolved as unwanted, or edited
      off the signature (``_own_bug_out_of_sight``); FIXED and DUPLICATE carry on to the gates
      that own them;
    * a ``daily_cap`` bound, because the pipeline itself has none and a bad gate at 3/day
      is a nuisance while a bad gate at 300/day is an incident;
    * if an OPEN bug already references the signature AND that bug belongs to this crash's own
      application (``_split_by_application``) AND it is not a ``[meta]`` tracker
      (``_split_out_metas``) AND it can be shown to be about this regression
      (``_bug_for_this_regression``, which needs the candidate's landing date and refuses the
      venue without one), comment there instead of filing a duplicate — and if that
      lookup FAILS we skip entirely rather than risk the duplicate;
    * if we are about to file a NEW bug and a bug on this signature was RESOLVED FIXED AFTER
      this crash's build was produced, the crash is a pre-fix report of a defect somebody has
      already fixed: skip (``_fixed_after_build_bug``). That lookup is the one gate here that
      fails OPEN — a second fail-closed BMO request would turn one flaky call into a silent
      global filing stop, for a rule measured to fire on 1 filing in 52;
    * never twice on one BUG for one signature (``Dossier.already_commented``), which is a
      different question from "never twice for one crash": several proto-signature clusters of
      the same signature are analysed independently and all land on the same bug.

    A RELEASE filing is titled ``[new in release] Crash in [@ ...]`` and nominates the crash's
    version for tracking (``cf_tracking_firefox<major>`` = ?, in the create, ``_train_flags``);
    both come from ``config.get_agent_autofile(channel)`` via the preview.

    ``regressed_by`` is set — under the pushlog-window gate, on a bug we filed ourselves, and in
    its own PUT (see ``_link_regressed_by`` and ``report_bug.build_bug_preview``). Because we now
    write the field the feedback loop reads, ``models.Feedback.classify`` is told what we claimed:
    our own write agreeing with us is ``unconfirmed``, not ``correct``."""
    channel = uuid_info.get("channel")
    product = uuid_info.get("product")
    # THE PRODUCT HOLD OUTRANKS EVERYTHING, the operator's per-run instruction included. Fenix
    # ships triaged with its filing held (`agent.autofile.products.Fenix.enabled: false`,
    # plans/16 D4) and the hold has to bind a `POST /api/tasks/trigger` with `file_bug: true`
    # as much as the sweep: `run_options` is read right below because `file_bug: false` must
    # beat an armed channel, so a hold placed after that read would be one HTTP call away from
    # a bug in `Firefox for Android` in a reader's eyes -- a `True` there is only "no
    # instruction", but the ORDER is what gets checked, so the hold comes first. Its string is
    # distinct from "autofile disabled" so `orchestrator._autofile` RECORDS the decline: a
    # week of held Fenix verdicts at the filing rung is the number the arm decision needs, and
    # a hold that leaves no trace measures nothing (beta's hold was the same instrument).
    if config.autofile_product_held(product):
        return {"filed": False, "channel": channel, "product": product,
                "skipped": "autofile held for product {!r} (triage-only)".format(product)}
    # THE OPERATOR'S INSTRUCTION FOR THIS CRASH comes next: a run triggered through
    # `/api/tasks/trigger` with `file_bug: false` writes nothing whatever the channel's policy
    # says, and this is the reason its Bug column should carry. Sticky on the dossier, so the
    # reaper's re-run and a retrigger click honour it too (`Dossier._STICKY_PAYLOAD_KEYS`).
    if models.Dossier.run_options(uuid).get("autofile") is False:
        return {"filed": False, "channel": channel,
                "skipped": "filing disabled for this run (triggered with file_bug: false)"}
    # The crash's own channel AND product: `product=None` is byte-identical to the one-argument
    # call, and a product overlay may one day tighten a channel's policy (`daily_cap`, `skip`)
    # the day Fenix is armed -- read now so arming is a config edit, not a code edit.
    cfg = config.get_agent_autofile(channel, product=product)
    # THE PRODUCT GATE, and it fails CLOSED like the channel gate under it: a product nobody
    # has DECIDED about -- Focus the day it is ingested, or a `uuid_info` with no product at
    # all -- files nothing. It is needed even with the hold above because Fenix nightly and
    # Firefox nightly share the channel label: `autofile_channel_declared("nightly")` says yes
    # to a Fenix dossier, so without a product predicate any new product would inherit
    # nightly's ARMED policy by default (`config.autofile_product_declared`).
    if not config.autofile_product_declared(product):
        return {"filed": False, "channel": channel, "product": product,
                "skipped": "product {!r} has no autofile configuration".format(product)}
    # THE CHANNEL GATE, and it fails CLOSED. `get_agent_channels()` inside `enqueue_agent` was
    # the ONLY thing keeping filing nightly-only -- and `enqueue_agent(..., force=True)` bypasses
    # it by design, which is precisely what `retrigger_agent` (a tasks.html click, and a BULK
    # retrigger) calls. With `AUTOFILE_BUGS=1` live in prod, the day `INGEST_CHANNELS` gained a
    # channel -- no deploy, no code change -- one retrigger would have filed on it under
    # nightly's rules, `comment_on_existing: comment`, i.e. a comment on somebody's open bug.
    #
    # A channel with no `agent.autofile.channels.<ch>` entry and no explicit `enabled` files
    # NOTHING, in the same direction as every other gate in this function.
    if not config.autofile_channel_declared(channel):
        return {"filed": False,
                "skipped": "channel {!r} has no autofile configuration".format(channel)}
    if not cfg["enabled"]:
        # TWO DIFFERENT SILENCES, and only one of them is worth hearing about. The global
        # switch being off is the ordinary observe-only state, so `_maybe_autofile` suppresses
        # that log line — otherwise every run on every channel prints one. A channel held by
        # an explicit `channels.<ch>.enabled: false` is a DECISION about that channel, and it
        # is the whole instrument of plan #18's Phase 4 ("beta triaged, beta filing held"):
        # a hold that leaves no trace cannot tell anyone how much it would have filed, which
        # makes the phase measure nothing. Distinguished by the STRING, because that is what
        # the caller keys on.
        if config.autofile_channel_held(channel):
            return {"filed": False, "channel": channel,
                    "skipped": "autofile held for channel {!r} (triage-only)".format(channel)}
        return {"filed": False, "skipped": "autofile disabled"}
    # TWO REASONS TO FILE, and the verdict is only the first. The second — a bug on this
    # signature whose fix is already in this build — is checked LATER (it costs a BMO request),
    # after every cheap gate below has had its chance to stop the run for free.
    over_floor = confidence is not None and confidence >= cfg["min_confidence"]
    fileable = bool(verdict in cfg["verdicts"] and over_floor)
    # A DELIBERATE SUPPRESSION OUTRANKS THE SECOND REASON, and is checked before every other
    # gate so its message is the one the reader gets. Each `suppression`-kind flag in
    # `corroborations.REGISTRY` turned the verdict into an abstain for a reason about THIS
    # CRASH rather than about the verdict's strength: a probable hardware bit flip, a
    # misbehaving machine, a candidate that is a backout, a mechanism compiled out of this
    # build. None of those become worth filing because a bug on the signature was fixed once.
    # This is the one place a suppressed run could reach a Bugzilla write, because the second
    # reason does not read the verdict at all.
    if not fileable:
        suppressed = sorted(k for k in corroborations.suppressions()
                            if ((dossier or {}).get("corroborations") or {}).get(k))
        if suppressed:
            return {"filed": False,
                    "skipped": "suppressed by {}".format(", ".join(suppressed))}

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
        return {"filed": False, "skipped": "already filed", "prior": prior,
                "bug": (prior or {}).get("bug") if isinstance(prior, dict) else None}

    since = datetime.now(timezone.utc) - timedelta(days=1)
    try:
        # PER CHANNEL AND PER PRODUCT. A shared cap lets one channel's burst spend another's
        # budget, and beta's selections are 48% concentrated in the 4 days after a merge --
        # exactly when a freshly uplifted regression is worth filing. The product because Fenix
        # nightly and Firefox nightly share the channel label, so without it the two would
        # spend one cap of 10 in both directions the day Fenix files (plans/16 §6.2).
        recent = models.Dossier.filed_bugs_since(since, channel=channel, product=product)
    except Exception as exc:                                # pragma: no cover - defensive
        return {"filed": False, "skipped": "cap check failed: {}".format(exc)}
    cap = cfg["daily_cap"]
    # `null` in config is NO cap (Calixte, 2026-09-17). At 2 on release the bound dropped a
    # culprit at 85 (0015b3bf, CheckLogMessage, no bug anywhere) behind two lesser filings, and
    # a capped finding is never revisited: this gate runs before the venue search and nothing
    # retries it. The knob stays -- a number here re-arms it without a code change.
    if cap is not None and recent >= cap:
        logger.warning("autofile: daily cap %s reached for %s/%s (%s in 24h) — not filing for %s",
                       cap, product or "?", channel or "?", recent, uuid)
        return {"filed": False, "skipped": "daily cap {} reached on {}".format(
            cap, channel or "?")}

    token = config.get_bugzilla_token()
    if not token:
        return {"filed": False, "skipped": "no Bugzilla API token configured"}

    signature = (uuid_info.get("signature") or "").strip()

    # AN `actionable` VERDICT FILES ON THE CRASH'S OWN FACTS (Calixte, 2026-09-17), so two facts
    # are checked here deterministically, before any venue work. First, a real POPULATION: the
    # spike path's own bar for filing on volume alone (`spike.real_installs`). On nightly the
    # median cited-mechanism `pre_existing` abstain is ONE installation -- the class developers
    # dismiss as hardware ("very few reports in common code paths are often hardware related");
    # on release the selector's 50-install floor already exceeds it. Read off the same Socorro
    # aggregation the bug's volume sentence quotes, so the floor and the sentence cannot
    # disagree. The second fact, no open bug on the signature, is checked once the venues are
    # known below.
    actionable = fileable and verdict == "actionable"
    if actionable:
        from crashclouseau import report_bug
        floor = config.get_spike("real_installs", product, channel)
        _first, stats = report_bug.fetch_signature_stats(uuid, uuid_info)
        installs = (stats or {}).get("installs")
        if installs is None:
            return {"filed": False,
                    "skipped": "population unknown; an actionable crash is filed on its volume"}
        if installs < floor:
            return {"filed": False,
                    "skipped": "{} installation{} on this signature, below the actionable floor "
                               "of {}".format(installs, "" if installs == 1 else "s", floor)}

    # THE SECOND REASON TO FILE. A verdict we cannot file on is the ordinary case (90% of runs
    # abstain), so this is the last gate rather than an early one: everything above it is local
    # and free, and this is one BMO request, cached per signature.
    incomplete_fix = None
    if not fileable:
        corro = (dossier or {}).get("corroborations") or {}
        first_seen = corro.get("signature_first_seen_ever") or corro.get(
            "signature_first_seen_windowed")
        incomplete_fix = _incomplete_fix_bug(
            signature, uuid_info.get("buildid"), uuid_info.get("product"), channel,
            first_seen=first_seen)
        if not incomplete_fix:
            if verdict not in cfg["verdicts"]:
                return {"filed": False, "skipped": "verdict {} not fileable".format(verdict)}
            return {"filed": False, "skipped": "confidence {} below {}".format(
                confidence, cfg["min_confidence"])}
        logger.info("autofile: %s — bug %s owns this signature, its fix %s landed %s and the "
                    "crash is still here; filing on that rather than on the verdict (%s/%s)",
                    uuid, incomplete_fix["id"], incomplete_fix["node"],
                    incomplete_fix["pushdate"], verdict, confidence)

    # ONE BUG PER SIGNATURE, in two halves that differ by the channel's policy.
    #
    # It is the only guard that survives the bug we filed being CLOSED or RESTRICTED.
    # `_open_bugs_for_signature` is unauthenticated and filters `resolution: "---"`, so a bug we
    # filed and a human then resolved INVALID/WORKSFORME -- or that WE filed restricted, or that
    # a human restricted afterwards -- is invisible below, `_bug_for_this_regression` is never
    # asked, and `already_commented` is only consulted for a CHOSEN venue. Measured on our own
    # 60 filings: 4 of the 18 nightly-filed signatures that also crash on beta would collect a
    # second bug (DUPLICATE / INVALID / INVALID / WORKSFORME). And on nightly itself, 2026-09-16:
    # FIVE restricted bugs in five hours on one signature of one build (2072488, 2072492,
    # 2072493, 2072502, 2072521), one per proto-signature cluster, each needinfo'ing the same
    # developer, because this query was then asked only on a `skip` channel.
    #
    # The one DB query is made on every channel; what differs is what a hit means.
    # On a channel that never writes on existing bugs a prior filing is a FULL STOP, before the
    # venue search is even made. A derivation and not a new knob: a policy of "never touch an
    # existing bug" cannot also want a SECOND bug for a signature it has already filed one for
    # -- that is the same duplicate the policy exists to avoid, wearing our own bug number.
    # CHANNEL-BLIND, and that is the entire point: the 22.2% above is nightly-filed bugs that a
    # beta run would file a second time; scoping the lookup to the crash's own channel would
    # make it blind to exactly that population and leave it asserting nothing.
    # THE CRASH'S OTHER NAMES (`sigfamily`, recorded by the run): the venue search asks for the
    # bug under every one of them, the FIXED/known-on-train lookups too, and a venue reached
    # through the old name gets the new one attached (`_attach_signature`).
    family = _signature_family(dossier)
    spellings = _family_spellings(signature, family)
    own_count = len(utils.lambda_siblings(signature))
    # Byte-identical calls when the crash has no other name, so the lookups' contracts (and
    # the thirty-odd tests that pin them by name) do not move for the ordinary crash.
    widened = {"spellings": spellings} if len(spellings) > own_count else {}
    existing = (_open_bugs_for_signature(signature, family=family) if widened
                else _open_bugs_for_signature(signature))
    if existing is None:
        return {"filed": False, "skipped": "signature lookup failed; not risking a duplicate"}
    # A BUCKET-HOLDER SIGNATURE -- one an open ``[meta]`` tracker carries -- takes one bug PER
    # BUCKET, not one per signature (:jstutte, bugs 2073349 c1 and 2069191 c5, 2026-09-18): the
    # main-thread wait it names is shared by every cause under it, and the tracker exists so
    # that each cause gets its own bug, without the signature, blocking it. So a prior filing of
    # ours on the signature is a stop only when it was about THIS bucket -- or when neither
    # bucket is known, which fails toward the stop as every dedup here does. The bucket is the
    # awaited thread's key (``hang.bucket_key``, stamped by the orchestrator as
    # ``hang_awaited_work``) and was recorded on the earlier filing (``bucket`` below); on
    # 2071528 the second analysis was a different cohort (`nsSegmentedBuffer::Clear`) and went
    # onto the audio-session bucket bug as a comment, which is the mix-up this avoids.
    held_by_meta = bool(_split_out_metas(
        _split_by_application(existing, uuid_info.get("product"))[0])[1])
    this_bucket = _bucket_of(dossier)
    from crashclouseau import report_bug

    this_bucket_title = report_bug.bucket_title(dossier) if held_by_meta else ""
    # On a bucket-holder signature, ask the database for THIS bucket. The old signature-only
    # query always returned the oldest filing, so after A and B had both been filed a second B
    # compared itself with A and was filed again. Unknown historical identities still fail
    # closed inside the query.
    prior_sig = models.Dossier.already_filed_for_signature(
        signature, bucket=this_bucket or None, bucket_title=this_bucket_title or None
    ) if held_by_meta else models.Dossier.already_filed_for_signature(signature)
    if not prior_sig and not held_by_meta:
        # ...OR UNDER ONE OF ITS OTHER NAMES. 2072875 and 2073159 were DUPs of our own 2072488,
        # the Linux and Fenix spellings of the Windows name we had filed restricted two days
        # earlier -- invisible to the anonymous venue search AND to this exact-signature query.
        for other in spellings[own_count:]:
            hit = models.Dossier.already_filed_for_signature(other)
            if hit:
                prior_sig = dict(hit, via_signature=other) if isinstance(hit, dict) else hit
                break
    if prior_sig and held_by_meta and _different_bucket(prior_sig, this_bucket):
        logger.info("autofile: our bug %s on %r is bucket %r; this crash is bucket %r of a "
                    "signature held by a [meta] tracker -- a bucket bug may be filed for %s",
                    prior_sig.get("bug"), signature, prior_sig.get("bucket"), this_bucket, uuid)
        prior_sig = None
    if prior_sig and (actionable or config.comment_mode(cfg["comment_on_existing"]) != "comment"):
        logger.info("autofile: already filed bug %s for %r on %s (from %s) — not filing "
                    "again for %s", prior_sig.get("bug") or "?", signature, channel or "?",
                    prior_sig.get("uuid") or "?", uuid)
        under = ("this crash under its other name `{}`".format(prior_sig["via_signature"])
                 if isinstance(prior_sig, dict) and prior_sig.get("via_signature")
                 else "this signature")
        return {"filed": False, "bug": prior_sig.get("bug"),
                "skipped": "already filed bug {} for {} on {}".format(
                    prior_sig.get("bug") or "?", under, channel or "?"),
                "prior_signature_filing": prior_sig}
    # On a `comment` channel our own OPEN bug is the ordinary venue -- the search returns it and
    # `already_commented` declines the second analysis -- so the guard acts only when the search
    # CANNOT see our bug, and the reason decides (`_own_bug_out_of_sight`): restricted, resolved
    # as unwanted, or edited off the signature is a skip; FIXED or DUPLICATE carries on to the
    # gates that own those.
    if prior_sig:
        out_of_sight = _own_bug_out_of_sight(prior_sig, existing)
        if out_of_sight:
            logger.info("autofile: %s — not filing for %s (our filing was from %s)",
                        out_of_sight["skipped"], uuid, prior_sig.get("uuid") or "?")
            return out_of_sight
    # An open bug on this signature that belongs to ANOTHER application built on Gecko is not
    # a venue, however well it matches (``_split_by_application``) — and it must not read as
    # "an open bug exists" to the check below either, or a Thunderbird-only match would skip
    # the filing outright.
    existing, other_app = _split_by_application(existing, uuid_info.get("product"))
    if other_app:
        logger.info("autofile: open bug(s) %s reference this signature but belong to another "
                    "application (%s) — not a venue for a %s crash",
                    [b["id"] for b in other_app],
                    ", ".join(sorted({b.get("product") or "?" for b in other_app})),
                    uuid_info.get("product") or "?")
    # A ``[meta]`` tracker is not a venue either, for the same reason and with the same
    # treatment: it is dropped BEFORE the kill-switch below, because "an open bug exists, do
    # not write" means an open bug we could have written IN (``_split_out_metas``).
    existing, meta_bugs = _split_out_metas(existing)
    if meta_bugs:
        logger.info("autofile: open bug(s) %s reference this signature but are [meta] trackers "
                    "— not a venue for a crash report", [b["id"] for b in meta_bugs])
    # NO OPEN BUG on the signature in this application, for an `actionable` filing: an open bug
    # means someone can already act, and this filing exists only to put a crash in front of
    # someone. Decided whatever the channel's `comment_on_existing` says, with metas and other
    # applications excluded as everywhere else; `bug` names the venue, as on every decline that
    # is about one.
    if actionable and existing:
        return {"filed": False, "bug": existing[0]["id"],
                "skipped": "open bug {}{} exists; an actionable crash is filed only where no bug "
                           "is".format(existing[0]["id"], _via_clause(existing[0])),
                **_via_fields(existing[0])}
    # (mode/comment_allowed/withheld are resolved above, right after `cfg`.)
    # THREE MODES, not a boolean (``config.COMMENT_ON_EXISTING``). ``skip`` is what ``False``
    # always DID -- no comment AND no new bug, decided before anything asks whether that bug
    # could even be about this regression -- and two tests pin that meaning by name.
    # ``file_new`` is the mode for "file only crashes that have no bug in Bugzilla, and never
    # write on an existing one": no comment, but a new bug that NAMES the open bugs it declined
    # to comment on, or it reads as a broken deduplicator.
    mode = config.comment_mode(cfg["comment_on_existing"])
    comment_allowed = mode == "comment"
    # THE MEMORY-SAFETY CARVE-OUT, and it is a security regression that ``skip`` would otherwise
    # introduce rather than a pre-existing one. ``sensitive.is_withheld`` used to be consulted
    # ~90 lines below this point, so a poison-address crash whose signature has an open PUBLIC
    # bug would hit the skip first and produce NOTHING: no restricted bug, no comment, no record.
    # :mccr8 on bug 2065051 -- "Bugs on poison crashes like that should always be filed initially
    # a security issue" -- and the existing nightly path does exactly that (it declines the
    # public venue, files a NEW restricted bug, and names the public one as a probable duplicate
    # in comment 0, never as a ``see_also``). Reach: the deterministic poison gate fires on 1 of
    # 57 filings and 59.2% of beta signatures have an open venue, so ~1% of rung-70 verdicts --
    # small, and the highest-value 1%.
    withheld = sensitive.is_withheld((dossier or {}).get("corroborations"))
    if existing and mode == "skip" and not withheld:
        # `bug` on a `filed: False` result is the bug the decision was ABOUT -- the open venue
        # this crash was not written into -- as on every other decline shape below that names
        # one; the tasks view renders it as "not filed (bug N)".
        # A venue reached through the crash's other name says so: on a `skip` channel nothing
        # is written, not even the new name onto the old bug, so the decline is the only trace
        # (2073210 -> 1737467 on release).
        return {"filed": False, "bug": existing[0]["id"],
                "skipped": "open bug {}{} exists".format(
                    existing[0]["id"], _via_clause(existing[0])),
                **_via_fields(existing[0])}
    # WHICH of those open bugs, if any, can be about this regression — the oldest one often
    # cannot, and with no landing date NONE of them can be shown to
    # (``_bug_for_this_regression``). Resolved before the preview is built so a new bug filed
    # past an older one can say so, and say which of the two reasons it was.
    landed = _candidate_landed(dossier, channel)
    bug_id, predating = _bug_for_this_regression(
        existing,
        landed,
        cfg["comment_max_bug_age_days"],
        candidate_bug=((dossier or {}).get("candidate") or {}).get("bug"),
        signature=signature,
    )
    landing_unresolved = landed is None and bug_id is None and bool(predating)
    # ...AND THEN THE MODE OVERRIDES THE VENUE. Computed in this order on purpose:
    # ``landing_unresolved`` must describe what the EVIDENCE said, so a bug filed because of the
    # policy is not reported as one filed because an hg lookup failed. A venue we are not
    # allowed to use becomes a bug the new one references instead.
    never_comment = not comment_allowed
    # ...EXCEPT on a withheld crash, where the SECURITY branch below owns the same decision and
    # says something more useful about it. Both paths decline the venue and file a new bug; that
    # one additionally names the public bug as a probable duplicate, explains that the split is
    # because the report shows a memory-safety fault, and records `public_venue_declined` for the
    # audit trail. Overriding here first would set `bug_id = None`, so `if withhold and bug_id is
    # not None` could never fire and a restricted beta filing would carry the generic
    # "this filer does not comment on existing bugs" note instead — and lose the audit field.
    if never_comment and bug_id is not None and not withheld:
        predating = sorted({*(predating or []), bug_id})
        logger.info("autofile: bug %s could be the venue for %s but this channel (%s) is "
                    "%s — filing a new bug that references it instead",
                    bug_id, uuid, channel or "?", mode)
        bug_id = None
        landing_unresolved = False
    if predating and bug_id is None:
        if landing_unresolved:
            # WARNING, not info: this is the only path on which a filing is routed by a fact
            # the run never established, and ``_candidate_landed`` itself is silent when the
            # poisoned ``json_rev`` cache serves the ``None``. If the rule is ever wrong, this
            # line and ``venue_landing_unresolved`` below are how we would find out.
            #
            # It also costs this run the one-bug-one-analysis protection, and that is the
            # honest price rather than a bug: ``already_commented`` is only asked about a
            # CHOSEN venue, so two proto-signature clusters of the same crash that both go
            # hg-blind file two new bugs where one would have filed and the other skipped
            # (3 of 31 bugs were proto-split). At a ~2% blind rate that is ~0.2% of writes,
            # against the 94% wrong-venue rate this replaces.
            logger.warning(
                "autofile: could not resolve when %s landed — open bug(s) %s on this signature "
                "cannot be shown to be about it, filing a new bug for %s instead",
                ((dossier or {}).get("candidate") or {}).get("node") or "?", predating, uuid)
        else:
            logger.info("autofile: open bug(s) %s all predate the suspected regressor — filing "
                        "a new bug for %s rather than commenting there", predating, uuid)

    # ALREADY FIXED, AFTER THIS BUILD WAS PRODUCED. Only on the file-a-NEW-bug branch: an open
    # venue still gets its comment, because a bug someone is working on wants to know the crash
    # is still arriving. Our 2064537 was the textbook case — bug 2063862 was
    # RESOLVED FIXED 2026-08-17T08:07:20, the crash's build was 20260816083833, and we filed a
    # duplicate on 2026-08-18. Invisible for exactly one reason: ``resolution="---"``.
    if bug_id is None:
        fixed_by = _fixed_after_build_bug(
            signature, uuid_info.get("buildid"), uuid_info.get("product"), **widened)
        if fixed_by:
            bid = utils.get_buildid(uuid_info.get("buildid"))
            logger.info("autofile: bug %s was FIXED after build %s, so %s is a pre-fix report "
                        "of an already-fixed defect — not filing", fixed_by, bid, uuid)
            return {"filed": False, "bug": fixed_by,
                    "skipped": "already fixed by bug {} (the fix postdates build {})".format(
                        fixed_by, bid)}
        # KNOWN ON THIS TRAIN, AND NOT FIXED HERE. A RESOLVED bug on the exact signature whose
        # status flag for the crash's own train reads affected/wontfix/disabled/fix-optional
        # already IS this crash (bug 2016440 for our 2070489: FIXED in 156/157, wontfix for 155,
        # filed as a new bug anyway). Skipped rather than commented: the venue lookup's `skip`
        # policy on this channel applies to a closed venue as much as to an open one.
        known = _known_on_train_bug(
            signature, uuid_info.get("product"), _major_version(uuid_info.get("version")),
            **widened)
        if known:
            logger.info("autofile: bug %s already tracks %r on this train (%s = %s), so %s is "
                        "that bug and not a new one — not filing", known["id"], signature,
                        known["field"], known["flag"], uuid)
            return {"filed": False, "bug": known["id"], "known_on_train": known,
                    "skipped": "bug {} already tracks this signature on this train ({} = {}); "
                               "the fix is not here and this crash is that bug".format(
                                   known["id"], known["field"], known["flag"])}

    # ALREADY HAS A REGRESSOR, so there is nothing for us to say. Our comment makes ONE claim
    # -- this changeset caused it -- and a venue whose `regressed_by` is already set has
    # answered that question. Bug 2068006, 2026-09-01: yjuglaret set `regressed_by = 2056841`
    # at 09:19, read off a three-thread ABBA deadlock cycle in the stacks, and release
    # management had needinfo'd the regressor's author by 09:43. We commented at 10:27 naming a
    # DIFFERENT bug at 72% -- noise laid over a solved question, on a bug somebody was already
    # driving. It is the same failure the `regressed_by` PUT is already withheld for ("on
    # somebody else's open bug the field is often already curated", and on bug 2057980 ours
    # would have contradicted it); the comment is the louder half of it, and was ungated.
    #
    # THE FIELD, NOT THE `regression` KEYWORD. The keyword says a regression happened, which is
    # the question we answer; `regressed_by` says somebody has answered it. Gating on the
    # keyword would silence us on every bug bugbot has ever touched.
    #
    # Two exemptions, both because the comment is then not the claim this gate is about:
    #
    # * `incomplete_fix` -- that comment says a shipped fix did not hold and the crash is still
    #   arriving, which is news to whoever named the original cause. The new-bug half of that
    #   path strips `regression`/`regressed_by` from its payload for the same reason.
    # * `withheld` -- a memory-safety crash does not comment here at all; the security branch
    #   below declines the public venue and files a RESTRICTED bug. Skipping first would
    #   produce nothing, which is the regression the carve-out above this was written to stop.
    venue = next((b for b in existing if b["id"] == bug_id), {}) if bug_id is not None else {}
    named = venue.get("regressed_by") or []
    if named and not incomplete_fix and not withheld:
        logger.info("autofile: bug %s already names its regressor (%s) — %s has nothing to add",
                    bug_id, named, uuid)
        return {"filed": False, "bug": bug_id, "regressor_already_named": named,
                "skipped": "bug {} already names its regressor ({})".format(
                    bug_id, ", ".join("bug {}".format(b) for b in named))}

    # Said it once already. `already_filed` above is keyed on the UUID, which is the wrong
    # grain: one (signature, build) splits into one cluster per distinct stack, each analysed
    # and filed on its own, and they all resolve to the SAME bug here. Bug 2062934 collected
    # two identical analyses 80 seconds apart from two crashes on one machine. Skipping
    # entirely rather than falling through to a new bug — a duplicate on BMO is the worse of
    # the two noises.
    prior_comment = (models.Dossier.already_commented(bug_id, signature)
                     if bug_id is not None else None)
    # ...OR IT SITS THERE UNDER OUR OWN BUG NUMBER. A bug we filed on this signature that a
    # human then resolved DUPLICATE of the venue IS our analysis on the venue: it is in the
    # venue's duplicate list, its needinfo was answered by the dup, and the person who duped it
    # has read it. 2070711 restated 2069647's regressor claim word for word two days after
    # :teoxoy had duped 2069647 into 1976766. Same (bug, signature) grain as above, one hop out.
    via_duplicate = None
    if bug_id is not None and not prior_comment:
        for dup in venue.get("via_duplicates") or []:
            prior_comment = models.Dossier.already_commented(dup, signature)
            if prior_comment:
                via_duplicate = dup
                break
    if prior_comment:
        logger.info("autofile: bug %s already carries our analysis of %r (from %s%s) — not "
                    "commenting again for %s", bug_id, signature,
                    prior_comment.get("uuid") or "?",
                    ", via its duplicate bug {}".format(via_duplicate) if via_duplicate else "",
                    uuid)
        skipped = "already commented on bug {} for this signature".format(bug_id)
        if via_duplicate:
            skipped = "our bug {} on this signature is a duplicate of bug {}, which already " \
                      "carries the analysis".format(via_duplicate, bug_id)
        out = {"filed": False, "bug": bug_id, "skipped": skipped, "prior_comment": prior_comment}
        if via_duplicate:
            out["via_duplicate"] = via_duplicate
        return out

    # A NEW bug on a bucket-holder signature is a BUCKET BUG or nothing (`report_bug.
    # build_bug_preview`'s bucket mode: named for its cause, no `cf_crash_signature`, blocks the
    # tracker). Nothing in the verdict to name the bucket -- no `title`, no awaited work, no
    # mechanism sentence -- means the only bug we could file is the catch-all Jens asked us to
    # stop filing, so none is.
    if bug_id is None and meta_bugs and not report_bug.bucket_title(dossier):
        tracker = meta_bugs[0]["id"]
        logger.info("autofile: %r is held by [meta] bug %s and the verdict names no bucket to "
                    "file -- not filing a signature-titled bug for %s", signature, tracker, uuid)
        return {"filed": False, "bug": tracker, "meta_bugs": [b["id"] for b in meta_bugs],
                "skipped": "signature is held by [meta] bug {}; the verdict names no bucket to "
                           "file, and a bug titled by the signature would be a second "
                           "catch-all".format(tracker)}
    try:
        preview = report_bug.build_bug_preview(
            uuid_info, stack, dossier,
            related_bugs=predating if bug_id is None else None,
            landing_unresolved=landing_unresolved,
            other_app_bugs=other_app if bug_id is None else None,
            meta_bugs=meta_bugs if bug_id is None else None,
            never_comment=never_comment,
            incomplete_fix=incomplete_fix,
        )
    except Exception as exc:
        logger.error("autofile: preview build failed for %s", uuid, exc_info=True)
        return {"filed": False, "skipped": "preview failed: {}".format(exc)}
    if not preview:
        return {"filed": False, "skipped": "no candidate regressor to file against"}
    if incomplete_fix:
        # This bug exists because a shipped fix did not hold, so it must not also assert a
        # regression: no `regression` keyword and no `regressed_by`. `build_bug_preview`
        # already withholds both when there is no candidate; assert it here because this is
        # the path where a stale candidate could smuggle one in.
        preview["keywords"] = [k for k in preview.get("keywords") or [] if k != "regression"]
        preview["regressed_by"] = []
    # ``resolve_product_component`` is best-effort and returns empty on a Bugzilla read
    # failure or an unreadable regressor bug. Filing then gets rejected outright
    # ("Bad argument param sent to Bugzilla::Product::new") — but the real reason to check
    # here is that a HALF-resolved pair would file the bug into the wrong component, which
    # is worse than not filing: it lands on a team that has no idea why they got it.
    if not (preview.get("product") and preview.get("component")):
        return {"filed": False,
                "skipped": "product/component unresolved — refusing to file into the wrong "
                           "component"}

    # THE SECURITY VENUE, and both branches refuse rather than degrade. `withheld` is resolved
    # far above now (it has to outrank the `skip` mode); this is the same value.
    public_venue_declined = None
    withhold = withheld
    if withhold and not preview.get("groups"):
        # `build_bug_preview` could not resolve the product's security group, so the only
        # remaining options are "file it publicly" and "do not file". A lost lead is recoverable
        # -- the next crash on this signature files again -- and a public use-after-free is not.
        # Treeherder makes the same call, answering HTTP 400 "Cannot file security bug for
        # product without default security group" rather than falling through.
        logger.warning("autofile: %s is a memory-safety crash (%s) and no security group "
                       "resolved for product %r -- NOT filing", uuid,
                       "; ".join(((dossier or {}).get("corroborations") or {})
                                 .get("memory_unsafe_signals") or []),
                       preview.get("product"))
        return {"filed": False,
                "skipped": "memory-safety crash and no security group for product {!r}".format(
                    preview.get("product"))}
    if withhold and bug_id is not None:
        # The venue we picked is an EXISTING bug, and `_open_bugs_for_signature` is
        # unauthenticated by design, so that bug is public by construction -- posting the
        # analysis into it discloses exactly what the group would have protected, and no
        # `groups` on a create can reach this branch. So decline the venue and file a NEW
        # restricted bug instead, naming the public one as the probable duplicate.
        #
        # This knowingly creates something that looks like a duplicate, which is the OTHER thing
        # :mccr8 asked us to stop doing -- so it is recorded, and it is the lesser of the two: a
        # restricted bug marked "probably a dup of N" costs a triager one click, and the
        # alternative costs a disclosure. The third option, a private comment, needs insider-group
        # membership this account has not been verified to have, and a private comment never
        # enters sec triage or the bounty process at all.
        logger.warning("autofile: %s is a memory-safety crash; declining the PUBLIC venue "
                       "bug %s and filing restricted instead", uuid, bug_id)
        preview = dict(preview)
        # NOT `see_also`, and this cost a reading of BMO's source to get right. `add_see_also`
        # MIRRORS a local reference onto the referenced bug (Bugzilla/Bug.pm:3480-3487:
        # `$ref_bug->add_see_also($self->id, 'skip_recursion')`), so linking the public bug from
        # a restricted one puts a public "See Also: bug <restricted id>" on it -- advertising to
        # everyone that a restricted bug exists for this signature. That is a disclosure of
        # EXISTENCE we did not intend and cannot take back, on the one path built to avoid a
        # disclosure. The bug id goes in the restricted bug's own comment instead, where it is
        # just as useful to a triager and mirrors nowhere.
        preview["comment"] = "{}\n\n_Probably a duplicate of bug {}, which is open on this same "\
                             "signature. This bug was filed separately, and restricted, because "\
                             "the crash report shows a memory-safety fault and that bug is "\
                             "public._".format(preview["comment"], bug_id)
        public_venue_declined, bug_id = bug_id, None

    email = preview.get("needinfo_email") if cfg["needinfo"] else ""
    # THE CHANNEL, THE PRODUCT AND THE BUILD, on every result. Without them nothing downstream
    # can answer "how is beta doing": `feedback._filed_bugs` builds its ReviewNote row from
    # exactly these keys and `_NOTE_MODES = ("new_bug",)` is precisely beta's mode, so beta
    # filings would enter the review corpus pooled with nightly's -- and retuning either against
    # a pooled denominator is the mistake the hardware-noise work was written up to prevent
    # ("the denominator is the whole rule"). The product for the same reason one axis over:
    # Fenix nightly and Firefox nightly share the channel label, so "how is Fenix doing" is
    # unanswerable from the channel alone. `Dossier.list_tasks` reads the channel for the ops
    # view too.
    result = {"filed": False, "uuid": uuid, "signature": signature,
              "channel": channel, "product": product,
              "buildid": utils.get_buildid(uuid_info.get("buildid")),
              "at": datetime.now(timezone.utc).isoformat()}
    if withhold:
        # Persisted so the choice is auditable from the dossier, and so that "how often does the
        # security venue fire, and does anyone unrestrict it?" is answerable later from prod
        # rather than from a re-derivation. Same reason `predating_bugs` is recorded below.
        result["security_groups"] = preview.get("groups") or []
        result["memory_unsafe_signals"] = (((dossier or {}).get("corroborations") or {})
                                           .get("memory_unsafe_signals") or [])
    if public_venue_declined is not None:
        result["public_venue_declined"] = public_venue_declined
    if incomplete_fix:
        # WHICH OF THE TWO REASONS FILED THIS, recorded rather than re-derived, for the same
        # audit reason as ``predating_bugs`` below. Both re-derivations are lossy. "Not fileable
        # on the verdict" needs a join on ``uuidid`` -- ``verdicts.dossierid`` is never
        # populated, and joining on it silently counts every run as non-fileable, a mistake
        # already made once while auditing this path. And ``node``/``pushdate`` come from
        # ``models.Node.landing_for_bug``, whose rows ``Node.clean`` deletes after
        # ``get_ndays_of_data()`` days, so a month later our own DB cannot say which landing
        # was in the build. ``predates_days`` is the margin on condition 3's 7-day ownership
        # window -- the sign test is a sign test because the panel had one case inside it, and
        # this is the only place the next N cases get written down.
        pushed = incomplete_fix.get("pushdate")
        result["incomplete_fix"] = {
            "bug": incomplete_fix.get("id"),
            "node": incomplete_fix.get("node"),
            "pushdate": pushed.isoformat() if hasattr(pushed, "isoformat") else (
                str(pushed) if pushed else None),
            "predates_days": incomplete_fix.get("predates_days"),
        }
    try:
        if bug_id is not None:
            # A VENUE REACHED THROUGH THE CRASH'S OTHER NAME says so in the comment and gets
            # this name attached (Calixte, bug 2063003: a dup must add its signature to the
            # target). The attach is its own PUT after the comment, and a failure there is
            # recorded, never raised: the comment is posted.
            via_note = _venue_via_signature_note(signature, venue, family)
            text = preview["comment"] + ("\n\n" + via_note if via_note else "")
            _post_comment(bug_id, text, False, token)
            if via_note:
                result["venue_via_signature"] = venue.get("via_signature")
                result["venue_via_relation"] = venue.get("via_relation")
                result["signature_attached"] = _attach_signature(bug_id, signature, token)
            # The comment is already posted, so a failing needinfo must not escape: it would
            # skip ``record_filed_bug`` below and the next run would comment a second time on
            # the same bug. Lose the flag, keep the filing.
            outcome = _set_needinfo(bug_id, email, token) if email else None
            failed = isinstance(outcome, Exception)
            result.update({"filed": True, "bug": bug_id, "mode": "comment_on_existing",
                           "needinfo": None if failed else (email or None)})
            # HOW THE VENUE WAS FOUND, when it was through a duplicate rather than the bug's own
            # signature field: the same audit reason as `predating_bugs` — if a dup ever routes
            # us into the wrong bug, these rows are how we find out.
            if venue.get("via_duplicates"):
                result["venue_via_duplicates"] = venue["via_duplicates"]
            if failed:
                result["needinfo_failed"] = email
            elif outcome == _NEEDINFO_ALREADY:
                # ``needinfo`` above still names them — they ARE on the hook, which is what
                # the feedback loop reads it for — but WE did not put them there, and that
                # difference is the only way to tell a quiet no-op from a working ask when
                # this rule is next audited.
                result["needinfo_already_set"] = email
        else:
            # Put the train fields and needinfo in the create request.
            train_flags = _train_flags(preview, signature, product)
            payload = _create_payload(preview, email, train_flags)
            bug_id, dropped = _create_bug_keeping_the_bug(payload, token)
            if "needinfo" in dropped:
                result["needinfo_dropped"] = email
                email = ""
            # Filed PAST an open bug on the same signature. Recorded so the choice is
            # auditable from the dossier — this is the one place the filer knowingly creates
            # something that looks like a duplicate, and if the age rule is ever wrong these
            # rows are how we would find out.
            if predating:
                result["predating_bugs"] = predating
                # ...and WHICH of the two reasons, because they are not the same claim.
                # ``predating_bugs`` alone reads as "these were filed before the cause", which
                # is precisely what the run could NOT check here.
                if landing_unresolved:
                    result["venue_landing_unresolved"] = True
            # Same audit trail for the other reason we file past an open bug: it is somebody
            # else's application. If that judgement is ever wrong, these rows are how we find
            # out — the mistake it replaces was invisible from every side but the bug's.
            if other_app:
                result["other_app_bugs"] = [b["id"] for b in other_app]
            # And the third bucket, for the same audit reason (``_split_out_metas``).
            if meta_bugs:
                result["meta_bugs"] = [b["id"] for b in meta_bugs]
            # A BUCKET BUG: which bucket (the awaited work's key, the dedup grain above) and
            # under which title. `bucket` is what `Dossier.already_filed_for_signature` hands
            # the next run on this signature.
            if preview.get("bucket"):
                result["bucket_title"] = preview.get("title")
                if preview["bucket"].get("key"):
                    result["bucket"] = preview["bucket"]["key"]
            # Retry relations by PUT only if the create fallback removed them.
            wanted = preview.get("blocked") or []
            linked = _link_blockers(bug_id, wanted, token) if "blocks" in dropped else list(wanted)
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
            # Set the causal claim only on bugs we create; existing bugs may already be curated.
            regressors = preview.get("regressed_by") or []
            result["regressed_by"] = (_link_regressed_by(bug_id, regressors, token)
                                      if "regressed_by" in dropped else list(regressors))
            unset = [b for b in regressors if b not in result["regressed_by"]]
            if unset:
                result["regressed_by_unlinked"] = unset
                logger.warning("autofile: bug %s could not be marked regressed_by %s",
                               bug_id, unset)
            # If a 4xx forced the train fields off the create, retry each independently.
            if "train_flags" in dropped:
                landed, refused = _put_train_flags(bug_id, train_flags, token)
            else:
                landed, refused = train_flags, []
            _record_train_flags(result, landed, refused)
    except Exception as exc:
        logger.error("autofile: Bugzilla write failed for %s: %s", uuid, exc)
        # PERSIST THE REJECTION. A failed write used to return here having written nothing, so
        # the only trace was a log line on a dyno with no drain and a ~2h window. That is how
        # BMO refused three creates for an over-long summary — 2026-08-06, 08-13 and 08-27 —
        # and nobody knew until somebody asked about the third by hand. Two of them were rung 70.
        #
        # A SEPARATE KEY from `filed_bug`, deliberately: that one is the per-uuid idempotence
        # key (`already_filed`), and a failure recorded there would make a retrigger read as
        # "already filed" and permanently close a crash whose bug was never created. This key
        # is not in `_STICKY_PAYLOAD_KEYS` either, so a later successful run drops it rather
        # than leaving a stale error beside a real filing.
        try:
            models.Dossier.record_filing_error(uuid, {
                "at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc)[:500],
                "signature": signature,
                "title_len": len((preview or {}).get("title") or ""),
                "mode": "comment" if bug_id is not None else "new_bug",
            })
        except Exception:                                   # pragma: no cover - defensive
            logger.warning("autofile: could not record the filing error for %s", uuid)
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
