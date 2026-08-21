# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""The archetypes we ship with, and the loop that keeps them honest.

An archetype is a recurring crash SHAPE plus what a reviewer told us to check when we see it
(``models.Archetype``). Rows live in the DB so one can be added the day it is learned rather
than the next deploy; the ones below are the starting set, re-seeded idempotently at startup so
a fresh database is never blank and an edited row is never clobbered.

WHY THE SEED SET IS SO SMALL. Each of these cost a real bug and a real reviewer's afternoon. The
temptation is to brainstorm twenty; the reason not to is that an archetype is handed to the agent
as a prior, and a plausible-but-wrong prior is precisely how bug 2061961 happened — a fluent
mechanism story built on a fact nobody checked. A rule earns its row by having already been the
answer once.
"""
import hashlib
import json

from crashclouseau import models
from crashclouseau.logger import logger


# Bug 2062119. The pipeline filed a shutdown-phase null deref, named a changeset from 2022-12-13
# as the regressor and needinfo'd its author. Jens Stutte replied "I do not think bug 1768581 is
# the regressor", then worked out the real origin himself: bug 1412726 had converted `gJarHandler`
# from a raw pointer nulled in the destructor into a `StaticRefPtr` cleared by `ClearOnShutdown`,
# which decoupled nulling the GLOBAL from destroying the OBJECT — so a live channel's owning
# reference kept the object alive while the global it read went null underneath it. His note
# afterwards is this row: "maybe a general 'is a singleton involved that may not have a
# good/complete shutdown handling?'".
#
# THE ROW SHIPPED WITH THE WRONG CONTEXT PREDICATE, and this comment shipped with the wrong
# evidence for it. It used to read "4 of the canary's first 20 filings carry shutdown machinery
# on the stack, so this is not a one-off", citing `shutdownhang | __fstatat`,
# `AsyncShutdownTimeout | profile-change-teardown` and `MediaDecoder::SetCDMProxy` -- 3 of those
# 4 exemplars are the DELIBERATE-ABORT family, i.e. crashes where nothing was dereferenced and
# the guidance's opening sentence is simply false. (`MediaDecoder::SetCDMProxy` is 49 of 49
# `MOZ_DIAGNOSTIC_ASSERT(!switched)` at a 0x7ff... address over 6 months, so it was never even
# a firing -- it was cited from the signature, not from a replay.)
#
# MEASURED 2026-08-21 over 1051 Firefox-nightly processed crashes (150/day x 7 days,
# 2026-08-14..20), facts rebuilt exactly as `orchestrator._matching_archetypes` builds them and
# scored with the shipped matcher: 23 firings, of which 21 carried a `moz_crash_reason` (15x
# `AsyncShutdownTimeout | ...`, 3x "Shutdown hanging at step ...", 2x WlLogHandler, 1x
# ServiceWorkerRegistrar) and 3 were not in shutdown at all. On the hangs the sibling
# `shutdown-hang` row went into the SAME prompt saying "A shutdown hang is not a fault: nothing
# crashed" (58ffaf90-7ebc-4d66-b81c-572ae0260815, 60681b60-6501-417b-b869-eed580260820, and bug
# 2061969's 424b0ab0-af81-4b33-b045-83c5b0260808 is the same shape). Adding
# `require_shutdown_progress` + `no_moz_crash_reason` takes 23 to 2 -- both of them
# `nsJARProtocolHandler::MimeService`, this row's own crash -- while the 3-month genuine-fault
# corpus (96 nightly in-shutdown EXCEPTION_ACCESS_VIOLATION_READ reports) stays at 25 of 25.
#
# THREE FIXES THAT LOOK RIGHT AND ARE REFUTED, so nobody re-tries them:
#   * A non-zero `min_fault_address` ("0x0 means nothing was dereferenced") deletes 40% of the
#     row's CORRECT firings: 50 of those 96 crashes fault at exactly 0x0 with a recorded memory
#     access and no moz_crash_reason, and 032c9db1-f5c5-49a8-80ba-0c0500260616
#     (`URLQueryStringStripper::ManageObservers`, `mov rax, qword [rcx]`) is this archetype's
#     mechanism verbatim at 0x0 -- `URLQueryStringStripper::Shutdown()` <- the `GetSingleton`
#     lambda <- `mozilla::KillClearOnShutdown(mozilla::ShutdownPhase)`.
#   * Shrinking the stack alternation to the tokens that NAME the mechanism
#     (ClearOnShutdown|KillClearOnShutdown|StaticRefPtr). The motivating crashes are the
#     counter-example: none of those words appears anywhere in the stack of
#     413d6058-1cf5-4d04-afc0-994d70260819, b65b3c02-a1f3-4f4b-89aa-cd7e50260820 or any of the
#     13 nightly MimeService reports, because the singleton is read through an inlined
#     accessor. It scores 1 firing per 1051 reports and 0 on the crash it was learned from.
#   * Suppressing on Socorro's `report_type=hang`. b65b3c02 is report_type=crash, the family is
#     filed from both shapes, and bug 2063892's `shutdownhang` at 0x7ffc... is already excluded
#     by the address bound. The ABORT RECORD is the discriminator, not the hang label.
_SHUTDOWN_SINGLETON = {
    "slug": "shutdown-singleton",
    "title": "Null deref during shutdown — check the singleton's shutdown handling",
    "source_bug": 2062119,
    "matcher": {
        # A null base plus a field offset, not a wild pointer. 0x1000 is a page: above it the
        # address is not plausibly `nullptr->field` and this rule has nothing to say. KEPT, and
        # not an n=1 bound: 72 of the 96-report genuine-fault corpus are small and they are
        # {0x0: 50, 0x28: 13, 0x8: 2, 0x1c: 2, 0x470: 2, 0x10: 1, 0x14: 1, 0x80: 1}, with the
        # next value up at 0x80000 -- two orders of magnitude of empty space.
        "max_fault_address": 4096,
        # The process really was shutting down. 3 of the 23 measured firings were not:
        # 61228138-f329-49df-bd97-c1a000260819 is a MOZ_Crash during STARTUP
        # (`nsXREDirProvider::DoStartup` -> `ProfileStarted`) that matched only because the
        # crashing FUNCTION is named `GetShutdownPhase`.
        # The "25 of 25" figure above does NOT test this key: corpus B is selected on
        # `shutdown_progress` being set, so the condition is a tautology there. Measured on its
        # own instead -- over 3 months of Firefox-nightly SuperSearch, reports with one of the
        # stack tokens below in `proto_signature`, `shutdown_progress` UNSET, no
        # `moz_crash_reason` and a small address number 1, an `IPCError-browser | ShutDownKill`
        # at EXCEPTION_BREAKPOINT (a content process killed on purpose, nothing dereferenced).
        "require_shutdown_progress": True,
        # Nothing aborted deliberately, so a pointer really was read. This is the half of the
        # context that was never written and it is what the whole fix rests on.
        "no_moz_crash_reason": True,
        # AND-ed with the two above, never instead of them: `shutdown_progress` on its own,
        # without this alternation, fires 32/1051 and 72/96 -- strictly worse. The annotation
        # says the PROCESS is shutting down; the stack is what says THIS CODE is on the
        # shutdown path.
        #
        # Pruned 2026-08-21 to the branches that fire. Hits in the STACK field this key
        # actually tests, n=1051: ShutdownPhase 74, AppShutdown 70, AdvanceShutdownPhase 70,
        # and ZERO for `XPCOMShutdown` (the real symbol is `ShutdownXPCOM`, 16), `::Teardown`,
        # `UnloadLoaders`, `AsyncShutdownTimeout`, `shutdownhang`, `profile-change-teardown`.
        # The last three score 50/36/15 in the SIGNATURE, which this key does not read, and
        # moving them to a `signature` key would re-admit precisely the 18 deliberate-abort
        # firings the two conditions above remove -- so they are deleted, not relocated.
        # `ClearOnShutdown|KillClearOnShutdown` also scored 0 over the week and is kept as
        # cheap mechanism evidence: the token is there on 032c9db1, the one crash in the panel
        # where the clear is visible in the frames.
        # Re-checked independently against 3 months of Firefox-nightly `proto_signature`
        # (SuperSearch, 2026-05-21..08-21): `UnloadLoaders` 0, `XPCOMShutdown` 0 against
        # `ShutdownXPCOM` 1384, `AsyncShutdownTimeout` 0, and `::Teardown` 95 of which NONE is
        # in scope for this row (small address + shutdown_progress set + no abort record), so
        # deleting those branches cannot lose a firing.
        # Two branches are substring-subsumed and spelled out for legibility only:
        # `ShutdownPhase` already matches `AdvanceShutdownPhase(Internal)`, and `ClearOnShutdown`
        # already matches `KillClearOnShutdown`. Note also that `ShutdownPhase` matches as an
        # ARGUMENT TYPE -- `nsThreadManager::SpinEventLoopUntilInternal(..., mozilla::
        # ShutdownPhase)` accounts for 54 of its 74 lines -- which is why it is a shutdown-path
        # hint and never on its own evidence of anything.
        "stack": [
            r"ClearOnShutdown|KillClearOnShutdown|ShutdownPhase|AppShutdown|"
            r"AdvanceShutdownPhase",
        ],
    },
    "guidance": (
        "CHECK THE CONDITION FIRST — this row is a conditional and its condition is not in the "
        "frames. (1) Is `MOZ_CRASH_REASON` empty? If it is SET, the process aborted on purpose, "
        "NOTHING was dereferenced, and this row does not apply — say so and drop it. (2) Does "
        "the `Faulting instruction` fact show a memory READ through a register base (e.g. "
        "`mov rbx, qword [r15 + 0x28]`, or a bare `[reg]` when the address is 0x0)? "
        "IF BOTH HOLD: a small fault address during shutdown usually means a GLOBAL/SINGLETON "
        "was read after it was cleared, not a wild pointer. Ask: is the crashing access reached "
        "through a `StaticRefPtr`/`ClearOnShutdown`-managed global? SEARCHFOX THE DECLARATION — "
        "and expect the mechanism NOT to be on the stack: on bug 2062119's own crashes no frame "
        "mentions `ClearOnShutdown` or `StaticRefPtr` at all, because the singleton is read "
        "through an inlined accessor, so `it is not in the frames` is not an answer either way. "
        "If you find such a declaration, note that `ClearOnShutdown` nulls the GLOBAL while "
        "callers holding their own owning reference keep the OBJECT alive — an owning reference "
        "is NOT protection for the global, and every `gFoo->` deref in the callers is then a "
        "shutdown-time null deref. "
        "WHERE THE ORIGIN IS, once you have that declaration in hand: usually the changeset "
        "that converted the global to `StaticRefPtr` + `ClearOnShutdown`, which is often YEARS "
        "old and will NOT be in this build's pushlog window — search the declaration's history "
        "(`mcp__history__file_history` on the file that declares it, or blame the declaration) "
        "rather than the window. On bug 2062119 that was bug 1412726, from 2017, while the "
        "pipeline named a 2022 changeset. If you did NOT find such a declaration, do NOT take "
        "that search: leaving the pushlog window is the most expensive thing this row can ask "
        "for, and the declaration is the only evidence that would justify it. "
        "Also check whether the clear happens before a phase that still uses the object: the "
        "second fix there moved the clear to `CCPostLastCycleCollection` because chrome JS "
        "module loading still reads `omni.ja` through jar channels after `XPCOMShutdownFinal`. "
        "If the shutdown-handling gap is what you find, say so — a latent shutdown bug with no "
        "recent regressor is a perfectly good verdict, and better than naming a changeset."
    ),
}

# Bug 2064436. The pipeline filed a `shutdownhang` at 97% worth-investigating and explained it
# through "the `MediaTrackGrph` thread owned by `ThreadedDriver`". Andreas Pehrson closed it
# INVALID in three hours: "I see no proof in the profile that this parent process is using or has
# used a MediaTrackGraph. No MediaTrackGrph thread, no GraphRunner thread. And the graph has a
# timer that clears the shutdown blocker if the audio hw is blocked. The AudioIPC server threads
# seem to be doing something, but there are no AudioIPC client threads like there would be if we
# were doing audio in this process. There may be a MediaTrackGraph in a content process but then
# the shutdown blocker would live there too."
#
# The thread-existence half of that is NOT here: it is a checkable list, so it ships as a FACT
# (`triage._thread_inventory`) that also reaches the blind second opinion, and the analysed-thread
# half is a code fix (`inspector.thread_for_analysis`). What is left over is genuinely a prior —
# how to work a shutdown hang once you can see one properly — and that is what this row carries.
_SHUTDOWN_HANG = {
    "slug": "shutdown-hang",
    "title": "Shutdown hang — the blocked spin-loop names the subsystem; check it exists here",
    "source_bug": 2064436,
    "matcher": {
        # Socorro prefixes the signature of every shutdown hang, whatever the frames under it.
        "signature": [r"^shutdownhang \|"],
    },
    "guidance": (
        "A shutdown hang is not a fault: nothing crashed, a watchdog killed the process because "
        "the main thread stopped making progress. So do NOT look for a bad pointer or a recent "
        "edit to a stack frame. WHERE TO START, in order: (1) the `BLOCKED SPIN-EVENT-LOOP STACK` "
        "crash fact, when present — that is Socorro telling you the exact call the main thread is "
        "parked in and usually the named pool/service it is waiting for (e.g. "
        "`nsThreadPool::ShutdownWithTimeout BgIOThreadPool`), which is the single most "
        "informative line available and beats anything you can infer from the frames; "
        "(2) `Shutdown phase reached`, which bounds what can still be running; (3) the stack "
        "itself, which is the HUNG MAIN THREAD (not the watchdog) and reads outside-in — the "
        "innermost Gecko frame is who is waiting, not who is stuck. "
        "BEFORE YOU NAME A SUBSYSTEM, find its thread in the `THREADS IN THIS PROCESS` fact. If "
        "it is not there it was not running here and the mechanism is refuted. Two traps that "
        "cost bug 2064436: a subsystem present in a CONTENT process says nothing about a hang in "
        "the PARENT — its shutdown blocker would live in that process too, so a parent-process "
        "hang is not evidence about it; and many subsystems hold a TIMER that clears their own "
        "shutdown blocker on a stall, which means they cannot hang shutdown indefinitely, so "
        "check for one (searchfox the blocker's registration) before blaming them. "
        "Finally, expect NO regressor. A shutdown hang is usually a latent ordering or "
        "wait-for-ever bug that needed a timing change to become visible, so 'this is a real "
        "shutdown-ordering defect in X, no changeset in this window introduced it' is a good "
        "verdict — better than naming a changeset because the window had to contain one."
    ),
}

SEED_ARCHETYPES = (_SHUTDOWN_SINGLETON, _SHUTDOWN_HANG)


def _fingerprint(guidance, matcher):
    """Stable hash of a row's shipped TEXT, so ``seed`` can tell an untouched row from an
    edited one. Total by construction: anything that will not serialise fingerprints as ``""``,
    which is in no superseded set, so the row is left alone rather than failing a release."""
    try:
        blob = json.dumps({"guidance": guidance or "", "matcher": matcher or {}},
                          sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# Shipped texts this file has SUPERSEDED, per slug (``_fingerprint`` hashes). A stored row
# matching one of these is a previous deploy's seed that nobody has edited, so replacing it
# restores shipped text rather than clobbering somebody's work.
#
# WHY THIS IS NEEDED AT ALL: `seed(overwrite=False)` skipped on SLUG alone, so no change to an
# archetype could ever reach a database that already had the row. Every row edit since the
# feature landed was dead on arrival in prod, and invisibly so -- no page renders this table.
# The obvious fix, `overwrite=True`, is wrong in the measured direction: it reverts a
# `guidance` an operator tuned after a misfire, which is exactly the promise `seed` makes.
#
# 854a7c1d... is `shutdown-singleton` as first seeded (312e153) and unchanged until the
# 2026-08-21 context fix above -- the only text a production row can hold that we know was not
# written by hand. `shutdown-hang` is not being changed and so lists nothing.
_SUPERSEDED = {
    "shutdown-singleton": frozenset({
        "854a7c1dc52988e5df1da7db6dd442bcc0b8559690c6c609000c74b100625a4c",
    }),
}


def seed(overwrite=False):
    """Insert the built-in archetypes that are missing, and upgrade the untouched ones.
    Returns the slugs written.

    Does NOT overwrite an EDITED row: these are DB-editable on purpose, and a deploy silently
    reverting a tuned `guidance` (or re-enabling a row somebody turned off after it misfired)
    would make the table untrustworthy. ``overwrite=True`` is the deliberate "restore the
    shipped text" switch.

    It used to skip on SLUG alone, which is that same sentence read too literally: a row nobody
    has ever touched is not an edit. Skipping it meant no change to an archetype could reach a
    database that already had the row, so every row fix since the feature landed was dead on
    arrival in prod -- and since no page renders this table, the divergence between
    archetypes.py and what actually ran was invisible as well. A stored row whose text
    fingerprints to a version this file has SUPERSEDED (``_SUPERSEDED``) is upgraded; anything
    else is left exactly as it is."""
    written = []
    for spec in SEED_ARCHETYPES:
        row = (models.db.session.query(models.Archetype)
               .filter(models.Archetype.slug == spec["slug"]).one_or_none())
        if row is not None and not overwrite:
            if _fingerprint(row.guidance, row.matcher) not in _SUPERSEDED.get(
                    spec["slug"], ()):
                continue
            logger.info("archetypes: %s is an untouched older seed; upgrading it",
                        spec["slug"])
        models.Archetype.upsert(
            slug=spec["slug"], title=spec["title"], guidance=spec["guidance"],
            matcher=spec["matcher"], source_bug=spec.get("source_bug"),
            enabled=True if row is None else row.enabled,
        )
        written.append(spec["slug"])
    if written:
        logger.info("archetypes: seeded %s", written)
    return written


def seed_quietly():
    """``seed()`` for the startup path: never raises, so a DB that cannot take the rows (an
    older deploy, a read-only role) still starts and simply runs with no hints."""
    try:
        return seed()
    except Exception as exc:                                # pragma: no cover - defensive
        models.db.session.rollback()
        logger.warning("archetypes: could not seed built-ins: %s", exc)
        return []
