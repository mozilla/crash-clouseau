# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""What else has this crash been called, and did the old name stop when the new one started?

A Socorro signature is a lossy projection of a stack: skip lists, inlining, template decoration,
symbolication and Socorro-side prefixes all change the string without the crash changing. The
pipeline keys novelty, venue search, spike selection and the volume sentence on the exact string,
and its only rename detector -- the reprocessing inversion in ``sigage.age_facts`` -- fired 3
times in 5968 prod dossiers. Measured over the 150 crash bugs Clouseau filed between 2026-08-05
and 2026-09-18, ABOUT ONE FILING IN EIGHT WAS NAMED FOR A CRASH THAT ALREADY HAD ANOTHER NAME,
and in 17 of the 18 clearest cases an open bug on the old name existed at filing time
(``plans/24-signature-changes.md``). Six mechanisms, each with a human-corrected filing:

* a new infrastructure frame not on Socorro's skip list -- the Chromium sandbox update added
  ``CheckLogMessage::~CheckLogMessage`` and every sandbox ``CHECK()`` moved onto that one name
  (bug 2073210; the old name ``sandbox::InterceptionManager::PatchNtdll`` is frame 4 of our own
  stack, and bug 1737467 had carried it since 2021);
* a fix that moved the abort path -- ``OOM | unknown`` became ``OOM | large`` with a second
  ``crash_impl`` frame (2071620 INVALID, 2071606 DUP, both spike filings; the model itself wrote
  "a renamed form of a longstanding SpiderMonkey OOM abort" and the filer read neither its
  status nor its summary);
* a vendor bump that moved the failure point one frame down (wgpu: 2069647, DUP of 1976766,
  whose name ``WebGPUParent::MapCallback`` is frame 12 of our stack);
* a symbolication gap writing module names into the name (2070554 INVALID, 2069648 and 2061962
  DUPs of our own bugs);
* inlining / template / demangling spellings of one frame set (2072875, 2073159, 2070376);
* a pure function rename (2072770, titled ``[new in release]`` for a crash with 756 reports
  under its old name).

ONE DETERMINISTIC LOOKUP, AT SEED TIME, from data we already hold -- the signature S, the crash's
``proto_signature``, its product, channel and build:

1. **Candidate siblings** (``candidate_siblings``). Two or three SuperSearches in one round-trip,
   product-wide across every channel: ``signature=~<frame>`` for up to three SPECIFIC frames of
   S; ``proto_signature=~<frame>`` for the first three specific frames of the proto that are NOT
   in S (the callers the new name pushed off the string); and the generated spellings (the
   ``OOM | unknown/large/small`` kinds, the un-prefixed form, the lambda demanglings) exactly.
   A frame is specific when it is not a module name, a bare address, a frame on Socorro's own
   irrelevant/prefix lists (vendored under ``config/siglists``, matched the way siggen matches
   them), a Rust std frame, a JIT trampoline or one of the generic abort/OOM/hang words.
