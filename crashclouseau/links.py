# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Screen Bugzilla text against a host and repository allowlist.

Regexes cover bare http(s), ftp and www URLs and common Markdown link forms, including
code spans and blocks. They do not decode entities or Markdown escapes; write helpers
also check BMO's rendered HTML. Bare email addresses and bare mailto URLs are not screened.
"""

import html
import re
from urllib.parse import unquote, urlparse

from crashclouseau.logger import logger

ALLOWED_HOSTS = frozenset({
    "bugzilla.mozilla.org",
    "crash-stats.mozilla.org",
    "hg.mozilla.org",
    "searchfox.org",
})
# GitHub only for these repositories.
ALLOWED_GITHUB = ("/mozilla-firefox/firefox", "/mozilla/crash-clouseau")
REMOVED = "(link removed)"

# Angle-bracket destinations may contain spaces; plain ones exclude parentheses.
_DEST = r"(?:<(?P<angled>[^<>\n]*)>|(?P<plain>[^()\s<>]+))"
# Single-line reference definitions with optional titles.
_TITLE = r"(?:[ \t]+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^)\n]*\)))?"
_DEFINITION = re.compile(r"^ {0,3}\[[^\]\n]+\]:[ \t]*" + _DEST + _TITLE + r"[ \t]*$\n?", re.M)
# Inline links and images with un-nested labels.
_INLINE = re.compile(
    r"!?\[(?P<label>[^\[\]\n]*)\]\(\s*" + _DEST + r"(?:\s+\"[^\"\n]*\")?\s*\)")
# Fallback for nested labels and single-quoted titles.
_TARGET = re.compile(r"\]\(\s*" + _DEST + r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'))?\s*\)?")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_UNSAFE = re.compile(r"[\s\x00-\x1f\x7f]")
_BARE = re.compile(r"(?<![\w+.-])(?:(?:https?|ftp)://|www\.)[^\s<>\"'`]+", re.I)
_TRAILING = ".,;:!?*_~'\")]"


def allowed(url):
    """Check an HTTP(S) URL against the host and repository allowlist.

    Trim surrounding whitespace, normalize backslashes, and treat ``www.`` as HTTP.
    Reject remaining whitespace, ASCII controls, userinfo, and GitHub paths containing
    ``.`` or ``..`` segments after percent-decoding.
    """
    url = str(url or "").strip()
    if _UNSAFE.search(url):
        return False
    url = url.replace("\\", "/")
    if url.lower().startswith("www."):
        url = "http://" + url
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if parsed.scheme.lower() not in ("http", "https") or "@" in parsed.netloc:
        return False
    if host == "github.com":
        if any(seg in (".", "..") for seg in unquote(parsed.path).split("/")):
            return False
        path = parsed.path.rstrip("/")
        return any(path == p or path.startswith(p + "/") for p in ALLOWED_GITHUB)
    return host in ALLOWED_HOSTS


def _dest(m):
    return m.group("angled") if m.group("angled") is not None else (m.group("plain") or "")


def _leaves(target):
    """Detect a scheme, ``www.`` or ``//`` prefix after normalizing backslashes."""
    t = str(target or "").strip().replace("\\", "/")
    return bool(_SCHEME.match(t)) or t.startswith("//") or t.lower().startswith("www.")


def screen(text):
    """Return screened text and the removed URL strings.

    For disallowed matches, drop reference definitions, keep inline labels, cut fallback
    targets, and replace bare URLs with ``REMOVED``.
    """
    if not text:
        return text, []
    removed = []

    def definition(m):
        dest = _dest(m)
        if not _leaves(dest) or allowed(dest):
            return m.group(0)
        removed.append(dest)
        return ""

    def inline(m):
        dest = _dest(m)
        if not _leaves(dest) or allowed(dest):
            return m.group(0)
        removed.append(dest)
        return m.group("label")

    def target(m):
        dest = _dest(m)
        if not _leaves(dest) or allowed(dest):
            return m.group(0)
        removed.append(dest)
        return "]"

    def bare(m):
        url = m.group(0)
        # Treat trailing punctuation as surrounding text.
        core = url.rstrip(_TRAILING)
        if allowed(core):
            return url
        removed.append(core)
        return REMOVED + url[len(core):]

    text = _DEFINITION.sub(definition, text)
    text = _INLINE.sub(inline, text)
    text = _TARGET.sub(target, text)
    text = _BARE.sub(bare, text)
    return text, removed


def screen_write(text, where):
    """Screen text and log removed hosts (or URL prefixes if no host is available)."""
    out, removed = screen(text)
    if removed:
        hosts = sorted({_host(u) for u in removed})
        logger.warning("links: removed %d link(s) to %s from %s", len(removed),
                       ", ".join(hosts), where)
    return out


def _host(url):
    """Return the host for logging, falling back to the first 40 characters."""
    try:
        return urlparse(url if "//" in url else "http://" + url).hostname or url[:40]
    except ValueError:
        return url[:40]


class LinkRefused(RuntimeError):
    """A link field or rendered comment failed the link check."""


# URL-valued fields: validate rather than replace with prose.
_LINK_FIELDS = ("url", "see_also")
# Preserve crash signatures for signature matching.
_VERBATIM = ("cf_crash_signature",)


def screen_fields(fields, where):
    """Screen strings in a create/update payload's nested dictionaries and lists.

    Preserve ``cf_crash_signature``. Validate ``url`` and ``see_also`` separately, skipping
    empty values and ``remove`` entries. Other strings go through ``screen_write``.
    """
    out = {}
    for key, value in (fields or {}).items():
        if key in _VERBATIM:
            out[key] = value
        elif key in _LINK_FIELDS:
            _check_link_field(key, value, where)
            out[key] = value
        else:
            out[key] = _screen_value(value, "{} ({})".format(where, key))
    return out


def _screen_value(value, where):
    if isinstance(value, str):
        return screen_write(value, where)
    if isinstance(value, dict):
        return {k: _screen_value(v, where) for k, v in value.items()}
    if isinstance(value, list):
        return [_screen_value(v, where) for v in value]
    return value


def _check_link_field(key, value, where):
    values = [v for k, v in value.items() if k != "remove"] if isinstance(value, dict) else [value]
    urls = [u for v in values for u in (v if isinstance(v, list) else [v])]
    if any(u and not (isinstance(u, str) and allowed(u)) for u in urls):
        logger.warning("links: refusing %s, which sets %s outside the allowlist", where, key)
        raise LinkRefused("{} sets {} to a link outside the allowlist".format(where, key))


_HREF = re.compile(r"""\b(?:href|src)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)


def offsite(rendered):
    """List disallowed quoted ``href``/``src`` targets recognized by ``_leaves``, except mailto."""
    out = []
    for double, single in _HREF.findall(rendered or ""):
        url = html.unescape(double or single).strip()
        if url.lower().startswith("mailto:"):
            continue
        if _leaves(url) and not allowed(url):
            out.append(url)
    return out
