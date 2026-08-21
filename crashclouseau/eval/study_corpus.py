# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Adapt the frozen 289-bug regressor STUDY fixtures into an eval corpus (Phase-2 calibration).

Reads ``spike/regressor_dataset_blind/<bug>.json`` (agent INPUT: crash + the full first-bad
pushlog window, leakage-controlled) and ``spike/regressor_dataset/<bug>.json`` (answer key:
``regressed_by`` bugs + regressor landing revs + ``on_stack`` + ``first_build``), and freezes a
corpus (``manifest.json`` + ``<uuid>/case.json`` + ``<uuid>/processed_crash.json``) that
``eval.run rerun|score`` consumes — so calibration re-runs the SHIPPED pipeline on known-labeled
crashes without re-mining BMO/Socorro (no hgmo-406 risk).

Leak-free by construction: only crash-time facts (signature, stack, build rev, candidate window)
reach the agent; the regressor identity / fix / ``on_stack`` label live ONLY in the case's
ground-truth fields (metrics, never the prompt). ``is_offstack`` is RECOMPUTED from the inputs
(candidate files vs stack files) to mirror ``build_seed``, NOT read from the answer key. The
``.neg`` fixtures (regressor removed from the window) become ``is_negative`` cases: any
non-abstain on them is a false-investigate.

CLI: ``python -m crashclouseau.eval.study_corpus --blind spike/regressor_dataset_blind
--answer spike/regressor_dataset --out corpus_study [--limit N]``.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re

from crashclouseau import config, utils
from crashclouseau.agent.orchestrator import _looks_pref_flip, _tokens
from crashclouseau.eval.models import CorpusCase
from crashclouseau.logger import logger

# Real per-bug fixtures are ``<digits>.json`` / ``<digits>.neg.json``; skip index/aggregate
# files (``_sf_cases.json`` is a list; ``_off``/``_sample`` helpers carry non-fixture keys).
_FIXTURE_RE = re.compile(r"^(\d+)(\.neg)?\.json$")

# A Java/Kotlin exception signature (``java.lang.X`` / ``org.mozilla.fenix...ClassCastException``).
_JAVA_SIG_RE = re.compile(r"^(?:[A-Za-z_][\w$]*\.)+[A-Za-z_][\w$]*(?:Exception|Error)\b")
# BMO products Clouseau does NOT triage (mobile / Thunderbird). Kept in sync with the intent of
# ``corpus._is_target_crash`` (desktop Firefox native only); the study corpus spans all products.
#
# The Thunderbird/SeaMonkey half is DERIVED, not restated. This was the THIRD copy of
# ``config._OTHER_APP_PRODUCTS`` in the repo and it had already drifted to the opposite answer:
# it called ``Firefox for Android`` and ``GeckoView`` somebody else's, which config deliberately
# does not, and it omitted ``Calendar``, ``Chat Core`` and ``SeaMonkey``, which config does call
# foreign. Measured 2026-08-21 over the 287 blind fixtures: this literal dropped 29, config's
# list would have dropped 3, and 26 of 287 (9.1%) were classified differently by two lists in
# one repo with no test comparing them.
#
# The Android family stays a LITERAL and stays here, because the two lists answer different
# questions: config's map asks "whose bug would this venue be" and keeps Fenix/GeckoView on the
# Firefox side (they file into the same Gecko products), while this asks "can Clouseau triage
# this crash at all", and for a JVM/Android crash the answer is no until plans/16 lands. The
# union drops exactly what the literal dropped — 25 Firefox for Android, 2 MailNews Core, 1
# GeckoView, 1 Thunderbird — so this is a de-duplication with 0 fixtures moved. What it must
# NOT eat is the study corpus itself: 238 Core, 10 Toolkit, 7 Firefox, 2 External Software
# Affecting Firefox and 1 Application Services fixture, none of which is exclusive to any
# application and all of which stay.
_ANDROID_PRODUCTS = frozenset({
    "firefox for android", "fenix", "fennecandroid", "geckoview",
})
_NON_DESKTOP_PRODUCTS = _ANDROID_PRODUCTS | frozenset(
    p.lower() for p in config.get_other_app_products("Firefox")
)