2. **Relation** (``relate``). ``frame-variant``: at least one shared specific frame and a
   frame-list edit distance of at most two -- a frame renamed, inserted or dropped.
   ``pushed-down``: the candidate's one or two specific frames are all still in S's proto (the
   WHOLE proto, not a prefix -- wgpu's old name is frame 12): a new frame took over the string.
   ``spelling``: one of the generated spellings. A hang (``shutdownhang``) never relates to a
   non-hang and vice versa: that prefix is a crash KIND. An ``AsyncShutdownTimeout`` signature
   has no family at all -- its "frames" are blocker names, and variants are different hangs
   (bug 2067456, the one-blocker/two-blocker venue over-match).
3. **Classification** (``classify``), BY BUILD, ON THE CRASH'S CHANNEL, against S's first build
   there. Old builds keep reporting the old name for days after a rename lands (on 2071620,
   every date-keyed "after" report of the old name was an old build), so a date-keyed test
   misreads a rename as coexistence. A predecessor is a **handoff** when it had at least five
   reports on the builds of the 28 days before S's first build and, on builds from S's first
   on, at most a tenth of what its prior rate predicts for the time elapsed -- and that
   shortfall is SIGNIFICANT: the Poisson probability of seeing so few, were the name still
   reporting at its prior rate, is under 5% (``_HANDOFF_P_VALUE``). So a quiet name is never
   declared dead an hour after a new build (`undecided` instead), while a name at ten a day is
   ten hours after it (wgpu's `MapCallback`, bug 2069647: 281 reports before, none on the new
   build ten hours in, p = 1.4%). Anything else live on both sides is a **coexisting**
   sibling, one that predates S an **older** one; neither moves a clock. When a predecessor
   SHARES a specific frame with S (a spelling or a frame variant), a pushed-down predecessor
   sharing none is a sibling name's predecessor, not S's, and is dropped: the JS OOM change
   renamed two abort sites on one build, and each new name has exactly one old one.
4. **Two discriminators**, recorded because they change what the model may claim. ALIGNMENT:
   a code rename hands off at a build boundary and the old name keeps reporting on old builds
   afterwards (``build``); a skip-list change or a symbol gap hands off on a DATE, on every live
   build at once (``date``), and no changeset can be the cause of that. Off nightly only, where
   old builds live long enough to be seen; ``unknown`` otherwise. FAN-IN: one new name absorbing
   two or more predecessors with different frames below it is a catch-all minted by a generic
   frame (CheckLogMessage took every sandbox CHECK) -- the right action is a skip-list change.

NEVER RAISES, and a failed lookup is a fact, not an absence: ``lookup`` says ``failed`` and the
lists are empty, so no consumer may read "we could not ask" as "no predecessor". Cached per
(signature, channel, day): the seed, the spike sweep and the filer all ask about the same crash.

Kill switch only (``agent.signature_family.enabled`` / ``SIGNATURE_FAMILY_ENABLED``): this is
an external Socorro dependency, which is the one case a switch is for.
"""
import math
import os
import re
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from libmozdata import socorro
from libmozdata.connection import Query

from crashclouseau import config, sigage, utils
from crashclouseau.logger import logger

# Socorro's own skip lists, vendored (``bin/refresh_siglists.py``). siggen joins every line of a
# list into one alternation and uses ``re.match`` on the frame's normalised function name --
# ANCHORED AT THE START, NO ``$``: ``std::panicking::`` is a prefix entry, and
# ``core::ops::function::FnOnce::call_once<T>`` is listed while the un-templated ``call_once``
# is not, so both are matched by prefix here too.
SIGLISTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "config",
                            "siglists")
IRRELEVANT_SIGLIST = os.path.join(SIGLISTS_DIR, "irrelevant_signature_re.txt")
PREFIX_SIGLIST = os.path.join(SIGLISTS_DIR, "prefix_signature_re.txt")

# How many reports a predecessor must have had on the builds of the 28 days before S's first
# build to be a predecessor at all: fewer is not a name that "stopped", it is a name that was
# hardly there.
_MIN_BEFORE = 5
# Over how many days before S's first build the predecessor's prior rate is read.
_BEFORE_DAYS = 28
# How unlikely the predecessor's shortfall must be, were it still reporting at its prior rate,
# before its absence is evidence: the Poisson probability of at most the observed count given
# the predicted one. At 0.05, zero reports are decisive once three are predicted (p = e^-3);
# a name at 10/day is decisive seven hours after the build, one at 6 reports a month only
# after two weeks -- until then it is `undecided`, never a handoff.
_HANDOFF_P_VALUE = 0.05
# The share of the predicted reports the predecessor may still produce and be called handed
# off: the significance test alone would call a 10% dip on a loud name a handoff. The floor
# under it only shapes `undecided` (two stray reports on a new build are not a survival).
_HANDOFF_SHARE = 0.10
_HANDOFF_FLOOR = 2
# Live on both sides of S's first build needs this many reports on each.
_MIN_COEXIST = 3
# Days after the retention wall inside which S's own first report is "at the wall": S is older
# than Socorro's index and its first build is unknowable, so no handoff can be claimed. Wide
# enough to cover one WEEKLY index (`socorroYYYYWW`) that has already been dropped: crash-stats
# answers a range whose oldest week is gone with a `missing_index` entry in `errors` and the
# facets over the weeks it still has (live replay, 2026-09-19).
_WALL_MARGIN_DAYS = 10
# Alignment is only readable once old builds have had time to report after the handoff.
_MIN_ALIGNMENT_DAYS = 3
# Anchors per source, candidates classified per lookup, facet size of the discovery queries.
_MAX_ANCHORS = 3
_MAX_CANDIDATES = 8
_DISCOVERY_FACETS = 50
_BUILD_FACETS = 1000
_CACHE_SIZE = 256


# --------------------------------------------------------------------------------------------
# Frames
# --------------------------------------------------------------------------------------------
_ADDRESS_RE = re.compile(r"^@?0x[0-9a-fA-F]+$")
# A bare MODULE name: `ntdll.dll`, `libxul.so (deleted)`, `libwayland-client.so.0`,
# `<unknown in amdxx64.dll>`, `libxul.so@0x1234`. Socorro writes one into a signature only when
# it has no symbols for that frame's module.
_MODULE_RE = re.compile(
    r"^(?:<unknown in )?[\w.+-]+\.(?:dll|so(?:\.\d+)*|dylib|exe|sys|drv|ocx|cpl|framework)"
    r"(?:@0x[0-9a-fA-F]+)?(?: \(deleted\))?>?$", re.I)
# Rust's std/core/alloc and the panic runtime's bare symbols (`rust_begin_unwind`, `rust_panic`,
# `__rust_start_panic`): Socorro lists the namespaced machinery and not the bare ones.
_STD_RE = re.compile(r"^(?:<\s*)?(?:core|alloc|std)::|^<T as (?:core|alloc|std)::"
                     r"|^<(?:alloc|core|std)::|^(?:__)?rust_\w+$")
# The JIT's entry frames: every JS stack passes through them, so a shared one identifies
# nothing (the `EnterJit` false flag of the prototype).
_TRAMPOLINE_RE = re.compile(
    r"^(?:js::jit::)?(?:EnterJit|EnterBaseline\w*|MaybeEnterJit|MaybeEnterInterpreterTrampoline"
    r"|EnterInterpreterTrampoline)$"
    r"|^js::(?:Interpret|InternalCall|InternalCallOrConstruct|RunScript|Call|CallFromStack)$"
    r"|^(?:Interpret|InternalCall)$")
# The words Socorro or the crash reporter put in a name that are not frames: crash kinds, OOM
# size classes, the abort machinery. Not identity, whatever they share.
GENERIC_FRAMES = frozenset({
    "OOM", "large", "unknown", "small", "shutdownhang", "AsyncShutdownTimeout", "stackoverflow",
    "IPCError-browser", "IPCError-content", "ShutDownKill", "NS_ABORT_OOM", "abort", "Abort",
    "abort_with_suppression", "MOZ_Crash", "MOZ_CrashSequence", "MOZ_CrashOOL", "MOZ_CrashPrintf",
    "NS_PrintStackTrace", "NS_DebugBreak", "mozilla::ipc::FatalError",
    "mozilla::ipc::IProtocol::HandleFatalError", "IPC::MessageReader::FatalError",
    "mozilla::detail::InvalidArrayIndex_CRASH", "InvalidArrayIndex_CRASH",
})
_GENERIC_PREFIX_RE = re.compile(r"^js::AutoEnterOOMUnsafeRegion::crash")
# OS lock / wait / heap / memcpy primitives. Still SPECIFIC for the family (they are what
# identifies a catch-all predecessor such as `shutdownhang | RtlWaitOnAddress | WaitOnAddress`),
# but not SYMBOLS for `is_unsymbolicated`: a name made only of these and module names cannot be
# searched, blamed or deduplicated (`xul.dll | _PR_MD_UNLOCK | PR_Unlock | xul.dll`, bug
# 2061962, a DUP of our own 2061960).
_PRIMITIVE_RE = re.compile(
    r"^(?:_?PR_(?:MD_)?(?:UN)?LOCK|PR_(?:Un)?[Ll]ock|PR_Wait\w*|PR_(?:Enter|Exit)Monitor"
    r"|pthread_(?:mutex|cond|rwlock)_\w+|_?_pthread_\w+|__lll_lock_wait\w*|__psynch_\w+"
    r"|__ulock_wait\w*|mach_msg\w*|semaphore_wait\w*|futex_wait\w*|SYS_futex|syscall"
    r"|__kernel_vsyscall|__select|__poll|poll|select|nanosleep|__nanosleep"
    r"|Rtlp?(?:Enter|Leave|Try)CriticalSection\w*|Rtlp?WaitOn\w+|WaitOnAddress|(?:Nt|Zw)Wait\w+"
    r"|WaitFor(?:Single|Multiple)Object\w*|Sleep(?:Ex)?|Rtlp?SleepConditionVariable\w*"
    r"|SleepConditionVariable\w*|Rtlp?(?:Allocate|Free|ReAllocate|LowFragHeapAllocFromZone)"
    r"\w*Heap\w*|Rtlp\w*Heap\w*|HeapAlloc|HeapFree|(?:__libc_)?(?:malloc|free|calloc|realloc"
    r"|memalign|posix_memalign)|__(?:memcpy|memmove|memset|memcmp)\w*"
    r"|mem(?:cpy|move|set|cmp)(?:_\w+)?|__aeabi_\w+|__libc_\w+)$")

_HANG_PREFIX = "shutdownhang"
_OVERFLOW_PREFIX = "stackoverflow"
_OOM_KINDS = ("unknown", "large", "small")


def _load_siglist(path):
    """The compiled entries of one Socorro skip list, siggen's way (start-anchored prefixes).
    A line that is not a valid regex is skipped, as siggen's loader would refuse it."""
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    out.append(re.compile("^(?:" + line + ")"))
                except re.error:
                    continue
    except OSError as exc:  # pragma: no cover - a missing vendored file
        logger.warning("sigfamily: cannot read skip list %s: %s", path, exc)
    return tuple(out)


@lru_cache(maxsize=None)
def skip_patterns():
    """Socorro's irrelevant AND prefix lists, compiled once."""
    return _load_siglist(IRRELEVANT_SIGLIST) + _load_siglist(PREFIX_SIGLIST)


def socorro_skips(frame):
    """Would siggen skip *frame* when building a signature (either list)?"""
    return any(p.match(frame) for p in skip_patterns())


def frames(signature):
    """The ``|``-separated parts of *signature*, stripped, empties dropped."""
    return [p.strip() for p in str(signature or "").split(" | ") if p.strip()]


def is_module_frame(frame):
    return bool(_MODULE_RE.match(frame or ""))


def is_generic_frame(frame):
    return frame in GENERIC_FRAMES or bool(_GENERIC_PREFIX_RE.match(frame or ""))


def is_primitive_frame(frame):
    return bool(_PRIMITIVE_RE.match(frame or ""))


def specific_frame(frame):
    """Does *frame* identify a crash site? False for a module name, a bare address, a frame on
    Socorro's own skip lists, a Rust std frame, a JIT trampoline or a generic crash word."""
    f = (frame or "").strip()
    if len(f) < 3 or _ADDRESS_RE.match(f) or is_module_frame(f):
        return False
    if is_generic_frame(f) or _STD_RE.match(f) or _TRAMPOLINE_RE.match(f):
        return False
    return not socorro_skips(f)


def symbol_frames(signature):
    """The parts of *signature* that are symbols of SOMETHING -- not a module name, an address,
    a generic crash word or an OS primitive. Socorro's skip lists are deliberately NOT applied:
    a prefix-listed frame standing alone (`mozilla::detail::MutexImpl::mutexLock`,
    `IPC::ParamTraits<T>::Read`) is still a symbol somebody can search for."""
    out = []
    for f in frames(signature):
        if _ADDRESS_RE.match(f) or is_module_frame(f) or is_generic_frame(f):
            continue
        if is_primitive_frame(f):
            continue
        out.append(f)
    return out


def is_unsymbolicated(signature):
    """True when nothing in *signature* names code: every part is a bare address, a module name,
    a generic crash word or an OS lock/heap primitive. Such a name cannot be searched, blamed or
    deduplicated -- `libxul.so (deleted) | ... | libnspr4.so (deleted)` (2069648), `xul.dll |
    _PR_MD_UNLOCK | PR_Unlock | xul.dll` (2061962), `@0xe2ba40f948`. A partly symbolicated name
    (`OOM | unknown | memcpy_repmovs_Intel | mozilla::dom::RTCEncodedFrameBase::...`) is not."""
    return bool(frames(signature)) and not symbol_frames(signature)


_TEMPLATE_RE = re.compile(r"<[^<>]*>")
_IMPL_RE = re.compile(r"impl\$\d+")
_HASH_RE = re.compile(r"\$[0-9a-f]{6,}")
_RUST_TRAIT_RE = re.compile(r"^<(?P<type>[^<>]+?) as (?P<trait>[^<>]+)>::(?P<method>.+)$")


def normalize_frame(frame):
    """One spelling for every way a toolchain can write a frame: template arguments stripped
    (nested), lambda demanglings folded (``utils.lambda_family``), MSVC's ``impl$N`` and the
    Rust trait spelling ``<X<..> as Trait>::m`` both reduced to the module path + method, so
    ``style_traits::owned_slice::impl$1::drop`` (Windows) and ``<style_traits::owned_slice::
    OwnedSlice<T> as core::ops::drop::Drop>::drop`` (Linux) compare equal."""
    f = utils.lambda_family((frame or "").strip())
    prev = None
    while prev != f:
        prev = f
        f = _TEMPLATE_RE.sub("<>", f)
    f = f.replace("::<>", "").replace("<>", "")
    m = _RUST_TRAIT_RE.match(f)
    if m:
        path = m.group("type").split("::")
        module = "::".join(path[:-1]) if len(path) > 1 else path[0]
        f = "{}::{}".format(module, m.group("method"))
    f = _IMPL_RE.sub("impl$", f)
    f = f.replace("::impl$::", "::")
    f = _HASH_RE.sub("$", f)
    return f


def normalized_frames(signature):
    return [normalize_frame(f) for f in frames(signature)]


def specific_frames(signature):
    """The specific frames of *signature*, in order, in their ORIGINAL spelling."""
    return [f for f in frames(signature) if specific_frame(f)]


def _kind(signature):
    """The crash KIND a name carries in its first part -- `shutdownhang`, `stackoverflow`,
    `IPCError-*` -- or ``""``. Two names of different kinds are different crashes whatever
    frames they share."""
    fr = frames(signature)
    if not fr:
        return ""
    head = fr[0]
    if head in (_HANG_PREFIX, _OVERFLOW_PREFIX) or head.startswith("IPCError-"):
        return head
    return ""


def family_eligible(signature):
    """May *signature* have a family at all? Not an ``AsyncShutdownTimeout`` (blocker names are
    not frames), and not an empty name."""
    fr = frames(signature)
    return bool(fr) and fr[0] != "AsyncShutdownTimeout"


def generated_spellings(signature):
    """The spellings Socorro itself can give this crash: the three OOM size classes and the
    un-prefixed form of an ``OOM | <kind> | ...`` name (the OOM annotation is heuristic -- the
    2-frame `memcpy_repmovs_Intel | RTCEncodedFrameBase` ran the week before `OOM | unknown |`
    was prepended to it, bug 2060920), and every lambda demangling. Itself excluded."""
    sig = (signature or "").strip()
    out = set()
    fr = frames(sig)
    if len(fr) >= 3 and fr[0] == "OOM" and fr[1] in _OOM_KINDS:
        rest = " | ".join(fr[2:])
        for kind in _OOM_KINDS:
            out.add("OOM | {} | {}".format(kind, rest))
        out.add(rest)
    for s in list(out) + [sig]:
        out |= utils.lambda_siblings(s)
    out.discard(sig)
    out.discard("")
    return out


def _poisson_cdf(k, lam):
    """P(X <= k) for X ~ Poisson(lam). Summed directly: k is at most a tenth of lam here."""
    if lam <= 0:
        return 1.0
    term = math.exp(-lam)
    total = term
    for i in range(1, int(k) + 1):
        term *= lam / i
        total += term
    return min(1.0, total)


def _edit_distance(a, b):
    n, m = len(a), len(b)
    prev = list(range(m + 1))
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        for j in range(1, m + 1):
            same = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + same)
        prev = cur
    return prev[m]


