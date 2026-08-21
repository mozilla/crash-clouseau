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
# them is expensive and being right adds nothing. THREE lists, and the split is load-bearing: the
# skeptic prompt says something DIFFERENT about each (`agent.roles._COMPILED_OUT` renders all
# three, so prompt and gate cannot drift) and `is_build_flag_ground` reads them apart.
#
# CHANNEL_ON -- ON in the nightly we analyse, so "off" is simply the wrong answer.
# `MOZ_DIAGNOSTIC_ASSERT_ENABLED` is the expensive one: 9-11% of the crashes we analyse are
# MOZ_DIAGNOSTIC_ASSERT crashes (23/255 corpus_study, 9/83 corpus_ship, 20/216 corpus_neg75; 1106
# nightly reports in 30 days). `NDEBUG` is the counter-intuitive one and it belongs HERE, not
# with `DEBUG`: `moz.configure`'s `debug_defines` returns `["DEBUG", ...]` for a debug build and
# `["NDEBUG", "TRIMMED"]` otherwise, so an official opt Nightly DEFINES `NDEBUG` and
# `#ifdef NDEBUG` code is exactly the code that shipped. The one skeptic claim in the whole
# 8901-claim dump that mentions it says so itself -- "Nightly defines NDEBUG" (ANGLE asserts,
# a `pass`) -- so putting it on the "off" side would contradict the model's own correct read.
#
# THE REST OF BUILD_TYPE IS THE OPPOSITE CASE, and that distinction is measured rather than
# assumed. An official Nightly is an OPT build, so "this `#ifdef DEBUG` assertion is not in the
# shipped binary" is TRUE and free -- `mfbt/Assertions.h:563` is `#ifdef DEBUG` around
# `MOZ_ASSERT` with a `do {} while (false)` #else -- and 4 of the 21 real skeptic notes that
# reach `is_build_flag_ground` on the 1996-dossier dump are exactly that shape, all 4 correct.
# What is wrong is reaching the same conclusion through a moz.configure walk. Same for
# `RELEASE_OR_BETA`, which really is off on nightly.
#
# PLATFORM -- answered by the crash's own `OS:` line (`triage._crash_facts`), not by
# moz.configure. There is no `option()` behind `MOZ_WIDGET_GTK` for a walk to find anyway.
#
# THIS LIST IS THE LOCK, NOT A SECOND LOCK. An earlier version of this comment said
# `DEBUG`/`MOZ_DIAGNOSTIC_ASSERT_ENABLED` "have no `set_define` at all". That is FALSE for the
# second one: moz.configure:174-178 is `set_define("MOZ_DIAGNOSTIC_ASSERT_ENABLED", True,
# when=moz_debug | milestone.is_nightly | moz_dev_edition)`, and the nearest `option()` above it
# is `option("--enable-debug", nargs="?")` with NO `default=` -- literally the shape the walk
# below calls "off unless someone asks for it", for a macro that is on in every Nightly. What
# actually keeps it out of `_option_is_default_off` is the REGEX: the walk only accepts a BARE
# IDENTIFIER second argument (`set_define("MOZ_DEBUG", moz_debug)`, moz.configure:153), and this
# one passes `True` plus a `when=` keyword. `DEBUG`/`NDEBUG` really do have no `set_define`
# (moz.configure:187-194 folds them into the `MOZ_DEBUG_DEFINES` *list* via `set_config`), but
# that is an accident of how one file happens to be written today. Do not thin any of the three
# lists on the strength of it.
CHANNEL_ON_DENY = frozenset({
    "MOZ_DIAGNOSTIC_ASSERT_ENABLED", "NIGHTLY_BUILD", "EARLY_BETA_OR_EARLIER",
    "MOZILLA_OFFICIAL", "NDEBUG",
})
BUILD_TYPE_DENY = CHANNEL_ON_DENY | frozenset({
    "DEBUG", "MOZ_ASSERT_ENABLED", "RELEASE_OR_BETA",
})
PLATFORM_DENY = frozenset({
    "XP_WIN", "XP_UNIX", "XP_LINUX", "XP_MACOSX", "XP_DARWIN", "XP_IOS", "ANDROID",
    "MOZ_WIDGET_GTK", "MOZ_WIDGET_ANDROID", "MOZ_WIDGET_COCOA", "MOZ_X11", "MOZ_WAYLAND",
})
GUARD_DENY = BUILD_TYPE_DENY | PLATFORM_DENY

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
    """The text of a ``moz.configure`` AS OF the crash build, or ``""``.

    AN EMPTY ANSWER IS A DEAD GATE, NOT A VERDICT, so it is LOGGED. All
    ``_option_is_default_off`` can do with it is ``continue``, which is indistinguishable
    from "this macro is not established off" -- i.e. the whole compiled-out suppression
    silently stops existing, with the corroborations that would make it countable never
    written. That is one misconfiguration away: the caller passes ``pin_rev``, an empty
    ``pin_rev`` falls back to ``tip``, and m-c ``tip`` is periodically a ``.hgtags``-only
    commit whose manifest holds no ``js/moz.configure`` at all (hg-edge then answers 404,
    "not found in manifest"). The gate works today only because ``2d71e11`` made the pinned
    read resolve; nothing anywhere would have said so if it had not."""
    from . import hgedge

    try:
        text = hgedge.raw_file(path, rev or "tip", channel or "nightly") or ""
    except Exception as exc:                       # pragma: no cover - network
        logger.warning("compiled_out: cannot read %s@%s: %s", path, rev or "tip", exc)
        return ""
    if not text:
        logger.warning(
            "compiled_out: %s@%s came back EMPTY -- the guard walk cannot answer, so nothing will "
            "be suppressed on this run (rev=%r, channel=%r). Check pin_rev.",
            path, rev or "tip", rev, channel)
    return text


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
    function, so step one finds no bare name; ``MOZ_DIAGNOSTIC_ASSERT_ENABLED`` DOES have a
    ``set_define`` (moz.configure:174) and is kept out by the bare-identifier regex plus
    :data:`CHANNEL_ON_DENY`, not by its absence; and a switch carrying any ``default=`` is
    an expression we decline to evaluate.

    AND THE ORACLE IS WRONG WHERE IT WAS MEASURED, which is why nothing may treat a ``True``
    from here as proof on its own. Walking ``js/moz.configure`` at a real build node
    (8e966e6c894a) labels 9 macros default-off and 3 of those 9 are ON in official Nightly:
    ``MOZ_RUST_SIMD`` (``ac_add_options --enable-rust-simd`` in ``build/mozconfig.rust``,
    inherited by every official build), ``MOZ_INSTRUMENTS``
    (``browser/config/mozconfigs/macosx64/nightly``) and ``MOZ_PROFILING`` (implied by it at
    js/moz.configure:342). THIS WALK NEVER READS A MOZCONFIG. It is safe where it is used
    because :func:`hollow_symbols` additionally requires the symbol's whole body to vanish
    with the macro off -- a far rarer shape (1 symbol in 274 across the 56-filing corpus) --
    and it is why :func:`is_build_flag_ground` refuses to let an LLM run the same walk in
    prose and bind a verdict on the answer."""
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
            continue                               # unreadable (logged), NOT 'not off'
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


# --------------------------------------------------------------------------- #
# Does a skeptic `fail` REST on a compile-flag claim?
# --------------------------------------------------------------------------- #
# THE TWO VETOES ON THE VETO, both checked before the predicate can fire, both measured on the
# 1996-dossier prod dump (spike/_dossier_dump.jsonl, 2026-07-06..08-05).
#
# PLATFORM is deliberately WIDER than PLATFORM_DENY, because the skeptic writes prose, not macro
# names. It is what keeps 15 of the 39 build-guard fails (3 of the 8 binding vetoes) intact.
_PLATFORM_GROUND = re.compile(
    r"\b(?:%s|windows|win32|win64|linux|macos|mac ?os ?x|osx|darwin|android|ios|gtk\d?|"
    r"wayland|x11|cocoa|widget/(?:gtk|android|cocoa|windows))\b"
    % "|".join(sorted(PLATFORM_DENY)), re.I)
# BUILD TYPE is the other thing a build answers for free: an official Nightly is an opt build, so
# `#ifdef DEBUG` code is genuinely not in it. CASE-SENSITIVE on purpose -- a lowercase "debug
# tooling" is a WebRender cargo-feature note, which must still unbind. `NDEBUG` is deliberately
# NOT here: an opt build DEFINES it (:data:`CHANNEL_ON_DENY`), so "this `#ifdef NDEBUG` code is
# not in the build" is wrong by construction and must NOT be given a veto that makes it bind.
_BUILD_TYPE_GROUND = re.compile(r"\b(?:DEBUG|MOZ_ASSERT\w*|RELEASE_OR_BETA)\b")
_OPT_BUILD_GROUND = re.compile(r"\b(?:opt|non-?debug|release) (?:build|nightly)", re.I)

# The two halves of a build-flag GROUND, required together: a macro-shaped token or a
# configure/preprocessor word, AND a "the compiler left it out" conclusion. Requiring both is what
# keeps "the field is not defined on that type" and "line 2165 is not present in the diff" --
# ordinary contradiction fails -- out of this predicate entirely.
_MACRO_TOKEN = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_BUILD_FLAG_WORDS = re.compile(
    r"moz\.configure|set_define|--(?:enable|disable)-[\w-]+|#\s*if(?:n?def)?\b|\bifdef\b|"
    r"\bcompiled (?:in|into|out)\b|\bcompile[- ]time\b|\bbuild[- ]time\b|\bpreprocessor\b|"
    r"\bconfigure (?:switch|option|flag)\b", re.I)
# The conclusion comes in TWO TIERS, and that split is a replay result rather than taste. Tier 1
# is compile-specific enough to stand next to a bare macro name. Tier 2 ("off by default") is the
# vocabulary of PREFS and enum flags as much as of the build, so it only counts beside an actual
# build-flag word: on the dump, `ClearCache::RENDER_TARGETS only drains render_target_pool ...
# feature is pref-gated off by default` is a structural noise-kill that reads as a build claim
# the moment you accept any ALL_CAPS token plus any "off".
_OFF_COMPILED = re.compile(
    r"\bnot (?:compiled|defined|built)\b|\bundefined\b|\bcompiled out\b|\bnever compiled\b|"
    r"\bnot (?:in|present in|part of) (?:this|the) build\b", re.I)
_OFF_DEFAULT = re.compile(
    r"\bdefault[- ]off\b|\boff by default\b|\bdisabled by default\b|"
    r"\bnot (?:enabled|set|turned on|available)\b|\b(?:is|was|are|were) off\b(?!-)", re.I)


def is_build_flag_ground(note, citations=()):
    """Does this skeptic ``fail`` rest on "a CONFIGURE SWITCH kept this code out of the binary"?

    A question about the claim's GROUND, not its conclusion.
    ``agent.schema.Dossier._skeptic_veto`` uses it to decide whether a ``fail`` may turn a LEAD
    into an ABSTAIN; ``True`` means it may not, unless the deterministic gate agrees.

    WHY THE BINDING DECISION MOVED INTO CODE. Measured over 1996 prod dossiers
    (2026-07-06..08-05; 8901 skeptic claims, 1765 ``fail``): 124 claims reason about a build
    guard and 39 of them ``fail``. The ``set_define`` -> ``option()`` -> ``default=`` walk the
    prompt used to spell out serves 2 of those 39, and BOTH of those filings (2063782, 2063902)
    are now suppressed with no LLM at all by ``_apply_compiled_out_gate``, off a hollow symbol
    the diff ranker puts #1 of 8. That walk is a four-hop chain (symbol -> ``#ifdef`` ->
    ``set_define`` -> ``option``) run by the cheapest model tier, its oracle is measurably wrong
    3 times in 9 (see :func:`_option_is_default_off`), and its consequence is the harshest one we
    have -- an abstain, which files nothing, skips the second opinion and never reaches
    ``Feedback``, so a false one is invisible forever. The LLM keeps the doubt (its note still
    rides the dossier and the bug comment); the deterministic gate keeps the teeth.

    REPLAYED, because a rule whose failure mode is a false abstain cannot be audited by outcome.
    Over the same 1996 dossiers this predicate fires on 5 of 1765 ``fail``s (0.3%) and changes
    the outcome of 0 of the 216 stored (1b) vetoes. The five are the shapes it is for: the
    concurrent-marking pair (``--enable-gc-concurrent-marking``, ``JS_GC_CONCURRENT_MARKING
    undefined``), two WebRender ``#[cfg(feature = "replay")]`` notes, and a packaging script
    "never compiled/linked into any shipped Firefox binary". Note the corpus PREDATES the clause
    (``defe860`` landed 2026-08-21, the dump ends 08-05), so 0-of-216 is a statement of
    NARROWNESS, not of uselessness: the guard exists for what that clause will now produce.

    THE COUNTER-EXAMPLE THIS MUST NOT EAT -- crash 560c0f2f-07cc-46c6-950c-1d8240260731 (Firefox
    nightly 20260730132738, Windows NT 10.0.19044, signature ``mozilla::FileBlockCache::Flush``).
    Its only candidate, ``ff789e9f149e``, touches 6 files of which 4 are under ``widget/gtk/``;
    the skeptic ``fail``ed it with "GTK-gated Linux ibus/fcitx key-event plumbing, not compiled
    into Windows builds" and that fail was BINDING and RIGHT. That shape is 15 of the 39
    build-guard fails and 3 of the 8 binding vetoes in the month (sibling:
    8b7edf2e-7e4f-4a44-9b6d-a92370260731, same candidate, ``shutdownhang |
    InfallibleQuoteJSONString``). The deterministic gate is designed never to see it --
    ``_option_is_default_off`` returns False for all 20 :data:`GUARD_DENY` macros, verified 20/20
    at a real pinned node -- and there is no ``option()`` behind ``MOZ_WIDGET_GTK`` for the walk
    to find either. Hence :data:`_PLATFORM_GROUND`, checked first: a platform claim is one of the
    two build questions a crash report answers by itself, from the ``OS:`` line
    ``triage._crash_facts`` puts in every prompt. :data:`_BUILD_TYPE_GROUND` is the other, and it
    was found the same way -- 4 of the 21 notes the first draft of this predicate fired on were
    "`#ifdef DEBUG` / `MOZ_ASSERT` is compiled out of the opt Nightly", which is simply TRUE
    (``mfbt/Assertions.h:563`` is `#ifdef DEBUG` around `MOZ_ASSERT`, `do {} while (false)`
    otherwise) and cost nothing to establish. ``NDEBUG`` LOOKS LIKE IT BELONGS IN THAT VETO AND
    MUST NOT BE PUT THERE: `moz.configure`'s ``debug_defines`` emits ``["NDEBUG", "TRIMMED"]``
    for a non-debug build, so the opt Nightly DEFINES it and "`#ifdef NDEBUG` code is not in
    this build" is false. It is in :data:`CHANNEL_ON_DENY`, which forces this predicate True, so
    such a ``fail`` unbinds instead of silently abstaining a lead.

    TWO THINGS THE REPLAY REFUTED, both in the first draft, both dropped:

    * A PREF ARM. ``StaticPrefList``/``pref`` looked like a third ground (the prompt does route
      pref-gated paths to ``unverifiable``). On the dump it fires on 8 notes and in 7 of them the
      pref clause is an ADJUNCT to a structural kill -- "only drains render_target_pool, never
      touches cached_render_tasks; feature is pref-gated off by default" -- while the 8th is the
      skeptic REFUTING a pref argument ("Pref actually defaults to 2 ... the 'disabled by
      default' mitigating argument was wrong"). It caused both of the two outcome changes the
      first draft would have made, and both were wrong. Prefs stay in the PROMPT only.
    * ``default=`` AS AN OFF-CONCLUSION. It reads as "off" but a note that contains it usually
      says ``default=True`` -- the opposite -- and via the citations it silently fired on a
      blame-attribution kill (``seed_candidate_9005591b06bb``: "blame attributes every line to
      older, unrelated bugs"). Only ``no default=``-style prose survives, through the other
      alternatives.
    * "OFF BY DEFAULT" BESIDE A BARE ALL-CAPS TOKEN. It caught a sixth note,
      ``ClearCache::RENDER_TARGETS only drains render_target_pool, never touches
      cached_render_tasks; feature is pref-gated off by default`` -- an enum variant and a pref,
      read as a macro and a build flag. Hence the two OFF tiers: "off by default" now needs a
      real build-flag WORD beside it, while "not compiled"/"undefined" can stand next to a bare
      macro.

    FALSE-FIRE DIRECTION, stated because this is a regex over free text. It fires when the text
    carries BOTH a build-flag token and an "off" conclusion and NEITHER a platform nor a
    build-type word, so it also unbinds a correct kill the deterministic gate cannot see -- a
    cargo feature, ``USE_MEMFD_CREATE``, a build-only packaging script: the "other 11 of 39"
    bucket, 3 of the 5 it fires on. Those leads get FILED instead of dropped, which is the
    recoverable direction under the worth-investigating pivot (a needinfo a human closes, rather
    than an abstain nobody can count) but is a real cost. And a ``fail`` that contradicts a
    citation AND mentions a flag in the same note unbinds too -- except that the skeptic emits
    one result per CLAIM, so an unrelated contradiction normally lives in its own
    ``SkepticResult`` and still binds, which is exactly why ``_skeptic_veto`` applies this per
    result and not per dossier."""
    text = " ".join([note or ""] + [str(c) for c in (citations or [])])
    if not text.strip():
        return False
    off_hard = bool(_OFF_COMPILED.search(text))
    if _BUILD_FLAG_WORDS.search(text):
        grounded = off_hard or bool(_OFF_DEFAULT.search(text))
    else:
        grounded = off_hard and bool(_MACRO_TOKEN.search(text))
    if not grounded:
        return False
    if _PLATFORM_GROUND.search(text):
        return False
    # A CHANNEL_ON macro is checked BEFORE the build-type veto: `MOZ_DIAGNOSTIC_ASSERT_ENABLED is
    # off` is wrong by construction even when the same note also says `#ifdef DEBUG`.
    if any(re.search(r"\b%s\b" % m, text) for m in CHANNEL_ON_DENY):
        return True
    if _BUILD_TYPE_GROUND.search(text) or _OPT_BUILD_GROUND.search(text):
        return False
    return True
