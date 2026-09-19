# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Replay the signature-family lookup over the crash bugs Clouseau filed. READS ONLY.

    uv run python bin/replay_sigfamily.py                       # every filing in the audit
    uv run python bin/replay_sigfamily.py --acceptance           # the plans/24 acceptance set
    uv run python bin/replay_sigfamily.py --bug 2073210 --bug 2069647 --venues
    uv run python bin/replay_sigfamily.py --out /tmp/replay.json

For each filing (``spike/sigchange/analysis.json`` + the ``sweep*_N_M.json`` dumps: signature,
proto, uuid, product, channel, build, filing time) this asks ``sigfamily.lookup`` AS OF THE FILING
INSTANT (``until=``) and prints what the pipeline would now know: the handoff predecessors with
their numbers and alignment, the live siblings, the family's first-seen, whether the name is
unsymbolicated, and -- for the spike filings -- whether the sweep would have declined the spike
as a re-bucketing. ``--venues`` adds today's open Bugzilla bugs under every name of the crash,
marking the ones reached through another name; that half is today's BMO, not the filing day's
(1737467 carries `CheckLogMessage` since :bobowen attached it on 2026-09-18).

The acceptance set is plans/24 §4: 2073210 -> `PatchNtdll`; 2069647 -> `WebGPUParent::
MapCallback`; 2071620 / 2071606 -> a spike decline; 2070554 -> `WaitOnAddress`, date-aligned;
2069648 / 2061962 -> unsymbolicated; 2070376 -> the 3-frame BitSet spelling as a sibling; 2072770
-> `shutdownhang | CanEnterBaselineJIT`. And what must NOT change: 2061960 (`nsFind`), 2062286
(`FindSafeLength`), 2062119 (`MimeService`) -- FIXED filings on old names, no predecessor.

Two to four SuperSearches per filing, in two round-trips; the whole audit is ~150 filings and
takes a few minutes. Nothing is written anywhere.
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crashclouseau import sigfamily  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIT = os.path.join(ROOT, "spike", "sigchange")


def _has(names, part):
    return any(part in n for n in names)


# bug -> (what must hold, a predicate over the replay record)
ACCEPTANCE = {
    2073210: ("predecessor `sandbox::InterceptionManager::PatchNtdll`",
              lambda r: "sandbox::InterceptionManager::PatchNtdll" in r["predecessors"]),
    2069647: ("predecessor `mozilla::webgpu::WebGPUParent::MapCallback`",
              lambda r: any("WebGPUParent::MapCallback" in p for p in r["predecessors"])),
    2071620: ("spike decline (re-bucketing from `OOM | unknown | ...`)",
              lambda r: bool(r["spike_handoff"])),
    2071606: ("spike decline (re-bucketing from `OOM | unknown | ...`)",
              lambda r: bool(r["spike_handoff"])),
    2070554: ("predecessor `shutdownhang | ... WaitOnAddress`, date-aligned",
              lambda r: _has(r["predecessors"], "WaitOnAddress") and r["alignment"] == "date"),
    2069648: ("unsymbolicated", lambda r: r["unsymbolicated"]),
    2061962: ("unsymbolicated", lambda r: r["unsymbolicated"]),
    2070376: ("the 3-frame BitSet spelling among the siblings",
              lambda r: any(s.count(" | ") == 2 and "BitSetIter" in s for s in r["siblings"])),
    2072770: ("predecessor `shutdownhang | CanEnterBaselineJIT`",
              lambda r: "shutdownhang | CanEnterBaselineJIT" in r["predecessors"]),
    2061960: ("NO predecessor (FIXED on an old name)", lambda r: not r["predecessors"]),
    2062286: ("NO predecessor (FIXED on an old name)", lambda r: not r["predecessors"]),
    2062119: ("NO predecessor (FIXED on an old name)", lambda r: not r["predecessors"]),
}


def _load():
    rows = {r["bug"]: r for r in json.load(open(os.path.join(AUDIT, "analysis.json")))}
    recs = {}
    dumps = glob.glob(os.path.join(AUDIT, "sweep_*_*.json"))
    dumps += glob.glob(os.path.join(AUDIT, "sweepch_*_*.json"))
    for fn in sorted(dumps):
        for r in json.load(open(fn)):
            recs.setdefault(r["bug"], {}).update(
                {k: v for k, v in r.items()
                 if k in ("uuid", "proto", "crash", "product_q", "creation_time", "signature")})
    out = []
    for bug, r in sorted(rows.items()):
        rec = recs.get(bug, {})
        crash = rec.get("crash") or {}
        out.append({
            "bug": bug, "signature": r.get("sig") or rec.get("signature"),
            "proto": rec.get("proto") or r.get("proto"), "uuid": rec.get("uuid"),
            "product": crash.get("product") or rec.get("product_q") or "Firefox",
            "channel": crash.get("channel") or r.get("channel"),
            "build": crash.get("build"), "filed": rec.get("creation_time") or r.get("filed"),
            "prototype_class": r.get("class"), "status": r.get("status"),
            "resolution": r.get("res"), "dupe_of": r.get("dupe_of"),
        })
    return out


