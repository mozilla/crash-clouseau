"""Blind-SO precision check on the verdicts the schema-laxness fix RECOVERS.

The fix (39f5474) stops `_salvage` binning a verdict over a field the model could not know.
Measured over the prod corpus it recovers 37 verdicts — but recovery is not correctness, and
some of them become strong-evidence. This runs the calibrated blind second opinion over each
RECOVERED, REPORTED verdict and reports how many it refutes.

  uv run python spike/so_validate_recovered.py --plan    # free: list the set, estimate cost
  uv run python spike/so_validate_recovered.py --run     # spends real money
"""
import argparse
import asyncio
import copy
import importlib.util
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DUMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_dossier_dump.jsonl")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "SO_RECOVERED_VALIDATION.json")
PRE_FIX_REV = "b314116"      # the commit before the schema-laxness change


def _load_prefix_schema():
    """The schema as it was BEFORE the fix, as a scratch module (never touches the tree)."""
    src = subprocess.run(
        ["git", "show", "{}:crashclouseau/agent/schema.py".format(PRE_FIX_REV)],
        capture_output=True, text=True, check=True).stdout
    spec = importlib.util.spec_from_loader("_schema_prefix", loader=None, origin="schema.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_schema_prefix"] = mod
    exec(compile(src, "schema.py[{}]".format(PRE_FIX_REV), "exec"), mod.__dict__)
    return mod


def recovered_rows():
    """Dossiers whose verdict was DESTROYED before the fix and is REPORTED after it."""
    from crashclouseau.agent import schema as NEW
    OLD = _load_prefix_schema()
    out = []
    for line in open(DUMP):
        row = json.loads(line)
        raw = NEW._extract_last_json_block(row.get("result"))
        if not isinstance(raw, dict) or not isinstance(raw.get("verdict"), dict):
            continue
        before = OLD.parse_and_validate(copy.deepcopy(raw))
        if "validation failed" not in (before.verdict.abstain_reason or ""):
            continue                       # was not lost -> not a recovery
        after = NEW.parse_and_validate(copy.deepcopy(raw))
        decision = after.verdict.decision.value
        if decision == "abstain":
            continue                       # recovered, but still not reported: no risk
        cand = after.candidate
        out.append({
            "id": row["id"],
            "decision": decision,
            "confidence": after.verdict.confidence.value,
            "node": cand.node if cand else "",
            "bug": cand.bug if cand else None,
            "mechanism": (after.verdict.mechanism.statement if after.verdict.mechanism else "")[:300],
        })
    return out


def attach_uuids(rows):
    from sqlalchemy import create_engine, text
    url = os.environ["DATABASE_URL"].replace("postgres://", "postgresql://")
    ids = [r["id"] for r in rows]
    with create_engine(url, connect_args={"sslmode": "require"}).connect() as c:
        m = {r[0]: (r[1], r[2]) for r in c.execute(text(
            "select d.id, u.uuid, coalesce(s.signature,'') from dossiers d "
            "join uuids u on u.id=d.uuidid left join signatures s on s.id=u.signatureid "
            "where d.id = any(:ids)"), {"ids": ids})}
    for r in rows:
        r["uuid"], r["signature"] = m.get(r["id"], ("", ""))
    return [r for r in rows if r["uuid"]]


async def _one(row):
    from crashclouseau.agent.orchestrator import build_seed
    from crashclouseau.agent.second_opinion import run_second_opinion
    seed = build_seed(row["uuid"])
    cand = {"node": row["node"], "bug": row["bug"]} if row["node"] else None
    so = await run_second_opinion(seed, cand)
    if so is None:
        return {**row, "so": None, "error": "second opinion returned nothing"}
    return {**row, "so": {
        "mode": so.mode, "corroborates": so.corroborates, "confidence": so.confidence,
        "mechanism": so.mechanism, "refutation": so.refutation, "cost_usd": so.cost_usd,
    }}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="actually spend money")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    rows = attach_uuids(recovered_rows())
    if args.limit:
        rows = rows[:args.limit]
    print("RECOVERED + REPORTED verdicts: {}".format(len(rows)))
    by = {}
    for r in rows:
        by[(r["decision"], r["confidence"])] = by.get((r["decision"], r["confidence"]), 0) + 1
    for k in sorted(by):
        print("   {:<16} {:<9} x{}".format(k[0], k[1], by[k]))
    print("   with a candidate changeset: {}".format(sum(1 for r in rows if r["node"])))
    print("   estimated SO cost @ $1.07/run: ${:.0f}".format(1.07 * len(rows)))
    if not args.run:
        print("\n--plan only; re-run with --run to spend.")
        for r in rows:
            print("   {:>5} {:<16} {:<9} {} bug={}".format(
                r["id"], r["decision"], r["confidence"], (r["node"] or "-")[:12], r["bug"]))
        return

    results = []
    for i, r in enumerate(rows, 1):
        print("[{}/{}] dossier {} {} {} -> ".format(i, len(rows), r["id"], r["decision"],
                                                    (r["node"] or "-")[:12]), end="", flush=True)
        try:
            res = asyncio.run(_one(r))
        except Exception as exc:                                  # noqa: BLE001
            res = {**r, "so": None, "error": "{}: {}".format(type(exc).__name__, exc)}
        so = res.get("so")
        print("corroborates={} conf={} ${}".format(
            so["corroborates"], so["confidence"], so["cost_usd"]) if so else
            "FAILED ({})".format(res.get("error", "")[:60]))
        results.append(res)
        json.dump(results, open(OUT, "w"), indent=1, default=str)

    ok = [r for r in results if r.get("so")]
    ref = [r for r in ok if r["so"]["corroborates"] is False]
    cor = [r for r in ok if r["so"]["corroborates"] is True]
    hi = [r for r in ref if (r["so"]["confidence"] or "").lower() == "high"]
    print("\n=== RESULT ===")
    print("ran {} / {} ({} failed)".format(len(ok), len(results), len(results) - len(ok)))
    print("REFUTED    : {} ({} at high confidence)".format(len(ref), len(hi)))
    print("corroborated: {}".format(len(cor)))
    print("unsure      : {}".format(len(ok) - len(ref) - len(cor)))
    print("spend       : ${:.2f}".format(sum(r["so"]["cost_usd"] or 0 for r in ok)))
    print("written to", OUT)


if __name__ == "__main__":
    main()