def relate(signature, candidate, proto=None):
    """How *candidate* is tied to *signature*: ``"frame-variant"``, ``"pushed-down"``,
    ``"spelling"`` or ``None``. See the module docstring for the three."""
    sig = (signature or "").strip()
    cand = (candidate or "").strip()
    if not sig or not cand or sig == cand or not family_eligible(sig) or not family_eligible(cand):
        return None
    if _kind(sig) != _kind(cand):
        return None
    if cand in generated_spellings(sig):
        return "spelling"
    s_norm = normalized_frames(sig)
    c_norm = normalized_frames(cand)
    s_spec = [normalize_frame(f) for f in specific_frames(sig)]
    c_spec = [normalize_frame(f) for f in specific_frames(cand)]
    if not c_spec:
        return None
    shared = set(s_spec) & set(c_spec)
    if shared and (len(s_norm) > 1 or len(c_norm) > 1) and _edit_distance(s_norm, c_norm) <= 2:
        return "frame-variant"
    if not shared and len(c_spec) <= 2 and proto:
        proto_spec = {normalize_frame(f) for f in specific_frames(proto)}
        if all(f in proto_spec for f in c_spec):
            return "pushed-down"
    return None


def changed_frames(signature, predecessor):
    """``{"added": [...], "removed": [...]}`` -- the frames of *signature* not in *predecessor*
    and vice versa, in their original spellings (an OOM size class counts: `unknown` -> `large`
    IS the change on 2071606)."""
    s_fr = frames(signature)
    p_fr = frames(predecessor)
    s_norm = {normalize_frame(f) for f in s_fr}
    p_norm = {normalize_frame(f) for f in p_fr}
    added = [f for f in s_fr if normalize_frame(f) not in p_norm]
    removed = [f for f in p_fr if normalize_frame(f) not in s_norm]
    return {"added": added, "removed": removed}