def _is_target_crash(crash):
    """Keep only crashes Clouseau can triage: desktop Firefox NATIVE (non-Java). Drops
    Fenix/Android, Thunderbird, GeckoView and Java-stack crashes (unsupported — an off-stack
    pushlog lever can't help a crash Clouseau never runs on). Deliberately does NOT require a
    symbolicated source frame (unlike ``corpus._is_target_crash``): an opaque-top native crash
    (e.g. the study's ntdll case rescued via prior-signature) is exactly what the off-stack
    path exists to triage, so keep it."""
    sig = crash.get("signature") or ""
    if sig.startswith("java.") or _JAVA_SIG_RE.match(sig):
        return False
    return (crash.get("product") or "").strip().lower() not in _NON_DESKTOP_PRODUCTS


def _basenames(files):
    return {os.path.basename(f) for f in (files or []) if f}


def _iter_fixtures(blind_dir):
    """Yield ``(bug_id:str, is_neg:bool, path)`` for every real fixture in ``blind_dir``."""
    for path in sorted(glob.glob(os.path.join(blind_dir, "*.json"))):
        m = _FIXTURE_RE.match(os.path.basename(path))
        if m:
            yield m.group(1), bool(m.group(2)), path


def _load(path):
    with open(path) as handle:
        return json.load(handle)


def _first_build(answer):
    regs = answer.get("regressors") or []
    for r in regs:
        if isinstance(r, dict) and (r.get("first_build") or {}).get("revision"):
            return r["first_build"]
    return {}


def _regressor_nodes(answer):
    """Union of the regressors' landing revs (short-rev), the ground-truth regressor
    changesets used by the ``_hit`` node arm."""
    nodes = set()
    for r in answer.get("regressors") or []:
        if not isinstance(r, dict):
            continue
        for rev in r.get("landing_revs") or []:
            if rev:
                nodes.add(utils.short_rev(rev))
    return sorted(nodes)


def _regressor_bugs(answer):
    """``regressed_by`` bug ids (authoritative, alias-free) — the robust ``_hit`` bug arm."""
    bugs = set()
    for b in (answer.get("crash_bug") or {}).get("regressed_by") or []:
        try:
            bugs.add(int(b))
        except (TypeError, ValueError):
            continue
    for r in answer.get("regressors") or []:
        if isinstance(r, dict) and r.get("bug"):
            try:
                bugs.add(int(r["bug"]))
            except (TypeError, ValueError):
                pass
    return sorted(bugs)


def _regressor_authors(answer):
    """All commit authors of the true regressor's changesets (for the person-level metric)."""
    out = []
    for r in answer.get("regressors") or []:
        if not isinstance(r, dict):
            continue
        for c in r.get("changesets") or []:
            if isinstance(c, dict) and c.get("author"):
                out.append(c["author"])
    return sorted(set(out))


def _on_stack_label(answer):
    """True if ANY regressor touched a stack file; False if all off-stack; None if unknown."""
    regs = [r for r in (answer.get("regressors") or []) if isinstance(r, dict)]
    flags = [r.get("on_stack") for r in regs if r.get("on_stack") is not None]
    if not flags:
        return None
    return any(flags)


# The ``json_dump.crash_info`` keys a fixture is allowed to carry. Deliberately NOT
# ``crashing_thread``: see ``_processed_crash``.
_CRASH_INFO_KEYS = ("address", "type", "instruction", "assertion")


def _fetch_crash_info(uuid):
    """Socorro's ``json_dump.crash_info`` for ``uuid``, or ``{}``.

    Opt-in (``build_study_corpus(fetch_crash_info=True)`` / ``--fetch-crash-info``) because the
    rest of this module is offline and deterministic. Best-effort by design: a crash Socorro
    has expired is a fixture without fault fields, exactly as today."""
    if not uuid:
        return {}
    try:
        from libmozdata import socorro

        data = socorro.ProcessedCrash.get_processed(uuid).get(uuid) or {}
    except Exception as exc:  # pragma: no cover - network / expired / rate limit
        logger.warning("study_corpus: no processed crash for %s: %s", uuid, exc)
        return {}
    info = (data.get("json_dump") or {}).get("crash_info") or {}
    return {k: info[k] for k in _CRASH_INFO_KEYS if info.get(k) not in (None, "")}


