# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from flask import request, jsonify
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