def describe_change(signature, predecessor, relation=None):
    """One clause saying what changed between the two names, for a prompt or a bug:
    ``frame `X` became `Y```, ``a new frame `Y` took over the name from `X```, ..."""
    ch = changed_frames(signature, predecessor)
    added, removed = ch["added"], ch["removed"]
    fmt = lambda xs: ", ".join("`{}`".format(x) for x in xs)  # noqa: E731
    if added and removed and all(f in _OOM_KINDS for f in added + removed):
        return "the OOM size class {} became {}".format(fmt(removed), fmt(added))
    if relation == "pushed-down" and added and removed:
        return "the new frame{} {} took over the name from {}".format(
            "s" if len(added) > 1 else "", fmt(added), fmt(removed))
    if len(added) == 1 and len(removed) == 1:
        return "frame {} became {}".format(fmt(removed), fmt(added))
    if added and removed:
        return "frame{} {} replaced {}".format("s" if len(added) > 1 else "", fmt(added),
                                               fmt(removed))
    if added:
        return "frame{} {} {} added to the name".format(
            "s" if len(added) > 1 else "", fmt(added), "were" if len(added) > 1 else "was")
    if removed:
        return "frame{} {} {} dropped from the name".format(
            "s" if len(removed) > 1 else "", fmt(removed), "were" if len(removed) > 1 else "was")
    return "the two names spell the same frames differently"