def _processed_crash(crash, first_build, crash_info=None):
    """A minimal processed-crash dict in the shape ``runner._render_stack`` / ``triage.
    _crash_facts`` read: ``json_dump.threads[0].frames`` + product/build/version, plus
    whatever fault fields ``crash_info`` supplies.

    WHY THE FAULT FIELDS MATTER ENOUGH TO PLUMB. This function used to hardcode
    ``crash_info = {"crashing_thread": 0}``, so ``address`` / ``type`` / ``instruction`` could
    never reach a fixture no matter what the source held. Measured consequence: all 1257
    ``processed_crash.json`` files across the 10 committed corpus dirs carry
    ``{"crashing_thread": N}`` and nothing else; ``triage._crash_facts`` therefore emits a
    4-line block (Product / Version / Build ID / Analysed thread) with no "Fault address" line
    on 90/90 corpus_ship cases, ``orchestrator._fault_address`` returns None on all of them,
    and ``fault_address_offset_match`` appears 0 times in
    ``corpus_ship/results_gate_facts.jsonl`` — 0/64 positive arm, 0/26 negative arm. No
    back-test of the fault-offset corroboration gate is possible in EITHER arm, and that
    0-vs-0 non-answer is what a reader mistakes for "the gate never fires".

    ``crashing_thread`` IS STILL HARDCODED TO 0, AND MUST STAY THAT WAY. It is the one
    crash_info key that would break things: ``threads`` here is a ONE-element synthesis of the
    bug comment's stack, while the real index can be anything (46-thread minidumps are normal,
    and on a shutdown hang the crashing thread is the watchdog). ``corpus._is_target_crash``,
    ``corpus._stack_files`` and ``runner._render_stack`` all do
    ``threads[ct]["frames"] if ct < len(threads) else []``, so passing the real index through
    would silently empty the stack of every affected case.

    WHAT A RE-RECORD COSTS, measured 2026-08-21 against the live ProcessedCrash API: Socorro
    keeps a processed crash about six months. Of the 206 distinct real uuids in the committed
    corpora, sampled 4 per month: 0/28 fetchable for 2025-07 .. 2026-01, 21/23 for 2026-02
    onward — so ~80 of 206 can be re-recorded and the rest are gone for good. Of the 21
    recovered, 6 carry a gate-eligible fault (0x8, 0x10, 0x18, 0xe0, 0xf0, 0x1d), 4 are 0x0 and
    8 are large — i.e. a re-recorded corpus would give this gate roughly a 29% eligible arm to
    back-test on instead of zero. The fetch is one unauthenticated GET per case; the expensive
    part of a rebuild is re-running the agent, not this."""
    frames = []
    for f in crash.get("top_frames") or []:
        frames.append({
            "frame": f.get("stackpos"),
            "function": f.get("function") or "",
            "module": f.get("module") or "",
            "file": f.get("file") or "",
            "line": f.get("line") or 0,
        })
    info = {"crashing_thread": 0}
    for key in _CRASH_INFO_KEYS:
        value = (crash_info or {}).get(key)
        if value not in (None, ""):
            info[key] = value
    return {
        # These are Firefox crashes (the BMO product is "Core"); the crash-facts reader keys
        # off "product"/"version"/"build", not the BMO product taxonomy.
        "product": "Firefox",
        "version": first_build.get("version", ""),
        "build": first_build.get("buildid", ""),
        "release_channel": "nightly",
        "moz_crash_reason": crash.get("moz_crash_reason", ""),
        "json_dump": {
            "crash_info": info,
            "threads": [{"frames": frames}],
        },
    }


