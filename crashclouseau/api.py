# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import hmac
import os

from flask import request, jsonify, abort
from crashclouseau import models
from . import buginfo, java


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


# Name of the cookie that remembers a valid ``?token=``, so the secret appears in a URL at most
# once per browser instead of on every navigation (where it would land in Heroku's router log and
# in any Referer we emit).
VIEW_COOKIE = "clouseau_view"


def viewer_authorized() -> bool:
    """May this request see an analysis that `sensitive.py` withheld?

    Reuses ``API_WRITE_TOKEN`` rather than adding a second secret. That is deliberately not
    least-privilege -- a read token and a write token should differ -- but this deployment has
    exactly one reader, and a second config var nobody sets is a gate nobody can pass. Splitting
    it later is one line here.

    Accepts the token from a header (for ``/api/evidence``), a ``?token=`` query arg (browsers
    cannot send a custom header from the address bar) or ``VIEW_COOKIE`` (set once, from a valid
    query arg, by ``remember_viewer``).

    With ``API_WRITE_TOKEN`` unset this returns False, matching ``_require_write_token``: an
    unset secret must never read as "no authentication required". Here that means the withheld
    analyses are unreachable rather than public, which is the correct direction."""
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
    """Gate a route that SPENDS MONEY or mutates state behind the viewer token.

    ``/api/tasks/retrigger`` was ``@cross_origin()`` POST with no authorization of any kind.
    Measured on the deployed app: one anonymous ``GET /tasks.html`` returns 500 real uuids with
    ``retriggerTask('<uuid>')`` already wired, and 3,188 of the 3,488 rows in ``uuids`` have the
    ``crashstack`` rows ``build_seed`` needs — so each was one unauthenticated POST away from a
    full agent run, measured in production at a mean $1.70 and a maximum $8.49. There is no global
    spend cap (``max_cost_usd_per_crash`` warns, it does not abort) and no rate limit, and per the
    route's own docstring the re-run reaches ``_maybe_autofile`` like any other.

    WHY THE VIEWER TOKEN AND NOT ``_require_write_token``. That one reads ONLY the
    ``X-Clouseau-Token`` header, and ``static/clouseau.js``'s ``fetch`` sends no such header; the
    operator cannot add one from a browser, and ``VIEW_COOKIE`` is ``httponly`` so script cannot
    read the token to attach it. Gating on the write token would therefore have closed the hole and
    silently broken the tasks-view button — the "fails by doing less, with no log line" shape this
    repo keeps getting bitten by. ``viewer_authorized`` accepts the header OR ``?token=`` OR the
    cookie, so one ``?token=`` visit keeps the UI working.

    NOT the channel gate. ``bugzilla_apply.autofile_bug`` already fails closed on a channel with no
    autofile configuration, and ``enqueue_agent``'s ``force=True`` bypass is the feature the
    operator is asking for — neither is the thing to narrow here. The missing control was
    authentication, and it belongs on the route."""
    if not viewer_authorized():
        abort(403, "invalid or missing token")


def remember_viewer(response):
    """Persist a valid ``?token=`` as a cookie so it need not ride the URL again.

    Only ever called once the token has already been checked, and only when it arrived as a
    query arg -- a request authorised by the header or by the cookie has nothing to store.
    ``httponly`` because no script needs it; ``samesite="Lax"`` so it is not sent on
    cross-site requests."""
    if request.args.get("token") and viewer_authorized():
        response.set_cookie(VIEW_COOKIE, request.args["token"],
                            httponly=True, samesite="Lax", secure=True, max_age=90 * 86400)
    return response


def javast():
    data = request.get_json()
    channel = data["channel"]
    buildid = data["buildid"]
    stack = data["stack"]
    data["stack"] = java.reformat_java_stacktrace(stack, channel, buildid)
    return jsonify(data)


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
    # Requiring the uuid in a JSON body forces a preflight instead, and this route is no longer
    # in the CORS resource set (`__init__.py` scopes it to `/api/javast`), so the preflight has
    # nothing to approve it with.
    uuid = data.get("uuid") or ""
    if not uuid:
        abort(400, "No uuid provided")
    if not models.UUID.exists(uuid):
        abort(404, "Unknown uuid")
    return jsonify(orchestrator.retrigger_agent(uuid))
