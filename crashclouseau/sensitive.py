# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Is this crash's ANALYSIS too sensitive to publish?

WHY THIS EXISTS. On 2026-08-20 Clouseau filed bug 2065051 from crash
``41bb8c8a-3458-4803-90f8-a7a850260819``, whose fault address is ``0xe5e5e5e5e5e5e5e8`` —
mozjemalloc's ``kAllocPoison``, i.e. the process read memory the allocator had already
reclaimed. A human turned that bug into a security bug. Reviewing it, :mccr8 wrote: "Bugs on
poison crashes like that should always be filed initially a security issue."

He was right, and restricting the Bugzilla bug would not have contained it, because **we publish
the analysis ourselves**. Four days after BMO started answering ``102`` for bug 2065051,
``GET /crashstack.html?uuid=41bb8c8a-...`` still returned HTTP 200 to an anonymous request
carrying ``0xe5e5e5e5e5e5e5e8``, the phrase "Use-after-free", "poison" three times, and the
suspected regressor bug 2063742 ten times. ``/api/evidence?uuid=`` served the same as JSON, and
uuids are enumerable from ``reports.html`` and ``tasks.html``.

WHAT IS WITHHELD, AND WHY IT IS THE WHOLE ANALYSIS. Not the fault address — the MECHANISM. The
sentence that made bug 2065051 sensitive reads "`CanvasRenderingContext2D::SetFontInternal`'s
`MruCache` slot-overwrite releases a stale, already-freed `RefPtr<nsAtom> mLang`", and it names
the changeset that introduced the lifecycle. Redacting fields cannot help: ``diff.html``
highlights exactly the lines the analysis flagged, and ``bug.html`` renders the filed comment
verbatim including ``build_exposer_note``'s "so this is a use-after-free or an uninitialised
read". So the surfaces withhold everything or nothing.

WHAT IS NOT CLAIMED. The crash report itself stays world-readable on crash-stats with the same
address — that is Mozilla's policy and not ours to change here. What this withholds is the part
that turns a raw report into an exploit lead: the mechanism, the named regressor, and the
flagged lines. It also cannot un-publish anything already fetched or indexed.

WHY THIS DOES NOT REUSE ``orchestrator._looks_poison`` DIRECTLY. It reuses the byte set and the
dominance rule, but reads the address itself and adds a widened rule, deliberately WITHOUT
touching the originals. Since ``1dbfac6`` a poison fault feeds ``_classify_exposer`` and lands
strong-evidence ON the 70 filing floor, so making that predicate fire more often changes WHAT WE
FILE. Two of the fixes below would do exactly that, and neither has been measured for its effect
on filings:

* ``_fault_address`` (orchestrator.py:828) prefers ``json_dump.crash_info.address`` and falls
  back to Socorro's top-level ``address`` only when the first is EMPTY. On 6 of the 8 poison
  crashes in a 500-run prod snapshot the first is present but useless — ``0x0000000000000000``
  or ``0xffffffffffffffff`` — while the top-level field carries ``0xe5e5e5e5e5e5e5xx``. So
  ``exposer_strong`` is fail-open on 75% of poison crashes.
* ``_looks_poison``'s one-off-byte dominance rule breaks on any offset >= 0x100: over 89 nightly
  days it accepts 3,020 of the 3,500 reports whose address has a 4-byte poison PREFIX and misses
  480 (13.7%; only 45.5% recall on 0x4B).

Both are real defects and both belong in their own commit, measured against filing outcomes.
Here, over-firing costs one withheld page view on a single-reader site, so the widened rule is
free; there, it costs a wrongly-restricted bug. Same evidence, different price, so: different
predicate.