def _until(text):
    text = (text or "")[:19]
    try:
        return datetime.fromisoformat(text.replace("Z", "")).replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def replay(item, venues=False):
    sig = item["signature"] or ""
    until = _until(item["filed"])
    fam = sigfamily.lookup(sig, item["proto"], item["product"], item["channel"], item["build"],
                           until=until)
    spike_handoff = None
    if fam["predecessors"] and item["build"]:
        spike_handoff = sigfamily.handoff_for_spike(sig, item["proto"], item["product"],
                                                    item["channel"], item["build"], until=until)
    rec = {
        "bug": item["bug"], "signature": sig, "channel": item["channel"], "filed": item["filed"],
        "prototype_class": item["prototype_class"], "status": item["status"],
        "resolution": item["resolution"], "dupe_of": item["dupe_of"],
        "lookup": fam["lookup"], "s_first_build": fam.get("s_first_build"),
        "at_wall": fam.get("at_wall"),
        "predecessors": [p["signature"] for p in fam["predecessors"]],
        "predecessor_rows": [{k: p.get(k) for k in ("signature", "relation", "before", "after",
                                                    "after_dates", "expected_after", "alignment",
                                                    "first_seen_ever", "change")}
                             for p in fam["predecessors"]],
        "siblings": [s["signature"] for s in fam["siblings"]],
        "sibling_rows": [{k: s.get(k) for k in ("signature", "relation", "status", "before",
                                                "after", "total_all_channels")}
                         for s in fam["siblings"]],
        "family_first_seen_ever": fam.get("family_first_seen_ever"),
        "alignment": fam.get("alignment"), "fan_in": fam.get("fan_in"),
        "unsymbolicated": sigfamily.is_unsymbolicated(sig),
        "spike_handoff": (spike_handoff or {}).get("signature") if spike_handoff else None,
    }
    if venues and (fam["predecessors"] or fam["siblings"]):
        from crashclouseau import bugzilla_apply

        rows = bugzilla_apply._open_bugs_for_signature(sig, family=fam)
        rec["venues_today"] = [{"id": b["id"], "via": b.get("via_signature"),
                                "relation": b.get("via_relation"), "since": b.get("venue_since"),
                                "created": b.get("creation_time")} for b in rows or []]
    return rec


def _print(rec):
    head = "### {bug} [{status} {resolution} {dupe}] {ch} filed={filed} prototype={proto}".format(
        bug=rec["bug"], status=rec["status"], resolution=rec["resolution"] or "",
        dupe=rec["dupe_of"] or "", ch=rec["channel"], filed=(rec["filed"] or "")[:10],
        proto=rec["prototype_class"])
    print(head)
    print("    S = {}".format(rec["signature"][:150]))
    print("    lookup={} s_first_build={} at_wall={} unsymbolicated={} fan_in={} alignment={} "
          "family_first_seen_ever={} spike_handoff={}".format(
              rec["lookup"], rec["s_first_build"], rec["at_wall"], rec["unsymbolicated"],
              rec["fan_in"], rec["alignment"], rec["family_first_seen_ever"],
              "yes" if rec["spike_handoff"] else "no"))
    for p in rec["predecessor_rows"]:
        print("    - PREDECESSOR {rel} before={b} after={a} after_dates={ad} expected={e} "
              "align={al} ever={ev}\n        P = {s}".format(
                  rel=p["relation"], b=p["before"], a=p["after"], ad=p["after_dates"],
                  e=p["expected_after"], al=p["alignment"], ev=p["first_seen_ever"],
                  s=(p["signature"] or "")[:150]))
        if p.get("change"):
            print("        {}".format(p["change"][:200]))
    for s in rec["sibling_rows"]:
        print("    - sibling {st} ({rel}) before={b} after={a} all_channels={t}\n        P = {s}".format(
            st=s["status"], rel=s["relation"], b=s["before"], a=s["after"],
            t=s["total_all_channels"], s=(s["signature"] or "")[:150]))
    for v in rec.get("venues_today") or []:
        print("    - venue today: bug {} created {}{}".format(
            v["id"], (v["created"] or "")[:10],
            " via {} `{}` since {}".format(v["relation"], v["via"], (v["since"] or "")[:10])
            if v["via"] else ""))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bug", type=int, action="append", default=[])
    parser.add_argument("--acceptance", action="store_true",
                        help="only the plans/24 acceptance set, with a pass/fail table")
    parser.add_argument("--venues", action="store_true",
                        help="also list today's open BMO bugs under every name (a BMO read)")
    parser.add_argument("--out", help="write every replay record as JSON here")
    args = parser.parse_args(argv)
    items = _load()
    wanted = set(args.bug) | (set(ACCEPTANCE) if args.acceptance else set())
    if wanted:
        items = [i for i in items if i["bug"] in wanted]
    records = []
    for item in items:
        if not item["signature"]:
            print("### {} -- no signature in the audit data".format(item["bug"]))
            continue
        try:
            rec = replay(item, venues=args.venues)
        except Exception as exc:  # noqa: BLE001 - keep replaying
            print("### {} -- replay failed: {}".format(item["bug"], exc))
            continue
        records.append(rec)
        _print(rec)
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(records, fh, indent=1)
    by_bug = {r["bug"]: r for r in records}
    checks = [(b, why, fn) for b, (why, fn) in sorted(ACCEPTANCE.items()) if b in by_bug]
    if checks:
        print("\n=== acceptance (plans/24 section 4) ===")
        failed = 0
        for bug, why, fn in checks:
            ok = bool(fn(by_bug[bug]))
            failed += not ok
            print("  {} {}: {}".format("PASS" if ok else "FAIL", bug, why))
        print("  {} of {} hold".format(len(checks) - failed, len(checks)))
    counted = len(records)
    with_pred = sum(1 for r in records if r["predecessors"])
    unsym = sum(1 for r in records if r["unsymbolicated"])
    failed_lookups = sum(1 for r in records if r["lookup"] == "failed")
    print("\n{} filings replayed: {} with a handoff predecessor, {} unsymbolicated, {} lookups "
          "failed".format(counted, with_pred, unsym, failed_lookups))
    return 0


if __name__ == "__main__":
    sys.exit(main())
