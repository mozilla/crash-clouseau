# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Pinned hg-edge source reads (P1).

Read a Firefox source file AS OF a specific build revision -- the leak-free alternative to
tip-only searchfox source bodies, needed because an off-stack run's only evidence may be
code that the FIX later changed (reading it at tip can make a post-fix state look like the
cause). We go DIRECTLY to hg-edge.mozilla.org (the live mirror after the git migration;
hg.mozilla.org is frozen and 302s here anyway) so we control headers + retry, and send the
allowlisted ``crash-clouseau`` User-Agent via ``net.get``. A module-global semaphore caps
concurrent hg requests -- a whole-window read from a single worker IP would otherwise burst
hgmo's WAF 406 rate-limit -- and 406/429/5xx are retried with backoff + jitter. Best-effort:
every entry point returns None rather than raising, so a hg hiccup degrades the agent run
instead of aborting it.
"""
from __future__ import annotations

import random
import threading
import time

from libmozdata.hgmozilla import Mercurial

from crashclouseau import net
from crashclouseau.logger import logger

_TIMEOUT = 60
_MAX_RETRIES = 5
_RETRY_STATUS = {406, 429, 500, 502, 503, 504}
# Global concurrency gate, independent of worker/subagent count: even allowlisted we stay
# polite to hgmo's WAF. 8 sits comfortably under the 12-way burst the spike verified safe.
_SEM = threading.Semaphore(8)


def _edge_base(channel: str) -> str:
    """hg-edge repo base URL for a channel, derived from libmozdata's (frozen)
    hg.mozilla.org repo URL by swapping the host -- robust across channels/repos."""
    try:
        base = Mercurial.get_repo_url(channel or "nightly")
    except Exception:  # pragma: no cover - defensive; libmozdata shape change
        base = "https://hg.mozilla.org/mozilla-central"
    return base.replace("hg.mozilla.org", "hg-edge.mozilla.org")


def _get(url, params=None, as_json=False):
    """GET through the concurrency gate, retrying transient throttles (406/429/5xx) with
    backoff+jitter. Returns parsed json / text, or None on 404 / a hard error / exhausted
    retries. ``net.get`` stamps our allowlisted UA."""
    backoff = 1.0
    for attempt in range(_MAX_RETRIES):
        resp = None
        with _SEM:
            try:
                resp = net.get(url, params=params, timeout=_TIMEOUT)
            except Exception as exc:  # network blip -> retry
                logger.debug("hgedge: get %s failed (attempt %d): %s", url, attempt, exc)
        if resp is not None:
            if resp.status_code == 200:
                try:
                    return resp.json() if as_json else resp.text
                except ValueError:
                    return None
            if resp.status_code == 404:
                return None
            if resp.status_code not in _RETRY_STATUS:
                logger.debug("hgedge: %s -> %d (giving up)", url, resp.status_code)
                return None
        # No sleep after the final attempt (the loop is about to exit with no further
        # request) — that would waste the whole backoff during an hg-edge outage.
        if attempt < _MAX_RETRIES - 1:
            time.sleep(backoff + random.random() * 0.5)
            backoff = min(backoff * 2, 20)
    logger.warning("hgedge: %s exhausted retries", url)
    return None


def raw_file(path, rev, channel="nightly"):
    """Full text of a source file AS OF ``rev`` (hg-edge ``raw-file/<rev>/<path>``), or
    None. The path keeps its slashes; ``rev`` is a plain hg hash / ``tip``."""
    if not path or not rev:
        return None
    url = "{}/raw-file/{}/{}".format(_edge_base(channel), rev, path.lstrip("/"))
    return _get(url, as_json=False)


def annotate(path, rev, channel="nightly"):
    """Blame rows for a file AS OF ``rev`` (hg-edge ``json-annotate``): a list of
    ``{node, author, desc, lineno, line}`` dicts, or None. Provided for completeness /
    reuse; the agent's line-blame goes through ``mcp__history__blame`` (also pinned)."""
    if not path or not rev:
        return None
    url = "{}/json-annotate/{}/{}".format(_edge_base(channel), rev, path.lstrip("/"))
    data = _get(url, as_json=True)
    if not isinstance(data, dict):
        return None
    return data.get("annotate") or []
