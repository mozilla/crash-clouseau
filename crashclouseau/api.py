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
            "summary": models.Selection.summary(days),
            "days": days,
            "rows": models.Selection.recent(outcome, days),
        }
    )


def evidence():
    """Read-only verdict/dossier/recorded-actions JSON for the evidence panel (#12).
    Writes nothing to Bugzilla or the DB. ``verdict`` is ``None`` when no row exists."""
    from crashclouseau import bugzilla_apply

    uuid = request.args.get("uuid", "")
    if not uuid:
        abort(400, "No uuid provided")

    ev = bugzilla_apply.build_evidence(uuid)
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
    for this specific uuid. This is analysis only -- it writes nothing to Bugzilla."""
    from crashclouseau.agent import orchestrator

    data = request.get_json(silent=True) or {}
    uuid = data.get("uuid") or request.args.get("uuid", "")
    if not uuid:
        abort(400, "No uuid provided")
    return jsonify(orchestrator.retrigger_agent(uuid))
