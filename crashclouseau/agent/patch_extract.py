# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Patch-extraction foundation (#14).

Fetch a changeset's raw unified diff once and derive, per file/hunk, the
added/deleted text, the ``@@`` enclosing-function context (a per-hunk function
name for free — verified present in mozilla hg ``raw-rev`` git-format diffs), a
touched-identifier set, a cosmetic/whitespace-only flag, cheap regex change-tags,
churn counts, and file metadata (rename/copy/new/deleted/binary/mode-only).

Shared by Patch Scout (#07) and Data-flow Tracer (#08) so neither re-fetches or
re-splits diffs, and usable by the current line-proximity scorer. Pure + cacheable;
no LLM, no searchfox, no new deps. ``fetch_raw_diff`` takes an **hg** node (the DB
stores hg revs); callers holding a git hash convert via ``inspector.git2hg`` first.

NOTE: the small derived-index persistence (``patch_file``/``patch_func`` tables)
that the plan marks optional ("cheap to recompute") is intentionally deferred —
this module recomputes per run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import requests
from libmozdata.hgmozilla import RawRevision

from crashclouseau import config
from crashclouseau.logger import logger

_DEFAULTS = {"diff_byte_cap": 1_000_000, "min_identifier_len": 3, "timeout_secs": 30}


def _cfg(key):
    return config.get_patch_extraction_cfg().get(key, _DEFAULTS[key])


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Hunk:
    old_start: int
    new_start: int
    enclosing_function: str = ""  # raw ``@@ ... @@ <ctx>`` suffix (may be empty)
    added_lines: list = field(default_factory=list)    # [(new_lineno, text)]
    deleted_lines: list = field(default_factory=list)  # [(old_lineno, text)]


@dataclass
class FileDiff:
    filename: str
    old_path: str = ""
    status: str = "modified"  # modified|added|deleted|renamed|copied
    is_binary: bool = False
    mode_only: bool = False
    hunks: list = field(default_factory=list)


@dataclass
class PatchExtraction:
    node: str
    channel: str
    raw_diff: str | None
    files: list

    def is_empty(self):
        return not self.files

    def enclosing_functions(self):
        return enclosing_functions(self.files)

    def touched_identifiers(self, lang=None):
        return touched_identifiers(self.files, lang)

    def change_tags(self):
        return change_tags(self.files)

    def churn(self):
        return churn(self.files)


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #
_RAW_CACHE: dict = {}


def fetch_raw_diff(node, channel):
    """GET the raw unified diff for an hg *node*; truncate to the byte cap; return
    None (logged) on failure so callers degrade rather than raise. Cached per run."""
    key = (channel, node)
    if key in _RAW_CACHE:
        return _RAW_CACHE[key]
    text = None
    try:
        url = "{}/{}".format(RawRevision.get_url(channel), node)
        resp = requests.get(url, timeout=_cfg("timeout_secs"))
        if resp.status_code == 200 and resp.text:
            text = resp.text
            cap = _cfg("diff_byte_cap")
            if cap and len(text) > cap:
                text = text[:cap]
        else:
            logger.warning(
                "patch_extract: raw-rev %s %s -> HTTP %s", channel, node, resp.status_code
            )
    except Exception as exc:
        logger.warning("patch_extract: raw-rev fetch failed for %s: %s", node, exc)
    _RAW_CACHE[key] = text
    return text


# --------------------------------------------------------------------------- #
# Parse
# --------------------------------------------------------------------------- #
_FILE_RE = re.compile(r"^diff --git a/(.*?) b/(.*)$")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(?: (.*))?$")


def parse_hunks(raw_diff_text):
    """Parse a git-format unified diff into per-file/per-hunk structures."""
    files = []
    if not raw_diff_text:
        return files
    lines = raw_diff_text.splitlines()
    i, n = 0, len(lines)
    cur = None
    hunk = None
    old_ln = new_ln = 0

    while i < n:
        line = lines[i]
        m = _FILE_RE.match(line)
        if m:
            cur = FileDiff(filename=m.group(2), old_path=m.group(1), hunks=[])
            files.append(cur)
            hunk = None
            i += 1
            # file header block until first hunk or next file
            while i < n:
                header = lines[i]
                if header.startswith("diff --git ") or header.startswith("@@ "):
                    break
                if header.startswith("new file mode"):
                    cur.status = "added"
                elif header.startswith("deleted file mode"):
                    cur.status = "deleted"
                elif header.startswith("rename from"):
                    cur.status = "renamed"
                elif header.startswith("rename to "):
                    cur.filename = header[len("rename to "):].strip()
                elif header.startswith("copy from"):
                    cur.status = "copied"
                elif header.startswith("copy to "):
                    cur.filename = header[len("copy to "):].strip()
                elif header.startswith("Binary files") or header.startswith("GIT binary patch"):
                    cur.is_binary = True
                elif header.startswith("+++ "):
                    path = header[4:].strip()
                    if path != "/dev/null":
                        cur.filename = path[2:] if path.startswith("b/") else path
                i += 1
            continue

        hm = _HUNK_RE.match(line)
        if hm and cur is not None:
            old_ln = int(hm.group(1))
            new_ln = int(hm.group(2))
            hunk = Hunk(
                old_start=old_ln,
                new_start=new_ln,
                enclosing_function=(hm.group(3) or "").strip(),
            )
            cur.hunks.append(hunk)
            i += 1
            continue

        if hunk is not None:
            if line.startswith("+") and not line.startswith("+++"):
                hunk.added_lines.append((new_ln, line[1:]))
                new_ln += 1
            elif line.startswith("-") and not line.startswith("---"):
                hunk.deleted_lines.append((old_ln, line[1:]))
                old_ln += 1
            elif line.startswith(" "):
                old_ln += 1
                new_ln += 1
            # "\ No newline at end of file" and blank separators: ignore
        i += 1

    for fd in files:
        if not fd.hunks and not fd.is_binary and fd.status == "modified":
            fd.mode_only = True
    return files


# --------------------------------------------------------------------------- #
# Cheap derived signals (no LLM / searchfox)
# --------------------------------------------------------------------------- #
_FUNC_NAME_RE = re.compile(r"([A-Za-z_][\w:~]*)\s*\(")


def _func_name(ctx):
    """Function name out of a ``@@`` context suffix, e.g.
    'NS_IMETHODIMP HTMLEditor::NotifySelectionChanged(...)' -> 'HTMLEditor::NotifySelectionChanged'.
    hg truncates the context to ~40 chars, so the paren is often cut off; fall back to
    the last qualified/long identifier (the truncated name). Empty for non-code
    contexts (e.g. a TOML 'skip-if = [' table header)."""
    if not ctx:
        return ""
    paren = _FUNC_NAME_RE.findall(ctx)
    if paren:
        return paren[0]
    idents = re.findall(r"[A-Za-z_][\w:~]*", ctx)
    if not idents:
        return ""
    cand = idents[-1]
    return cand if ("::" in cand or len(cand) >= 4) else ""


def enclosing_functions(files):
    """Per file, the de-duplicated enclosing-function names touched. The primary,
    symbol-index-free join key to the call-graph neighborhood (#07)."""
    out = {}
    for fd in files:
        names, seen = [], set()
        for h in fd.hunks:
            name = _func_name(h.enclosing_function)
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        out[fd.filename] = names
    return out


_LANG_BY_EXT = {
    "cpp": "cpp", "cc": "cpp", "cxx": "cpp", "h": "cpp", "hpp": "cpp", "c": "cpp",
    "mm": "cpp", "m": "cpp", "rs": "rust", "js": "js", "jsm": "js", "mjs": "js",
    "jsx": "js", "ts": "js", "tsx": "js",
}
_KEYWORDS = {
    "cpp": {"if", "else", "for", "while", "do", "switch", "case", "break", "continue",
            "return", "const", "static", "void", "int", "bool", "char", "class",
            "struct", "enum", "namespace", "template", "typename", "public", "private",
            "protected", "virtual", "override", "new", "delete", "nullptr", "true",
            "false", "auto", "using", "typedef", "sizeof", "this", "operator", "inline",
            "explicit", "friend", "mutable", "volatile", "unsigned", "signed", "long",
            "short", "float", "double", "goto", "default", "try", "catch", "throw"},
    "rust": {"fn", "let", "mut", "if", "else", "match", "for", "while", "loop", "return",
             "struct", "enum", "impl", "trait", "pub", "use", "mod", "self", "super",
             "crate", "where", "move", "ref", "as", "dyn", "async", "await", "unsafe",
             "const", "static", "true", "false", "break", "continue", "type", "in"},
    "js": {"function", "var", "let", "const", "if", "else", "for", "while", "do",
           "switch", "case", "break", "continue", "return", "class", "extends", "new",
           "this", "super", "import", "export", "default", "try", "catch", "finally",
           "throw", "typeof", "instanceof", "true", "false", "null", "undefined",
           "void", "async", "await", "yield", "of", "in"},
}
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")


def lang_for(filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _LANG_BY_EXT.get(ext, "cpp")


def touched_identifiers(files, lang=None):
    """Identifiers in added+deleted text, minus keywords and sub-``min_identifier_len``
    tokens. A cheap candidate pre-filter (#07), not a gate. When *lang* is None it is
    inferred per file from the extension."""
    min_len = _cfg("min_identifier_len")
    out = set()
    for fd in files:
        kw = _KEYWORDS.get(lang or lang_for(fd.filename), set())
        for h in fd.hunks:
            for _, text in h.added_lines + h.deleted_lines:
                for tok in _IDENT_RE.findall(text):
                    if len(tok) >= min_len and tok not in kw:
                        out.add(tok)
    return out


_BRACES = {"{", "}", "};", "});", "),", "{}", "()", ")", "("}


def _norm(text):
    return re.sub(r"\s+", "", text)


def is_cosmetic(hunk):
    """True when the changed lines are a pure reflow/reindent/brace move: the
    whitespace-normalized added and deleted content (minus brace-only tokens) match."""
    add = [x for x in (_norm(t) for _, t in hunk.added_lines) if x and x not in _BRACES]
    dele = [x for x in (_norm(t) for _, t in hunk.deleted_lines) if x and x not in _BRACES]
    return sorted(add) == sorted(dele)


def file_is_cosmetic(file_diff):
    if file_diff.mode_only:
        return True
    if not file_diff.hunks:
        return file_diff.status in ("renamed", "copied")
    return all(is_cosmetic(h) for h in file_diff.hunks)


_TAG_PATTERNS = {
    "free": re.compile(r"\b(free|delete|Release|RefPtr|UniquePtr|Drop|dealloc|reset)\b"),
    "alloc": re.compile(r"\b(malloc|calloc|realloc|MakeUnique|MakeRefPtr|new)\b"),
    "null_check": re.compile(r"if\s*\(\s*!|==\s*nullptr|!=\s*nullptr|\bNS_ENSURE|\bnullptr\b"),
    "deref": re.compile(r"->|\.get\(|operator\*"),
    "bounds": re.compile(r"\[|\blength\b|\bsize\b|\bcount\b|\bindex\b|\bcapacity\b"),
    "lock": re.compile(r"\b(Lock|Mutex|Monitor|AutoLock)\b"),
    "assert": re.compile(
        r"\bMOZ_ASSERT|\bMOZ_DIAGNOSTIC|\bMOZ_CRASH|\bMOZ_RELEASE_ASSERT"
        r"|\bNS_ASSERTION|\bassert\s*\("
    ),
}


def change_tags(files):
    """Cheap regex pre-tags from a closed set. A pre-filter/feature, never a verdict."""
    tags = set()
    for fd in files:
        for h in fd.hunks:
            for _, text in h.added_lines + h.deleted_lines:
                for tag, pat in _TAG_PATTERNS.items():
                    if pat.search(text):
                        tags.add(tag)
    return tags or {"other"}


def churn(files):
    per_file = {}
    total_added = total_deleted = total_hunks = 0
    for fd in files:
        added = sum(len(h.added_lines) for h in fd.hunks)
        deleted = sum(len(h.deleted_lines) for h in fd.hunks)
        per_file[fd.filename] = {
            "added": added, "deleted": deleted, "hunks": len(fd.hunks)
        }
        total_added += added
        total_deleted += deleted
        total_hunks += len(fd.hunks)
    return {
        "files": len(files),
        "added": total_added,
        "deleted": total_deleted,
        "hunks": total_hunks,
        "per_file": per_file,
    }


def extract(node, channel):
    """Top-level: fetch -> parse -> derive. Pure/cacheable; never raises (a failed
    fetch yields a populated-but-text-empty result)."""
    raw = fetch_raw_diff(node, channel)
    return PatchExtraction(
        node=node, channel=channel, raw_diff=raw, files=parse_hunks(raw)
    )
