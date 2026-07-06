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
