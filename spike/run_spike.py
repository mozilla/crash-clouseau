"""Phase-0 runner: off-stack recall (call-graph neighborhood) vs stack-only.

Usage:
    python -m spike.run_spike --corpus spike/corpus.json --mode both

For each corpus case it fetches the crash, seeds from the crashing-thread frame
functions, builds a searchfox neighborhood (mechanical BFS and/or a Haiku loop),
and checks whether the regressor's changed functions are reached -- reporting
**stack-only recall** (today's baseline) beside **neighborhood recall** (the win).

Nothing here touches the DB or the ``crashclouseau`` package. It needs a curated
corpus (see ``spike/corpus.example.json``), ``searchfox-cli`` on ``PATH``, and --
for ``--mode llm`` -- ``ANTHROPIC_API_KEY``.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path

from . import crash, explore, regressor_funcs
from .searchfox_cli import DEFAULT_DEPTH, DEFAULT_REPO

log = logging.getLogger("spike.run_spike")

_TEMPLATE_RE = re.compile(r"<[^<>]*>")


def _norm(sym: str) -> str:
    """Normalise a symbol for comparison: drop args list, template args, spaces."""
    s = sym.split("(", 1)[0]
    prev = None
    while prev != s:  # collapse nested <...>
        prev = s
        s = _TEMPLATE_RE.sub("", s)
    return s.strip()


def _match(targets, symbols) -> dict:
    """target function name -> matched neighborhood symbol (tightened).

    Requires the full ``Class::method`` (or a namespaced suffix of it) to match --
    NOT just the trailing method component, which collided on common names
    (``create``, ``OnFocus`` seen in real runs). Bare names (Rust/C free functions)
    match on an exact ``::``-suffix and must be >= 6 chars. Truncated ``@@``
    headings (``WebGPUParent::RecvCreateB``) match as a guarded substring. Hits are
    still worth eyeballing, but this kills the last-component false positives.
    """
    norm_syms = {_norm(s) for s in symbols if s}
    hits: dict[str, str] = {}
    for t in targets:
        nt = _norm(t)
        if not nt:
            continue
        bare = "::" not in nt
        last = nt.split("::")[-1]
        for s in norm_syms:
            if s == nt or s.endswith("::" + nt):
                if bare and len(nt) < 6:  # guard short bare-name collisions
                    continue
                hits[t] = s
                break
            if not bare and len(last) >= 6 and nt in s:  # truncated @@ heading
                hits[t] = s
                break
    return hits


def _resolve_uuid(case: dict) -> str | None:
    """Frozen ``uuid`` from the case, else a best-effort SuperSearch resolution."""
    if case.get("uuid"):
        return case["uuid"]
    sig = case.get("signature")
    win = case.get("build_window") or {}
    if not sig:
        return None
    try:  # FIXME: SuperSearch param names/handler shape unverified (plan risk).
        from libmozdata import socorro

        params = {
            "signature": "=" + sig,
            "release_channel": case.get("channel", "nightly"),
            "product": "Firefox",
            "_columns": ["uuid"],
            "_results_number": 1,
        }
        if win.get("start") and win.get("end"):
            params["build_id"] = [">=" + win["start"] + "000000", "<=" + win["end"] + "235959"]
        found: list = []
        socorro.SuperSearch(
            params=params,
            handler=lambda j, d: d.extend(j.get("hits", [])),
            handlerdata=found,
        ).wait()
        if found:
            return found[0].get("uuid")
    except Exception as e:
        log.warning("SuperSearch uuid resolution failed for %r: %s", sig, e)
    return None


def _regressor_targets(case: dict, resolve_defs: bool, repo: str):
    funcs: set[str] = set()
    files: set[str] = set()
    for node in case.get("regressor_nodes", []):
        cf = regressor_funcs.changed_functions(
            node, channel=case.get("channel", "nightly"),
            vcs=case.get("vcs", "hg"), resolve=resolve_defs, repo=repo,
            cpp_only=bool(case.get("cpp")),
        )
        funcs |= cf.targets()
        files |= cf.files
    return funcs, files


def run_case(case: dict, args) -> dict:
    result: dict = {
        "clouseau_bug": case.get("clouseau_bug"),
        "regressor_bug": case.get("regressor_bug"),
        "regressor_nodes": case.get("regressor_nodes") or [],
        "skipped": None,
    }
    if not result["regressor_nodes"]:
        result["skipped"] = "no regressor_nodes"
        return result

    reg_funcs, reg_files = _regressor_targets(case, args.resolve, args.repo)
    result["regressor_funcs"] = sorted(reg_funcs)
    result["regressor_files"] = sorted(reg_files)
    if not reg_funcs:
        result["skipped"] = (
            "no regressor function targets (non-code diff, @@-parse failed, or a "
            "wrong/follow-up commit was picked for the regressor bug)"
        )
        return result

    uuid = _resolve_uuid(case)
    if not uuid:
        result["skipped"] = "no uuid (add one to the case, or fix SuperSearch resolution)"
        return result
    result["uuid"] = uuid

    data = crash.fetch_processed(uuid)
    if not data:
        result["skipped"] = "crash fetch failed (aged out of Socorro? install socorro-cli/libmozdata)"
        return result

    frames = crash.crashing_frames(data)
    seed = crash.frame_functions(frames)
    result["n_frames"] = len(frames)
    result["n_seed_funcs"] = len(seed)
    result["frame_files"] = sorted(crash.frame_files(frames))  # for miss classification

    stack_hits = _match(reg_funcs, seed)
    stack_file_hit = bool(reg_files & crash.frame_files(frames))
    result["stack_only_hit"] = bool(stack_hits) or stack_file_hit
    result["stack_matched"] = stack_hits

    the_brief = crash.brief(data, frames)
    modes = ["mechanical", "llm"] if args.mode == "both" else [args.mode]
    result["modes"] = {}
    for mode in modes:
        if mode == "mechanical":
            er = explore.mechanical_neighborhood(
                seed,
                hops=args.hops,
                depth=args.depth,
                repo=args.repo,
                budget_queries=args.budget_queries,
            )
        else:
            er = explore.llm_neighborhood(
                the_brief,
                seed,
                model=args.model,
                budget_queries=args.budget_queries,
                depth=args.depth,
                repo=args.repo,
            )
        hits = _match(reg_funcs, er.symbols)
        result["modes"][mode] = {
            **er.as_dict(),
            "neighborhood_hit": bool(hits),
            "matched": hits,
        }
    return result


def aggregate(results: list[dict], modes: list[str]) -> dict:
    scored = [r for r in results if not r.get("skipped")]
    n = len(scored)
    agg: dict = {"n_cases": n, "n_skipped": len(results) - n}
    if not n:
        return agg
    agg["stack_only_recall"] = round(sum(r["stack_only_hit"] for r in scored) / n, 3)
    # The cases that matter: stack-only already missed -> did the neighborhood add value?
    offstack = [r for r in scored if not r["stack_only_hit"]]
    agg["n_stack_missed"] = len(offstack)
    for mode in modes:

        def hit(r):
            return r["modes"].get(mode, {}).get("neighborhood_hit", False)

        agg[f"{mode}_recall"] = round(sum(hit(r) for r in scored) / n, 3)
        if offstack:
            agg[f"{mode}_recall_on_stack_misses"] = round(
                sum(hit(r) for r in offstack) / len(offstack), 3
            )
    return agg


def _print_table(results: list[dict], agg: dict, modes: list[str]) -> None:
    print("\n=== Phase-0 call-graph recall ===")
    header = f"{'clouseau':>9} {'regr':>7} {'stack':>6} " + " ".join(
        f"{m[:4]+':nbhd':>10}" for m in modes
    )
    print(header)
    for r in results:
        if r.get("skipped"):
            print(f"{str(r.get('clouseau_bug')):>9} {str(r.get('regressor_bug')):>7}  SKIP  ({r['skipped']})")
            continue
        row = f"{str(r['clouseau_bug']):>9} {str(r['regressor_bug']):>7} {'HIT' if r['stack_only_hit'] else '-':>6} "
        row += " ".join(
            f"{('HIT' if r['modes'][m]['neighborhood_hit'] else '-') + '/' + str(r['modes'][m]['neighborhood_size']):>10}"
            for m in modes
        )
        print(row)
    print("\n--- aggregate ---")
    for k, v in agg.items():
        print(f"  {k}: {v}")
    print(
        "\nGO if a neighborhood recall materially exceeds stack_only_recall on the "
        "stack-miss subset; NO-GO if searchfox holes dominate. Eyeball each HIT's "
        "`matched` pair before trusting it (name collisions).\n"
    )


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Phase-0 off-stack call-graph recall spike")
    ap.add_argument("--corpus", default="spike/corpus.json")
    ap.add_argument("--mode", choices=["mechanical", "llm", "both"], default="mechanical")
    ap.add_argument("--model", default=explore._MODEL_DEFAULT)
    ap.add_argument("--budget-queries", type=int, default=40)
    ap.add_argument("--hops", type=int, default=2, help="mechanical BFS hops")
    ap.add_argument("--depth", type=int, default=DEFAULT_DEPTH, help="searchfox --depth")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument(
        "--resolve",
        action="store_true",
        help="canonicalise regressor funcs via searchfox --define (network, drift-prone)",
    )
    ap.add_argument("--out", default="spike/results.json")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    corpus_path = Path(args.corpus)
    if not corpus_path.exists():
        raise SystemExit(
            f"corpus not found: {corpus_path}\n"
            "Copy spike/corpus.example.json to it and curate it, "
            "or run `python -m spike.mine_corpus`."
        )
    cases = json.loads(corpus_path.read_text())
    modes = ["mechanical", "llm"] if args.mode == "both" else [args.mode]

    results = []
    for i, case in enumerate(cases, 1):
        label = (
            f"[{i}/{len(cases)}] clouseau_bug={case.get('clouseau_bug')} "
            f"regressor_bug={case.get('regressor_bug')}"
        )
        log.info("%s -- running", label)
        r = run_case(case, args)
        if r.get("skipped"):
            log.warning("%s -- SKIPPED: %s", label, r["skipped"])
        results.append(r)

    agg = aggregate(results, modes)
    out = {
        "config": {
            "mode": args.mode,
            "model": args.model,
            "budget_queries": args.budget_queries,
            "hops": args.hops,
            "depth": args.depth,
            "repo": args.repo,
            "resolve": args.resolve,
            "corpus": str(corpus_path),
        },
        "aggregate": agg,
        "cases": results,
    }
    Path(args.out).write_text(json.dumps(out, indent=2))
    _print_table(results, agg, modes)
    log.info("wrote %s", args.out)


if __name__ == "__main__":
    main()
