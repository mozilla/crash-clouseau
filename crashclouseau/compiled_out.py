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
import contextvars
import re

from .logger import logger
from .searchfox import repo_for_channel

# THE CHANNEL THE CURRENT RUN IS ANALYSING, as a context variable.
#
# A CONTEXT VARIABLE AND NOT AN ARGUMENT, for exactly one reason: the consumer that most needs
# it is ``agent.schema.Dossier._skeptic_veto``, a pydantic ``model_validator`` that decides
# whether a skeptic ``fail`` may abstain a lead. A validator sees only the model, and the
# dossier has no channel field -- adding one would put a channel the MODEL could set into the
# handoff, which is the class of field ``parse_and_validate`` strips on purpose. Threading it
# through ``parse_and_validate`` would miss the ``_salvage`` path and ``dossier_from_db_json``.
#
# ``orchestrator.run_evidence_agent`` sets it once per run; ``eval/runner`` does the same. A
# ContextVar (not a module global) so the eval harness's concurrent cases cannot read each
# other's channel.
#
# THE DEFAULT IS "nightly" AND THAT IS A DEGRADATION, NOT A GUESS: it is the partition this
# gate applied unconditionally for a year, so a path that forgets to set the channel behaves
# exactly as it did before this change rather than in some new way. Every function here takes
# an explicit ``channel`` too, and callers that have one should pass it.
_BUILD_CHANNEL = contextvars.ContextVar("crashclouseau_build_channel", default="nightly")


def build_channel():
    """The channel of the run currently being analysed (default ``"nightly"``)."""
    return _BUILD_CHANNEL.get()


def set_build_channel(channel):
    """Set the current run's channel; returns the token to ``reset`` with."""
    return _BUILD_CHANNEL.set((channel or "nightly").lower())


def reset_build_channel(token):
    """Undo a ``set_build_channel``. Best-effort: a token from another context just no-ops."""
    try:
        _BUILD_CHANNEL.reset(token)
    except ValueError:  # pragma: no cover - token from a different context
        pass

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
#
# AND EVERY ONE OF THOSE SENTENCES IS ABOUT NIGHTLY. Three of the five "ON, never conclude
# off" macros are OFF on beta and one of the three "genuinely off" ones is ON, so the same
# table applied to a beta crash is wrong in BOTH directions and no single relaxation fixes it.
# Read from mozilla-beta's own source on 2026-08-25 (`hgedge.raw_file(..., channel="beta")`):
#
#   build/moz.configure/init.configure -- "if we have 'a1' in GRE_MILESTONE, we're building
#     Nightly (define NIGHTLY_BUILD) - otherwise, we're building Release/Beta";
#     `set_define("NIGHTLY_BUILD", milestone.is_nightly)`,
#     `set_define("RELEASE_OR_BETA", milestone.is_release_or_beta)`, and
#     `is_early_beta_or_earlier = is_nightly` with the comment "EARLY_BETA_OR_EARLIER is an
#     alias for NIGHTLY_BUILD, pending its removal".
#   moz.configure -- `set_define("MOZ_DIAGNOSTIC_ASSERT_ENABLED", True,
#     when=moz_debug | milestone.is_nightly | moz_dev_edition)`.
#
# So on beta: NIGHTLY_BUILD, EARLY_BETA_OR_EARLIER and MOZ_DIAGNOSTIC_ASSERT_ENABLED are OFF,
# and RELEASE_OR_BETA is ON. `MOZ_DIAGNOSTIC_ASSERT_ENABLED` is keyed on the RAW
# `release_channel` and not on our stored label, because `moz_dev_edition` puts it back ON for
# Developer Edition -- which Socorro files as `aurora`, and which is 36-41% of the channel.
# MOZILLA_OFFICIAL and NDEBUG are ON on every official opt build, so they do not move; DEBUG
# and MOZ_ASSERT_ENABLED are off on every official opt build, so they do not either.
#
# `nightly`'s partition below is BYTE-IDENTICAL to the two frozensets this replaced. That is
# deliberate: this change must be a pure extension on the channel the pipeline has run on for
# a year.


