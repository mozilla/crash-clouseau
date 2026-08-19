# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""On-stack / off-stack labeling (#13).

For each corpus case, decide whether the true regressor changeset touched a file
that appears on the crash stack — the denominator that makes off-stack recall
meaningful (off-stack = the regressor is reachable only through the call graph).

Uses the regressor's touched files (via #14 ``patch_extract`` on the regressor hg
node) intersected with the frozen crash's stack files. Both sides are live-ish
(patch fetch + frame-uri parsing); not exercised in unit tests."""
from __future__ import annotations

import json
import os

from crashclouseau import inspector
from crashclouseau.agent import patch_extract
from crashclouseau.logger import logger


def _stack_basenames(case):
    if not case.crash_json_path:
        return set()
    try:
        with open(case.crash_json_path) as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return set()
    dump = data.get("json_dump", {})
    # See `inspector.thread_for_analysis`: the watchdog thread of a hang carries no source
    # file, so labelling off it would silently produce an empty file set.
    ct = inspector.thread_for_analysis(data) or 0
    threads = dump.get("threads", [])
    frames = threads[ct]["frames"] if ct < len(threads) else []
    names = set()
    for frame in frames:
        uri = frame.get("file")
        if not uri:
            continue
        try:
            filename, _ = inspector.get_path_node(uri)
        except Exception:
            filename = uri
        if filename:
            names.add(os.path.basename(filename))
    return names


def derive_onstack_label(case):
    """True if ANY regressor changeset touched a file on the crash stack; False if all
    are off-stack; None if undeterminable (no regressor node / empty diffs)."""
    nodes = case.regressor_nodes or ([case.regressor_node] if case.regressor_node else [])
    if not nodes:
        return None
    reg_files = set()
    for node in nodes:
        try:
            ext = patch_extract.extract(node, case.channel)
        except Exception:  # pragma: no cover - network
            continue
        reg_files |= {os.path.basename(fd.filename) for fd in ext.files}
    if not reg_files:
        logger.warning("eval: no diff files for regressor(s) %s", nodes)
        return None
    return bool(reg_files & _stack_basenames(case))
