# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Were the second opinion's REFUTATIONS right? Check them with arithmetic, not an LLM.

Of the 17 verify-mode refutations the blind second opinion (#SO) produced in the first three
prod days, 10 of 10 HIGH-confidence ones (and 3 of 7 medium) rest on the same argument:

    "this signature's first-seen buildid is WEEKS OR MONTHS BEFORE the candidate landed,
     therefore the candidate cannot be the regressor."

That claim needs no judgement to test. Two lookups settle it per lead:

    signature first-seen buildid   <- Socorro SuperSearch, build_id facet
    candidate changeset pushdate   <- hg.mozilla.org

If first-seen genuinely predates the push, the primary pipeline named a changeset that landed
AFTER the crash already existed — a real precision failure, not second-opinion pessimism. The
CORROBORATED leads act as the control: if they do NOT show the same pattern, the SO is
discriminating on a real signal rather than refusing everything.

Two honest caveats, both stated in the output:
  * The Socorro window bounds how far back first-seen can be seen. Truncation can only make
    first-seen look NEWER than it is, which biases against the timing argument — so a
    "predates" verdict here is conservative.
  * Signature reuse: an old signature can acquire a NEW cause. "Predates" is therefore strong
    evidence that this candidate is not THE origin, not proof the lead is worthless.

Input is the JSON array dumped from the canary DB (see --in). No LLM calls, so this is free.

  DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
    uv run python spike/verify_so_timing_claims.py --in /tmp/so_leads.json --out spike/SO_TIMING_VERIFICATION.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import crashclouseau.net  # noqa: E402,F401  (stamps the allowlisted UA for hg.mozilla.org)
from libmozdata import socorro  # noqa: E402
from libmozdata.hgmozilla import Revision  # noqa: E402


def first_seen_buildid(signature, product="Firefox", channel="nightly", days=364):
    """Oldest buildid this signature appears in, within `days`. Returns (buildid, n_builds,
    total_crashes, window_start) — buildid None when the lookup found nothing.

    Asks Socorro to SORT by build_id ascending and returns the first row, rather than paging a
    build_id facet: the facet is ordered by COUNT, so truncating it can silently drop the
    oldest build — exactly the value we need — and an untruncated facet_size is rejected (400).

    Note `date` filters the crash REPORT date, not the build date, so a short window still
    surfaces very old builds (a crash reported today from a January build reports January's
    buildid). Socorro hard-rejects a range over 365 days, and the implicit "to now" upper bound
    pushes an exact 365 over the line — hence the clamp to 364.
    """
    days = max(1, min(int(days or 364), 364))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    params = {
        "signature": "=" + signature,
        "product": product,
        "date": ">=" + since,
        "_columns": ["build_id", "date"],
        "_sort": "build_id",
        "_results_number": 1,
    }
    if channel:
        params["release_channel"] = channel
    got = {}

    def handler(json_, data):
        data["result"] = json_

    socorro.SuperSearch(params=params, handler=handler, handlerdata=got).wait()
    data = got.get("result") or {}
    hits = data.get("hits") or []
    buildid = None
    for hit in hits:
        if hit.get("build_id"):
            buildid = str(hit["build_id"])
            break
    return buildid, len(hits), data.get("total", 0), since


def _buildid_to_dt(buildid):
    """Firefox buildids are YYYYMMDDHHMMSS in UTC."""
    try:
        return datetime.strptime(str(buildid)[:14], "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return None


def push_date(node, channel="nightly"):
    try:
        rev = Revision.get_revision(channel, node)
    except Exception as exc:                                            # noqa: BLE001
        return None, "fetch failed: {}: {}".format(type(exc).__name__, exc)
    if not rev or not rev.get("node"):
        return None, "not found"
    ts = rev.get("pushdate") or rev.get("date")
    if isinstance(ts, (list, tuple)):
        ts = ts[0] if ts else None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc), None
    except (TypeError, ValueError):
        return None, "unparseable date {!r}".format(ts)


def check(lead, days, product):
    sig = lead.get("signature") or ""
    node = lead.get("cand_node") or ""
    channel = lead.get("channel") or "nightly"
    out = {
        "uuid": lead.get("uuid"),
        "signature": sig,
        "candidate_node": node,
        "candidate_bug": lead.get("cand_bug"),
        "so_corroborates": lead.get("corrob"),
        "so_confidence": lead.get("so_conf"),
    }
    buildid, n_builds, total, window = first_seen_buildid(sig, product, channel, days)
    out.update({"first_seen_buildid": buildid, "n_builds": n_builds,
                "socorro_crashes": total, "window_start": window})
    pushed, err = push_date(node, channel)
    out["candidate_pushdate"] = pushed.isoformat() if pushed else None
    if err:
        out["pushdate_error"] = err
    seen = _buildid_to_dt(buildid)
    out["first_seen_date"] = seen.isoformat() if seen else None
    if seen is None or pushed is None:
        out["timing_verdict"] = "unknown"
        return out
    delta_days = round((pushed - seen).total_seconds() / 86400.0, 1)
    out["days_candidate_landed_after_first_seen"] = delta_days
    # The window's own left edge: if first-seen sits ON it, the signature may well be older
    # and the true gap even larger. Flag it so the number is not over-read.
    out["first_seen_at_window_edge"] = bool(
        seen.strftime("%Y-%m-%d") <= window
    )
    if delta_days > 1:
        out["timing_verdict"] = "candidate POSTDATES first-seen (SO's argument holds)"
    elif delta_days < -1:
        out["timing_verdict"] = "candidate predates first-seen (consistent with a regressor)"
    else:
        out["timing_verdict"] = "same day (inconclusive)"
    return out


def summarize(rows):
    def bucket(pred):
        sel = [r for r in rows if pred(r)]
        holds = [r for r in sel if str(r.get("timing_verdict", "")).startswith("candidate POST")]
        known = [r for r in sel if r.get("timing_verdict") != "unknown"]
        return {
            "n": len(sel),
            "n_checkable": len(known),
            "n_candidate_postdates_first_seen": len(holds),
            "rate_of_checkable": (round(len(holds) / len(known), 3) if known else None),
            "median_days_after": (
                sorted(r["days_candidate_landed_after_first_seen"] for r in holds)[len(holds) // 2]
                if holds else None
            ),
        }

    return {
        "refuted_high_conf": bucket(
            lambda r: r["so_corroborates"] in (False, "false") and r["so_confidence"] == "high"
        ),
        "refuted_any": bucket(lambda r: r["so_corroborates"] in (False, "false")),
        "corroborated": bucket(lambda r: r["so_corroborates"] in (True, "true")),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--in", dest="infile", required=True,
                   help="JSON array of leads (uuid, signature, cand_node, cand_bug, corrob, so_conf)")
    p.add_argument("--out", default="spike/SO_TIMING_VERIFICATION.json")
    p.add_argument("--days", type=int, default=364, help="Socorro look-back window")
    p.add_argument("--product", default="Firefox")
    args = p.parse_args()

    with open(args.infile) as handle:
        leads = json.load(handle)
    rows = []
    for i, lead in enumerate(leads, 1):
        row = check(lead, args.days, args.product)
        rows.append(row)
        print("[{}/{}] {} {} cand={} -> {}".format(
            i, len(leads), row["so_corroborates"], row["so_confidence"],
            row["candidate_node"], row.get("timing_verdict")))
    summary = summarize(rows)
    with open(args.out, "w") as handle:
        json.dump({"summary": summary, "rows": rows,
                   "window_days": args.days}, handle, indent=2)
    print("\n" + json.dumps(summary, indent=2))
    print("\nwrote {}".format(args.out))


if __name__ == "__main__":
    main()