_CHANNEL_MACROS = {
    # channel -> (on: claiming these are OFF is wrong,
    #             off: these really are absent from the build, and it is free to say so)
    "nightly": (
        frozenset({"MOZ_DIAGNOSTIC_ASSERT_ENABLED", "NIGHTLY_BUILD", "EARLY_BETA_OR_EARLIER",
                   "MOZILLA_OFFICIAL", "NDEBUG"}),
        frozenset({"DEBUG", "MOZ_ASSERT_ENABLED", "RELEASE_OR_BETA"}),
    ),
    "beta": (
        frozenset({"MOZILLA_OFFICIAL", "NDEBUG", "RELEASE_OR_BETA"}),
        frozenset({"DEBUG", "MOZ_ASSERT_ENABLED", "NIGHTLY_BUILD", "EARLY_BETA_OR_EARLIER",
                   "MOZ_DIAGNOSTIC_ASSERT_ENABLED"}),
    ),
    # Developer Edition: beta plus `moz_dev_edition`, which restores MOZ_DIAGNOSTIC_ASSERT.
    "aurora": (
        frozenset({"MOZILLA_OFFICIAL", "NDEBUG", "RELEASE_OR_BETA",
                   "MOZ_DIAGNOSTIC_ASSERT_ENABLED"}),
        frozenset({"DEBUG", "MOZ_ASSERT_ENABLED", "NIGHTLY_BUILD", "EARLY_BETA_OR_EARLIER"}),
    ),
    "release": (
        frozenset({"MOZILLA_OFFICIAL", "NDEBUG", "RELEASE_OR_BETA"}),
        frozenset({"DEBUG", "MOZ_ASSERT_ENABLED", "NIGHTLY_BUILD", "EARLY_BETA_OR_EARLIER",
                   "MOZ_DIAGNOSTIC_ASSERT_ENABLED"}),
    ),
}

# Macros whose OFF-ness follows from the CHANNEL ALONE and which are worth detecting as a
# hollow-symbol guard, per channel. `guard_deny` keeps them OUT of its deny list so
# `guard_macros` can see them, and `_default_off_switch` answers them from the channel instead
# of walking `moz.configure` (there is no `option()` behind a milestone predicate to find).
#
# WHY THIS EXISTS: on beta, a symbol whose entire body sits inside `#ifdef NIGHTLY_BUILD` is
# the single most common way a symbol is genuinely hollow -- and it was undetectable, because
# NIGHTLY_BUILD sat in the deny list `guard_macros` subtracts. Compare the nightly evidence
# that built this gate: 3 of the 4 confirmed JS-owner refutations were compiled-out cases and
# the guarded thing was a HOLLOW SYMBOL every time (`gc::AutoMarkingLock`).
#
# EMPTY ON NIGHTLY, so nightly's behaviour is unchanged. `RELEASE_OR_BETA` would be a correct
# member there (that code really is absent from a nightly) and is deliberately left out: the
# walk answers "" for it anyway (`set_define("RELEASE_OR_BETA", milestone.is_release_or_beta)`
# has no `option()` behind it), so admitting it would buy a lookup and no detection, and this
# change is not the place to move nightly.
_CHANNEL_OFF_HOLLOW = {
    "beta": frozenset({"NIGHTLY_BUILD", "EARLY_BETA_OR_EARLIER"}),
    "aurora": frozenset({"NIGHTLY_BUILD", "EARLY_BETA_OR_EARLIER"}),
    "release": frozenset({"NIGHTLY_BUILD", "EARLY_BETA_OR_EARLIER"}),
}

# How each channel's OFF macros read in the published suppression, when the answer comes from
# the channel rather than from a configure switch.
_CHANNEL_OFF_PHRASE = {
    "NIGHTLY_BUILD": "is defined only when the milestone is a nightly (`a1`), and this crash "
                     "is on {channel}",
    "EARLY_BETA_OR_EARLIER": "is an alias for `NIGHTLY_BUILD` (init.configure: "
                             "`is_early_beta_or_earlier = is_nightly`), and this crash is on "
                             "{channel}",
}


