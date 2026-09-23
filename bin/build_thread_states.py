# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Build ``config/thread_states.json``: the thread states common to several signatures' dumps.

    uv run python bin/build_thread_states.py --from DIR           # processed-crash JSON files
    uv run python bin/build_thread_states.py --fetch --per-cell 2  # sample hang reports

Count each state once per signature; retain keys seen in at least ``--min-signatures``.
Skip analysed, crashing and recognized crash-reporter threads. ``hang.census`` ranks
states in this table after other states; the prompt and tools cap the displayed rows.
"""
import argparse
import datetime
import glob
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_CELLS = (("Windows NT", "amd64"), ("Windows NT", "x86"), ("Windows NT", "arm64"),
          ("Mac OS X", None), ("Linux", None))


def _states(raw):
    from crashclouseau import hang, inspector

    dump = raw.get("json_dump") or {}
    threads = dump.get("threads") or []
    skip = {inspector.thread_for_analysis(raw),
            (dump.get("crash_info") or {}).get("crashing_thread")}
    kept = [t for i, t in enumerate(threads) if isinstance(t, dict) and i not in skip]
    return {hang.state_key(t["frames"]) for t in kept
            if t.get("frames") and not hang.is_reporter(t)}


def _from_dir(path):
    for name in sorted(glob.glob(os.path.join(path, "*.json"))):
        with open(name) as handle:
            yield json.load(handle)


def _fetch(signatures, per_cell, days):
    from libmozdata import socorro
    from crashclouseau import inspector

    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()

    def search(params):
        got = {}
        socorro.SuperSearch(params=params, handler=lambda j, d: d.update(j),
                            handlerdata=got).wait()
        return got

    top = search({"product": "Firefox", "signature": "^shutdownhang", "date": ">=" + since,
                  "_facets": "signature", "_facets_size": signatures, "_results_number": 0})
    for row in (top.get("facets") or {}).get("signature") or []:
        for platform, arch in _CELLS:
            params = {"product": "Firefox", "signature": "=" + row["term"], "date": ">=" + since,
                      "platform": platform, "_columns": "uuid", "_results_number": per_cell,
                      "_sort": "-date"}
            if arch:
                params["cpu_arch"] = arch
            for hit in search(params).get("hits") or []:
                raw = inspector.get_crash_data(hit["uuid"])
                if isinstance(raw, dict) and raw.get("json_dump"):
                    yield raw


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="source", help="directory of processed-crash JSON files")
    parser.add_argument("--fetch", action="store_true", help="sample shutdown hangs from Socorro")
    parser.add_argument("--signatures", type=int, default=60)
    parser.add_argument("--per-cell", type=int, default=2)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--min-signatures", type=int, default=3)
    parser.add_argument("--out", default=os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "config", "thread_states.json"))
    args = parser.parse_args()
    if bool(args.source) == bool(args.fetch):
        parser.error("give exactly one of --from and --fetch")
    reports = (_from_dir(args.source) if args.source
               else _fetch(args.signatures, args.per_cell, args.days))
    seen, count = defaultdict(set), 0
    for raw in reports:
        count += 1
        for key in _states(raw):
            seen[key].add(raw.get("signature") or "")
    signatures = {s for sigs in seen.values() for s in sigs}
    states = {k: len(v) for k, v in sorted(seen.items()) if k and len(v) >= args.min_signatures}
    out = {"built": datetime.date.today().isoformat(), "reports": count,
           "signatures": len(signatures), "min_signatures": args.min_signatures,
           "states": states}
    with open(args.out, "w") as handle:
        json.dump(out, handle, indent=1, sort_keys=True)
        handle.write("\n")
    print("{} reports, {} signatures, {} states kept of {}".format(
        count, len(signatures), len(states), len(seen)))


if __name__ == "__main__":
    main()
