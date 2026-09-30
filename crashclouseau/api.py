# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import hmac
import os

from flask import request, jsonify, abort
from crashclouseau import disclosure, models
from . import buginfo


def _require_write_token():
    """Gate the routes that WRITE to Bugzilla behind a shared secret.

    ``/api/evidence/apply`` posts comments and sets needinfo flags on production BMO using
    the deployment's API key. It is ``@cross_origin()`` and was reachable by anyone who
    knew a uuid — and uuids are enumerable from the public reports pages and from
    ``/api/evidence``. Its docstring claimed protection from "an explicit browser
    ``confirm()``", but that dialog lived in the apply UI, which was removed in the
    informative-only phase; a client-side dialog was never an authorization control anyway.

    Set ``API_WRITE_TOKEN`` and send it as ``X-Clouseau-Token``. With the variable UNSET the
    route is refused outright rather than left open: an unset secret must not mean "no
    authentication required" on the one route that can write to a bug tracker."""
    expected = os.getenv("API_WRITE_TOKEN", "")
    if not expected:
        abort(503, "write API disabled (no API_WRITE_TOKEN configured)")
    supplied = request.headers.get("X-Clouseau-Token", "")
    if not hmac.compare_digest(supplied, expected):
        abort(403, "invalid or missing X-Clouseau-Token")


# Cookie accepted by the token gate. remember_viewer() currently has no route callers.
VIEW_COOKIE = "clouseau_view"


def viewer_authorized() -> bool:
    """May this request read withheld analyses via a token or Mozilla sign-in?"""
    from crashclouseau import auth

    return _token_authorized() or auth.current_user() is not None


def _token_authorized() -> bool:
    """Check API_WRITE_TOKEN in the header, query string or viewer cookie.

    An unset API_WRITE_TOKEN denies access."""
    expected = os.getenv("API_WRITE_TOKEN", "")
    if not expected:
        return False
    for supplied in (request.headers.get("X-Clouseau-Token", ""),
                     request.args.get("token", ""),
                     request.cookies.get(VIEW_COOKIE, "")):
        if supplied and hmac.compare_digest(supplied, expected):
            return True
    return False


def _require_viewer():
    """Require the token for retriggering; Google sign-in grants no write access.

    Accept the cookie because the tasks button sends no X-Clouseau-Token header."""
    if not _token_authorized():
        abort(403, "invalid or missing token")


def remember_viewer(response):
    """Set a viewer cookie when a token query argument accompanies token authorization.

    No routes currently call this helper."""
    if request.args.get("token") and _token_authorized():
        response.set_cookie(VIEW_COOKIE, request.args["token"],
                            httponly=True, samesite="Lax", secure=True, max_age=90 * 86400)
    return response


def bugs():
    sgn = request.args.get("signature", "")
    data = buginfo.get_bugs(sgn)
    return jsonify(data)


def reports():
    signatures = request.args.getlist("signatures")
    if not signatures:
        abort(400, "No signatures provided")

    product = request.args.get("product")
    if product and product not in models.PRODUCT_TYPE.enums:
        abort(400, f"The product must be one of: {models.PRODUCT_TYPE.enums}")

    channel = request.args.get("channel")
    if channel and channel not in models.CHANNEL_TYPE.enums:
        abort(400, f"The channel must be one of: {models.CHANNEL_TYPE.enums}")

    res = models.Signature.get_reports(signatures, product, channel)

    return jsonify(res)


def selection():
    """Read-only: what the spike selector decided, including what it declined.

    ``?signature=X`` answers "why is there no analysis for X"; without one it returns the
    recent feed, optionally filtered by ``?outcome=`` (``untestable_prefix`` is the
    blind-spot feed). Reads the log only — it never re-runs the selector."""
    # Stripped: a pasted signature usually carries whitespace, and an unstripped miss
    # answers `{"rows": []}` -- indistinguishable from "never considered", which is the
    # one thing this endpoint exists to tell apart.
    sgn = request.args.get("signature", "").strip()
    product = request.args.get("product") or None
    channel = request.args.get("channel") or None
    if product and product not in models.PRODUCT_TYPE.enums:
        abort(400, f"The product must be one of: {models.PRODUCT_TYPE.enums}")
    if channel and channel not in models.CHANNEL_TYPE.enums:
        abort(400, f"The channel must be one of: {models.CHANNEL_TYPE.enums}")
    if sgn:
        rows = models.Selection.for_signature(sgn, product, channel)
        return jsonify({"signature": sgn, "rows": rows})

    outcome = request.args.get("outcome") or None
    if outcome is not None and outcome not in models.SELECTION_OUTCOMES:
        abort(400, f"The outcome must be one of: {sorted(models.SELECTION_OUTCOMES)}")
    try:
        days = int(request.args.get("days", 14))
    except ValueError:
        abort(400, "days must be an integer")
    days = max(1, min(days, 90))
    return jsonify(
        {
            # `product`/`channel` are validated 20 lines up and were then DROPPED, so
            # `?channel=release` returned nightly's rows with a 200. See `Selection.recent`.
            # A channel with no ingested rows now correctly answers `{"rows": []}` -- that is
            # the right answer for release under `INGEST_CHANNELS="nightly beta"`, not an outage.
            "summary": models.Selection.summary(days, product, channel),
            "days": days,
            "rows": models.Selection.recent(outcome, days, product=product,
                                            channel=channel),
        }
    )