# A ``_default_off_switch`` answer that is a CHANNEL sentence rather than a ``--enable-x``
# switch name is prefixed with this, so the one consumer that renders it into a bug comment
# (``orchestrator._apply_compiled_out_gate``) can pick the right wording instead of printing
# "off unless someone passes `is defined only when the milestone is ...`".
_CHANNEL_OFF_MARK = "channel:"


def is_channel_off_answer(answer):
    """Did :func:`_default_off_switch` answer from the CHANNEL rather than with a switch?"""
    return isinstance(answer, str) and answer.startswith(_CHANNEL_OFF_MARK)


def channel_off_phrase(answer):
    """The prose half of a channel answer (``is_channel_off_answer`` first)."""
    return answer[len(_CHANNEL_OFF_MARK):] if is_channel_off_answer(answer) else answer


def _partition(channel):
    return _CHANNEL_MACROS.get((channel or "").lower(), _CHANNEL_MACROS["nightly"])


def channel_on_deny(channel=None):
    """Macros ON in a *channel* build: concluding they are "off" is simply the wrong answer."""
    return _partition(channel or build_channel())[0]


def channel_off(channel=None):
    """Macros genuinely ABSENT from a *channel* build: saying so is true, and free -- read it
    off the build type, never off a ``moz.configure`` walk."""
    return _partition(channel or build_channel())[1]


def build_type_deny(channel=None):
    """Both halves: every macro the ``moz.configure`` walk must not be asked about."""
    on, off = _partition(channel or build_channel())
    return on | off


def guard_deny(channel=None):
    """What ``guard_macros`` subtracts: the build-type and platform macros, MINUS the ones this
    channel answers by itself and that are worth catching as a hollow guard
    (``_CHANNEL_OFF_HOLLOW`` -- empty on nightly, so nightly is unchanged)."""
    channel = (channel or build_channel() or "").lower()
    return (build_type_deny(channel) | PLATFORM_DENY) - _CHANNEL_OFF_HOLLOW.get(
        channel, frozenset()
    )


# The nightly partition, kept as module constants because that is what a year of docstrings,
# `agent.roles._COMPILED_OUT` and `tests/test_compiled_out_guard.py` refer to. They are the
# `nightly` entry above and nothing else may read them for a non-nightly crash.
CHANNEL_ON_DENY = _CHANNEL_MACROS["nightly"][0]
BUILD_TYPE_DENY = CHANNEL_ON_DENY | _CHANNEL_MACROS["nightly"][1]
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


def guard_macros(text, channel=None):
    """Every macro named by an ``#if``/``#ifdef`` inside *text*, minus :func:`guard_deny` for
    this crash's channel.

    Not :data:`GUARD_DENY` any more, and the difference only shows off nightly: on beta
    ``NIGHTLY_BUILD``/``EARLY_BETA_OR_EARLIER`` come BACK into scope, because a symbol whose
    whole body is inside ``#ifdef NIGHTLY_BUILD`` is hollow in a beta build and that is the
    commonest shape there. ``guard_deny("nightly")`` == ``GUARD_DENY``, so nightly is
    unchanged."""
    deny = guard_deny(channel)
    found = []
    for line in text.splitlines():
        m = _DIRECTIVE.match(line)
        if not m or m.group(1) not in ("if", "ifdef", "ifndef", "elif"):
            continue
        for name in _MACRO_NAME.findall(m.group(2)):
            if name not in found and name not in deny:
                found.append(name)
    return found