# --------------------------------------------------------------------------------------------
# Socorro
# --------------------------------------------------------------------------------------------
def _query(params, got, key):
    """One batched SuperSearch. The whole answer is kept, ``errors`` included: a `missing_index`
    error (the oldest weekly index of the range is gone) comes WITH the facets over the weeks
    crash-stats still has, and reading it as a failure threw away every answer on the first
    live replay. ``_usable`` is the test the consumers apply."""
    def handler(json_, data):
        data[key] = json_
    return Query(socorro.SuperSearch.URL, params=params, handler=handler, handlerdata=got)


def _usable(result):
    """Did this SuperSearch answer with facets? An error list beside them is noted, not fatal."""
    if not isinstance(result, dict) or not isinstance(result.get("facets"), dict):
        return False
    if result.get("errors"):
        logger.debug("sigfamily: SuperSearch answered with errors %s (facets kept)",
                     result["errors"])
    return True


def _run(queries):
    """One round-trip for the whole batch. Raises on a transport failure; the caller turns that
    into ``lookup: failed``. Patched by the tests."""
    if queries:
        socorro.SuperSearch(queries=queries).wait()


def _day(dt_):
    return dt_.strftime("%Y-%m-%d")


def _date_range(since, until):
    return [">=" + _day(since), "<" + _day(until + timedelta(days=1))]


def _facet_terms(result, facet):
    out = {}
    for row in ((result or {}).get("facets") or {}).get(facet) or []:
        term = row.get("term")
        if term is None:
            continue
        out[str(term)] = out.get(str(term), 0) + int(row.get("count") or 0)
    return out


def _anchors(signature, proto):
    """``(signature_anchors, proto_anchors)``: the specific frames of S (up to three) and the
    first three specific frames of the proto that are not in S, in stack order, original
    spellings -- so a Rust panic's `std::panicking` machinery never becomes an anchor."""
    sig_anchors = specific_frames(signature)[:_MAX_ANCHORS]
    in_sig = {normalize_frame(f) for f in frames(signature)}
    proto_anchors = []
    for f in frames(proto or ""):
        if normalize_frame(f) in in_sig or not specific_frame(f) or f in proto_anchors:
            continue
        proto_anchors.append(f)
        if len(proto_anchors) >= _MAX_ANCHORS:
            break
    return sig_anchors, proto_anchors


