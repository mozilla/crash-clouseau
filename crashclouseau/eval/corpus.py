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
from datetime import datetime, timezone

from libmozdata import socorro
from libmozdata.bugzilla import Bugzilla

from crashclouseau import config, inspector, models, utils
from crashclouseau.eval.models import CorpusCase
from crashclouseau.logger import logger

_CLOUSEAU_ALIAS = "clouseau"
_BZ_FIELDS = ["id", "regressed_by", "cf_crash_signature", "assigned_to",
              "product", "component"]


def _first_signature(raw):
    if not raw:
        return ""
    sigs = utils.get_signatures([raw])
    return sigs[0] if sigs else ""


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
    for rec in records:
        sig = _first_signature(rec.get("cf_crash_signature"))
        if not sig:
            continue
        uuids = resolve_uuids(sig)
        if not uuids:
            logger.warning("eval: no nightly uuid for %r", sig)
            continue
        uuid = uuids[0]
        data = inspector.get_crash_data(uuid)
        if not data:
            continue
        case_dir = os.path.join(corpus_dir, uuid)
        os.makedirs(case_dir, exist_ok=True)
        crash_path = os.path.join(case_dir, "processed_crash.json")
        with open(crash_path, "w") as handle:
            json.dump(data, handle)
        reg_git = (rec.get("regressed_by") or [None])[0]
        reg_hg = inspector.git2hg(reg_git) if reg_git else ""
        case = CorpusCase(
            uuid=uuid,
            signature=sig,
            regressor_node=reg_hg or "",
            regressor_bug=rec.get("id"),
            crash_json_path=crash_path,
            seed_nodes=_seed_nodes(uuid),
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
