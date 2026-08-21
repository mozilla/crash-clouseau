# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Is the mechanism's own machinery even IN the build that crashed?

WHY THIS EXISTS. Three of the four module-owner refutations of our `Core :: JavaScript*`
filings say the same thing, all three from Jon Coppeard, all three about concurrent marking:

* bug 2063782 -- "It does not. Is it possible to make Clouseau see this somehow?"
* bug 2063902 -- "Concurrent marking is not compiled in by default."
* bug 2062114 -- "Concurrent marking is under development and is not present in any release
  builds so is not relevant to these crashes."

Two of them named the SAME changeset, by Coppeard, so we needinfo'd the author of a default-off
subsystem three times about his own inert code. That is the failure mode a subsystem behind a
default-off flag produces by construction: its commits fill the pushlog window while its code
cannot run, so it is a candidate magnet that manufactures false regressors.

THE OBVIOUS CHECK IS THE WRONG ONE, and this was measured before anything was built. "A cited
symbol behind a default-off `moz.configure` option" fires on **0 of 3**: the cited lines are
ordinary always-compiled code (`CacheIRStubInfo::fieldType` has no enclosing `#ifdef` within 140
lines) and NEITHER named changeset so much as mentions `JS_GC_CONCURRENT_MARKING` in its diff.

What is actually guarded is a HOLLOW SYMBOL. `js::gc::AutoMarkingLock` (`js/src/gc/Cell.h`) is a
real class that compiles, links and shows up in searchfox -- and whose members, constructor body
and destructor body are EACH wrapped in `#ifdef JS_GC_CONCURRENT_MARKING`. Its own comment says
so: "This is a no op outside concurrent marking builds." A mechanism resting on that lock is
vacuous in a default build while every symbol in it is real. So the test is on the symbol's BODY:
strip the guarded regions and ask whether any function body that had statements now has none.

