# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Does the corpus label noise actually corrupt the CALIBRATION, or only `regressor_node`?

`audit_corpus_labels.py` audited the SINGULAR `regressor_node` — the field my second-opinion
experiment fed as "the" candidate — and found 23% of it broken. It is tempting to conclude the
Phase-2 calibration table was therefore fit on noisy labels. That does not follow, and this
script settles it instead of assuming.

`metrics._hit` is `case_nodes & dossier_nodes` OR `case_bugs & dossier_bugs`. So calibration
depends on:
  * `regressor_nodes` — the PLURAL list, of which `regressor_node` is merely `sorted(...)[0]`
  * `regressor_bugs` — straight from Bugzilla `regressed_by`, untouched by node resolution

A case can therefore have a broken `regressor_node` while `regressor_nodes` still contains the
correct changeset, in which case the hit is legitimate and the calibration is unaffected.

The precise question: is any recorded HIT explained ONLY by a junk node? This audits every node
in every positive's list and cross-references the recorded `cited_node`. Free (hg only).

  DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
    uv run python spike/audit_all_regressor_nodes.py --corpus corpus_ship --out spike/ALL_NODES_AUDIT.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import crashclouseau.net  # noqa: E402,F401  (allowlisted UA for hg.mozilla.org)
from audit_corpus_labels import _NOOP_PATTERNS, _bug_in_desc, _pushed, _rev  # noqa: E402
from verify_so_timing_claims import _buildid_to_dt  # noqa: E402

import re  # noqa: E402


def _classify(node, channel, build_dt, case_bugs):
    """Why (if at all) this node cannot be the regressor of a crash in ``build_dt``."""
    out = {"node": node, "problems": []}
    rev = _rev(channel or "nightly", node)
    if not rev.get("node"):
        out["problems"].append("node not found on hg")
        return out
    desc = (rev.get("desc") or "").strip()
    out["desc"] = desc.splitlines()[0][:120] if desc else ""
    pushed = _pushed(rev)
    out["pushdate"] = pushed.isoformat() if pushed else None
    out["backedout"] = bool(rev.get("backedoutby"))
    if build_dt and pushed:
        delta = round((pushed - build_dt).total_seconds() / 86400.0, 1)
        out["days_after_build"] = delta
        if delta > 0:
            out["problems"].append("IMPOSSIBLE: landed {}d after the crash build".format(delta))
    low = desc.lower()
    for pattern, label in _NOOP_PATTERNS:
        if re.search(pattern, low):
            out["problems"].append("NO-OP COMMIT: {}".format(label))
            break
    desc_bug = _bug_in_desc(desc)
    out["bug_in_desc"] = desc_bug
    if case_bugs and desc_bug and desc_bug not in case_bugs:
        out["problems"].append("BUG MISMATCH: desc says bug {}".format(desc_bug))
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--corpus", default="corpus_ship")
    p.add_argument("--out", default="spike/ALL_NODES_AUDIT.json")
    p.add_argument("--workers", type=int, default=8)
    args = p.parse_args()

    cases = {}
    for path in glob.glob(os.path.join(args.corpus, "*", "case.json")):
        c = json.load(open(path))
        if not c.get("is_negative") and c.get("regressor_nodes"):
            cases[c["uuid"]] = c

    jobs = []
    for c in cases.values():
        try:
            raw = json.load(open(c["crash_json_path"]))
        except (OSError, json.JSONDecodeError):
            raw = {}
        build_dt = _buildid_to_dt(raw.get("build"))
        bugs = set(c.get("regressor_bugs") or [])
        for node in c["regressor_nodes"]:
            jobs.append((c["uuid"], node, c.get("channel"), build_dt, bugs))

    print("auditing {} (case, node) pairs across {} positives...".format(len(jobs), len(cases)))
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        got = list(pool.map(
            lambda j: {"uuid": j[0], **_classify(j[1], j[2], j[3], j[4])}, jobs
        ))

    by_uuid = {}
    for row in got:
        by_uuid.setdefault(row["uuid"], []).append(row)
    junk = {(r["uuid"], r["node"]) for r in got if r["problems"]}

    rows = [json.loads(line) for line in open(os.path.join(args.corpus, "results.jsonl"))
            if line.strip()]
    hits = [r for r in rows if r.get("hit") and not r.get("is_negative")]

    node_hit, node_hit_junk, bug_only, clean_nodes_left = 0, 0, 0, 0
    tainted = []
    for r in hits:
        c = cases.get(r["uuid"])
        if c is None:
            continue
        cited = r.get("cited_node")
        nodes = set(c.get("regressor_nodes") or [])
        if not cited or cited not in nodes:
            bug_only += 1
            continue
        node_hit += 1
        if (r["uuid"], cited) in junk:
            node_hit_junk += 1
            # Would the hit survive on the strength of the OTHER, clean nodes / the bug match?
            others = [n for n in nodes if (r["uuid"], n) not in junk]
            if others:
                clean_nodes_left += 1
            else:
                tainted.append({"uuid": r["uuid"], "cited_node": cited,
                                "problems": [x["problems"] for x in by_uuid[r["uuid"]]
                                             if x["node"] == cited]})

    summary = {
        "n_positives": len(cases),
        "n_node_pairs": len(jobs),
        "n_junk_nodes": len(junk),
        "pct_junk_nodes": round(100.0 * len(junk) / len(jobs), 1) if jobs else None,
        "n_positive_hits": len(hits),
        "hit_via_cited_node": node_hit,
        "hit_via_bug_match_only": bug_only,
        "hit_where_cited_node_is_junk": node_hit_junk,
        "  of_those_case_still_has_a_clean_node": clean_nodes_left,
        "TAINTED_HITS_no_clean_node_remains": len(tainted),
        "tainted": tainted,
    }
    with open(args.out, "w") as handle:
        json.dump({"summary": summary, "nodes": got}, handle, indent=2)
    print(json.dumps(summary, indent=2))
    print("\nwrote {}".format(args.out))


if __name__ == "__main__":
    main()
