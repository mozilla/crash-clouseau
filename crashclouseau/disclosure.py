# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Screen recognized bug references before publication.

An anonymous Bugzilla read determines visibility; these reads send no API key.
An authenticated account may have access that anonymous readers lack. IDs omitted
from a successful response, including nonexistent bugs, count as nonpublic.

``screen`` removes list items containing nonpublic references and reports any
remaining references. The write helpers check public comment bodies and the
summary and description of new bugs created without groups."""
import re

from crashclouseau import net
from crashclouseau.logger import logger

_BUG_REF = re.compile(
    r"\bbugs?[ \t]*#?[ \t]*(\d{5,8})\b"
    r"|show_bug\.cgi\?id=(\d{5,8})\b"
    r"|\bbugzil\.la/(\d{5,8})\b"
    r"|bugzilla\.mozilla\.org/(\d{5,8})\b",
    re.I)
_BUG_LIST = re.compile(
    r"\bbugs[ \t]+(\d{5,8}(?:[ \t]*(?:,[ \t]*and|,|/|&|and)[ \t]*\d{5,8})+)", re.I)
_NUMBER = re.compile(r"\d{5,8}")
_HEX = re.compile(r"\b[0-9a-f]{7,40}\b", re.I)
_FENCE = re.compile(r"(```.*?```)", re.S)
_BLANK = re.compile(r"(\n[ \t]*\n)")
_ITEM = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+")
_BATCH = 100
_TIMEOUT = 30


class DisclosureRefused(RuntimeError):
    """A public write names a nonpublic bug or its visibility cannot be checked."""


def bug_refs(text):
    """Extract 5–8 digit IDs from supported ``bug N``, ``bugs N, M`` and URL forms."""
    out = set()
    for m in _BUG_REF.finditer(text or ""):
        out.add(int(next(g for g in m.groups() if g)))
    for m in _BUG_LIST.finditer(text or ""):
        out.update(int(n) for n in _NUMBER.findall(m.group(1)))
    return out


def node_refs(text, bugs_by_node):
    """Resolve 7–40 digit hexadecimal tokens through ``{node: bug}``.

    Either the token or the known node may be a prefix of the other."""
    known = [(str(n).lower(), int(b)) for n, b in (bugs_by_node or {}).items() if n and b]
    if not text or not known:
        return set()
    out = set()
    for m in _HEX.finditer(text):
        token = m.group(0).lower()
        out.update(b for n, b in known if n.startswith(token) or token.startswith(n))
    return out


def refs(text, bugs_by_node=None):
    return bug_refs(text) | node_refs(text, bugs_by_node)


def public_bugs(ids):
    """Return anonymously readable IDs, or ``None`` if a request fails or lacks ``bugs``."""
    ids = sorted({int(i) for i in ids if i})
    if not ids:
        return set()
    from crashclouseau.bugzilla_apply import _bz_rest

    seen = set()
    for i in range(0, len(ids), _BATCH):
        chunk = ids[i:i + _BATCH]
        try:
            r = net.get(_bz_rest(), params={"id": ",".join(str(b) for b in chunk),
                                            "include_fields": "id"}, timeout=_TIMEOUT)
            r.raise_for_status()
            bugs = (r.json() or {}).get("bugs")
        except Exception as exc:                                   # noqa: BLE001
            logger.warning("disclosure: could not read bugs %s anonymously: %s", chunk, exc)
            return None
        if bugs is None:
            logger.warning("disclosure: no bug list in the answer for %s", chunk)
            return None
        seen.update(int(b["id"]) for b in bugs if b.get("id"))
    return seen & set(ids)


def nonpublic(ids):
    """Return IDs absent from the anonymous result, or ``None`` on a failed lookup."""
    ids = {int(i) for i in ids if i}
    public = public_bugs(ids)
    return None if public is None else ids - public


def withdraw(text, hidden, bugs_by_node=None):
    """Return ``(text, withdrawn_ids)`` after removing list items naming hidden bugs.

    Remove an emptied list's lead-in too; preserve triple-backtick fenced blocks."""
    if not text or not hidden:
        return text, set()
    withdrawn = set()
    chunks = []
    for chunk in _FENCE.split(text):
        if chunk.startswith("```"):
            chunks.append(chunk)
            continue
        parts = _BLANK.split(chunk)
        kept = []
        for i in range(0, len(parts), 2):
            par = _without_items(parts[i], hidden, bugs_by_node, withdrawn)
            if par is None:
                continue
            if kept:
                kept.append(parts[i - 1])
            kept.append(par)
        chunks.append("".join(kept))
    if not withdrawn:
        return text, withdrawn
    return "".join(chunks).strip("\n"), withdrawn


def _without_items(par, hidden, bugs_by_node, withdrawn):
    lead, items = [], []
    for line in par.split("\n"):
        if _ITEM.match(line):
            items.append([line])
        elif items:
            items[-1].append(line)
        else:
            lead.append(line)
    kept = []
    for item in items:
        body = "\n".join(item)
        named = refs(body, bugs_by_node) & hidden
        if named:
            withdrawn.update(named)
        else:
            kept.append(body)
    if len(kept) == len(items):
        return par
    if not kept:
        return None
    return "\n".join(lead + kept)


def screen(text, bugs_by_node=None, also=()):
    """Return ``{text, withdrawn, left}``, or ``None`` on a failed visibility lookup.

    Remove list items naming nonpublic bugs. ``left`` contains nonpublic references
    in the remaining text or in ``also``, which represents references outside it."""
    also = {int(b) for b in also if b}
    hidden = nonpublic(refs(text, bugs_by_node) | also)
    if hidden is None:
        return None
    new, withdrawn = withdraw(text, hidden, bugs_by_node)
    left = (refs(new, bugs_by_node) | also) & hidden
    return {"text": new, "withdrawn": sorted(withdrawn), "left": sorted(left)}


def check_public_write(text, bug_id=None):
    """Check recognized bug references before a public write.

    ``bug_id=None`` means a new bug without groups. For comments, a target absent
    from the anonymous result permits nonpublic references. Otherwise raise
    ``DisclosureRefused`` for nonpublic references or a failed visibility lookup.
    Text with no recognized references requires no lookup."""
    named = bug_refs(text)
    if not named:
        return
    target = int(bug_id) if bug_id else None
    public = public_bugs(named | ({target} if target else set()))
    if public is None:
        raise DisclosureRefused("could not check that the bugs this text names are public")
    if target is not None and target not in public:
        return
    hidden = sorted(named - public)
    if hidden:
        logger.warning("disclosure: refusing a public write%s naming bug(s) %s",
                       " on bug {}".format(target) if target else "", hidden)
        raise DisclosureRefused("the text names a bug that is not public")
