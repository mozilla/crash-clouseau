# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Corpus mining + freezing (#13).

Mine clouseau-aliased BMO bugs (their regressed_by + cf_crash_signature), resolve
representative nightly UUIDs, and FREEZE each case (processed-crash JSON + regressor
hg node + seed snapshot) to disk before the 30-day retention purge deletes it. The
frozen corpus is self-contained so re-runs/scoring need no live BMO/Socorro.

NOTE: the mining query + SuperSearch calls are live BMO/Socorro I/O (not exercised
in unit tests); the exact `blocked`=clouseau filter may need the real meta-bug id.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone

from libmozdata import socorro
from libmozdata.bugzilla import Bugzilla

from crashclouseau import config, inspector, models, pushlog, utils
from crashclouseau.eval.models import CorpusCase
from crashclouseau.logger import logger

_CLOUSEAU_ALIAS = "clouseau"
_BZ_FIELDS = ["id", "regressed_by", "cf_crash_signature", "assigned_to",
              "product", "component"]


def _first_signature(raw):
    if not raw:
        return ""
    # get_signatures returns an (unordered) set; pick deterministically so a re-freeze
    # of the same bug yields the same case.
    sigs = utils.get_signatures([raw])
    return sorted(sigs)[0] if sigs else ""


def mine_clouseau_bugs(start_date, end_date):
    """BMO bugs blocking the `clouseau` alias with a regressor + crash signature."""
    params = {
        "include_fields": _BZ_FIELDS,
        "f1": "blocked", "o1": "equals", "v1": _CLOUSEAU_ALIAS,
        "f2": "creation_ts", "o2": "greaterthaneq", "v2": start_date,
        "f3": "creation_ts", "o3": "lessthaneq", "v3": end_date,
    }
    records = []

    def handler(bug, data):
        if bug.get("regressed_by") and bug.get("cf_crash_signature"):
            data.append(bug)

    Bugzilla(params, bughandler=handler, bugdata=records).get_data().wait()
    logger.info("eval: mined %d clouseau bugs", len(records))
    return records


def _is_target_crash(data):
    """Keep only crashes Clouseau can triage: desktop Firefox, native (non-Java), with at
    least one symbolicated Firefox frame (a source file). Drops Fenix/Fennec/Thunderbird
    (unsupported), Java crashes, and system-only stacks like ``<unknown in ntdll.pdb>``."""
    if not data or data.get("java_stack_trace"):
        return False
    if (data.get("product") or "Firefox") != "Firefox":
        return False
    dump = data.get("json_dump") or {}
    ct = (dump.get("crash_info") or {}).get("crashing_thread", 0) or 0
    threads = dump.get("threads") or []
    frames = threads[ct]["frames"] if ct < len(threads) else []
    return any(f.get("file") for f in frames)


def _stack_files(data):
    """Basenames of source files on the crashing thread. Frame ``file`` is ``path:<rev>``
    (the source-link suffix), so strip the rev with ``inspector.get_path_node`` before
    taking the basename — otherwise nothing matches a changeset's clean filenames."""
    dump = data.get("json_dump") or {}
    ct = (dump.get("crash_info") or {}).get("crashing_thread", 0) or 0
    threads = dump.get("threads") or []
    frames = threads[ct]["frames"] if ct < len(threads) else []
    files = set()
    for f in frames:
        uri = f.get("file")
        if not uri:
            continue
        try:
            filename, _ = inspector.get_path_node(uri)
        except Exception:  # pragma: no cover - defensive
            filename = uri
        if filename:
            files.add(os.path.basename(filename))
    return files


