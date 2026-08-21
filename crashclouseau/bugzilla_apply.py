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

from crashclouseau import config, models, utils
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

    The PUT is atomic and strict: one unknown id rejects the WHOLE list with code 101, so a bug
    that is restricted, wrong, or simply not visible to this account would otherwise also cost us
    the ``clouseau`` meta-bug link. Hence the retry with the aliases alone — dormant while the
    preview sends ``["clouseau"]`` and nothing else (the regressor is linked by ``regressed_by``
    now), which is why identical attempts are collapsed rather than posted twice.

    Best-effort throughout — a bug that is filed but unlinked is a small loss; an exception here
    would strand a filing we have already made."""
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
    """Set ``regressed_by`` on a freshly-created bug. Returns what actually got linked.

    Its OWN PUT, deliberately not another key in the one ``_link_blockers`` sends. Create discards
    ``regressed_by`` exactly as silently as it discards ``blocks`` (probed on allizom: 200, and
    the field comes back empty), and the PUT is atomic ACROSS fields as well as within one --
    ``{"blocks": {"add": ["clouseau"]}, "regressed_by": {"add": [<unknown bug>]}}`` came back
    404/code 101 with the perfectly good blocks add dropped too. A regressor bug we cannot read
    is the ordinary case rather than the exotic one (BMO answers 102 for 2043188, which cost 2 of
    the first 3 filings their blocker link), so sharing one PUT would put the meta-bug link back
    at the mercy of the field likeliest to fail.

    No retry with a shorter list, because there is nothing to shorten: the pipeline names one
    changeset, so a rejection means the claim cannot land at all. Best-effort like the blockers —
    the bug is already filed and the changeset is named in the comment prose."""
    if not regressors:
        return []
    try:
        _put_bug(bug_id, {"regressed_by": {"add": list(regressors)}}, token)
        return list(regressors)
    except Exception as exc:
        logger.warning("autofile: setting regressed_by %s on bug %s failed: %s",
                       regressors, bug_id, exc)
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
    signature in its ``cf_crash_signature``, which is a bare substring match, so the union the
    caller keeps is identical: the exact form changes the keep-set for 0 of the 200 and moves
    the venue for 0. What it guards is a shape the panel does not contain — a crash bug with an
    EMPTY ``cf_crash_signature`` (1891138, 2009859 and 1960108 each have one) whose summary
    carries a longer signature. Case-insensitive because BMO's ``substring`` operator is, and
    anything this sees was already returned by that search."""
    low = (summary or "").lower()
    sig = (signature or "").strip().lower()
    return bool(sig) and any(form.format(sig) in low for form in _SUMMARY_CRASH_FORMS)


def _row_is_about(bug, signature):
    """Does this BMO row really carry *signature*, or did the OR over-match?

    One request asks ``cf_crash_signature`` for the bare signature OR ``short_desc`` for the
    prefix ``[@ sig``, and the response does not say which clause matched, so both are
    re-checked here. The cf side is a plain substring — that field only ever holds signatures,
    which is why it needs no form test at all. The summary side demands the exact crash-bug
    form (``_summary_is_about``), because the prefix over-matched a LONGER signature for 3 of
    the top 200 nightly signatures. Measured cost and benefit both zero on that panel — all
    three over-matches are cf-reachable anyway, so this re-check changes the keep-set for 0/200
    and the venue for 0/200. It is insurance against an over-match on a bug whose
    ``cf_crash_signature`` is empty, not a save the panel witnessed."""
    sig = (signature or "").strip()
    if sig and sig.lower() in (bug.get("cf_crash_signature") or "").lower():
        return True
    return _summary_is_about(bug.get("summary"), sig)


