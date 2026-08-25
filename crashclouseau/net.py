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

# Two more libmozdata defaults are pinned on the same class, for the same reason as the
# timeout below: left alone they compose into a dead web dyno.
#
# 1. ``MAX_RETRIES = 256`` with ``backoff_factor=1`` over ``[429, 500, 502, 503, 504]``. The
#    sleeps go 1, 2, 4, 8 ... capped at urllib3's 120s, so ONE request can keep retrying for
#    ~8.5 hours. Bounded to two retries (~3s of backoff), which is still a real retry: BMO's
#    rate limiter holds for ~45 min and no retry count outwaits it, so failing fast and
#    letting the caller degrade is the only useful answer to a 429.
# 2. ``TIMEOUT = 30`` is the ENTIRE budget gunicorn gives a request, so one stalled read
#    spends all of it. That does not cost a slow page, it costs the page: the abort arrives
#    as ``SystemExit`` raised inside the blocked thread, a BaseException that no
#    ``except Exception`` best-effort guard catches, so crashstack.html 500s instead of
#    rendering without the block it was waiting on. Seen 2026-08-25: a ``/rest/bug?id=2048793``
#    read for the bug preview timed out and took the page down with it (H12,
#    ``service=30000ms``, gunicorn ``WORKER TIMEOUT``).
#
# Tightened to ``(connect 5, read 6)`` for the two services the WEB path talks to. Both are
# measured at 0.3-0.6s -- including the heaviest shape either one is asked for anywhere in the
# tree, ``datacollector.get_proto_small``'s ``_aggs.proto_signature`` aggregation with
# ``_facets_size=10000`` over a 7-day window (0.4-0.5s) -- so 6s is >10x headroom, and it is
# the RETRIES that carry a genuinely slow minute upstream (3 attempts), not the patience of a
# single one. Worst case is then 3*6+3 = 21s, which fits inside a request with room to render.
#
# Deliberately NOT applied to the hg clients, which keep libmozdata's 30: hg.mozilla.org takes
# 8-13s to answer a json-annotate at the best of times (see below), and the agent leans on that
# client for every pinned blame/history/patch read.
#
# All of this has to be set on the CLASS. ``Connection.__init__`` reads
# ``Connection.MAX_RETRIES`` when it builds the urllib3 ``Retry``, and its per-call
# ``max_retries=`` kwarg is dead code -- assigned to ``self`` after that ``Retry`` exists.
try:  # pragma: no cover - trivial global stamp, exercised via the hg tools
    from libmozdata.connection import Connection as _LmdConnection

    if not _LmdConnection.USER_AGENT:
        _LmdConnection.USER_AGENT = USER_AGENT

    from libmozdata.bugzilla import BugzillaBase as _LmdBugzillaBase
    from libmozdata.socorro import Socorro as _LmdSocorro

    _LmdConnection.MAX_RETRIES = 2
    _LmdBugzillaBase.TIMEOUT = _LmdSocorro.TIMEOUT = (5, 6)
except Exception:
    pass


# ``requests`` defaults to NO timeout, i.e. block forever if the far end goes quiet. Inside
# an RQ worker that is not a slow request, it is a lost run: the job hangs until RQ SIGKILLs
# the work-horse at ``job_timeout``, the dossier is left stuck at ``running``, the orphan
# reaper re-enqueues it, and the whole analysis is paid for again — twice, then abandoned at
# ``reap_max_attempts``. Seen on 0cf2a052-2eae-4228-824f-6284d0260728, whose stale-signature
# gate is the one code path that has to ask hg.mozilla.org (8-13s at the best of times, and
# it 406-throttles) for a candidate's push date.
#
# ``(connect, read)``: 10s to establish a connection, then 60s of SILENCE allowed. The read
# half is a gap-between-bytes limit, NOT a total-duration cap, so a large-but-progressing
# response (an hg raw-rev of a big patch, a symbol file) still completes.
DEFAULT_TIMEOUT = (10, 60)


def _with_ua(kwargs):
    # Merge our UA into any caller-supplied headers without clobbering an explicit one, and
    # bound the request unless the caller asked for something specific (setdefault, so an
    # explicit timeout — including None for "wait forever" — always wins).
    headers = dict(kwargs.pop("headers", None) or {})
    headers.setdefault("User-Agent", USER_AGENT)
    kwargs["headers"] = headers
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    return kwargs


def get(url, **kwargs):
    return requests.get(url, **_with_ua(kwargs))


def post(url, **kwargs):
    return requests.post(url, **_with_ua(kwargs))


def put(url, **kwargs):
    return requests.put(url, **_with_ua(kwargs))