BACK-TESTED over all 56 filings the canary has made (274 symbols resolved): exactly ONE hollow
symbol exists in the whole corpus, `gc::AutoMarkingLock`, and it fires on exactly TWO filings,
2063782 and 2063902 -- both true positives -- with **zero** hits on the other 54, including all
16 a human FIXED or duplicated. 2062114 is not reachable this way and is left uncaught: its
citation is real code and its changeset's four identifiers contain nothing hollow.
"""
import re

from .logger import logger

# Macros we will not reason about even if the resolver somehow answers, because being wrong about
# them is expensive and being right adds nothing. `NIGHTLY_BUILD` and `EARLY_BETA_OR_EARLIER` are
# ON in the only channel we analyse; `DEBUG`/`MOZ_DIAGNOSTIC_ASSERT_ENABLED` decide whether an
# assertion exists, which is a different question from whether a mechanism can happen; platform
# macros are answered by the crash's own OS, not by moz.configure. Measured: none of these can
# reach `_option_is_default_off` anyway -- `NIGHTLY_BUILD`/`EARLY_BETA_OR_EARLIER` are fed by
# `milestone.*` rather than an `option()`, and `DEBUG`/`MOZ_DIAGNOSTIC_ASSERT_ENABLED` have no
# `set_define` at all -- so this list is a second lock on a door that is already shut.
GUARD_DENY = frozenset({
    "DEBUG", "NDEBUG", "MOZ_DIAGNOSTIC_ASSERT_ENABLED", "MOZ_ASSERT_ENABLED",
    "NIGHTLY_BUILD", "EARLY_BETA_OR_EARLIER", "RELEASE_OR_BETA", "MOZILLA_OFFICIAL",
    "XP_WIN", "XP_UNIX", "XP_LINUX", "XP_MACOSX", "XP_DARWIN", "XP_IOS", "ANDROID",
    "MOZ_WIDGET_GTK", "MOZ_WIDGET_ANDROID", "MOZ_WIDGET_COCOA", "MOZ_X11", "MOZ_WAYLAND",
})

# How many identifiers off the candidate's diff are worth a searchfox lookup. The diff is ranked
# by how often an identifier appears in CHANGED lines, because the thing a patch is about is the
# thing it touches most: on `3f0439a2aec8` -- the changeset behind BOTH catchable refutations --
# `gc::AutoMarkingLock` is #1 at 13 occurrences, so even a cap of 3 would find it. 8 costs a mean
# of 5.1 lookups per filing across the corpus, against a mean of 35.9 (max 1230) uncapped.
MAX_DIFF_SYMBOLS = 8

# `Namespace::Type`, which is what a C++ symbol worth looking up in searchfox looks like. A bare
# identifier is not worth a lookup: too ambiguous, and the corpus shows the interesting ones are
# always qualified.
_QUALIFIED = re.compile(r"\b((?:[A-Za-z_]\w*::)+[A-Z]\w+)\b")

_DIRECTIVE = re.compile(r"^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)$")
_MACRO_NAME = re.compile(r"\b([A-Z][A-Z0-9_]{3,})\b")
# A function/ctor/dtor signature that opens its body on the same line. Crude on purpose: a body
# this misses is simply not analysed, which can only lose a detection, never invent one.
_SIGNATURE = re.compile(r"^\s*(?:[A-Za-z_~][\w:~<>,*&\s]*)\([^;]*\)\s*(?:const\s*)?(?:noexcept\s*)?\{\s*$")
_BLOCK_KEYWORD = re.compile(r"^\s*(if|for|while|switch|catch|else|do|return)\b")
# Anything that is not pure punctuation -- `}`, `};`, `{` alone are structure, not behaviour.
_SUBSTANTIVE = re.compile(r"[^\s{}();:]")


def _decides_on(kind, expr, macro):
    """``True``/``False`` when this branch is taken only with *macro* defined/undefined,
    ``None`` when the condition says nothing about it."""
    mentions = re.search(r"\b%s\b" % re.escape(macro), expr or "") is not None
    if kind == "ifdef":
        return True if mentions else None
    if kind == "ifndef":
        return False if mentions else None
    if kind in ("if", "elif") and mentions:
        if re.match(r"^\s*!\s*defined\s*\(\s*%s\s*\)" % re.escape(macro), expr or ""):
            return False
        return True
    return None


def lines_with_macro_off(text, macro):
    """The lines of *text* that survive when *macro* is NOT defined.

    Conditions that do not mention *macro* keep BOTH branches. We cannot evaluate them, and
    keeping them can only make a symbol look less hollow than it is -- the safe direction."""
    out, stack = [], []
    for line in text.splitlines():
        m = _DIRECTIVE.match(line)
        if m:
            kind, expr = m.group(1), m.group(2)
            if kind in ("if", "ifdef", "ifndef"):
                stack.append(_decides_on(kind, expr, macro))
            elif kind == "elif" and stack:
                stack[-1] = _decides_on(kind, expr, macro)
            elif kind == "else" and stack:
                stack[-1] = None if stack[-1] is None else (not stack[-1])
            elif kind == "endif" and stack:
                stack.pop()
            continue
        if any(v is True for v in stack):
            continue
        out.append(line)
    return out


def _substantive(lines):
    """How many lines carry behaviour, ignoring blanks, comments and bare punctuation."""
    n, in_comment = 0, False
    for line in lines:
        s = line.strip()
        if in_comment:
            in_comment = "*/" not in s
            continue
        if s.startswith("/*"):
            in_comment = "*/" not in s
            continue
        if not s or s.startswith("//") or not _SUBSTANTIVE.search(s):
            continue
        n += 1
    return n


def function_bodies(text):
    """``[(name, body_text)]`` for brace-delimited bodies whose opening line reads as a
    function, constructor or destructor signature."""
    lines, out, i = text.splitlines(), [], 0
    while i < len(lines):
        if _SIGNATURE.match(lines[i]) and not _BLOCK_KEYWORD.match(lines[i]):
            depth, j = 0, i
            while j < len(lines):
                depth += lines[j].count("{") - lines[j].count("}")
                if depth <= 0 and j > i:
                    break
                j += 1
            out.append((re.sub(r"\s*\(.*$", "", lines[i]).strip(), "\n".join(lines[i:j + 1])))
            i = j + 1
            continue
        i += 1
    return out


def hollow_functions(text, macro):
    """Names of function bodies in *text* that have statements normally and NONE when *macro*
    is undefined -- i.e. the "no op outside X builds" shape."""
    hollow = []
    for name, body in function_bodies(text):
        inner = body.splitlines()[1:-1]
        if not _substantive(inner):
            continue
        off = lines_with_macro_off(body, macro)
        if not _substantive(off[1:-1] if len(off) > 1 else []):
            hollow.append(name)
    return hollow


def guard_macros(text):
    """Every macro named by an ``#if``/``#ifdef`` inside *text*, minus :data:`GUARD_DENY`."""
    found = []
    for line in text.splitlines():
        m = _DIRECTIVE.match(line)
        if not m or m.group(1) not in ("if", "ifdef", "ifndef", "elif"):
            continue
        for name in _MACRO_NAME.findall(m.group(2)):
            if name not in found and name not in GUARD_DENY:
                found.append(name)
    return found


def _configure_text(path, channel, rev):
    """The text of a ``moz.configure`` AS OF the crash build, or ``""``."""
    from . import hgedge

    try:
        return hgedge.raw_file(path, rev or "tip", channel or "nightly") or ""
    except Exception as exc:                       # pragma: no cover - network
        logger.warning("compiled_out: cannot read %s: %s", path, exc)
        return ""


