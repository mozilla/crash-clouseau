# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from flask import request, jsonify, abort
from crashclouseau import models
from . import buginfo, java


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

    The ONLY write path to Bugzilla in the product, reached only from the POST route
    behind an explicit browser ``confirm()``. Trusts only ``{uuid, indices}``; the
    persisted action bodies are re-read server-side."""
    from crashclouseau import bugzilla_apply

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