def spikes():
    """Read-only: the REAL spikes the pipeline escalated (``agent.spike_escalation``) -- what
    fired, what the investigator concluded, what was filed or why not. ``?signature=X`` narrows
    to one signature; ``?channel=`` / ``?product=`` filter; ``?limit=`` caps (default 200)."""
    product = request.args.get("product") or None
    channel = request.args.get("channel") or None
    if product and product not in models.PRODUCT_TYPE.enums:
        abort(400, f"The product must be one of: {models.PRODUCT_TYPE.enums}")
    if channel and channel not in models.CHANNEL_TYPE.enums:
        abort(400, f"The channel must be one of: {models.CHANNEL_TYPE.enums}")
    try:
        limit = int(request.args.get("limit", 200))
    except ValueError:
        abort(400, "limit must be an integer")
    limit = max(1, min(limit, 1000))
    rows = models.SpikeEscalation.recent(limit=limit, product=product, channel=channel)
    sgn = request.args.get("signature", "").strip()
    if sgn:
        rows = [r for r in rows if r.get("signature") == sgn]
    # Withheld findings and their filing metadata need separate redaction.
    if not viewer_authorized():
        for r in rows:
            withheld = bool(r.get("findings")) and not disclosure.public_findings(r.get("filing"))
            if withheld:
                r["findings"] = None
            r["filing"] = disclosure.public_filing(r.get("filing"), withheld)
    return jsonify({"rows": rows})


def evidence():
    """Read-only verdict/dossier/recorded-actions JSON for the evidence panel (#12).
    Writes nothing to Bugzilla or the DB. ``verdict`` is ``None`` when no row exists."""
    from crashclouseau import bugzilla_apply

    uuid = request.args.get("uuid", "")
    if not uuid:
        abort(400, "No uuid provided")

    ev = bugzilla_apply.build_evidence(uuid, public=not viewer_authorized())
    if ev is None:
        return jsonify({"uuid": uuid, "verdict": None})
    return jsonify(ev)


def apply_actions():
    """Execute the human-confirmed subset of recorded Bugzilla actions (#12).

    Human-triggered Bugzilla writes. Requires ``X-Clouseau-Token`` (see
    ``_require_write_token``) — this posts to production BMO with the deployment's API key,
    so it cannot be left open to anyone holding a uuid. Trusts only ``{uuid, indices}``;
    the persisted action bodies are re-read server-side."""
    from crashclouseau import bugzilla_apply

    _require_write_token()
    data = request.get_json(silent=True) or {}
    uuid = data.get("uuid", "")
    indices = data.get("indices")
    if not uuid:
        abort(400, "No uuid provided")
    ok_indices = isinstance(indices, list) and all(
        isinstance(i, int) and not isinstance(i, bool) for i in indices
    )
    if not ok_indices:
        abort(400, "indices must be a list of integers")

    try:
        results = bugzilla_apply.apply_recorded_actions(uuid, indices)
    except LookupError:
        abort(404, "No dossier for uuid")

    return jsonify({"uuid": uuid, "results": results})