def candidate_siblings(signature, proto, product, since, until):
    """``{candidate_signature: total_reports_all_channels}`` for every signature Socorro holds
    that could be another name of this crash, plus the generated spellings' counts. Raises on a
    failed round-trip. Product-wide and across every channel: BitSet's sibling lived on another
    channel (2070376)."""
    sig_anchors, proto_anchors = _anchors(signature, proto)
    exact = sorted(generated_spellings(signature))
    if not sig_anchors and not proto_anchors and not exact:
        return {}
    common = {"product": product or "Firefox", "date": _date_range(since, until),
              "_results_number": 0, "_facets": "signature", "_facets_size": _DISCOVERY_FACETS}
    got = {}
    queries = []
    for i, anchor in enumerate(sig_anchors):
        queries.append(_query({**common, "signature": "~" + anchor}, got, "sig:{}".format(i)))
    for i, anchor in enumerate(proto_anchors):
        queries.append(_query({**common, "proto_signature": "~" + anchor}, got,
                              "proto:{}".format(i)))
    if exact:
        queries.append(_query({**common, "signature": ["=" + s for s in exact]}, got,
                              "spellings"))
    _run(queries)
    found = {}
    for key, result in got.items():
        if not _usable(result):
            logger.warning("sigfamily: discovery query %s for %r answered %s", key, signature,
                           (result or {}).get("errors") if isinstance(result, dict) else result)
            continue
        for term, count in _facet_terms(result, "signature").items():
            if term != signature:
                found[term] = max(found.get(term, 0), count)
    return found


def _build_params(signature, product, channel, since, until):
    params = {
        "signature": "=" + signature,
        "product": product or "Firefox",
        "date": _date_range(since, until),
        "_results_number": 0,
        "_facets": "build_id",
        "_facets_size": _BUILD_FACETS,
        "_histogram.date": "release_channel",
        "_histogram.interval": "1d",
    }
    if channel:
        params["release_channel"] = utils.get_search_channel(channel)
    return params


def _builds_and_days(result):
    """``(sorted [(buildid, count)], {day: count})`` off one build-facet + date-histogram."""
    builds = sorted((b, n) for b, n in _facet_terms(result, "build_id").items() if b.isdigit())
    days = {}
    for row in ((result or {}).get("facets") or {}).get("histogram_date") or []:
        day = str(row.get("term") or "")[:10]
        if day:
            days[day] = days.get(day, 0) + int(row.get("count") or 0)
    return builds, days


def classify(signature, candidates, product, channel, since, until, proto=None):
    """The timeline test for every candidate, on the crash's channel, by build. Returns
    ``(s_first_build, at_wall, rows)`` where each row carries the candidate's relation and the
    numbers the decision used (``before``, ``after``, ``after_dates``, ``expected_after``,
    ``first_build``, ``status``, ``alignment``). Raises on a failed round-trip."""
    got = {}
    queries = [_query(_build_params(signature, product, channel, since, until), got, "S")]
    for i, cand in enumerate(candidates):
        queries.append(_query(_build_params(cand, product, channel, since, until), got,
                              "P:{}".format(i)))
    _run(queries)
    s_result = got.get("S")
    if not _usable(s_result):
        raise RuntimeError("build history of {!r} unreadable: {}".format(
            signature, (s_result or {}).get("errors") if isinstance(s_result, dict) else s_result))
    s_builds, _s_days = _builds_and_days(s_result)
    if not s_builds:
        return None, False, []
    s_first_build = s_builds[0][0]
    s_first_dt = sigage._buildid_to_dt(s_first_build)
    if s_first_dt is None:
        return None, False, []
    at_wall = s_first_dt <= since + timedelta(days=_WALL_MARGIN_DAYS)
    elapsed_days = max((until - s_first_dt).total_seconds() / 86400.0, 0.01)
    s_after = sum(n for b, n in s_builds if b >= s_first_build)
    family = config.channel_family(channel) if channel else None
    rows = []
    for i, cand in enumerate(candidates):
        result = got.get("P:{}".format(i))
        if not _usable(result):
            logger.warning("sigfamily: build history of %r unreadable: %s", cand,
                           (result or {}).get("errors") if isinstance(result, dict) else result)
            continue
        builds, days = _builds_and_days(result)
        window_start = s_first_dt - timedelta(days=_BEFORE_DAYS)
        before = 0
        for b, n in builds:
            if b >= s_first_build:
                continue
            b_dt = sigage._buildid_to_dt(b)
            if b_dt is not None and b_dt >= window_start:
                before += n
        after = sum(n for b, n in builds if b >= s_first_build)
        after_dates = sum(n for d, n in days.items() if d >= _day(s_first_dt))
        total = sum(n for _b, n in builds)
        first_build = builds[0][0] if builds else None
        rate = before / float(_BEFORE_DAYS)
        expected_after = rate * elapsed_days
        p_value = _poisson_cdf(after, expected_after)
        decisive = p_value <= _HANDOFF_P_VALUE
        threshold = max(_HANDOFF_FLOOR, _HANDOFF_SHARE * expected_after)
        quiet_after = after <= _HANDOFF_SHARE * expected_after
        handoff = (not at_wall and before >= _MIN_BEFORE and quiet_after and decisive)
        older = bool(first_build) and first_build < s_first_build
        if handoff:
            status = "handoff"
        elif before >= _MIN_BEFORE and after <= threshold and not at_wall:
            status = "undecided"
        elif before >= _MIN_COEXIST and after >= _MIN_COEXIST:
            status = "coexisting"
        elif total == 0:
            status = "other_channel"
        elif older and before >= 1:
            status = "older"
        elif after >= 1 and not older:
            status = "younger"
        else:
            status = "quiet"
        alignment = None
        if handoff:
            alignment = "unknown"
            if elapsed_days >= _MIN_ALIGNMENT_DAYS and family in ("release", "beta", "esr"):
                still_on_old_builds = after_dates > max(_HANDOFF_FLOOR, _HANDOFF_SHARE * before)
                alignment = "build" if still_on_old_builds else "date"
        rows.append({
            "signature": cand,
            "relation": relate(signature, cand, proto),
            "status": status,
            "before": before,
            "after": after,
            "after_dates": after_dates,
            "expected_after": round(expected_after, 1),
            "p_value": round(p_value, 4),
            "total_channel": total,
            "first_build": first_build,
            "older": older,
            "alignment": alignment,
            "s_after": s_after,
        })
    return s_first_build, at_wall, rows


