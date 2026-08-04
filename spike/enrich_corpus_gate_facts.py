# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Backfill the ONLINE-ONLY gate facts onto an eval corpus's results, so the calibration can
score the pipeline we actually ship.

WHY. `eval/runner.py` calls `apply_deterministic_gates`, but three of the gates that matter
read facts nobody resolves offline: the stale-signature downweight needs
`signature_first_seen_buildid` + a candidate pushdate (`study_corpus.py` writes
`"pushdate": None` for every candidate), and both backout gates need hg lookups that live in
the online-only resolvers. So all 90 rows of `corpus_ship/results.jsonl` carry only
`call_path_verified` / `offstack_observe_only` / `exposer_*`, and the fitted rung -> P table
describes a pipeline that has not existed since 2026-07-27.

That matters beyond tidiness. The shipped table maps rung 70 and rung 85 to the SAME 0.9714,
because isotonic regression pooled them -- rung 85 scores WORSE than rung 70 on this corpus
(13/14 vs 21/21 positives-only; 9/13 vs 17/21 with negatives). A ladder keyed on the rung
alone cannot express "97%, unless the signature is 283 days older than the candidate", which
is the single strongest known predictor of a wrong lead. Conditioning on the flags needs the
flags, and this is where they come from.

Resolves per CITED node rather than per window candidate: the flags only ever apply to the
candidate the agent actually picked, which is 68 distinct nodes across the 90 rows instead of
several thousand. One Socorro lookup per case, one hg `json-rev` per node (cached, and it
carries pushdate + backedoutby + desc together), and a `json-pushes` only for nodes that are
themselves backouts.

Read-only against hg/Socorro; writes one jsonl. Resumable -- rows already present in the
output file are skipped, because hg 406-rate-limits a bulk reader and this will get
interrupted.

    uv run python spike/enrich_corpus_gate_facts.py corpus_ship
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau import pushlog, sigage  # noqa: E402


def _cases(corpus_dir):
    """uuid -> case.json, for the signature + channel the crash was triaged on."""
    out = {}
    for name in os.listdir(corpus_dir):
        path = os.path.join(corpus_dir, name, "case.json")
        if os.path.isfile(path):
            with open(path) as handle:
                case = json.load(handle)
            out[case.get("uuid", name)] = case
    return out


def _node_facts(node, channel, cache):
    """Everything the two backout gates and the stale gate need about one changeset.

    Tri-states are preserved as None so the caller can tell "clean" from "unknown" -- the
    gates treat them very differently and collapsing them here would bake in the wrong one."""
    if node in cache:
        return cache[node]
    rev = sigage.json_rev(node, channel)
    desc = rev.get("desc") or ""
    facts = {
        "resolved": bool(rev.get("node")),
        "pushdate": rev.get("pushdate"),
        "backedout_by": (rev.get("backedoutby") or "") if rev.get("node") else None,
        "is_backout": bool(desc) and pushlog.is_backed_out(desc),
        "backout_of_same_push": None,
        "desc_first_line": desc.splitlines()[0][:160] if desc else "",
    }
    if facts["is_backout"]:
        facts["backout_of_same_push"] = sigage.same_push_backout_target(node, channel)
    cache[node] = facts
    return facts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("corpus_dir")
    ap.add_argument("--out", default=None, help="default <corpus_dir>/results_gate_facts.jsonl")
    ap.add_argument("--sleep", type=float, default=0.5, help="pause between hg reads")
    args = ap.parse_args(argv)

    out_path = args.out or os.path.join(args.corpus_dir, "results_gate_facts.jsonl")
    done = set()
    if os.path.exists(out_path):
        with open(out_path) as handle:
            for line in handle:
                try:
                    done.add(json.loads(line)["uuid"])
                except Exception:
                    pass
        print("resuming: %d rows already enriched" % len(done))

    cases = _cases(args.corpus_dir)
    with open(os.path.join(args.corpus_dir, "results.jsonl")) as handle:
        rows = [json.loads(line) for line in handle]
    print("%d result rows, %d cases, %d distinct cited nodes"
          % (len(rows), len(cases), len({r.get("cited_node") for r in rows if r.get("cited_node")})))

    node_cache, first_seen_cache = {}, {}
    with open(out_path, "a") as out:
        for i, row in enumerate(rows, 1):
            uuid = row.get("uuid")
            if uuid in done:
                continue
            case = cases.get(uuid, {})
            signature = case.get("signature") or ""
            channel = case.get("channel") or "nightly"

            if signature not in first_seen_cache:
                first_seen_cache[signature] = sigage.first_seen_buildid(
                    signature, channel=channel
                )
            first_seen = first_seen_cache[signature]

            node = row.get("cited_node") or ""
            facts = _node_facts(node, channel, node_cache) if node else {}
            landed_after = None
            if first_seen and facts.get("pushdate"):
                landed_after = sigage.days_landed_after_first_seen(
                    first_seen, facts["pushdate"]
                )

            enriched = {
                **row,
                "signature": signature,
                "channel": channel,
                "signature_first_seen_buildid": first_seen,
                "candidate_pushdate": facts.get("pushdate"),
                "candidate_landed_after_first_seen_days": landed_after,
                "candidate_backedout_by": facts.get("backedout_by"),
                "candidate_is_backout": facts.get("is_backout"),
                "candidate_backout_of_same_push": facts.get("backout_of_same_push"),
                "candidate_desc": facts.get("desc_first_line", ""),
                "candidate_resolved": facts.get("resolved", False),
            }
            out.write(json.dumps(enriched) + "\n")
            out.flush()
            print("  [%d/%d] %s node=%s first_seen=%s landed_after=%s is_backout=%s"
                  % (i, len(rows), uuid[:8], node or "-", first_seen, landed_after,
                     facts.get("is_backout")))
            time.sleep(args.sleep)

    print("wrote %s" % out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
