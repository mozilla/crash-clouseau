# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Area-experts selection (#15 phase 2).

Given the ranked candidate changesets (from ``orchestrator.build_seed``) and their
authors (from local ``models.Node.authors_for`` — migration-proof, no network), pick a
small set of developers who recently worked in the crashing area: someone to ASK, not
necessarily the cause. Pure/deterministic and DB-free (the caller does the query), so
it unit-tests trivially. Skips noise-flagged candidates, backed-out changesets, bot
authors, and the empty author; de-dupes and caps.
"""
from __future__ import annotations

# Substrings that mark an automated / non-human committer we should never needinfo.
_BOT_MARKERS = (
    "ffxbld", "no-reply", "noreply", "servo-vcs-sync", "l10n-bumper",
    "release+", "cron@", "seabld", "tbirdbld", "bot@",
)


def _is_bot(email: str, name: str, nick: str) -> bool:
    blob = " ".join((email or "", name or "", nick or "")).lower()
    return any(m in blob for m in _BOT_MARKERS)


def area_experts(candidates, authors_by_node, *, max_experts=3):
    """Return up to ``max_experts`` area-expert dicts (``AreaExpert`` shape) built from
    the authors of the top NON-noise, non-backed-out candidate changesets.

    ``candidates`` is build_seed's ranked list ([{node, score, bug, backedout, noise}]);
    ``authors_by_node`` maps a node hash -> {email, real, nick, ...}. De-duped by author
    identity (email, else name, else nick), so the same dev isn't listed twice."""
    experts: list[dict] = []
    seen: set[str] = set()
    for c in candidates:
        if c.get("noise") or c.get("backedout"):
            continue
        info = authors_by_node.get(c.get("node"))
        if not info:
            continue
        email = info.get("email", "")
        name = info.get("real", "")
        nick = info.get("nick", "")
        ident = (email or name or nick).lower()
        if not ident or ident in seen or _is_bot(email, name, nick):
            continue
        seen.add(ident)
        bug = c.get("bug")
        experts.append({
            "name": name,
            "email": email,
            "nick": nick,
            "node": c.get("node", ""),
            "bug": bug,
            "reason": "authored candidate {}{}".format(
                c.get("node", ""), " (bug {})".format(bug) if bug else ""
            ),
        })
        if len(experts) >= max_experts:
            break
    return experts