def _fan_in(predecessors):
    """How many DISTINCT predecessors handed off to S: predecessors sharing a specific frame are
    spellings of one, so the four `WaitOnAddress` names of 2070554 count once."""
    groups = []
    for p in predecessors:
        spec = {normalize_frame(f) for f in specific_frames(p["signature"])}
        for g in groups:
            if g & spec:
                g |= spec
                break
        else:
            groups.append(set(spec))
    return len(groups)


def _empty(signature, product, channel, status):
    return {"signature": signature, "product": product, "channel": channel, "lookup": status,
            "s_first_build": None, "at_wall": False, "predecessors": [], "siblings": [],
            "family_first_seen_ever": None, "family_first_seen_ever_date": None,
            "alignment": None, "fan_in": 0, "changed_frames": None}


_CACHE = OrderedDict()


def _cache_key(signature, proto, product, channel, until):
    return (signature, proto or "", product or "", channel or "", _day(until))


def lookup(signature, proto=None, product="Firefox", channel="nightly", buildid=None, until=None):
    """Everything the pipeline needs to know about this crash's other names, or an empty answer
    that says why (``lookup``: ``ok``, ``no_candidates``, ``no_history``, ``ineligible``,
    ``disabled``, ``failed``). Never raises.

    ``predecessors`` are the names this crash was called before S and that STOPPED when S
    started (handoff), loudest first; ``siblings`` are the other spellings still live, older or
    on another channel. ``family_first_seen_ever`` is the oldest ``SignatureFirstDate`` over S
    and its handoff predecessors -- the crash's age, as opposed to the name's.
    ``changed_frames`` and ``alignment`` describe the loudest predecessor; ``fan_in`` counts
    distinct predecessors (two or more is a catch-all).

    ``until`` (a UTC datetime) is the moment the question is asked, for replays; default now."""
    sig = (signature or "").strip()
    cfg = config.get_agent_signature_family()
    until = until or datetime.now(timezone.utc)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if not cfg["enabled"]:
        return _empty(sig, product, channel, "disabled")
    if not sig or not family_eligible(sig):
        return _empty(sig, product, channel, "ineligible")
    key = _cache_key(sig, proto, product, channel, until)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    out = _lookup(sig, proto, product, channel, cfg, until)
    if out.get("lookup") != "failed":
        _CACHE[key] = out
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    return out


def _lookup(sig, proto, product, channel, cfg, until):
    since = until - timedelta(days=int(cfg["days"]))
    try:
        found = candidate_siblings(sig, proto, product, since, until)
    except Exception as exc:  # pragma: no cover - network
        logger.warning("sigfamily: sibling discovery failed for %r: %s", sig, exc)
        return _empty(sig, product, channel, "failed")
    related = []
    for cand, total in found.items():
        rel = relate(sig, cand, proto)
        if rel:
            related.append((cand, rel, total))
    if not related:
        return dict(_empty(sig, product, channel, "no_candidates"))
    related.sort(key=lambda r: -r[2])
    candidates = [c for c, _r, _t in related[: int(cfg["max_candidates"])]]
    totals_all = {c: t for c, _r, t in related}
    try:
        s_first_build, at_wall, rows = classify(sig, candidates, product, channel, since, until,
                                                proto=proto)
    except Exception as exc:  # pragma: no cover - network
        logger.warning("sigfamily: classification failed for %r: %s", sig, exc)
        return _empty(sig, product, channel, "failed")
    if s_first_build is None:
        out = _empty(sig, product, channel, "no_history")
        out["siblings"] = [{"signature": c, "relation": r, "status": "unclassified",
                            "total_all_channels": t} for c, r, t in related]
        return out
    predecessors = [r for r in rows if r["status"] == "handoff"]
    # A predecessor that shares a specific frame with S (a spelling, a frame variant) is S's
    # own old name; beside one, a pushed-down predecessor sharing no frame with S or with it is
    # the old name of a SIBLING that appeared on the same build (the JS OOM change renamed two
    # abort sites at once), not a second predecessor of S.
    sharing = [r for r in predecessors if r["relation"] in ("frame-variant", "spelling")]
    if sharing:
        keep = {normalize_frame(f) for f in specific_frames(sig)}
        for r in sharing:
            keep |= {normalize_frame(f) for f in specific_frames(r["signature"])}

        def own_or_shared(r):
            spec = {normalize_frame(f) for f in specific_frames(r["signature"])}
            return r in sharing or bool(keep & spec)

        predecessors = [r for r in predecessors if own_or_shared(r)]
    predecessors.sort(key=lambda r: -r["before"])
    siblings = [dict(r, total_all_channels=totals_all.get(r["signature"], 0))
                for r in rows if r["status"] not in ("handoff", "quiet")]
    for row in predecessors:
        row["changed_frames"] = changed_frames(sig, row["signature"])
        row["change"] = describe_change(sig, row["signature"], row["relation"])
    ever = {}
    if predecessors:
        try:
            ever = sigage.first_seen_ever_facts([sig] + [p["signature"] for p in predecessors])
        except Exception as exc:  # pragma: no cover - the callee already swallows
            logger.warning("sigfamily: SignatureFirstDate failed for %r: %s", sig, exc)
            ever = {}
    for row in predecessors:
        facts = ever.get(row["signature"]) or {}
        row["first_seen_ever"] = facts.get("first_build")
        row["first_seen_ever_date"] = facts.get("first_date")
    family_builds = [f["first_build"] for f in ever.values() if f.get("first_build")]
    family_first = min(family_builds) if family_builds else None
    family_first_date = None
    if family_first:
        family_first_date = next((f.get("first_date") for f in ever.values()
                                  if f.get("first_build") == family_first), None)
    top = predecessors[0] if predecessors else None
    return {
        "signature": sig, "product": product, "channel": channel, "lookup": "ok",
        "s_first_build": s_first_build, "at_wall": at_wall,
        "predecessors": predecessors, "siblings": siblings,
        "family_first_seen_ever": family_first,
        "family_first_seen_ever_date": family_first_date,
        "alignment": top["alignment"] if top else None,
        "fan_in": _fan_in(predecessors),
        "changed_frames": top["changed_frames"] if top else None,
    }