def retrigger():
    """Re-run triage for one uuid from the tasks view (error/running/stalled). If the
    task is still running its RQ job is stopped first so we don't pay for two runs. The
    run is forced past the nightly/proto/skip-existing gates since the operator asked
    for this specific uuid.

    IT CAN WRITE TO BUGZILLA. This line used to say "analysis only -- it writes nothing to
    Bugzilla", which was never true: the re-run is an ordinary run and reaches ``_maybe_autofile``
    like any other. On 2026-08-24 a 20-uuid retrigger experiment put a second copy of one analysis
    on bug 2065072 and filed a new bug 2066051. ``retrigger_agent`` logs a warning when the crash
    has already been filed, and ``Dossier._STICKY_PAYLOAD_KEYS`` keeps the ``filed_bug`` record
    across the reset so the idempotence keys still hold — but a crash that has NEVER filed (an
    abstain, or a create BMO rejected) will file on the re-run if the new verdict qualifies, which
    is usually the point of retriggering it.

    Requires the viewer token (``_require_viewer``): header, ``?token=`` or ``VIEW_COOKIE``. It was
    open to anyone holding a uuid until 2026-08-31, and uuids are listed 500 at a time by
    ``/tasks.html``."""
    from crashclouseau.agent import orchestrator

    # BEFORE anything else, including the body parse. This route had no authorization at all
    # (see `_require_viewer`), and it is the one route that spends money per call.
    _require_viewer()
    data = request.get_json(silent=True) or {}
    # The `request.args` fallback is deliberately GONE. A query-string uuid made this
    # triggerable by a plain cross-site HTML form POST -- the one request shape that carries
    # cookies without a preflight. `VIEW_COOKIE` is `samesite="Lax"` so a cross-site POST
    # withholds it today, but that is CSRF closed by one keyword in an unrelated function.
    # Requiring the uuid in a JSON body forces a preflight instead, and this route carries no
    # `@cross_origin()` and is in no CORS resource set (`__init__.py`'s set is now empty), so the
    # preflight has nothing to approve it with.
    uuid = data.get("uuid") or ""
    if not uuid:
        abort(400, "No uuid provided")
    if not models.UUID.exists(uuid):
        abort(404, "Unknown uuid")
    return jsonify(orchestrator.retrigger_agent(uuid))


# Each uuid is a ~$1-3 run; a list is a deliberate batch, not a bulk tool.
_MAX_TRIGGER_UUIDS = 20


def trigger():
    """Analyse one crash -- or a short list -- on request, whether or not the pipeline ever
    selected it: ``POST {"uuids": [...], "file_bug": false, "show_in_tasks": true}`` (or
    ``"uuid": "..."``). A uuid Socorro knows and we never ingested is fetched and scored first
    (``trigger.ingest``). ``file_bug`` (default FALSE) decides whether the run may write to
    Bugzilla; ``show_in_tasks`` (default true) whether it is listed on tasks.html. Both are
    recorded on the dossier (``run_options``) and honoured by the run and by later re-runs.

    ``file_bug: true`` lets the run through the ordinary filing gates; it does not ARM a product
    whose filing is held (``agent.autofile.products.<product>.enabled: false`` -- Fenix, plans/16
    D4): ``autofile_bug`` refuses those with ``autofile held for product`` the way it refuses a
    held channel, so a Fenix uuid triggered with ``file_bug: true`` is analysed and files
    nothing. Each result names its ``product`` and ``channel`` so that is visible from the reply.

    Per-uuid outcomes come back in ``results``; a uuid that cannot be analysed says why there
    (``ok: false``, ``error``) rather than failing the whole call. Requires the WRITE token in
    the ``X-Clouseau-Token`` header: this is for scripts, it spends money per uuid and it can
    reach Bugzilla, so the header-only gate is the right one (``_require_write_token``)."""
    from crashclouseau import trigger as trigger_mod

    _require_write_token()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, "a JSON object body is required")
    uuids = data.get("uuids")
    if uuids is None and data.get("uuid"):
        uuids = [data["uuid"]]
    if not isinstance(uuids, list) or not uuids:
        abort(400, "uuids must be a non-empty list (or pass one uuid)")
    if len(uuids) > _MAX_TRIGGER_UUIDS:
        abort(400, "at most {} uuids per call".format(_MAX_TRIGGER_UUIDS))
    if not all(isinstance(u, str) for u in uuids):
        abort(400, "uuids must be strings")
    file_bug = data.get("file_bug", False)
    show_in_tasks = data.get("show_in_tasks", True)
    if not isinstance(file_bug, bool) or not isinstance(show_in_tasks, bool):
        abort(400, "file_bug and show_in_tasks must be JSON booleans")
    unique = []
    for u in uuids:
        if u not in unique:
            unique.append(u)
    results = trigger_mod.trigger_many(unique, file_bug=file_bug, show_in_tasks=show_in_tasks)
    return jsonify({"file_bug": file_bug, "show_in_tasks": show_in_tasks, "results": results})
