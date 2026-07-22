# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Clouseau evidence tools, exposed to the agent as in-process MCP servers via
``crashclouseau.vendor.agent_tools.build_sdk_server`` (#02)."""
from __future__ import annotations


def pin_node(build_rev: str, node: str) -> str:
    """Pinned-mode revision selection, SHARED by the history + source tools so they cannot
    drift. When ``build_rev`` is set (a P1 off-stack run pinned to the crash build),
    redirect the default OR an explicit ``tip``/``default`` to ``build_rev`` so a pinned
    read can never fall through to tip — which would leak the post-build fix and let the
    agent cite post-fix code as the crash mechanism. An explicit, non-tip node the agent
    passes is always honored. With no ``build_rev`` (on-stack), returns the node unchanged
    (default ``tip``). This is a DETERMINISTIC guard, not a model-compliance request."""
    if build_rev and (not node or node in ("tip", "default")):
        return build_rev
    return node or "tip"