def handoff_for_spike(signature, proto, product, channel, buildid, until=None):
    """The predecessor this SPIKE is the re-bucketing of, or ``None``: S first appears on the
    channel on the spiking build's own day and a handoff predecessor lost what S gained. The
    pre-LLM decline that would have stopped 2071620 and 2071606 for $0."""
    if not buildid:
        return None
    fam = lookup(signature, proto, product, channel, buildid, until=until)
    if not fam["predecessors"] or not fam.get("s_first_build"):
        return None
    if sigage.buildid_day(fam["s_first_build"]) != sigage.buildid_day(buildid):
        return None
    return dict(fam["predecessors"][0], s_first_build=fam["s_first_build"],
                alignment=fam.get("alignment"), fan_in=fam.get("fan_in"))


def spellings(family):
    """Every other name of the crash a *family* (``lookup``'s dict, or the seed/corroboration
    facts) knows: handoff predecessors first, then live siblings. Empty for no family."""
    out = []
    fam = family or {}
    for row in fam.get("predecessors") or []:
        s = row.get("signature") if isinstance(row, dict) else row
        if s and s not in out:
            out.append(s)
    for row in fam.get("siblings") or []:
        s = row.get("signature") if isinstance(row, dict) else row
        if s and s not in out:
            out.append(s)
    return out


def seed_facts(family):
    """The seed keys ``orchestrator.build_seed`` carries for the prompt and the gate, off one
    ``lookup`` answer. Literal keys, one place, so the seed and the tests agree on the names."""
    fam = family or {}
    return {
        "signature_predecessors": fam.get("predecessors") or [],
        "signature_siblings": fam.get("siblings") or [],
        "signature_family_first_seen_ever": fam.get("family_first_seen_ever"),
        "signature_handoff_build": fam.get("s_first_build") if fam.get("predecessors") else None,
        "signature_handoff_alignment": fam.get("alignment"),
        "signature_fan_in": fam.get("fan_in") or 0,
        "signature_family_lookup": fam.get("lookup"),
    }


def family_from_seed(seed):
    """The ``lookup``-shaped dict back out of a seed (or a dossier's ``crash``) that carries
    ``seed_facts``, so the filer and the spike path can ask ``spellings`` of either."""
    s = seed or {}
    return {
        "predecessors": s.get("signature_predecessors") or [],
        "siblings": s.get("signature_siblings") or [],
        "s_first_build": s.get("signature_handoff_build"),
        "family_first_seen_ever": s.get("signature_family_first_seen_ever"),
        "alignment": s.get("signature_handoff_alignment"),
        "fan_in": s.get("signature_fan_in") or 0,
        "lookup": s.get("signature_family_lookup"),
    }


def family_from_corroborations(corroborations):
    """The same, off the persisted ``corroborations`` facts
    (``orchestrator._record_signature_age_facts``): the predecessor list, the live siblings and
    the handoff build survive there; ``None`` when the run recorded no lookup at all."""
    c = corroborations or {}
    if not c.get("signature_family_lookup"):
        return None
    top, change = c.get("signature_predecessor"), c.get("signature_predecessor_change")
    return {
        # The loudest predecessor keeps its recorded change sentence, so the venue comment says
        # the same thing the brief and the bug's age note said.
        "predecessors": [dict({"signature": p}, **({"change": change} if (p == top and change) else {}))
                         for p in c.get("signature_predecessors") or []],
        "siblings": [{"signature": s} for s in c.get("signature_siblings_live") or []],
        "s_first_build": c.get("signature_handoff_build"),
        "family_first_seen_ever": c.get("signature_family_first_seen_ever"),
        "alignment": c.get("signature_handoff_alignment"),
        "fan_in": c.get("signature_fan_in") or 0,
        "lookup": c.get("signature_family_lookup"),
    }


def clear_cache():
    _CACHE.clear()