def _configure_text(path, channel, rev):
    """The text of a ``moz.configure`` AS OF the crash build, or ``""``.

    AN EMPTY ANSWER IS A DEAD GATE, NOT A VERDICT, so it is LOGGED. All
    :func:`_default_off_switch` can do with it is ``continue``, which is indistinguishable
    from "this macro is not established off" -- i.e. the whole compiled-out suppression
    silently stops existing, with the corroborations that would make it countable never
    written. That is one misconfiguration away, and CONFIRMED 2026-08-21 by probing the
    endpoint rather than reasoning about it. ``raw-file`` serves a path out of the MANIFEST of
    the rev it resolves, and m-c's tagging pushes land on a separate ``tags-unified`` head whose
    manifest holds ``.hgtags`` and nothing else: ``raw-file/073c906f9e41/js/moz.configure``
    answers **404 "not found in manifest"** while ``.hgtags`` at the same rev answers 200. hg
    ``tip`` is the newest changeset in the repo whatever branch it sits on, so tip IS that commit
    from the moment it lands until the next m-c push -- 17 of the 33 mozilla-central pushes
    between 2026-08-18 and 2026-08-21 were ``.hgtags``-only, and tip sat on one for 113,477 of
    that window's 298,395 seconds (38%). BEWARE THE PROBE THAT PROVES NOTHING: at a good minute
    ``raw-file/tip/js/moz.configure`` answers 200 (40,264 bytes, measured the same day), which
    says only that tip happened to be a normal changeset just then. The test has to be AT a
    ``.hgtags``-only rev. The gate works today only because ``2d71e11`` made the pinned read
    resolve; nothing anywhere would have said so if it had not, and the other ways to empty it
    are a rev that does not resolve, a path that moved, or hg-edge 406-ing the caller.
    The log line stays either way: the failure is silent by construction, since all
    :func:`_default_off_switch` can do with "" is ``continue``."""
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


