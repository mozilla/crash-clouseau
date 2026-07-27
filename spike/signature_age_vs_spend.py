# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""How much of the pipeline's spend goes to crashes that are not NEW REGRESSIONS at all?

The whole premise of the triage pipeline is "the regressor is somewhere in this build's
pushlog window". That premise only holds if the signature is actually NEW as of that build.
The blind second opinion kept refuting leads on exactly this ground — verified independently in
`verify_so_timing_claims.py`: for 10 of 10 high-confidence refutations, the signature's
first-seen buildid predated the named candidate by a median of ~6 months.

If a signature has existed for months, then no changeset in the current window introduced it,
and the pipeline is being asked an unanswerable question — at full price. This script measures
the size of that population and the money attached to it, using the SAME free lookup:

    signature first-seen buildid (Socorro)  vs  the crash's own build date (our DB)

Input is a JSON array of {signature, runs, usd, crash_buildid, reported} dumped from the canary
DB. No LLM calls.

  DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
    uv run python spike/signature_age_vs_spend.py --in /tmp/prod_sigs.json --out spike/SIGNATURE_AGE_VS_SPEND.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from verify_so_timing_claims import _buildid_to_dt, first_seen_buildid   # noqa: E402


def _parse_build_dt(value):
    """The DB stores a build as a timestamptz, not a buildid string."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None


def _one(row, days, product, channel):
    sig = row.get("signature") or ""
    out = dict(row)
    try:
        buildid, _n, total, window = first_seen_buildid(sig, product, channel, days)
    except Exception as exc:                                            # noqa: BLE001
        out["error"] = "{}: {}".format(type(exc).__name__, exc)
        return out
    seen = _buildid_to_dt(buildid)
    build = _parse_build_dt(row.get("crash_buildid"))
    out.update({
        "first_seen_buildid": buildid,
        "first_seen_date": seen.isoformat() if seen else None,
        "socorro_crashes_1y": total,
        "window_start": window,
    })
    if seen is None or build is None:
        out["age_days"] = None
        return out
    if build.tzinfo is None:
        build = build.replace(tzinfo=timezone.utc)
    # How long the signature had ALREADY existed when the build we triaged was produced.
    out["age_days"] = round((build - seen).total_seconds() / 86400.0, 1)
    return out


def summarize(rows, thresholds=(7, 30, 90)):
    def usd(rs):
        return round(sum(float(r.get("usd") or 0) for r in rs), 2)

    known = [r for r in rows if r.get("age_days") is not None]
    unknown = [r for r in rows if r.get("age_days") is None]
    total_usd = usd(rows)
    out = {
        "n_signatures": len(rows),
        "n_runs": sum(int(r.get("runs") or 0) for r in rows),
        "total_usd": total_usd,
        "n_age_unknown": len(unknown),
        "usd_age_unknown": usd(unknown),
        "buckets": [],
    }
    for t in thresholds:
        old = [r for r in known if r["age_days"] > t]
        out["buckets"].append({
            "signature_already_older_than_days": t,
            "n_signatures": len(old),
            "n_runs": sum(int(r.get("runs") or 0) for r in old),
            "n_reported_leads": sum(int(r.get("reported") or 0) for r in old),
            "usd": usd(old),
            "pct_of_spend": (round(100.0 * usd(old) / total_usd, 1) if total_usd else None),
        })
    fresh = [r for r in known if r["age_days"] <= 7]
    out["genuinely_new_within_7d"] = {
        "n_signatures": len(fresh),
        "n_runs": sum(int(r.get("runs") or 0) for r in fresh),
        "n_reported_leads": sum(int(r.get("reported") or 0) for r in fresh),
        "usd": usd(fresh),
    }
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--in", dest="infile", required=True)
    p.add_argument("--out", default="spike/SIGNATURE_AGE_VS_SPEND.json")
    p.add_argument("--days", type=int, default=364)
    p.add_argument("--product", default="Firefox")
    p.add_argument("--channel", default="nightly")
    p.add_argument("--workers", type=int, default=6)
    args = p.parse_args()

    with open(args.infile) as handle:
        rows = json.load(handle)
    print("looking up first-seen for {} signatures...".format(len(rows)))
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        done = list(pool.map(
            lambda r: _one(r, args.days, args.product, args.channel), rows
        ))
    summary = summarize(done)
    with open(args.out, "w") as handle:
        json.dump({"summary": summary, "rows": done}, handle, indent=2)
    print(json.dumps(summary, indent=2))
    print("\nwrote {}".format(args.out))


if __name__ == "__main__":
    main()
