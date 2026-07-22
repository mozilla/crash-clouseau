# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Thin ``requests`` wrappers that stamp an identifying User-Agent on every direct HTTP
call we make to Mozilla services (lando, hg json-pushes / raw-rev, buildhub, crash-stats,
Bugzilla writes, the symbol server). libmozdata already sets this UA for the services IT
wraps (Bugzilla/Socorro/lando); these helpers give our OWN requests the same identity
instead of the default ``python-requests/x.y`` — good citizenship (infra can identify /
contact us) and it avoids being throttled as an anonymous bot.

The UA is read from the shared mozdata config (``[User-Agent] name``, the same key
libmozdata uses), defaulting to ``crash-clouseau``. (searchfox is queried through the
external ``searchfox-cli`` binary, which sets its own UA — not covered here.)
"""
import requests

try:
    from libmozdata import config as _lmdconfig
    USER_AGENT = _lmdconfig.get("User-Agent", "name", "crash-clouseau") or "crash-clouseau"
except Exception:  # pragma: no cover - config missing/broken -> still identify ourselves
    USER_AGENT = "crash-clouseau"


# libmozdata's own hg client (Annotate/FileInfo/Revision/RawRevision, all subclasses of
# connection.Connection) never actually sets its User-Agent: connection.py's
# ``if not self.USER_AGENT: config.get("User-Agent", "name", ...)`` fetches the configured
# name but DISCARDS the return value, so every hg json-annotate / json-filelog / raw-rev
# request goes out with User-Agent: None and is 406-rate-limited by hg.mozilla.org. The
# `crash-clouseau` UA is allowlisted, so stamp it on the base class (no subclass overrides
# USER_AGENT, verified) — this fixes the agent's pinned blame/history/patch-diff tools,
# which lean on that client heavily during an off-stack run. Best-effort; libmozdata may
# be absent in a stripped unit env.
try:  # pragma: no cover - trivial global stamp, exercised via the hg tools
    from libmozdata.connection import Connection as _LmdConnection

    if not _LmdConnection.USER_AGENT:
        _LmdConnection.USER_AGENT = USER_AGENT
except Exception:
    pass


def _with_ua(kwargs):
    # Merge our UA into any caller-supplied headers without clobbering an explicit one.
    headers = dict(kwargs.pop("headers", None) or {})
    headers.setdefault("User-Agent", USER_AGENT)
    kwargs["headers"] = headers
    return kwargs


def get(url, **kwargs):
    return requests.get(url, **_with_ua(kwargs))


def post(url, **kwargs):
    return requests.post(url, **_with_ua(kwargs))


def put(url, **kwargs):
    return requests.put(url, **_with_ua(kwargs))
