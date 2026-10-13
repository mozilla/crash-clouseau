# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Apply Firefox's ``.mailmap`` to commit authors.

Cache entries per process for 24 hours. Failed fetches keep the cache and
retry on the next lookup after one hour.
"""

import re
import threading
import time

from crashclouseau import net
from crashclouseau.logger import logger

URL = "https://raw.githubusercontent.com/mozilla-firefox/firefox/main/.mailmap"
_TTL = 24 * 3600
_RETRY = 3600
# Optional names and a second address; trailing comments are ignored.
_ENTRY = re.compile(r"^([^<]*)<([^<>]*)>(?:([^<]*)<([^<>]*)>)?")
# Match one complete author identity.
_USER = re.compile(r"^([^<>]*?)\s*<([^<>\s]+@[^<>\s]+)>$")

_lock = threading.Lock()
_cache = {"entries": {}, "until": 0.0}


def parse(text):
    """Parse entries as ``{(old_email, old_name): (new_name, new_email)}``.

    Keys are lowercase; ``None`` means any old name or an unchanged output field.
    Repeated entries retain fields omitted by later lines."""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        m = _ENTRY.match(line) if not line.startswith("#") else None
        if not m:
            continue
        name, email, old_name, old_email = (g.strip() if g else g for g in m.groups())
        if old_email is None:
            # `Name <commit email>` replaces the name only.
            old_email, old_name, email = email, "", None
        if not old_email:
            continue
        key = (old_email.lower(), old_name.lower() or None)
        prev = out.get(key, (None, None))
        out[key] = (name or prev[0], email or prev[1])
    return out


def _entries():
    with _lock:
        now = time.monotonic()
        if now >= _cache["until"]:
            try:
                r = net.get(URL, timeout=net.SERVICE_TIMEOUT)
                r.raise_for_status()
                _cache["entries"] = parse(r.text)
                _cache["until"] = now + _TTL
            except Exception as exc:
                logger.warning("mailmap: cannot read %s, keeping %d entries: %s",
                               URL, len(_cache["entries"]), exc)
                _cache["until"] = now + _RETRY
        return _cache["entries"]


def canonical(name, email):
    """Map ``(name, email)``; preserve fields with no replacement."""
    name, email = (name or "").strip(), (email or "").strip()
    if not email:
        return name, email
    entries = _entries()
    key = email.lower()
    new = entries.get((key, name.lower() or None)) or entries.get((key, None))
    if not new:
        return name, email
    return new[0] or name, new[1] or email


def aliases(address):
    """Return ``address`` and commit addresses that may map to it, all lowercase.

    Check each identity with ``canonical`` to apply name-specific overrides."""
    address = (address or "").strip().lower()
    if not address:
        return set()
    return {address} | {old for (old, _), (_, new) in _entries().items()
                        if (new or old).lower() == address}


def author(text, email=""):
    """Map ``Name <email>``, a bare name or a bare email, preserving its form.

    ``email`` supplies the address for a bare name."""
    text = (text or "").strip()
    m = _USER.match(text)
    if m:
        name, mail = canonical(m.group(1), m.group(2))
        return "{} <{}>".format(name, mail) if name else "<{}>".format(mail)
    if "@" in text and not any(c in text for c in " <>"):
        return canonical("", text)[1]
    if text and email:
        return canonical(text, email)[0]
    return text