def _open_bugs_for_signature(signature):
    """OPEN bugs referencing *signature* as
    ``[{"id", "creation_time", "product", "keywords"}, ...]``, oldest first.

    Every field beyond the id is there because the oldest open bug is not automatically the
    right place to comment: ``creation_time`` for ``_bug_for_this_regression``, ``product``
    for ``_split_by_application``, ``keywords`` for ``_split_out_metas``.

    Read-only and unauthenticated (public bugs only, which is the right scope: we must not
    reason about a security bug we can only see because the filing account can).

    ``cf_crash_signature`` is matched on the BARE signature, not the ``[@ signature]`` form:
    bug 1990812 carries ``[@ mozilla::MediaDecoder::SetCDMProxy ]`` — with a trailing space —
    so the bracketed form missed it and we filed 2060922 as a near-duplicate of a REOPENED bug
    for the exact same crash. The SUMMARY half is the other way round and ungated: it asks for
    the crash-bug form ``[@ sig`` and then keeps only an exact ``[@ sig]``/``[@ sig ]``
    (``_summary_is_about``), which is what the retired ``_is_specific_signature`` length test
    was really reaching for. One request, so the rows are re-checked here rather than in a
    second query — BMO's OR does not say which clause matched.

    Oldest first, because among the bugs that could be about this crash the earliest is the
    canonical one, carrying whatever discussion already exists; newest-first would prefer a
    recent duplicate, including one we filed ourselves. Only a tie-break, though —
    ``_bug_for_this_regression`` decides which of them qualify at all."""
    if not signature:
        return []
    sig = signature.strip()
    params = {
        "include_fields": "id,summary,status,resolution,creation_time,product,keywords,"
                          "cf_crash_signature",
        "j_top": "OR",
        "f1": "cf_crash_signature", "o1": "substring", "v1": sig,
        "f2": "short_desc", "o2": "substring", "v2": "[@ " + sig,
        "resolution": "---",
    }
    try:
        r = net.get(_bz_rest(), params=params, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        bugs = (r.json() or {}).get("bugs") or []
    except Exception as exc:                                   # pragma: no cover - network
        # Fail CLOSED: if we cannot tell whether a bug exists, do not file a possible
        # duplicate. A missed filing is recoverable; a duplicate on BMO is not.
        logger.warning("autofile: signature bug lookup failed for %r: %s", signature, exc)
        return None
    return [
        {"id": b["id"], "creation_time": b.get("creation_time"),
         "product": b.get("product"), "keywords": b.get("keywords") or []}
        for b in sorted(bugs, key=lambda b: b.get("id", 0))
        if b.get("id") and _row_is_about(b, sig)
    ]


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
    try:
        r = net.get("{}/{}/history".format(_bz_rest(), bug_id), timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        history = ((r.json() or {}).get("bugs") or [{}])[0].get("history") or []
    except Exception as exc:                                   # pragma: no cover - network
        logger.warning("autofile: history lookup failed for bug %s: %s", bug_id, exc)
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


def _bug_for_this_regression(bugs, landed, max_age_days, candidate_bug=None):
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
    predating = []
    for bug in bugs or []:
        created = sigage.to_datetime(bug.get("creation_time"))
        if created is None:
            return bug["id"], []
        if (landed - created).total_seconds() / 86400.0 <= max_age_days:
            return bug["id"], predating
        reopened = _last_reopened(bug["id"])
        if reopened is not None and (landed - reopened).total_seconds() / 86400.0 <= max_age_days:
            return bug["id"], predating
        predating.append(bug["id"])
    return None, predating


def _fixed_after_build_bug(signature, buildid, product):
    """The id of a bug on *signature* that was RESOLVED FIXED **after** *buildid* was produced,
    or ``None``. In one line: is this crash a pre-fix report of a defect somebody has already
    fixed?

    Asked only when we are about to file a NEW bug. ``_open_bugs_for_signature`` filters
    ``resolution="---"`` and must keep doing so — a closed bug is not a comment venue — so this
    is a SIBLING asking the other question, not a widening of that one, and the two param dicts
    are deliberately duplicated (30+ tests mock that function by name and one pins the exact
    three-key row it returns). Keep them in step by hand.

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
    params = {
        "include_fields": "id,summary,status,resolution,product,cf_crash_signature,"
                          "cf_last_resolved",
        "j_top": "OR",
        "f1": "cf_crash_signature", "o1": "substring", "v1": sig,
        "f2": "short_desc", "o2": "substring", "v2": "[@ " + sig,
    }
    try:
        r = net.get(_bz_rest(), params=params, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        bugs = (r.json() or {}).get("bugs") or []
    except Exception as exc:                                   # pragma: no cover - network
        logger.warning("autofile: fixed-bug lookup failed for %r: %s — filing anyway",
                       signature, exc)
        return None
    ours, _theirs = _split_by_application(bugs, product)
    for bug in sorted((b for b in ours if b.get("id")), key=lambda b: b["id"]):
        if (bug.get("resolution") or "").upper() != "FIXED" or not _row_is_about(bug, sig):
            continue
        resolved = sigage.to_datetime(bug.get("cf_last_resolved"))
        if resolved is not None and resolved > build_dt:
            return bug["id"]
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
    rather than at the call site, and each one fails CLOSED except where marked otherwise:

    * disabled unless ``AUTOFILE_BUGS`` is on (a real kill-switch: it writes to production
      BMO on a schedule, so it has to be stoppable without a deploy);
    * verdict must be reported and at/above ``min_confidence`` (70 = the ``probable`` rung);
    * never twice for one crash (``Dossier.already_filed``), which matters because the
      orphan reaper re-runs a crashed run and would otherwise re-file on recovery;
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

    ``regressed_by`` is set — under the pushlog-window gate, on a bug we filed ourselves, and in
    its own PUT (see ``_link_regressed_by`` and ``report_bug.build_bug_preview``). Because we now
    write the field the feedback loop reads, ``models.Feedback.classify`` is told what we claimed:
    our own write agreeing with us is ``unconfirmed``, not ``correct``."""
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

    signature = (uuid_info.get("signature") or "").strip()
    existing = _open_bugs_for_signature(signature)
    if existing is None:
        return {"filed": False, "skipped": "signature lookup failed; not risking a duplicate"}
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
    if existing and not cfg["comment_on_existing"]:
        return {"filed": False, "skipped": "open bug {} exists".format(existing[0]["id"])}
    # WHICH of those open bugs, if any, can be about this regression — the oldest one often
    # cannot, and with no landing date NONE of them can be shown to
    # (``_bug_for_this_regression``). Resolved before the preview is built so a new bug filed
    # past an older one can say so, and say which of the two reasons it was.
    landed = _candidate_landed(dossier, uuid_info.get("channel"))
    bug_id, predating = _bug_for_this_regression(
        existing,
        landed,
        cfg["comment_max_bug_age_days"],
        candidate_bug=((dossier or {}).get("candidate") or {}).get("bug"),
    )
    landing_unresolved = landed is None and bug_id is None and bool(predating)
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
            signature, uuid_info.get("buildid"), uuid_info.get("product"))
        if fixed_by:
            bid = utils.get_buildid(uuid_info.get("buildid"))
            logger.info("autofile: bug %s was FIXED after build %s, so %s is a pre-fix report "
                        "of an already-fixed defect — not filing", fixed_by, bid, uuid)
            return {"filed": False, "bug": fixed_by,
                    "skipped": "already fixed by bug {} (the fix postdates build {})".format(
                        fixed_by, bid)}

    # Said it once already. `already_filed` above is keyed on the UUID, which is the wrong
    # grain: one (signature, build) splits into one cluster per distinct stack, each analysed
    # and filed on its own, and they all resolve to the SAME bug here. Bug 2062934 collected
    # two identical analyses 80 seconds apart from two crashes on one machine. Skipping
    # entirely rather than falling through to a new bug — a duplicate on BMO is the worse of
    # the two noises.
    prior_comment = (models.Dossier.already_commented(bug_id, signature)
                     if bug_id is not None else None)
    if prior_comment:
        logger.info("autofile: bug %s already carries our analysis of %r (from %s) — not "
                    "commenting again for %s", bug_id, signature,
                    prior_comment.get("uuid") or "?", uuid)
        return {"filed": False, "bug": bug_id,
                "skipped": "already commented on bug {} for this signature".format(bug_id),
                "prior_comment": prior_comment}

    from crashclouseau import report_bug
    try:
        preview = report_bug.build_bug_preview(
            uuid_info, stack, dossier,
            related_bugs=predating if bug_id is None else None,
            landing_unresolved=landing_unresolved,
            other_app_bugs=other_app if bug_id is None else None,
            meta_bugs=meta_bugs if bug_id is None else None,
        )
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

    email = preview.get("needinfo_email") if cfg["needinfo"] else ""
    result = {"filed": False, "uuid": uuid, "signature": signature,
              "at": datetime.now(timezone.utc).isoformat()}
    try:
        if bug_id is not None:
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
            # The structured causal claim, in its own PUT (see ``_link_regressed_by``). Only on
            # a bug we FILED: on somebody else's open bug the field is often already curated —
            # 2 of the 6 filings that commented on an existing bug found a human-set
            # ``regressed_by`` there, and on bug 2057980 ours would have contradicted it.
            regressors = preview.get("regressed_by") or []
            result["regressed_by"] = _link_regressed_by(bug_id, regressors, token)
            unset = [b for b in regressors if b not in result["regressed_by"]]
            if unset:
                result["regressed_by_unlinked"] = unset
                logger.warning("autofile: bug %s could not be marked regressed_by %s",
                               bug_id, unset)
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
