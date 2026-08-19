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
# 4 of the canary's first 20 filings carry shutdown machinery on the stack, so this is not a
# one-off: `shutdownhang | __fstatat`, `AsyncShutdownTimeout | profile-change-teardown`,
# `MediaDecoder::SetCDMProxy`, and 2062119 itself.
_SHUTDOWN_SINGLETON = {
    "slug": "shutdown-singleton",
    "title": "Null deref during shutdown — check the singleton's shutdown handling",
    "source_bug": 2062119,
    "matcher": {
        # A null base plus a field offset, not a wild pointer. 0x1000 is a page: above it the
        # address is not plausibly `nullptr->field` and this rule has nothing to say.
        "max_fault_address": 4096,
        "stack": [
            r"ClearOnShutdown|KillClearOnShutdown|ShutdownPhase|AppShutdown|"
            r"AdvanceShutdownPhase|AsyncShutdownTimeout|shutdownhang|"
            r"XPCOMShutdown|profile-change-teardown|::Teardown|UnloadLoaders",
        ],
    },
    "guidance": (
        "A small fault address during shutdown usually means a GLOBAL/SINGLETON was read after "
        "it was cleared, not a wild pointer. Ask: is the crashing access reached through a "
        "`StaticRefPtr`/`ClearOnShutdown`-managed global (searchfox the declaration)? If so, "
        "note that `ClearOnShutdown` nulls the GLOBAL while callers holding their own owning "
        "reference keep the OBJECT alive — an owning reference is NOT protection for the global, "
        "and every `gFoo->` deref in the callers is then a shutdown-time null deref. "
        "WHERE THE ORIGIN IS: usually the changeset that converted the global to "
        "`StaticRefPtr` + `ClearOnShutdown`, which is often YEARS old and will NOT be in this "
        "build's pushlog window — search the declaration's history (`mcp__history__file_history` "
        "on the file that declares it, or blame the declaration) rather than the window. On bug "
        "2062119 that was bug 1412726, from 2017, while the pipeline named a 2022 changeset. "
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


def seed(overwrite=False):
    """Insert the built-in archetypes that are missing. Returns the slugs written.

    Does NOT overwrite an existing row by default: these are DB-editable on purpose, and a
    deploy silently reverting a tuned `guidance` (or re-enabling a row somebody turned off after
    it misfired) would make the table untrustworthy. ``overwrite=True`` is the deliberate
    "restore the shipped text" switch."""
    written = []
    for spec in SEED_ARCHETYPES:
        row = (models.db.session.query(models.Archetype)
               .filter(models.Archetype.slug == spec["slug"]).one_or_none())
        if row is not None and not overwrite:
            continue
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
