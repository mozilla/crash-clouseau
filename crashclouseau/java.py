# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""JVM (Java/Kotlin) crash stacks: from Socorro's report to scored frames.

Fenix nightly is the only JVM product ingested and its APKs are R8-minified, so the line
numbers Socorro reports are the REMAPPED ones: at the build revision of crash 3c426d92
(2026-09-11) ``Keystore.generateKey(Keystore.kt:269)`` points inside ``encryptBytes`` while
``fun generateKey`` is line 221, and ``Keystore.<init>(Keystore.kt:51)`` is a blank KDoc line
inside another class. The FILE and the METHOD are right, the LINE is not, and the mapping
drifts per build (plan 16 measured 269 = a KDoc ``*/`` at dc7f12a8cbce). Under
``config.java_trust_line_numbers()`` false every frame carries ``line_trusted: False``, the
scorer falls back to file/method matching (``models.Changeset._fuzzy_score``) and the method
span is located HERE, in the source at the build revision (``method_span``).

FRAME ORDER. Socorro's structured ``java_exception.exception.values`` lists the causes ROOT
FIRST and the outer exception LAST: on 3c426d92 ``values[0]`` is the 23-frame
``KeyStoreException`` the platform threw and ``values[1]`` the 20-frame ``ProviderException``
whose frames are exactly the ``java_stack_trace`` text and whose type + top frame is the
Socorro signature. We store the OUTER exception's frames first (stackpos 0..) because that is
the stack the signature, the text and the bug comment show, then each cause block in order
``values[-2], ..., values[0]`` (root cause last), skipping a frame identical in (module,
function, filename, lineno) to one already kept -- a cause shares its tail with the exception
that wrapped it, so on 3c426d92 the 23-frame cause contributes 4 new frames (its own platform
frames plus ``engineGenerateKey`` at a different line), not 23.
"""

import bisect
import html
import os
import re
import time

from libmozdata.hgmozilla import Mercurial
from libmozdata.lando import LandoCommitMapAPI, LandoMissingCommit

from . import config, hgedge, models, net, tools
from .logger import logger


# must match 'at android.os.Parcel.readException(Parcel.java:1552)', and -- the line number
# being optional -- 'at a.B.c(Native Method)', 'at a.B.c(Unknown Source:12)' and the
# line-less '(Foo.java)' Socorro writes in a signature.
JAVA_PAT1 = re.compile(r"^at\ ([^\(]+)\(([^:)]*)(?::([0-9]*))?\)$")
# must match $123 in MyClass$123 or MyClass$Inner
JAVA_PAT2 = re.compile(r"\$.*")
JAVA_PAT3 = re.compile(r"\([^:)]+(?::[0-9]*)?\)$")

# The JVM sources of the tree, as the GitHub trees API lists them: paths RELATIVE to
# ``mobile/android`` (``android-components/components/lib/dataprotect/src/main/java/...``).
JVM_ROOT = "mobile/android/"
JVM_EXTS = (".kt", ".java")
# One request for the whole subtree at the build's GIT commit -- verified 2026-09-15: 200,
# 24,510 entries, ``truncated: false``, 5,859 .kt + 235 .java, 7 MB. Pinned to the sha (not
# ``main``) so the index matches the build the frames come from; a file added since is
# created by the pushlog anyway (``.kt`` is an interesting extension).
GITHUB_TREE_URL = (
    "https://api.github.com/repos/mozilla-firefox/firefox/git/trees/{}:mobile/android"
    "?recursive=1"
)
# ``File.get_ids`` is one SELECT + one multi-row INSERT per call; 500 keeps the ``IN`` list
# well under Postgres' parameter limit and the whole 6,100-path refresh at ~13 round trips.
FILE_INDEX_CHUNK = 500
# How many distinct source files one stack may read from hg-edge to locate its methods: the
# example stack resolves 5 files; a pathological 50-frame stack must not turn into 50 reads.
MAX_SOURCE_READS = 8
# ... and how long, wall clock, before the remaining files are left unread. The reads run on
# the ONE serial scoring chain shared with desktop and, for a trigger, on the web dyno's single
# gunicorn worker under the router's 30 s limit: measured 4 files = 23.7 s on 3c426d92 with
# hgedge's agent-sized 5 tries / 60 s, so each read gets ONE attempt with a (connect, read)
# timeout of its own, the read half shrinking to what is left of the budget.
MAX_SOURCE_SECONDS = 20
SOURCE_TIMEOUT = (5, 15)
# Socorro caps `java_exception` at 50 frames and `inspector.inspect_stacktrace` clamps a native
# stack the same; the `java_stack_trace` TEXT is not capped (a release StackOverflowError
# carries 339 `at` lines) and nothing below the 50th frame is scored or shown.
MAX_FRAMES = 50
# `models.SweepMark` name (prefix) under which `refresh_file_index` records the `builds.id` it
# last indexed: one mark per (product, channel), because the mark never moves backwards and a
# second TC product's newest build would otherwise chase the first's id every tick.
FILE_INDEX_MARK = "jvm_file_index"

# What R8 / the JVM write where a real filename should be. ``R8$$SyntheticClass`` is the
# ``$$ExternalSyntheticLambdaN`` classes (4 of the 20 frames of 3c426d92), ``r8-map-id-*``
# the newer R8 marker, ``SourceFile`` ProGuard's, and the JVM's two for JNI / no debug info.
_SYNTHETIC_FILENAMES = ("R8$$SyntheticClass", "SourceFile", "Native Method", "Unknown Source")
# Methods the compiler minted: a Kotlin lambda's ``invoke``/``invokeSuspend``, Java's
# ``lambda$foo$0`` and ``access$100`` bridges, the static initializer. Their "span" is not a
# method the source declares, so `method_span` refuses them rather than match the wrong thing.
_SYNTHETIC_METHODS = frozenset({"invoke", "invokeSuspend", "<clinit>"})
_SYNTHETIC_METHOD_PREFIXES = ("lambda$", "access$")

_LANDO = None


def _is_synthetic_filename(filename):
    if not filename or filename in _SYNTHETIC_FILENAMES:
        return True
    return filename.startswith("r8-map-id-") or not filename.endswith(JVM_EXTS)


def _split_frame_path(path):
    """``pkg.Class$Inner.method`` -> (``pkg.Class$Inner``, ``method``). A method name never
    contains a dot (``<init>`` included), so the last one is the split."""
    module, _, function = path.rpartition(".")
    return module, function


def _pkg_and_class(module):
    """``mozilla.components.lib.dataprotect.SecurePreferencesImpl23$$ExternalSyntheticLambda0``
    -> (``mozilla/components/lib/dataprotect``, ``SecurePreferencesImpl23``): the package as
    a path and the OUTER class (everything from the first ``$`` dropped: inner, anonymous,
    lambda and R8-synthetic classes all live in the outer class's file)."""
    parts = module.split(".")
    pkgpath = "/".join(JAVA_PAT2.sub("", p) for p in parts[:-1])
    return pkgpath, JAVA_PAT2.sub("", parts[-1])


def _frame(module, function, line, original, stackpos, node, trusted):
    internal = config.is_java_package(module)
    return {
        "original": original,
        "filename": "",
        "module": module,
        "changesets": [],
        "function": function,
        "node": node if internal else "",
        "line": line,
        "internal": internal,
        "stackpos": stackpos,
        "line_trusted": trusted,
    }


def _int_line(value):
    """The reported line as an int, 0 when absent. NEVER -1: ``inspector.get_simplified_hash``
    skips ``line == -1`` frames (a native frame with no line), and a JVM stack encoded that
    way would hash to "" and dedup against every other empty hash."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _frames_from_exception(java_exception, node, trusted):
    """Frames + their raw filenames from Socorro's structured ``java_exception`` in the order
    the module docstring describes (outer exception first, then the causes, deduplicated),
    the merged list capped at ``MAX_FRAMES``."""
    values = ((java_exception or {}).get("exception") or {}).get("values") or []
    frames, raw = [], []
    seen = set()
    for i, value in enumerate(reversed(values)):
        for f in ((value or {}).get("stacktrace") or {}).get("frames") or []:
            if len(frames) >= MAX_FRAMES:
                return frames, raw
            module = f.get("module") or ""
            function = f.get("function") or ""
            filename = f.get("filename") or ""
            lineno = f.get("lineno")
            key = (module, function, filename, lineno)
            # The outer exception (i == 0) is kept VERBATIM -- its stackpos must mirror the
            # text, and a frame repeated at different depths (`SynchronizedLazyImpl.getValue`
            # three times on 3c426d92) is real recursion, not duplication. Only a cause's
            # frames are deduplicated, against everything already kept.
            if i and key in seen:
                continue
            seen.add(key)
            original = "at {}.{}({}{})".format(
                module, function, filename, ":{}".format(lineno) if lineno is not None else ""
            )
            frames.append(
                _frame(module, function, _int_line(lineno), original, len(frames), node, trusted)
            )
            raw.append(filename)
    return frames, raw


def _frames_from_text(st, node, trusted):
    """The text fallback: the first ``MAX_FRAMES`` ``at ...`` lines of ``java_stack_trace``
    (the OUTER exception only -- Socorro's text has no ``Caused by:`` block)."""
    frames, raw = [], []
    lines = (x.strip() for x in st.split("\n"))
    for line in (x for x in lines if x.startswith("at ")):
        if len(frames) >= MAX_FRAMES:
            break
        m = JAVA_PAT1.match(line)
        module = function = filename = ""
        lineno = 0
        if m:
            path, filename, lineno = m.groups()
            module, function = _split_frame_path(path)
            lineno = _int_line(lineno)
            if filename in ("Native Method", "Unknown Source"):
                filename = ""
        frames.append(_frame(module, function, lineno, line, len(frames), node, trusted))
        raw.append(filename)
    return frames, raw


def _resolve_files(frames, raw, get_full_path):
    """Give each INTERNAL frame its in-tree path (D9) and return ``(files, resolved)``: the
    filenames to look up in the changesets and the subset that really resolved.

    Two passes because a synthetic frame borrows from a sibling that may sit BELOW it: on
    3c426d92 ``SecurePreferencesImpl23$$ExternalSyntheticLambda0.invoke(R8$$SyntheticClass)``
    is frame 4 and the ``SecurePreferencesImpl23.getString(SecureAbove22Preferences.kt)`` it
    borrows from is frame 6 -- and the class is NOT the file there, so guessing
    ``SecurePreferencesImpl23.kt`` would have missed. ``IconButtonKt$$ExternalSyntheticLambda3``
    has no sibling: ``FooKt`` is the JVM facade of the top-level functions of ``Foo.kt``.

    A resolution is accepted only when ``get_full_path`` returned something OTHER than its
    input (it hands the input back on a miss -- callers rely on that contract) that looks
    like a path. An explicit ``File.kt`` that misses keeps its package-relative candidate as
    the filename, as it always did: human-readable, and harmless to ``Changeset.find`` (exact
    match on in-tree names) -- but it is neither borrowed from nor read from hg-edge."""
    files, resolved = set(), set()
    borrow = {}
    pending = []
    for d, filename in zip(frames, raw):
        if not d["internal"]:
            continue
        pkgpath, outer = _pkg_and_class(d["module"])
        if _is_synthetic_filename(filename):
            pending.append((d, pkgpath, outer))
            continue
        cand = pkgpath + "/" + filename if pkgpath else filename
        full = get_full_path(cand) or cand
        d["filename"] = full
        files.add(full)
        if full != cand and "/" in full:
            resolved.add(full)
            borrow.setdefault((pkgpath, outer), full)
    for d, pkgpath, outer in pending:
        full = borrow.get((pkgpath, outer))
        if not full:
            if outer.endswith("Kt") and len(outer) > 2:
                names = (outer[:-2] + ".kt",)
            else:
                names = (outer + ".kt", outer + ".java")
            for name in names:
                cand = pkgpath + "/" + name if pkgpath else name
                got = get_full_path(cand)
                if got and got != cand and "/" in got:
                    full = got
                    break
        if full:
            d["filename"] = full
            files.add(full)
            resolved.add(full)
            borrow.setdefault((pkgpath, outer), full)
    return files, resolved


def _locate_methods(frames, resolved, node, channel):
    """Stamp ``method_lines`` on the internal frames whose method ``method_span`` finds in the
    source at the build revision. One hg-edge read per (file, node), at most
    ``MAX_SOURCE_READS`` files and ``MAX_SOURCE_SECONDS`` of wall clock per stack; a file that
    cannot be read -- or is not read because the budget is spent -- costs its frames the
    method rung (a scoring refinement), never the run. Logged, because a silent miss here
    looks exactly like a changeset that touched the file but not the method.

    ``retries=1`` and a short timeout because this is NOT the agent's leisurely off-stack
    read: one 5xx blip under hgedge's defaults is ~17 s per file (5 tries, 1+2+4+8 s backoff)
    and a hang a minute, on a chain where every second delays desktop's scoring too."""
    if not node or not resolved:
        return
    started = time.monotonic()
    sources = {}
    over_cap, out_of_time = set(), set()
    located = wanted = 0
    for d in frames:
        filename = d["filename"]
        if not d["internal"] or filename not in resolved:
            continue
        if d["function"] in _SYNTHETIC_METHODS or d["function"].startswith(
            _SYNTHETIC_METHOD_PREFIXES
        ):
            continue
        wanted += 1
        if filename not in sources:
            if len(sources) >= MAX_SOURCE_READS:
                over_cap.add(filename)
                continue
            remaining = MAX_SOURCE_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                out_of_time.add(filename)
                continue
            connect, read = SOURCE_TIMEOUT
            sources[filename] = hgedge.raw_file(
                filename, node, channel, retries=1, timeout=(connect, min(read, remaining))
            )
            if sources[filename] is None:
                logger.info("java: no source for %s at %s on %s", filename, node, channel)
        source = sources[filename]
        if not source:
            continue
        span = method_span(source, d["function"], d["module"].rsplit(".", 1)[-1])
        if span:
            d["method_lines"] = span
            located += 1
    if out_of_time:
        logger.warning(
            "java: %d source files not read at %s: the %d s budget was spent after %d",
            len(out_of_time), node, MAX_SOURCE_SECONDS, len(sources),
        )
    if wanted:
        logger.info(
            "java: located %d of %d method spans at %s (%d files read, %d over cap, %.1f s)",
            located, wanted, node, len(sources), len(over_cap), time.monotonic() - started,
        )


def inspect_java_stacktrace(
    st, node, get_full_path=None, java_exception=None, channel="nightly", locate_methods=True
):
    """``(frames, files)`` for a JVM crash: the frame dicts ``inspector.inspect_stacktrace``
    produces for a native stack (``original, filename, changesets, module, function, line,
    node, internal, stackpos``) plus ``line_trusted`` (``config.java_trust_line_numbers()``)
    and, for an internal frame whose method was found in the source, ``method_lines``
    ``(first, last)``; ``files`` are the filenames to look up in the changesets.

    The structured ``java_exception`` is preferred (it separates module / function / file /
    line and carries the causes the text omits); the ``java_stack_trace`` text is the
    fallback. ``module`` is the fully-qualified class, ``internal`` is
    ``config.is_java_package(module)``, ``node`` the build node on internal frames.
    ``get_full_path`` defaults to ``models.File.get_full_path`` at CALL time (tests inject a
    stub or patch the model). ``locate_methods`` False skips the hg-edge source reads behind
    ``method_lines`` altogether (``_locate_methods``): the method rung is a refinement of the
    score, and a caller answering a web request cannot afford up to 20 s of it."""
    if get_full_path is None:
        get_full_path = models.File.get_full_path
    trusted = config.java_trust_line_numbers()
    frames, raw = _frames_from_exception(java_exception, node, trusted)
    if not frames:
        if not st:
            return [], set()
        frames, raw = _frames_from_text(st, node, trusted)
    files, resolved = _resolve_files(frames, raw, get_full_path)
    if locate_methods and not trusted:
        _locate_methods(frames, resolved, node, channel)
    return frames, files


# ---------------------------------------------------------------------------------------
# Method spans in the source at the build revision


def _skip_literal(text, i):
    """Index just past the string / char literal or comment starting at ``text[i]``, or
    ``i`` when nothing starts there. Braces inside ``"${...}"``, ``'{'`` and KDoc must not
    count as code."""
    n = len(text)
    c = text[i]
    if text.startswith('"""', i):
        j = text.find('"""', i + 3)
        return n if j < 0 else j + 3
    if c in "\"'":
        j = i + 1
        while j < n and text[j] != c:
            j += 2 if text[j] == "\\" else 1
        return min(j + 1, n)
    if text.startswith("//", i):
        j = text.find("\n", i)
        return n if j < 0 else j
    if text.startswith("/*", i):
        j = text.find("*/", i + 2)
        return n if j < 0 else j + 2
    return i


def _mask_literals(text):
    """``text`` with every character of a string / char literal or comment turned into a
    space -- newlines kept, so every offset and line number is the source's. The declaration
    patterns below then cannot match prose or a quoted example: a ``// legacy: fun foo() {
    bar() }`` comment produced a span for a method that no longer exists, and made the name
    AMBIGUOUS (``_pick`` -> None) when the real ``fun foo`` was still declared."""
    out = []
    i = start = 0
    n = len(text)
    while i < n:
        j = _skip_literal(text, i)
        if j == i:
            i += 1
            continue
        out.append(text[start:i])
        out.append(re.sub(r"[^\n]", " ", text[i:j]))
        start = i = j
    out.append(text[start:])
    return "".join(out)


def _skip_balanced(text, i, open_c, close_c):
    """``text[i]`` is ``open_c``; the index just past its matching ``close_c``, or -1 when
    the text ends first. Literals and comments are skipped, nothing else is interpreted."""
    n = len(text)
    depth = 0
    while i < n:
        j = _skip_literal(text, i)
        if j != i:
            i = j
            continue
        c = text[i]
        if c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


# What a line may start with when it CONTINUES the previous statement of an expression body
# (Kotlin's safe-call / elvis / boolean chains and the branches of an `if`/`try` expression).
# Not a closing bracket: an open one keeps the depth above 0 until it closes, and a `}` at
# depth 0 is the enclosing class's. The keywords are WHOLE words: a prefix test took
# `elsewhere()` on the next line for an `else` branch and stretched `fun a() = 1` over it.
_CONTINUATION = re.compile(r"\?[.:]|\.|&&|\|\||\b(?:else|catch|finally)\b")


def _statement_end(text, i):
    """Index of the last character of the statement that starts at ``text[i]`` (an
    expression body): it ends at the first newline where every bracket is closed and the
    next line is not a continuation."""
    n = len(text)
    while i < n and text[i] in " \t\r\n":
        i += 1
    depth = 0
    last = i
    while i < n:
        j = _skip_literal(text, i)
        if j != i:
            last = j - 1
            i = j
            continue
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "\n" and depth <= 0:
            k = i + 1
            while k < n and text[k] in " \t\r\n":
                k += 1
            if k >= n or not _CONTINUATION.match(text, k):
                return last
        if c not in " \t\r\n":
            last = i
        i += 1
    return last


def _lineno(line_starts, idx):
    return bisect.bisect_right(line_starts, idx)


def _body_span(text, sig_start, paren_open, line_starts):
    """``(first, last)`` lines of a declaration whose parameter list opens at ``paren_open``:
    a ``{`` body by brace matching, a Kotlin ``=`` body as the statement's line range, None
    for an abstract / interface declaration (no body to crash in)."""
    n = len(text)
    i = _skip_balanced(text, paren_open, "(", ")")
    if i < 0:
        return None
    first = _lineno(line_starts, sig_start)
    while i < n:
        c = text[i]
        if c == "{":
            j = _skip_balanced(text, i, "{", "}")
            return (first, _lineno(line_starts, (n if j < 0 else j) - 1))
        if c == "=":
            nxt = text[i + 1] if i + 1 < n else ""
            prev = text[i - 1] if i else ""
            if nxt in ("=", ">") or prev in ("!", "<", ">"):
                # `==` / `!=` / `<=` / `>=` / `=>` compare, they open no expression body:
                # Java's `assert foo(1) == 2;` (`assert` reads as `foo`'s return type) made
                # the `=` of `==` the body of a `foo` that is only called there.
                i += 2 if nxt in ("=", ">") else 1
                continue
            return (first, _lineno(line_starts, _statement_end(text, i + 1)))
        if c == ";":
            return None
        if c in "<(":
            j = _skip_balanced(text, i, c, ">" if c == "<" else ")")
            if j < 0:
                return None
            i = j
            continue
        if c == "\n":
            # A Kotlin declaration may end here (abstract / expect) -- unless the next
            # non-blank text still belongs to the signature.
            k = i + 1
            while k < n and text[k] in " \t\r\n":
                k += 1
            if k < n and (text[k] in "{=:" or text.startswith(("throws", "where"), k)):
                i = k
                continue
            return None
        i += 1
    return None


_CLASS_DECL = r"\b(?:class|object|interface|enum)\s+{}\b"
_NAMED = re.compile(r"^[A-Za-z_]\w*$")


def _class_span(text, class_name, line_starts):
    """The body span of the class named by ``class_name`` (``Outer$Inner`` tries ``Inner``
    then ``Outer``; ``Foo$1`` / ``Foo$start$2`` fall back to ``Foo``, whose body contains the
    anonymous / lambda class). A Kotlin class with no body (``class Foo(val x: Int)``) spans
    its header. Exactly one declaration of that name, or None."""
    parts = [p for p in class_name.split("$") if _NAMED.match(p)]
    for name in reversed(parts):
        matches = list(re.finditer(_CLASS_DECL.format(re.escape(name)), text))
        if len(matches) != 1:
            if matches:
                return None
            continue
        m = matches[0]
        first = _lineno(line_starts, m.start())
        i, n = m.end(), len(text)
        while i < n:
            c = text[i]
            if c == "{":
                j = _skip_balanced(text, i, "{", "}")
                return (first, _lineno(line_starts, (n if j < 0 else j) - 1))
            if c in "<(":
                j = _skip_balanced(text, i, c, ">" if c == "<" else ")")
                if j < 0:
                    return None
                i = j
                continue
            if c == "\n":
                k = i + 1
                while k < n and text[k] in " \t\r\n":
                    k += 1
                if k < n and (text[k] in "{:," or text.startswith("where", k)):
                    i = k
                    continue
                return (first, _lineno(line_starts, i - 1))
            i += 1
        return (first, _lineno(line_starts, n - 1))
    return None


def _kotlin_fun_re(name):
    # `fun name(`, with an optional type-parameter list and receiver: `fun <T> Foo.name(`.
    return re.compile(r"\bfun\b(?:\s*<[^>]*>)?\s*(?:[\w.]+\.)?" + re.escape(name) + r"\s*\(")


def _java_method_re(name):
    # `name(` at DECLARATION position: modifiers, an optional type-parameter list, then a
    # return type before the name -- which a call (`name(...)`, `x = name(`, `return name(`)
    # never has.
    return re.compile(
        r"^[ \t]*(?:(?:public|private|protected|static|final|synchronized|abstract|native"
        r"|default|strictfp)\s+)*(?:<[^>\n]*>\s*)?(?!return\b|new\b|throw\b|else\b)"
        r"[\w.$]+(?:\s*<[^>\n]*>)?(?:\s*\[\s*\])*\s+" + re.escape(name) + r"\s*\(",
        re.M,
    )


def _brace_depth(text, pos):
    """How many ``{`` are open at ``pos`` (literals and comments skipped)."""
    depth = i = 0
    while i < pos:
        j = _skip_literal(text, i)
        if j != i:
            i = j
            continue
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return depth


# A JVM accessor the Kotlin compiler minted for a property: `getAccount` / `setAccount` for
# `val account`, `isReady` for `val isReady`.
_ACCESSOR = re.compile(r"^(get|set|is)([A-Z]\w*)$")


def _property_candidates(text, function, cls, line_starts):
    """The spans of the ``val``/``var`` a JVM accessor name stands for, at MEMBER depth only
    (one brace inside the frame's class, or at most one deep without a class): on
    3c426d92 ``FxaAccountManager.getAccount`` is ``private val account by lazy { ... }`` at
    line 116, and the local ``val account = authenticatedAccount()`` at 258 is not it."""
    acc = _ACCESSOR.match(function)
    if not acc:
        return []
    kind, rest = acc.groups()
    prop = function if kind == "is" else rest[0].lower() + rest[1:]
    want = _brace_depth(text, line_starts[cls[0] - 1]) + 1 if cls else None
    out = set()
    for m in re.finditer(r"\b(?:val|var)\s+" + re.escape(prop) + r"\b", text):
        depth = _brace_depth(text, m.start())
        if depth != want if want is not None else depth > 1:
            continue
        end = _statement_end(text, m.end())
        out.add((_lineno(line_starts, m.start()), _lineno(line_starts, end)))
    return sorted(out)


def _pick(candidates, cls):
    """Exactly one candidate -- inside the frame's class when that is known -- or None."""
    if not candidates:
        return None
    if cls:
        inside = [s for s in candidates if cls[0] <= s[0] <= cls[1]]
        if len(inside) == 1:
            return inside[0]
        if inside:
            return None
    return candidates[0] if len(candidates) == 1 else None


def method_span(source_text, function, class_name=""):
    """``(first_line, last_line)`` of ``function``'s declaration in ``source_text`` (1-based,
    inclusive), or None when it is not there, is ambiguous, or is compiler-minted.

    Kotlin ``fun name(`` (generics and receivers allowed) and Java ``Type name(`` at
    declaration position; the body is brace-matched from the first ``{`` after the
    signature, an expression-bodied Kotlin ``fun x() = ...`` spans its statement. ``<init>``
    is the class body -- the constructor, the ``init`` blocks and the property initialisers
    all live there. A ``getX``/``setX``/``isX`` with no ``fun`` is a Kotlin property's JVM
    accessor and spans the ``val``/``var`` declaration. ``class_name`` narrows an overloaded
    / re-implemented name to the class the frame names: ``getString`` is declared four times
    in ``SecureAbove22Preferences.kt`` and only ``SecurePreferencesImpl23``'s is the frame's.
    Verified against the sources at 8590488daa5e: ``Keystore.generateKey`` -> (221, 233),
    ``Keystore.<init>`` -> (192, 345), ``Keystore.available`` (expression-bodied) ->
    (212, 212), ``FxaAccountManager.getAccount`` -> (116, 116)."""
    if not source_text or not function:
        return None
    if function in _SYNTHETIC_METHODS or function.startswith(_SYNTHETIC_METHOD_PREFIXES):
        return None
    # Everything below reads the MASKED text (comments and literals blanked, same offsets):
    # the scanners' own `_skip_literal` calls are then no-ops, and no pattern can match
    # inside a comment or a string.
    text = _mask_literals(source_text)
    line_starts = [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]
    cls = _class_span(text, class_name, line_starts) if class_name else None
    if function == "<init>":
        return cls

    # By span VALUE: the Java pattern also matches a Kotlin `fun` line from column 0 (`fun`
    # reads as its return type), so one declaration can be found twice.
    spans = set()
    for pat in (_kotlin_fun_re(function), _java_method_re(function)):
        for m in pat.finditer(text):
            span = _body_span(text, m.start(), m.end() - 1, line_starts)
            if span:
                spans.add(span)
    candidates = sorted(spans)
    if not candidates:
        candidates = _property_candidates(text, function, cls, line_starts)
    return _pick(candidates, cls)


# ---------------------------------------------------------------------------------------
# The JVM file index


def _full_hg_node(node, channel):
    if node and len(node) >= 40:
        return node
    url = "{}/json-rev/{}".format(hgedge._edge_base(channel), node)
    r = net.get(url)
    if r.status_code != 200:
        logger.warning("java: json-rev %s on %s -> %d", node, channel, r.status_code)
        return None
    return (r.json() or {}).get("node")


def _git_sha(node, channel):
    """The GIT commit of an hg node via Lando. Lando answers ``hg2git`` for a FULL 40-char
    hash only (verified 2026-09-15: ``8590488daa5e`` -> ``LandoMissingCommit``, the 40-char
    form -> ``88fa72d2f463...``), and the builds table stores 12, so hg-edge ``json-rev``
    expands it first."""
    global _LANDO
    full = _full_hg_node(node, channel)
    if not full:
        return None
    if _LANDO is None:
        _LANDO = LandoCommitMapAPI()
    try:
        return _LANDO.hg2git(full).git_hash
    except LandoMissingCommit:
        logger.warning("java: lando has no git commit for %s", full)
        return None


def _jvm_paths(tree):
    return [
        JVM_ROOT + e["path"]
        for e in tree
        if e.get("type") == "blob" and (e.get("path") or "").endswith(JVM_EXTS)
    ]


def _add_missing_files(paths):
    """Insert the paths the ``files`` table lacks, in chunks; how many were new. Through
    ``File.get_ids`` (insert-if-absent) and NOT ``File.populate(check=False)``, which adds
    blindly and raises on the unique name the second time it runs."""
    added = 0
    for start in range(0, len(paths), FILE_INDEX_CHUNK):
        chunk = paths[start:start + FILE_INDEX_CHUNK]
        rows = models.db.session.query(models.File.name).filter(models.File.name.in_(chunk))
        existing = {r[0] for r in rows}
        missing = [p for p in chunk if p not in existing]
        if missing:
            models.File.get_ids(missing)
            added += len(missing)
    return added


def _github_headers():
    """``Authorization`` for the GitHub API when ``GITHUB_TOKEN`` is set (5,000 requests an
    hour per token). Unauthenticated, GitHub allows 60 an hour PER ORIGINATING IP, and Heroku
    dynos share egress IPs, so that budget may already be spent by a neighbour. Read at call
    time: setting the config var must not need a code reload."""
    token = os.getenv("GITHUB_TOKEN")
    return {"Authorization": "Bearer " + token} if token else {}


def _index_mark(channel, product):
    return "{}:{}/{}".format(FILE_INDEX_MARK, product, channel)


def _build_row_id(bid, channel, product):
    """``builds.id`` of the (``bid``, ``product``, ``channel``) row, or None."""
    row = (
        models.db.session.query(models.Build.id)
        .filter(models.Build.buildid == bid, models.Build.product == product,
                models.Build.channel == channel)
        .first()
    )
    return row[0] if row else None


def refresh_file_index(channel, product):
    """Index the tree's ``mobile/android/**.{kt,java}`` at the newest build of
    (``product``, ``channel``) into ``models.File`` -- the rows ``File.get_full_path`` needs
    to turn ``mozilla/components/lib/dataprotect/Keystore.kt`` into its in-tree path -- and
    return how many paths were new. Never raises: an index miss costs a frame its path, not
    the tick.

    IDEMPOTENT PER BUILD, so ``update.update_builds`` may call it every tick: the
    ``builds.id`` of the last build whose tree was indexed is kept in ``models.SweepMark``
    (``_index_mark``), and when the newest build is that one this returns 0 with no HTTP at
    all. The mark is set only AFTER the paths are inserted, so a failed fetch -- GitHub's 403
    rate limit, a 404 on a commit GitHub has not mirrored yet, a network error -- is retried
    on the next tick instead of waiting ~12 h for the next Fenix build (2 a day) with the
    index stale. A 403 therefore costs at most one request per 20-minute tick, 3 an hour,
    far under 60/h unauthenticated or 5,000/h with ``GITHUB_TOKEN`` (``_github_headers``)."""
    try:
        bid = models.Build.get_max_buildid(channel, product)
        if bid is None:
            logger.info("java: no %s/%s build to index the JVM files at", product, channel)
            return 0
        node = models.Build.get_changeset(bid, channel, product)
        if not node:
            logger.warning("java: newest %s/%s build has no revision", product, channel)
            return 0
        mark = _index_mark(channel, product)
        row_id = _build_row_id(bid, channel, product)
        if row_id is not None and row_id == models.SweepMark.get(mark):
            return 0
        sha = _git_sha(node, channel)
        if not sha:
            return 0
        r = net.get(GITHUB_TREE_URL.format(sha), headers=_github_headers())
        if r.status_code != 200:
            # 403 is GitHub's rate limit (the remaining budget says whose: 0 = ours or a
            # neighbour's on the shared IP), 404 an unknown commit / path. Retried next tick.
            logger.warning(
                "java: GitHub tree at %s -> %d (X-RateLimit-Remaining %s); JVM file index "
                "not refreshed, retried next tick",
                sha, r.status_code, r.headers.get("X-RateLimit-Remaining"),
            )
            return 0
        data = r.json() or {}
        if data.get("truncated"):
            logger.warning("java: GitHub tree at %s is truncated; the index may miss files",
                           sha)
        paths = _jvm_paths(data.get("tree") or [])
        added = _add_missing_files(paths)
        if row_id is not None:
            models.SweepMark.set(mark, row_id)
        logger.info("java: JVM file index at %s (%s): %d paths, %d new", node, sha[:12],
                    len(paths), added)
        return added
    except Exception as e:
        logger.warning("java: JVM file index not refreshed for %s/%s: %s", product, channel, e)
        return 0


def populate_java_files():
    """What ``create.create`` calls on a fresh database: index the JVM files at the newest
    Fenix nightly build (a no-op, with a log line, before any build row exists)."""
    return refresh_file_index("nightly", "Fenix")


# ---------------------------------------------------------------------------------------
# Presentation


def reformat_java_stacktrace(
    st, channel, buildid, product="Fenix", get_full_path=None, get_changeset=None
):
    """The stack text with each of OUR frames' ``(File.kt:12)`` turned into an hg annotate
    link at the build revision (test-only since ``/api/javast`` was retired)."""
    if not st:
        return ""
    if get_full_path is None:
        get_full_path = models.File.get_full_path
    if get_changeset is None:
        get_changeset = tools.get_changeset

    node = get_changeset(buildid, channel, product)
    if not node:
        return html.escape(st)

    res = ""
    repo_url = Mercurial.get_repo_url(channel)
    lines = list(st.split("\n"))
    N = len(lines)
    for i in range(N):
        line = lines[i]
        m = JAVA_PAT1.match(line.strip())
        line = html.escape(line)
        added = False
        if m:
            path, filename, linenumber = m.groups()
            module, _ = _split_frame_path(path)
            ours = config.is_java_package(module) and bool(linenumber)
            if ours and not _is_synthetic_filename(filename):
                base_path, _ = _pkg_and_class(module)
                repo_filename = get_full_path(base_path + "/" + filename)
                r = '(<a href="{}/annotate/{}/{}#l{}">{}:{}</a>)'
                r = r.format(
                    repo_url, node, repo_filename, linenumber, filename, linenumber
                )
                res += JAVA_PAT3.sub(r, line)
                added = True
        if not added:
            res += line
        if i < N - 1:
            res += "\n"

    return res


def write_java_fixture(uuid, path):
    """Dev helper: save the JVM fields of a processed crash as a test fixture (what
    ``tests/java/fenix_3c426d92.json`` is)."""
    import json
    from libmozdata import socorro

    data = socorro.ProcessedCrash.get_processed(uuid)[uuid]
    keep = ("product", "build", "release_channel", "signature", "java_stack_trace",
            "java_exception")
    with open(path, "w") as Out:
        json.dump({k: data.get(k) for k in keep}, Out, indent=1)