FAIL-SAFE DIRECTION, stated once. Everywhere else in this codebase an exception is swallowed so
a run never breaks. Here an unparseable or absent answer must resolve to SENSITIVE, because a
withheld page costs a click and a published use-after-free cannot be taken back.
"""

# DUPLICATED from `agent.orchestrator`, on purpose, and pinned against it by
# `tests/test_sensitive.py::test_the_constants_have_not_diverged`. Importing them would pull in
# `agent.orchestrator` -> `crashclouseau.worker` -> a live Redis connection at import time, so a
# web request rendering a page would depend on the queue being configured. This module is
# imported by `bugzilla_apply`, which the web app imports on every request; it must stay
# dependency-free. A test that fails on divergence is the cheaper coupling.
_POISON_BYTES = frozenset({0xE5, 0xE4, 0x5A, 0xDD, 0xCD, 0xCC, 0xFD, 0xAB, 0xBE, 0xFB, 0x2B, 0x4B})
_MAX_FIELD_FAULT = 0x1000

# Sentinels that are PRESENT in `json_dump.crash_info.address` but carry no information: a null
# deref and an all-ones read. `_fault_address` treats them as answers because it only tests for
# emptiness, which is how the poison address underneath them goes unseen.
_USELESS_ADDRESSES = frozenset({0, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF})

# How many leading bytes must be one poison byte for the WIDENED rule. Four, because that is the
# width at which the value stops being a plausible user-space pointer: 0xe5e5e5e5________ is not
# an address any allocator hands out, whatever the low half holds. This is argued from shape and
# NOT measured for false positives -- tests/poison/poison_fault_panel.json cannot answer it,
# because the panel applies the one-off-byte rule BEFORE recording, so the 480 reports this rule
# is for are censored out of the census. That is exactly why the rule is confined to withholding.
_PREFIX_BYTES = 4


def _parse(addr):
    """One Socorro address field -> int, or None."""
    if addr in (None, ""):
        return None
    s = str(addr).strip()
    try:
        return int(s, 16) if s.lower().startswith("0x") else int(s)
    except (TypeError, ValueError):
        return None


def fault_address(raw_crash):
    """The faulting address, preferring whichever field actually carries one.

    Unlike ``orchestrator._fault_address`` this rejects a PRESENT-but-useless
    ``crash_info.address`` (null / all-ones) and falls through to Socorro's normalized
    top-level ``address``, which is where the poison value lives on 6 of 8 measured cases."""
    raw = raw_crash or {}
    info = (raw.get("json_dump") or {}).get("crash_info") or {}
    primary = _parse(info.get("address"))
    if primary is not None and primary not in _USELESS_ADDRESSES:
        return primary
    fallback = _parse(raw.get("address"))
    if fallback is not None and fallback not in _USELESS_ADDRESSES:
        return fallback
    return primary if primary is not None else fallback


def _bytes_of(fault):
    parts = []
    x = fault
    while x:
        parts.append(x & 0xFF)
        x >>= 8
    return parts


def looks_poison_dominant(fault) -> bool:
    """``orchestrator._looks_poison``'s rule, re-implemented here so this module has no
    behavioural coupling to the gate that moves the rung. Kept byte-identical on purpose;
    ``tests/test_sensitive.py`` pins the two against each other over the poison census."""
    if fault is None or fault <= _MAX_FIELD_FAULT:
        return False
    parts = _bytes_of(fault)
    if len(parts) < 2:
        return False
    top = max(set(parts), key=parts.count)
    return top in _POISON_BYTES and parts.count(top) >= max(2, len(parts) - 1)


def looks_poison_prefix(fault) -> bool:
    """True when the TOP ``_PREFIX_BYTES`` bytes are all one poison byte, whatever follows.

    Catches the case the dominance rule drops: a fault at an offset >= 0x100 into a poisoned
    object, e.g. ``0xe5e5e5e5e5e50128``, where the low bytes are the offset rather than the
    fill. 13.7% of poison-prefix reports over 89 nightly days."""
    if fault is None or fault <= _MAX_FIELD_FAULT:
        return False
    parts = _bytes_of(fault)
    if len(parts) < _PREFIX_BYTES:
        return False
    top = parts[-_PREFIX_BYTES:]
    return len(set(top)) == 1 and top[0] in _POISON_BYTES


def memory_unsafe_signals(raw_crash) -> list[str]:
    """The DETERMINISTIC evidence that this crash touched memory it did not own, as a list of
    human-readable reasons; empty means no such evidence.

    Reads only Socorro's own fields. It deliberately consults NOTHING the model wrote: over 491
    prod dossiers the optional crash-brief fields ``faulting_address``, ``reason``,
    ``moz_crash_reason`` and every ``phc_*`` are filled 0 times, while the deterministic address
    is parseable on 500 of 500 — a gate built on the brief would pass review, pass tests written
    against the schema, ship, and fire zero times.

    And it does not consult ``failure_class``/``data_flow.operation`` either, which is a
    MEASURED exclusion rather than a stylistic one. That union fires on 43 of 500 runs (8.6%)
    and on 2 of 11 new-bug filings (18%), and every human who read those bugs left them public
    -- including bug 2064600, whose ``failure_class`` is ``uaf`` and whose real fault address is
    ``0xffffffffffffffff``, a hardware bit flip that tnikkel diagnosed as such. Across all 57
    filings the deterministic poison address fires on exactly one, bug 2065051 -- which is
    exactly the one a human restricted. The model's label would have restricted eighteen times
    as many bugs and still not matched that judgement."""
    fault = fault_address(raw_crash)
    out = []
    if looks_poison_dominant(fault):
        out.append("fault address {:#x} is allocator poison".format(fault))
    elif looks_poison_prefix(fault):
        out.append("fault address {:#x} has a {}-byte poison prefix".format(
            fault, _PREFIX_BYTES))
    return out


def is_withheld(corroborations) -> bool:
    """Whether the persisted flag says to withhold this dossier's analysis.

    Reads the PERSISTED answer rather than recomputing: a render-time Socorro read would make
    every page depend on Socorro being reachable, and the fail-safe direction would then blank
    the whole site on a Socorro blip. The flag is written once, by
    ``orchestrator.apply_deterministic_gates``.

    A dossier written before the flag existed has no key, and reads as NOT withheld. That is a
    deliberate, bounded hole rather than a fail-open default: the alternative -- treating every
    pre-flag dossier as sensitive -- withholds the entire back catalogue, and the honest fix is
    the one-off backfill in ``bin/backfill_memory_unsafe.py``, which is the same predicate over
    the same field. Until that has run, assume nothing here protects an old dossier."""
    return bool((corroborations or {}).get("memory_unsafe"))
