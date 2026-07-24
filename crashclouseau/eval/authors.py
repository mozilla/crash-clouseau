# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Changeset -> author resolution for the PERSON-LEVEL eval metric (#13).

The pivoted goal is "get the RIGHT PERSON investigating," so precision should be scored at
the person a needinfo would reach, not just the exact changeset ("gold" = the regressor
changeset; "silver" = a co-author/reviewer of it). A ``lead`` that blames the wrong
changeset but the SAME author as the true regressor is still a good triage outcome.

``author_of`` is a best-effort, cached hg lookup (allowlisted ``crash-clouseau`` UA per the
hgmo rate-limit note); it returns "" on any failure so scoring degrades to changeset-level
rather than breaking. ``author_email`` normalises "Name <e@mail>" -> "e@mail" for matching.
"""
from __future__ import annotations

import functools
import json
import re
import urllib.request

_UA = {"User-Agent": "crash-clouseau"}
_EMAIL_RE = re.compile(r"<([^>]+)>")


def author_email(author: str) -> str:
    """Normalise a commit author ("Real Name <e@mail>") to a lowercased email for matching;
    falls back to the trimmed string when there is no angle-bracket email."""
    if not author:
        return ""
    m = _EMAIL_RE.search(author)
    return (m.group(1) if m else author).strip().lower()


@functools.lru_cache(maxsize=8192)
def author_of(node: str) -> str:
    """The hg commit author for a short/long rev, or "" (best-effort). Tries mozilla-central
    then integration/autoland (a landing may only exist on autoland at the crash build)."""
    if not node:
        return ""
    for repo in ("mozilla-central", "integration/autoland"):
        try:
            req = urllib.request.Request(
                "https://hg.mozilla.org/{}/json-rev/{}".format(repo, node), headers=_UA
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                user = json.load(resp).get("user")
            if user:
                return user
        except Exception:
            continue
    return ""


def same_person(cited_author: str, regressor_authors) -> bool:
    """True if the accused changeset's author is one of the true regressor's authors — the
    'silver nugget': the right person to needinfo even when the exact changeset is wrong."""
    ce = author_email(cited_author)
    if not ce:
        return False
    return ce in {author_email(a) for a in (regressor_authors or [])}