# WHICH ``default=`` WE ARE WILLING TO READ -- and the dated counter-example for why reading
# one at all is the fix.
#
# THE SHIPPED WALK REFUSED EVERY ``default=``, INCLUDING A LITERAL ``default=False``, which is
# the strongest evidence a switch can carry. Measured across the 26 distinct build revs of the
# 52-filing panel: ``--enable-gc-concurrent-marking`` -- the switch behind the ONLY macro this
# gate has ever fired on -- carried ``default=False,`` until that line was DELETED at
# 11b07d869739 (buildid 20260811085340). So the walk answers False at the 14 older revs and True
# at the 12 newer ones, and 27 of the 52 filings sit on the blind side. THE FEATURE DID NOT
# CHANGE; THE SPELLING DID: with no ``default=`` at all, ``option("--enable-x")`` is off unless
# someone asks for it, which is exactly what ``default=False`` said. At the older rev EIGHT of
# js/moz.configure's 46 ``option()`` calls (49 distinct switch names) carried a literal falsy
# default -- --enable-decorators, --enable-portable-baseline-interp[-force],
# --enable-aot-ics[-force,-enforce], this one, and --wasm-no-experimental, which the walk never
# sees because `_switches_for` only yields `--enable-`/`--disable-` names; at tip ZERO do. Count
# the CALLS, not the string: `grep -c "option("` answers 53 because it also counts
# `@deprecated_option`, four `imply_option`s, `system_lib_option` and one `def
# ..._option`. The shipped answer tracked a moz.configure CODING STYLE at the build rev, with no
# signal to anyone that it had.
#
# EVERY NON-LITERAL IS STILL DECLINED -- ``default=milestone.is_nightly``,
# ``default=depends(when=moz_debug)``, ``default=jit_default``: 21 such options at tip, none of
# them literal. Evaluating a moz.configure expression is precisely the multi-hop guess this
# module refuses to let an LLM make in prose (:func:`is_build_flag_ground`). A literal TRUTHY
# default is declined too, and for the opposite reason: ``default=True`` says the switch is ON.
#
# MEASURED EFFECT OF THE RELAXATION: the answer for ``JS_GC_CONCURRENT_MARKING`` becomes
# clock-invariant (26 of 26 True, against 12 of 26 shipped); all 20 :data:`GUARD_DENY` macros
# still answer False, verified 20/20 at build node 477c0df9965c -- a real pinned rev, not tip;
# and 0 of the 52 filings change outcome, because ``gc::AutoMarkingLock`` is still the only
# hollow symbol in the 270-symbol corpus and a new True has nothing else to fire on.
_STRING_LITERAL = re.compile(
    r'"""(?:.|\n)*?"""|\'\'\'(?:.|\n)*?\'\'\'|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'')
_DEFAULT_ARG = re.compile(r"(?<![\w.])default\s*=\s*([^,)\n]*)")
_LITERAL_OFF = frozenset({"False", "0", "None", '""', "''"})


def _default_is_off(call):
    """``True`` when this ``option(...)`` call has no ``default=``, or a LITERAL falsy one.

    String literals are blanked before the search (an empty one stays empty, so ``default=""``
    survives) because a ``default=`` written inside a ``help=`` text is not the keyword
    argument, and reading it as one would be a false "off" -- the direction that costs a real
    lead.

    WHAT IT DOES NOT CLOSE, said out loud because the guard above is only one of the two routes:
    the search is LEFTMOST, so a ``default=`` nested inside another argument's value
    (``option("--enable-x", when=depends(y, default=False), default=jit_default)``) would win
    over the option's own and answer a false "off". Measured 2026-08-21 over all 38 ``*.configure``
    files at m-c tip: ZERO ``--enable-``/``--disable-`` options have two ``default=`` tokens, and
    the only literal falsy defaults outside js/moz.configure sit on ``env=`` options and
    ``--with-`` options, neither of which :func:`_switches_for` can yield. So the hole is real
    and currently empty; if it ever fills, parse the call instead of scanning it."""
    bare = _STRING_LITERAL.sub(
        lambda m: '""' if not m.group(0).strip("\"'") else '"s"', call)
    m = _DEFAULT_ARG.search(bare)
    return m is None or m.group(1).strip() in _LITERAL_OFF


def _default_off_switch(macro, client, channel="nightly", rev=""):
    """The ``--enable-x`` switch *macro* needs when it is OFF unless asked for, else ``""``.

    The walk, its three deliberate holes and the three places its oracle is measurably WRONG are
    documented on :func:`_option_is_default_off`, which is this function's boolean face; the
    ``default=`` rule is documented above :data:`_LITERAL_OFF`.

    IT RETURNS THE SWITCH NAME, not a bare ``True``, because that name is the one thing the
    published suppression needs and could not get: "off unless someone passes
    `--enable-gc-concurrent-marking`" is a sentence the module owner reading the bug can check in
    ten seconds, and "comes from a moz.configure switch that is off unless someone asks for it"
    -- what the gate said for a month -- is not. It was in hand here the whole time.

    A CHANNEL-OFF MACRO SHORT-CIRCUITS THE WALK. ``NIGHTLY_BUILD`` on beta is off because of the
    milestone, not because of a switch: ``set_define("NIGHTLY_BUILD", milestone.is_nightly)`` has
    no ``option()`` behind it, so the walk below would answer "" and the hollow symbol would go
    undetected. The phrase returned instead is a channel sentence rather than a switch name --
    see :data:`_CHANNEL_OFF_PHRASE` and ``orchestrator._apply_compiled_out_gate``, which renders
    whichever it gets."""
    channel = (channel or build_channel() or "nightly").lower()
    if macro in _CHANNEL_OFF_HOLLOW.get(channel, frozenset()):
        phrase = _CHANNEL_OFF_PHRASE.get(macro)
        if phrase:
            return _CHANNEL_OFF_MARK + phrase.format(channel=channel)
    try:
        # The crash's OWN tree: `moz.configure` is not the same file on beta as on central
        # (`is_early_beta_or_earlier` is an alias for `is_nightly`, and
        # MOZ_DIAGNOSTIC_ASSERT_ENABLED is gated on the milestone), and the answer this walk
        # returns is published in a bug comment as a fact about the build that crashed.
        hits = client.search('set_define("%s"' % macro, limit=4,
                             repo=repo_for_channel(channel).value)
    except Exception as exc:                       # pragma: no cover - network/binary
        logger.warning("compiled_out: set_define search failed for %s: %s", macro, exc)
        return ""
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
        # A `--disable-x` switch means the feature is ON unless someone turns it off: not
        # established off, whatever its default says.
        if not switches or any(sw.startswith("--disable-") for sw in switches):
            continue
        calls = [_option_call(text, sw) for sw in switches]
        if all(calls) and all(_default_is_off(c) for c in calls):
            return switches[0]
    return ""


def _option_is_default_off(macro, client, channel="nightly", rev=""):
    """``True`` when *macro* comes from a ``moz.configure`` switch that is OFF unless asked for.

    Answers ``False`` for everything it cannot walk end to end, because the cost of a wrong "off"
    is suppressing a real lead. The walk is ``set_define("MACRO", expr)`` -> the ``@depends``
    above ``def expr`` -> ``option("--enable-x")`` with no ``default=``.

    Three things fall off that walk on purpose, and they are the ones that would hurt:
    ``NIGHTLY_BUILD`` and ``EARLY_BETA_OR_EARLIER`` are fed by ``milestone.*`` rather than a
    function, so step one finds no bare name; ``MOZ_DIAGNOSTIC_ASSERT_ENABLED`` DOES have a
    ``set_define`` (moz.configure:174) and is kept out by the bare-identifier regex plus
    :data:`CHANNEL_ON_DENY`, not by its absence; and a switch whose ``default=`` is a
    NON-LITERAL expression (``milestone.is_nightly``, ``depends(...)``) is one we decline to
    evaluate. A LITERAL falsy ``default=`` IS read, and refusing to read it was a real defect:
    it made this function's answer track a moz.configure coding style at the build rev (False at
    14 of 26 panel build revs, True at 12, flipping on an edit that deleted ``default=False,``
    and changed nothing about the build). The dated counter-example and the sweep are above
    :data:`_LITERAL_OFF`; the walk itself now lives in :func:`_default_off_switch`, which also
    hands back the switch NAME for the published reason string.

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
    return bool(_default_off_switch(macro, client, channel, rev))


def mechanism_symbols(mechanism, diff_text="", limit=MAX_DIFF_SYMBOLS):
    """The symbols worth asking about, most-cited first.

    Two sources, because the corpus shows one is not enough. The mechanism's own CITATIONS reach
    the hollow symbol on bug 2063902 (a `diff_line` whose ``content`` is
    ``gc::AutoMarkingLock lock(...)``) but not on 2063782, whose single citation is ordinary code.
    The candidate's DIFF reaches both, ranked by occurrences in changed lines.

    NEITHER SOURCE LICENSES THE WORD "MECHANISM", which is why :func:`statement_provenance`
    exists and why the gate's reason string is split. Replayed over the 52-filing panel: of the
    269 diff-derived symbol slots this function produced, only 35 (13%) are named anywhere in
    the analysis actually published to the bug, and 45 of the 52 filings carry at least one
    diff-derived symbol the prose never mentions. A citation is better and still not proof --
    70 of 83 slots (84%). On the two filings that fire, bug 2063782 reaches
    ``gc::AutoMarkingLock`` only through the diff top-8 (rank #1 of 8, 13 occurrences) and bug
    2063902's "citation" is a ``diff_line`` whose content is a DELETED line of the candidate's
    own patch -- so 0 of 2 reach the symbol through a citation independent of the changeset.

    A THIRD SOURCE WAS TRIED AND IS DEAD, recorded so it is not tried again: feeding
    ``Claim.statement`` in as well adds 87 distinct qualified names across the panel, costs 87
    more searchfox definitions, finds 0 new hollow symbols, and does NOT recover bug 2062114
    (the third owner refutation), whose statement names ``BufferAllocator::TraceEdge`` and
    friends, none of them hollow. ``Claim.statement`` earns its keep as a PROVENANCE test
    instead."""
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


def statement_provenance(symbol, mechanism):
    """``"mechanism"`` when the published mechanism STATEMENT names *symbol*, else ``"diff"``.

    THE PREDICATE THAT LICENSES THE SENTENCE, and it was free the whole time.
    ``_apply_compiled_out_gate`` published "the mechanism rests on `{symbol}`" for whatever
    :func:`mechanism_symbols` handed it -- half of which is the candidate's diff ranked by
    occurrence count, a list the published prose names 13% of the time (35 of 269 slots; 45 of
    52 filings carry at least one it never names). ``Claim.statement`` was already on the
    dossier and nothing read it.

    IT IS A SUBSET OF THE COMMENT, NOT THE WHOLE COMMENT, so read the two numbers apart.
    ``report_bug._explanation_comment`` (report_bug.py:999-1006) renders
    ``mechanism.statement`` AND ``consistency.statement``; this function reads only the first.
    Over the panel that gap is large -- of the 35 diff slots the COMMENT names, only 20 are
    named in ``mechanism.statement`` (7% of 269), and of the 70 citation slots the comment names
    only 43 are (52% of 83). So a symbol the comment DID name can still score ``"diff"`` here,
    42 slots' worth on the panel. That is the safe direction (the weaker sentence), and it is
    the right field: the sentence being licensed is "the MECHANISM rests on X", which the
    consistency paragraph does not assert. Widen it only with a measurement, not a hunch.

    MEASURED ON THE 52-FILING PANEL, 2 hits and 0 false: both filings the gate fires on name
    ``AutoMarkingLock`` verbatim in their statement (2 of 2 -- so on today's evidence the
    corrected sentence is the one that still ships), and no other filing's statement does
    (0 of 50). It is a per-SYMBOL test, not a per-filing one, and the diff wording is not a
    rarity: it is what a future hollow hit gets whenever the prose never mentioned the symbol,
    true of 234 of the 269 diff-derived slots on the panel (87%).

    Matched on the last ``::`` component as well as the whole name, because a statement writes
    ``gc::AutoMarkingLock`` on first use and ``AutoMarkingLock`` afterwards -- and on WORD
    BOUNDARIES, because a plain substring test reads a LONGER identifier as a mention of a
    shorter one. On the panel that is 15 slots and it inflates the diff hit rate from 13% to
    19% with no extra truth in it: bug 2063782's own ``js::jit::AttachBaselineCacheIRStub``
    scores only inside ``AttachBaselineCacheIRStubLocked``, ``ASRKind::Scroll`` only inside
    ``ActiveScrolledRoot``, ``gl::GLContext`` only inside ``WebGLContext``. A boundary does NOT
    stop the qualified case (``Zone`` still matches inside ``JS::shadow::Zone``), which is
    right: ``::`` is not a word character and the prose did name the type."""
    text = (mechanism.get("statement") if isinstance(mechanism, dict)
            else getattr(mechanism, "statement", "")) or ""
    if not isinstance(text, str) or not text:
        return "diff"
    for name in (symbol, (symbol or "").rsplit("::", 1)[-1]):
        if name and re.search(r"\b%s\b" % re.escape(name), text):
            return "mechanism"
    return "diff"


def hollow_symbols(symbols, client=None, channel="nightly", rev=""):
    """``{symbol: {"macro": X, "functions": [...], "switch": "--enable-x"}}`` for each symbol
    that is a no-op in a default build. Never raises: a lookup we cannot make is a symbol we say
    nothing about.

    TWO CLOCKS, ONE QUESTION, AND ONLY ONE OF THEM IS PINNED -- deliberately, and this note is
    the whole reason the next session does not have to re-derive it. The SWITCH is read from the
    ``moz.configure`` of *rev*, the build that crashed (:func:`_default_off_switch`). The
    symbol's BODY comes from ``client.define`` -- searchfox, which indexes ~tip and takes no
    revision flag at all.

    HOLDING THE BODY AT *rev* TOO IS MEASURED AND NOT WORTH THE CODE TODAY. Re-extracting
    ``js/src/gc/Cell.h`` at each of the 52-filing panel's 26 distinct build revs yields
    ``JS_GC_CONCURRENT_MARKING`` -> ``AutoMarkingLock``/``~AutoMarkingLock`` at 26 of 26 --
    byte-identical to the tip answer, 0 disagreements -- so a second extractor (an hg fetch plus
    the class-body slicing searchfox is doing for us) buys nothing measurable.

    WHAT IT WOULD CATCH, said out loud so the null result is not read as a proof: a guard added
    AFTER the build, which makes a symbol that WAS live at build time look hollow now. That is a
    FALSE SUPPRESSION, and a false suppression here is an abstain -- it files nothing, skips the
    second opinion and never reaches ``Feedback`` -- so it is invisible to every outcome
    measurement this repo has. 0-of-26 is how often the tip answer was wrong about ONE macro
    over three weeks, not evidence that the failure mode does not exist."""
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
            source = client.define(symbol, repo=repo_for_channel(channel).value).source or ""
        except Exception:
            continue                               # unknown symbol: say nothing
        for macro in guard_macros(source, channel):
            functions = hollow_functions(source, macro)
            if not functions:
                continue
            if macro not in checked:
                checked[macro] = _default_off_switch(macro, client, channel, rev)
            if checked[macro]:
                found[symbol] = {"macro": macro, "functions": functions,
                                 "switch": checked[macro]}
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
# The nightly off-side regex, kept as a constant because the docstrings name it. `MOZ_ASSERT\w*`
# is deliberately broader than the macro name -- a note says "MOZ_ASSERT" or
# "MOZ_ASSERT_UNREACHABLE" far more often than it says "MOZ_ASSERT_ENABLED" -- so `_off_ground`
# expands that one member rather than escaping it.
_BUILD_TYPE_GROUND = re.compile(r"\b(?:DEBUG|MOZ_ASSERT\w*|RELEASE_OR_BETA)\b")
_OFF_GROUND_PATTERN = {"MOZ_ASSERT_ENABLED": r"MOZ_ASSERT\w*"}
_OFF_GROUND_CACHE = {}


def _off_ground(off):
    """A regex matching any macro in *off*, i.e. "this really is absent from the build"."""
    key = tuple(sorted(off))
    if key not in _OFF_GROUND_CACHE:
        _OFF_GROUND_CACHE[key] = re.compile(
            r"\b(?:%s)\b" % "|".join(_OFF_GROUND_PATTERN.get(m, re.escape(m)) for m in key)
        )
    return _OFF_GROUND_CACHE[key]


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


def is_build_flag_ground(note, citations=(), channel=None):
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
    # A CHANNEL-ON macro is checked BEFORE the build-type veto: `MOZ_DIAGNOSTIC_ASSERT_ENABLED is
    # off` is wrong by construction even when the same note also says `#ifdef DEBUG`.
    #
    # BOTH SETS ARE PER CHANNEL, and this predicate INVERTS between them, which is why no single
    # relaxation could have made it right on beta: a beta note "behind `#ifdef NIGHTLY_BUILD`,
    # not compiled into this build" used to match the nightly ON list, return True, and DISCARD a
    # correct noise-kill; and "this `#ifdef RELEASE_OR_BETA` code is not in the build" used to
    # match the nightly off-side regex, return False, and let a claim that is wrong on beta
    # ABSTAIN a good lead.
    # `channel or build_channel()`, exactly like `channel_on_deny` / `channel_off` /
    # `build_type_deny` / `guard_deny`. `_skeptic_veto` is this function's only caller and it has
    # NO channel to pass -- being a pydantic validator is why the ContextVar exists at all -- so
    # a bare `_partition(channel)` made the ContextVar's single documented consumer read
    # nightly's table for a beta crash, and a correct "behind `#ifdef NIGHTLY_BUILD`" noise-kill
    # went on unbinding while `skeptic_build_flag_unbound` recorded that the rule had worked.
    on, off = _partition(channel or build_channel())
    if any(re.search(r"\b%s\b" % m, text) for m in on):
        return True
    if _off_ground(off).search(text) or _OPT_BUILD_GROUND.search(text):
        return False
    return True