def _seed_candidates(data, limit=20):
    """Approximate build_seed's scored candidates WITHOUT the DB: interesting-file
    changesets pushed in the ``ndays`` before the crash's build that touch a file on the
    crash stack, ranked by overlap. Queried by DATE (json-pushes), NOT buildid->revision
    (Buildhub is dead for that). Same KIND of seed as prod; like prod it won't surface an
    OFF-stack regressor (whose files aren't on the stack), so the agent must still reach
    those via the call graph. Frozen so the eval exercises the seed path, not cold."""
    buildid = data.get("build")
    channel = data.get("release_channel") or "nightly"
    if not buildid:
        return []
    try:
        end = utils.get_build_date(buildid)
    except Exception:
        return []
    start = end - timedelta(days=config.get_ndays())
    try:
        chgsets = pushlog.pushlog(start, end, channel) or []
    except Exception as exc:  # pragma: no cover - network
        logger.warning("eval: pushlog %s..%s failed: %s", start, end, exc)
        return []
    stack_files = _stack_files(data)
    cands = []
    for cs in chgsets:
        if cs.get("merge"):
            continue
        overlap = stack_files & {os.path.basename(f) for f in cs.get("files", [])}
        if overlap:
            cands.append({
                "node": cs["node"], "bug": cs.get("bug"),
                "backedout": cs.get("backedout"), "score": len(overlap),
            })
    cands.sort(key=lambda c: -c["score"])
    return cands[:limit]


def mine_regression_bugs(start_date, end_date, severities=("S1", "S2", "S3", "critical")):
    """INDEPENDENT ground truth: recent regression bugs (a ``regressed_by`` changeset +
    a crash signature), NOT restricted to the clouseau alias — used when that alias is
    empty, and non-circular (we don't score Clouseau on bugs Clouseau filed). Restricted
    to higher severities so the corpus stays crashes worth triaging."""
    params = {
        "include_fields": _BZ_FIELDS,
        "f1": "regressed_by", "o1": "isnotempty",
        "f2": "cf_crash_signature", "o2": "isnotempty",
        "f3": "creation_ts", "o3": "greaterthaneq", "v3": start_date,
        "f4": "creation_ts", "o4": "lessthaneq", "v4": end_date,
        "f5": "bug_severity", "o5": "anyexact", "v5": ",".join(severities),
    }
    records = []

    def handler(bug, data):
        if bug.get("regressed_by") and bug.get("cf_crash_signature"):
            data.append(bug)

    Bugzilla(params, bughandler=handler, bugdata=records).get_data().wait()
    logger.info("eval: mined %d regression bugs", len(records))
    return records


def resolve_uuids(signature, channel="nightly", limit=3):
    """Representative nightly UUID(s) for a signature via SuperSearch."""
    params = {
        "signature": "=" + signature,
        "release_channel": channel,
        "_columns": ["uuid"],
        "_results_number": limit,
    }
    uuids = []

    def handler(res, data):
        for hit in res.get("hits", []):
            if hit.get("uuid"):
                data.append(hit["uuid"])

    socorro.SuperSearch(params=params, handler=handler, handlerdata=uuids).wait()
    return uuids


_BACKOUT_RE = re.compile(r"back(?:ed)?\s*out", re.I)


def _regressor_nodes(bug_ids, channels=("nightly", "beta", "release")):
    """Landing changeset short-revs for the regressor bug(s) — the changesets that
    INTRODUCED the regression, parsed from their Bugzilla landing comments via
    libmozdata (backout comments skipped). This is the ACTUAL ground-truth regressor:
    ``regressed_by`` is a list of bug IDs, NOT changesets, so the crash bug's own id is
    useless here. Best-effort — a bug whose landings don't parse (e.g. a git-only landing
    comment) just contributes no nodes, and bug-id matching still carries the eval."""
    if not bug_ids:
        return []
    comments: dict = {}

    def handler(data, bugid):
        comments[bugid] = data.get("comments", [])

    try:
        Bugzilla(
            [str(b) for b in bug_ids], commenthandler=handler
        ).get_data().wait()
    except Exception as exc:  # pragma: no cover - network
        logger.warning("eval: could not fetch regressor comments for %s: %s", bug_ids, exc)
        return []

    nodes = set()
    for cmts in comments.values():
        for landing in Bugzilla.get_landing_comments(cmts, list(channels)):
            if _BACKOUT_RE.search(landing["comment"].get("text", "")):
                continue
            nodes.add(landing["revision"][:12])
    return sorted(nodes)