def _candidates(window, stack_files, signature, is_offstack, max_candidates):
    """Build ``build_seed``-shape candidates from the frozen window.

    OFF-STACK: the FULL window (``score=None``), pref-flip tagged and ranked the way
    ``_offstack_candidates`` does (non-backout, then pref-flip, then signature<->desc token
    overlap; no prior-sig/date signal offline), capped to ``max_candidates``. ON-STACK: only
    candidates whose files touch a stack file (``score=|overlap|``), ranked by score — the
    leak-free proxy for prod's DB-scored seed."""
    sig_tokens = _tokens(signature)
    out = []
    if is_offstack:
        ranked = sorted(
            (c for c in window if isinstance(c, dict)),
            key=lambda c: (
                0 if c.get("backedout") else 1,
                1 if _looks_pref_flip(c.get("desc"), c.get("files")) else 0,
                len(sig_tokens & _tokens(c.get("desc", ""))) if sig_tokens else 0,
            ),
            reverse=True,
        )
        for c in ranked[:max_candidates]:
            desc = (c.get("desc") or "").strip()
            out.append({
                "node": c.get("node"),
                "score": None,
                "bug": c.get("bug") if isinstance(c.get("bug"), int) else None,
                "backedout": bool(c.get("backedout")),
                "pushdate": None,
                "noise": False,
                "desc": desc.splitlines()[0][:200] if desc else "",
                "prior_sig": False,
                "pref_flip": bool(_looks_pref_flip(c.get("desc"), c.get("files"))),
            })
    else:
        scored = []
        for c in window:
            if not isinstance(c, dict):
                continue
            overlap = len(_basenames(c.get("files")) & stack_files)
            if overlap:
                desc = (c.get("desc") or "").strip()
                scored.append({
                    "node": c.get("node"),
                    "score": overlap,
                    "bug": c.get("bug") if isinstance(c.get("bug"), int) else None,
                    "backedout": bool(c.get("backedout")),
                    "pushdate": None,
                    "noise": False,
                    "desc": desc.splitlines()[0][:200] if desc else "",
                })
        out = sorted(scored, key=lambda c: -c["score"])
    return out


def _case_from_fixture(bug_id, is_neg, blind, answer, corpus_dir, fetch_crash_info=False):
    """Assemble one CorpusCase + write processed_crash.json; returns the case (or None).

    ``fetch_crash_info`` re-reads the fault fields from Socorro for the uuid the ANSWER KEY
    already carries (233 of 289 study answers have one). It is off by default so the normal
    build stays offline and deterministic; see ``_processed_crash`` for why the fields matter
    and what fraction is still within Socorro's retention."""
    crash = blind.get("crash") or {}
    signature = crash.get("signature", "")
    stack_files = _basenames(crash.get("stack_files"))
    window = blind.get("candidate_window") or []
    # Leak-free RUN split (mirror build_seed): no candidate touched a stack file => off-stack.
    is_offstack = not any(
        _basenames(c.get("files")) & stack_files
        for c in window if isinstance(c, dict)
    )
    max_candidates = config.get_agent_offstack()["max_candidates"]
    candidates = _candidates(window, stack_files, signature, is_offstack, max_candidates)

    uuid = (answer.get("crash_stack") or {}).get("uuid") or "bug{}".format(bug_id)
    if is_neg:
        uuid = "{}-neg".format(uuid)

    first_build = _first_build(answer)
    pin_rev = (blind.get("build_hg_rev") or first_build.get("revision") or "")
    # NEGATIVE arm: the regressor is removed from the window, so ground truth is "no regressor
    # here" — empty sets make _hit always False (any non-abstain = false-investigate).
    reg_nodes = [] if is_neg else _regressor_nodes(answer)
    reg_bugs = [] if is_neg else _regressor_bugs(answer)
    # Stack-only seed set (the on-stack scored proxy): the overlapping candidate nodes.
    seed_nodes = [c["node"] for c in candidates if c.get("score")] if not is_offstack else []

    case_dir = os.path.join(corpus_dir, uuid)
    os.makedirs(case_dir, exist_ok=True)
    crash_path = os.path.join(case_dir, "processed_crash.json")
    # The blind fixture is a Bugzilla-COMMENT parse (spike/collect_regressor_dataset.py) and
    # has never carried fault fields — nothing is being "kept" here, they have to be fetched.
    # Keyed on the uuid, so a `.neg` case gets the same crash facts as its positive twin.
    crash_info = _fetch_crash_info(uuid.replace("-neg", "")) if fetch_crash_info else {}
    with open(crash_path, "w") as handle:
        json.dump(_processed_crash(crash, first_build, crash_info), handle)

    case = CorpusCase(
        uuid=uuid,
        signature=signature,
        regressor_node=(reg_nodes[0] if reg_nodes else ""),
        regressor_nodes=reg_nodes,
        regressor_bug=(reg_bugs[0] if reg_bugs else None),
        regressor_bugs=reg_bugs,
        # Kept even for negatives (the removed regressor's authors) so the person-level
        # metric can tell "routed to the right person anyway" (silver) from a true miss.
        regressor_authors=_regressor_authors(answer),
        channel="nightly",
        crash_json_path=crash_path,
        seed_nodes=seed_nodes,
        candidates=candidates,
        on_stack_label=(None if is_neg else _on_stack_label(answer)),
        is_offstack=is_offstack,
        is_negative=is_neg,
        pin_rev=pin_rev,
    )
    with open(os.path.join(case_dir, "case.json"), "w") as handle:
        handle.write(case.model_dump_json(indent=2))
    return case