def _option_call(text, switch):
    """The full text of the ``option("<switch>", ...)`` call, or ``""``. Brace-balanced because
    the arguments routinely wrap over several lines and ``default=`` is often the last one."""
    m = re.search(r'option\(\s*"%s"' % re.escape(switch), text)
    if not m:
        return ""
    depth, i = 0, text.index("(", m.start())
    for j in range(i, min(len(text), i + 4000)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    return ""


def _switches_for(text, func):
    """The ``--enable-``/``--disable-`` switches that ``def func`` depends on."""
    m = re.search(r"^def\s+%s\s*\(" % re.escape(func), text, re.M)
    if not m:
        return []
    head = text[max(0, m.start() - 600):m.start()]
    depends = re.findall(r"@depends\(([^)]*)\)", head)
    if not depends:
        return []
    return re.findall(r'"(--(?:enable|disable)-[\w-]+)"', depends[-1])


def _option_is_default_off(macro, client, channel="nightly", rev=""):
    """``True`` when *macro* comes from a ``moz.configure`` switch that is OFF unless asked for.

    Answers ``False`` for everything it cannot walk end to end, because the cost of a wrong "off"
    is suppressing a real lead. The walk is ``set_define("MACRO", expr)`` -> the ``@depends``
    above ``def expr`` -> ``option("--enable-x")`` with no ``default=``.

    Three things fall off that walk on purpose, and they are the ones that would hurt:
    ``NIGHTLY_BUILD`` and ``EARLY_BETA_OR_EARLIER`` are fed by ``milestone.*`` rather than a
    function, so step one finds no bare name; ``DEBUG`` and ``MOZ_DIAGNOSTIC_ASSERT_ENABLED``
    have no ``set_define`` at all; and a switch carrying any ``default=`` is an expression we
    decline to evaluate."""
    try:
        hits = client.search('set_define("%s"' % macro, limit=4)
    except Exception as exc:                       # pragma: no cover - network/binary
        logger.warning("compiled_out: set_define search failed for %s: %s", macro, exc)
        return False
    for hit in hits:
        if not hit.file.endswith("moz.configure"):
            continue
        m = re.search(r'set_define\(\s*"%s"\s*,\s*([A-Za-z_]\w*)\s*\)' % re.escape(macro),
                      hit.text)
        if not m:
            continue                               # `milestone.is_nightly` -- not a switch
        text = _configure_text(hit.file, channel, rev)
        if not text:
            continue
        switches = _switches_for(text, m.group(1))
        # A `--disable-x` switch means the feature is ON unless someone turns it off, and any
        # `default=` is an expression we will not guess at. Either way: not established off.
        if not switches or any(sw.startswith("--disable-") for sw in switches):
            continue
        if all(_option_call(text, sw) and "default=" not in _option_call(text, sw)
               for sw in switches):
            return True
    return False


def mechanism_symbols(mechanism, diff_text="", limit=MAX_DIFF_SYMBOLS):
    """The symbols worth asking about, most-cited first.

    Two sources, because the corpus shows one is not enough. The mechanism's own CITATIONS reach
    the hollow symbol on bug 2063902 (a `diff_line` whose ``content`` is
    ``gc::AutoMarkingLock lock(...)``) but not on 2063782, whose single citation is ordinary code.
    The candidate's DIFF reaches both, ranked by occurrences in changed lines."""
    out = []
    citations = mechanism.get("citations") if isinstance(mechanism, dict) else \
        getattr(mechanism, "citations", None)
    for cite in (citations or []):
        def field(name):
            v = cite.get(name) if isinstance(cite, dict) else getattr(cite, name, "")
            return (v or "").strip() if isinstance(v, str) else ""
        sid = field("symbol_id")
        if sid and sid not in out:
            out.append(sid)
        for name in _QUALIFIED.findall(field("content")):
            if name not in out:
                out.append(name)
    changed = [ln[1:] for ln in (diff_text or "").splitlines()
               if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
    counts = {}
    for name in _QUALIFIED.findall("\n".join(changed)):
        counts[name] = counts.get(name, 0) + 1
    for name in sorted(counts, key=lambda k: (-counts[k], k))[:limit]:
        if name not in out:
            out.append(name)
    return out


def hollow_symbols(symbols, client=None, channel="nightly", rev=""):
    """``{symbol: {"macro": X, "functions": [...]}}`` for each symbol that is a no-op in a
    default build. Never raises: a lookup we cannot make is a symbol we say nothing about."""
    found = {}
    if not symbols:
        return found
    if client is None:
        from .searchfox import SearchfoxClient

        try:
            client = SearchfoxClient()
        except Exception as exc:                   # pragma: no cover - binary missing
            logger.warning("compiled_out: no searchfox client: %s", exc)
            return found
    checked = {}
    for symbol in symbols:
        try:
            source = client.define(symbol).source or ""
        except Exception:
            continue                               # unknown symbol: say nothing
        for macro in guard_macros(source):
            functions = hollow_functions(source, macro)
            if not functions:
                continue
            if macro not in checked:
                checked[macro] = _option_is_default_off(macro, client, channel, rev)
            if checked[macro]:
                found[symbol] = {"macro": macro, "functions": functions}
                break
    return found