def _seed_nodes(uuid):
    res, _ = models.CrashStack.get_by_uuid(uuid)
    nodes = set()
    for frame in res.get("frames", []):
        nodes.update((frame.get("changesets") or {}).keys())
    return sorted(nodes)


def _write_manifest(corpus_dir, cases, harvested_iso):
    digest = hashlib.sha256(
        "".join(sorted(c.uuid for c in cases)).encode()
    ).hexdigest()[:16]
    manifest = {
        "corpus_hash": digest,
        "harvested": harvested_iso,
        "n_cases": len(cases),
        "uuids": [c.uuid for c in cases],
    }
    with open(os.path.join(corpus_dir, "manifest.json"), "w") as handle:
        json.dump(manifest, handle, indent=2)
    return digest


def freeze(records, corpus_dir=None):
    """Freeze each mined record to disk; returns the list of frozen CorpusCases."""
    corpus_dir = corpus_dir or config.get_eval().get("corpus_dir", "corpus")
    os.makedirs(corpus_dir, exist_ok=True)
    cases = []
    seen: set = set()
    for rec in records:
        sig = _first_signature(rec.get("cf_crash_signature"))
        if not sig:
            continue
        # Many regression signatures no longer crash on nightly (fixed there) but still
        # do on beta/release — try each channel so the corpus isn't starved by the
        # nightly-only + 30-day-retention constraint.
        uuid = channel = None
        for ch in ("nightly", "beta", "release"):
            found = resolve_uuids(sig, channel=ch)
            if found:
                uuid, channel = found[0], ch
                break
        if not uuid:
            logger.warning("eval: no uuid (any channel) for %r", sig)
            continue
        if uuid in seen:  # two bugs can resolve to the same representative crash
            continue
        seen.add(uuid)
        data = inspector.get_crash_data(uuid)
        if not _is_target_crash(data):
            logger.info("eval: skip non-target crash %s (%r)", uuid, sig[:60])
            continue
        case_dir = os.path.join(corpus_dir, uuid)
        os.makedirs(case_dir, exist_ok=True)
        crash_path = os.path.join(case_dir, "processed_crash.json")
        with open(crash_path, "w") as handle:
            json.dump(data, handle)
        reg_bugs = [int(b) for b in (rec.get("regressed_by") or []) if b]
        reg_nodes = _regressor_nodes(reg_bugs)
        case = CorpusCase(
            uuid=uuid,
            signature=sig,
            regressor_node=(reg_nodes[0] if reg_nodes else ""),
            regressor_nodes=reg_nodes,
            regressor_bug=(reg_bugs[0] if reg_bugs else None),
            regressor_bugs=reg_bugs,
            channel=channel,
            crash_json_path=crash_path,
            seed_nodes=_seed_nodes(uuid),
            candidates=_seed_candidates(data),
        )
        with open(os.path.join(case_dir, "case.json"), "w") as handle:
            handle.write(case.model_dump_json(indent=2))
        cases.append(case)
    _write_manifest(corpus_dir, cases, datetime.now(timezone.utc).isoformat())
    logger.info("eval: froze %d cases to %s", len(cases), corpus_dir)
    return cases


def load_corpus(corpus_dir=None):
    """Load the frozen corpus; returns (cases, corpus_hash)."""
    corpus_dir = corpus_dir or config.get_eval().get("corpus_dir", "corpus")
    with open(os.path.join(corpus_dir, "manifest.json")) as handle:
        manifest = json.load(handle)
    cases = []
    for uuid in manifest.get("uuids", []):
        path = os.path.join(corpus_dir, uuid, "case.json")
        with open(path) as handle:
            cases.append(CorpusCase.model_validate_json(handle.read()))
    return cases, manifest.get("corpus_hash", "")


def save_case(case, corpus_dir=None):
    """Persist a (re-labeled) case back to disk."""
    corpus_dir = corpus_dir or config.get_eval().get("corpus_dir", "corpus")
    path = os.path.join(corpus_dir, case.uuid, "case.json")
    with open(path, "w") as handle:
        handle.write(case.model_dump_json(indent=2))
