# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Audit the study corpus's `regressor_node` labels. Arithmetic only, no LLM.

Prompted by `so_instrument_calibration.py`: of the 7 cases where the blind second opinion
"wrongly" refuted a KNOWN regressor, most turned out not to be instrument errors at all —
the LABEL was wrong. Observed by hand:

  * two labels are Lando AUTO-FORMATTING commits (entire diff is whitespace/bracket movement)
  * one node belongs to a different bug than the label claims (a Fenix UI-test revert)
  * two landed AFTER the crashing build was produced, which is causally impossible

That matters beyond one experiment: this corpus is what the Phase-2 calibration table was fit
on, so label noise propagates into the shipped `p_worth_investigating` numbers. And it means the
measured sensitivity (72%) is an UNDER-estimate of the instrument.

Three checks per positive case:

  1. IMPOSSIBLE — regressor_node pushed AFTER the crash build's buildid. A regressor must land
     before the build that crashes. This one needs no judgement and has no caveat (unlike the
     "signature first-seen" argument, which signature reuse can confound).
  2. NO-OP COMMIT — the changeset description marks it as an automated reformat / backout /
     merge, i.e. something that cannot introduce a crash by itself.
  3. BUG MISMATCH — the bug number in the changeset description differs from `regressor_bug`.

  DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
    uv run python spike/audit_corpus_labels.py --corpus corpus_ship --out spike/CORPUS_LABEL_AUDIT.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import crashclouseau.net  # noqa: E402,F401  (allowlisted UA for hg.mozilla.org)
from crashclouseau.eval.corpus import load_corpus                        # noqa: E402
from libmozdata.hgmozilla import Revision                                # noqa: E402
from verify_so_timing_claims import _buildid_to_dt                       # noqa: E402

# Descriptions that cannot themselves introduce a crash.
_NOOP_PATTERNS = (
    (r"apply code formatting via lando", "lando auto-format"),
    (r"\bno bug\b.*\b(reformat|format|clang-format|rustfmt|prettier)", "auto-format"),
    (r"^backed out changeset", "backout"),
    (r"^merge (mozilla-central|autoland|inbound|beta|release)", "merge"),
    (r"^no bug - tagging", "release tagging"),
    (r"add tests? for|^test-only", "test-only"),
)


def _bug_in_desc(desc):
    m = re.search(r"\bbug[ \t]*#?[ \t]*(\d{5,8})\b", desc or "", re.I)
    return int(m.group(1)) if m else None


def _rev(channel, node):
    try:
        return Revision.get_revision(channel, node) or {}
    except Exception:                                                    # noqa: BLE001
        return {}


def _pushed(rev):
    ts = rev.get("pushdate") or rev.get("date")
    if isinstance(ts, (list, tuple)):
        ts = ts[0] if ts else None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def audit(case):
    out = {
        "uuid": case.uuid,
        "signature": case.signature,
        "regressor_node": case.regressor_node,
        "regressor_bug": case.regressor_bug,
        "is_offstack": bool(getattr(case, "is_offstack", False)),
        "problems": [],
    }
    try:
        with open(case.crash_json_path) as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        raw = {}
    build_dt = _buildid_to_dt(raw.get("build"))
    out["crash_buildid"] = raw.get("build")
    out["crash_build_date"] = build_dt.isoformat() if build_dt else None

    rev = _rev(case.channel or "nightly", case.regressor_node)
    if not rev.get("node"):
        out["problems"].append("node not found on hg")
        return out
    desc = (rev.get("desc") or "").strip()
    pushed = _pushed(rev)
    out["regressor_desc"] = desc.splitlines()[0][:160] if desc else ""
    out["regressor_pushdate"] = pushed.isoformat() if pushed else None
    out["backedout"] = bool(rev.get("backedoutby"))

    # 1. Causally impossible: the "regressor" landed after the crashing build was produced.
    if build_dt and pushed:
        delta = round((pushed - build_dt).total_seconds() / 86400.0, 1)
        out["days_pushed_after_build"] = delta
        if delta > 0:
            out["problems"].append(
                "IMPOSSIBLE: landed {}d AFTER the crash build".format(delta)
            )
    # 2. A changeset that cannot introduce a crash on its own.
    low = desc.lower()
    for pattern, label in _NOOP_PATTERNS:
        if re.search(pattern, low):
            out["problems"].append("NO-OP COMMIT: {}".format(label))
            break
    # 3. The changeset's own bug does not match the label's bug.
    desc_bug = _bug_in_desc(desc)
    out["bug_in_desc"] = desc_bug
    if case.regressor_bug and desc_bug and desc_bug != case.regressor_bug:
        out["problems"].append(
            "BUG MISMATCH: desc says bug {}, label says {}".format(
                desc_bug, case.regressor_bug)
        )
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--corpus", default="corpus_ship")
    p.add_argument("--out", default="spike/CORPUS_LABEL_AUDIT.json")
    p.add_argument("--workers", type=int, default=6)
    args = p.parse_args()

    cases, _ = load_corpus(args.corpus)
    positives = [c for c in sorted(cases, key=lambda c: c.uuid)
                 if c.regressor_node and not getattr(c, "is_negative", False)]
    print("auditing {} labelled-positive cases...".format(len(positives)))
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(audit, positives))

    bad = [r for r in rows if r["problems"]]
    kinds = {}
    for r in bad:
        for prob in r["problems"]:
            kinds[prob.split(":")[0]] = kinds.get(prob.split(":")[0], 0) + 1
    summary = {
        "n_positives": len(rows),
        "n_with_problems": len(bad),
        "pct_with_problems": round(100.0 * len(bad) / len(rows), 1) if rows else None,
        "by_kind": kinds,
        "n_backedout": sum(1 for r in rows if r.get("backedout")),
    }
    with open(args.out, "w") as handle:
        json.dump({"summary": summary, "rows": rows}, handle, indent=2)
    print(json.dumps(summary, indent=2))
    print()
    for r in bad:
        print("  {} [{}]".format(r["uuid"][:13], r["regressor_node"]))
        print("     {}".format(r.get("regressor_desc", "")[:110]))
        for prob in r["problems"]:
            print("     -> {}".format(prob))
    print("\nwrote {}".format(args.out))


if __name__ == "__main__":
    main()