def _write_manifest(corpus_dir, cases):
    digest = hashlib.sha256(
        "".join(sorted(c.uuid for c in cases)).encode()
    ).hexdigest()[:16]
    manifest = {
        "corpus_hash": digest,
        "source": "study-fixtures",
        "n_cases": len(cases),
        "n_negative": sum(1 for c in cases if c.is_negative),
        "n_offstack": sum(1 for c in cases if c.is_offstack),
        "uuids": [c.uuid for c in cases],
    }
    with open(os.path.join(corpus_dir, "manifest.json"), "w") as handle:
        json.dump(manifest, handle, indent=2)
    return digest


def build_study_corpus(blind_dir, answer_dir, corpus_dir, limit=None,
                       fetch_crash_info=False):
    """Freeze the study fixtures into ``corpus_dir``; returns the list of CorpusCases.

    A positive fixture with no matching answer key is skipped (can't score it). A negative
    ``.neg`` reuses its bug's answer key only for the build rev / uuid — its regressor sets
    stay empty. ``fetch_crash_info`` adds one Socorro GET per case to recover the fault
    address/type/instruction (see ``_processed_crash``); default off keeps the build offline."""
    os.makedirs(corpus_dir, exist_ok=True)
    cases = []
    skipped = 0
    skipped_nontarget = 0
    for bug_id, is_neg, path in _iter_fixtures(blind_dir):
        answer_path = os.path.join(answer_dir, "{}.json".format(bug_id))
        if not os.path.exists(answer_path):
            skipped += 1
            continue
        try:
            blind = _load(path)
            answer = _load(answer_path)
            if not isinstance(blind, dict) or not isinstance(answer, dict):
                skipped += 1
                continue
            if not _is_target_crash(blind.get("crash") or {}):
                skipped_nontarget += 1
                continue
            case = _case_from_fixture(bug_id, is_neg, blind, answer, corpus_dir,
                                      fetch_crash_info=fetch_crash_info)
        except Exception as exc:  # pragma: no cover - defensive per-fixture
            logger.warning("study_corpus: skip %s: %s", path, exc)
            skipped += 1
            continue
        if case is not None:
            cases.append(case)
        if limit and len(cases) >= limit:
            break
    digest = _write_manifest(corpus_dir, cases)
    logger.info(
        "study_corpus: froze %d cases (%d neg, %d off-stack; skipped %d no-answer-key, "
        "%d non-target Java/mobile/TB) to %s [hash %s]",
        len(cases), sum(c.is_negative for c in cases),
        sum(bool(c.is_offstack) for c in cases), skipped, skipped_nontarget,
        corpus_dir, digest,
    )
    return cases


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m crashclouseau.eval.study_corpus")
    parser.add_argument("--blind", default="spike/regressor_dataset_blind",
                        help="dir of leakage-controlled blind fixtures")
    parser.add_argument("--answer", default="spike/regressor_dataset",
                        help="dir of answer-key fixtures")
    parser.add_argument("--out", default=None, help="corpus output dir")
    parser.add_argument("--limit", type=int, default=None,
                        help="freeze only the first N fixtures (cheap dry-run)")
    parser.add_argument("--fetch-crash-info", action="store_true",
                        help="re-read fault address/type/instruction from Socorro per case "
                             "(one GET each; only ~6 months of crashes are still retained)")
    args = parser.parse_args(argv)
    corpus_dir = args.out or config.get_eval().get("corpus_dir", "corpus")
    build_study_corpus(args.blind, args.answer, corpus_dir, limit=args.limit,
                       fetch_crash_info=args.fetch_crash_info)


if __name__ == "__main__":
    main()
