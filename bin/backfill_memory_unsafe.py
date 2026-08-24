# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Set ``corroborations['memory_unsafe']`` on the dossiers written before the flag existed.

    uv run python bin/backfill_memory_unsafe.py            # report only, writes nothing
    uv run python bin/backfill_memory_unsafe.py --apply    # write the flag

WHY THIS IS NOT OPTIONAL. ``crashclouseau/sensitive.py`` withholds an analysis by reading a
PERSISTED flag, and ``orchestrator._record_sensitivity`` only writes it on runs that happen after
it shipped. So until this has run, every dossier already in the database reads as NOT withheld --
including the one that started this: crash ``41bb8c8a-3458-4803-90f8-a7a850260819``, whose
analysis of bug 2065051 is what :mccr8 reviewed, and whose ``crashstack.html`` was still serving
``0xe5e5e5e5e5e5e5e8`` and "Use-after-free" anonymously four days after BMO restricted the bug.
Shipping the gate without running this closes the door on new crashes and leaves the back
catalogue open.

WHERE IT RUNS. Wherever ``DATABASE_URL`` points at the real database. Like ``bin/predeploy.py``
this refuses rather than guessing: with no reachable DB it exits non-zero instead of reporting a
reassuring zero.

WHAT IT READS. Socorro's public SuperSearch, in batches, for each uuid's normalized ``address``
field -- NOT ``json_dump.crash_info.address``, which is present-but-useless (``0x0`` or all-ones)
on 6 of 8 measured poison crashes. No credential is needed. A uuid Socorro no longer knows (its
retention is finite) cannot be judged; those are counted and listed under ``unknown`` rather than
silently treated as safe, because "we could not tell" and "it is fine" are different answers and
only one of them is true.

IDEMPOTENT. It only ever ADDS the flag, never clears it, so a second run is a no-op and a run
that is interrupted can simply be repeated.
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crashclouseau import db, models, sensitive           # noqa: E402
from crashclouseau.logger import logger                   # noqa: E402

_SUPERSEARCH = "https://crash-stats.mozilla.org/api/SuperSearch/"
# Socorro accepts repeated `uuid` params; 50 keeps the URL well inside any sane limit while
# making the whole back catalogue a handful of requests.
_BATCH = 50


def _addresses(uuids):
    """``{uuid: address}`` for the uuids Socorro still has."""
    params = [("uuid", u) for u in uuids]
    params += [("_columns", "uuid"), ("_columns", "address"),
               ("_results_number", str(len(uuids)))]
    url = _SUPERSEARCH + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=120) as fh:
        data = json.load(fh)
    return {h["uuid"]: h.get("address") for h in data.get("hits") or []
            if h.get("uuid")}


def _rows():
    """``[(uuid, payload)]`` for every dossier that has a payload and no flag yet."""
    q = (db.session.query(models.UUID.uuid, models.Dossier.payload)
         .select_from(models.Dossier)
         .join(models.UUID, models.UUID.id == models.Dossier.uuidid)
         .order_by(models.Dossier.id))
    out = []
    for uuid, payload in q.all():
        dossier = (payload or {}).get("dossier") or {}
        if (dossier.get("corroborations") or {}).get("memory_unsafe"):
            continue                     # already flagged; idempotent
        out.append((uuid, payload))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the flag (default: report only)")
    args = ap.parse_args()

    try:
        rows = _rows()
    except Exception as exc:                              # pragma: no cover - operational
        print("cannot read the dossiers ({}: {}). Point DATABASE_URL at the real database "
              "and re-run -- refusing to report zero from a database I cannot see."
              .format(type(exc).__name__, exc))
        return 2

    print("dossiers without the flag: {}".format(len(rows)))
    by_uuid = dict(rows)
    fires, unknown = [], []
    uuids = list(by_uuid)
    for i in range(0, len(uuids), _BATCH):
        batch = uuids[i:i + _BATCH]
        try:
            got = _addresses(batch)
        except Exception as exc:                          # pragma: no cover - operational
            print("  batch {}-{} failed ({}); leaving those alone".format(
                i, i + len(batch), exc))
            continue
        for uuid in batch:
            if uuid not in got:
                unknown.append(uuid)
                continue
            signals = sensitive.memory_unsafe_signals({"address": got[uuid]})
            if signals:
                fires.append((uuid, signals))
        print("  ...{}/{}".format(min(i + _BATCH, len(uuids)), len(uuids)))

    print("\nwould withhold: {}".format(len(fires)))
    for uuid, signals in fires:
        print("  {}  {}".format(uuid, "; ".join(signals)))
    if unknown:
        print("\nUNKNOWN to Socorro, so NOT judged ({}): {}{}".format(
            len(unknown), ", ".join(unknown[:10]),
            " ..." if len(unknown) > 10 else ""))
        print("  These are not 'safe' -- they are unanswered. Socorro's retention is finite, so "
              "an old crash cannot be re-checked; decide by hand if any of them matter.")

    if not args.apply:
        print("\nreport only; re-run with --apply to write the flag")
        return 0

    for uuid, signals in fires:
        payload = dict(by_uuid[uuid] or {})
        dossier = dict(payload.get("dossier") or {})
        corr = dict(dossier.get("corroborations") or {})
        corr["memory_unsafe"] = True
        corr["memory_unsafe_signals"] = signals
        dossier["corroborations"] = corr
        payload["dossier"] = dossier
        # Straight through `upsert`, so `_STICKY_PAYLOAD_KEYS` carries `filed_bug` forward
        # rather than this backfill erasing the Bugzilla ledger it does not own.
        models.Dossier.upsert(uuid, payload=payload, commit=False)
        logger.info("backfill: withholding %s (%s)", uuid, "; ".join(signals))
    db.session.commit()
    print("\nflagged {}".format(len(fires)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
