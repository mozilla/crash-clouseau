# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Give a filing we made BEFORE bucket bugs existed its bucket identity.

    uv run python bin/backfill_bucket.py --bug 2073349 --bug 2071528 \\
        --bug 2069191 --title 2069191="Socket thread priority event queue (TRR events) could \\
        starve regular even processing during shutdown"            # report only, writes nothing
    ... the same with --apply                                       # write

WHY. Since 2026-09-18 a signature an open ``[meta]`` tracker holds takes one bug per BUCKET
(``bugzilla_apply.autofile_bug``, ``spike_escalation.file_spike_bug``), and a prior filing of
ours stops a new one only when it is the same bucket -- or when its bucket is UNKNOWN, which is
what every filing made before that date is. So our three legacy filings on the two shutdown-hang
families that matter most (2073349 on the pool-shutdown family, 2071528 the spike filing Jens
turned into the audio-session bucket, 2069191 on necko's) match every bucket and stop every new
one -- the unfiled Linux CUPS bucket included -- until they carry an identity. This writes it.

WHAT IT WRITES. For every record that filed bug N -- the ordinary filer's
``Dossier.payload["filed_bug"]`` rows and the spike table's ``payload["filing"]`` rows -- the
crash report is re-read from Socorro and ``hang.awaited_summary`` gives the bucket KEY (the
awaited thread's work and blocking call, e.g. ``suggest::store::SuggestStoreInner::ingest |
viaduct::client::Client::send_sync``) and a title. A report whose awaited thread was idle has no
key (2069191: the socket thread was in its poll); it takes ``--title N=...`` -- the bug's own
summary, as its owner retitled it -- and no key, which under the identity rules means one bug
per signature for non-hang buckets while a KEYED hang bucket on the same signature may still be
filed. ``--bucket N=...`` overrides the key the same way.

IDEMPOTENT. Fills only the missing fields unless ``--force``; a second run is a no-op. Refuses
to write anything without ``--apply``. Like ``bin/backfill_memory_unsafe.py`` it runs wherever
``DATABASE_URL`` points at the real database.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crashclouseau import db, hang, models                 # noqa: E402
from crashclouseau.logger import logger                   # noqa: E402


def fields_for(raw, title=None, bucket=None):
    """``{"bucket", "bucket_title"}`` for a processed crash *raw*, from its awaited work unless
    overridden. Empty strings where nothing is known -- an unknown is written as nothing."""
    try:
        work = hang.awaited_summary(raw or {}) or {}
    except Exception:  # pragma: no cover - defensive
        work = {}
    return {"bucket": str(bucket or work.get("bucket") or ""),
            "bucket_title": str(title or work.get("title") or "")}


def apply_fields(filing, fields, force=False):
    """The filing record with *fields* merged in: ``(new_record, changed)``. Only the missing
    fields are filled unless *force*; an empty value never overwrites a recorded one."""
    out = dict(filing or {})
    changed = False
    for key in ("bucket", "bucket_title"):
        value = fields.get(key) or ""
        if not value:
            continue
        if out.get(key) and not force:
            continue
        if out.get(key) != value:
            out[key] = value
            changed = True
    return out, changed


def _pairs(values):
    """``["2069191=Some title"]`` -> ``{2069191: "Some title"}``."""
    out = {}
    for item in values or []:
        bug, _, text = item.partition("=")
        if not text:
            raise SystemExit("expected BUG=TEXT, got {!r}".format(item))
        out[int(bug)] = text
    return out


def records_for_bug(bug):
    """Every record that filed *bug*: ``[{"table", "uuid", "filing", "row"}]``."""
    out = []
    fb = models.Dossier.payload["filed_bug"]
    rows = (db.session.query(models.UUID.uuid, models.Dossier)
            .join(models.UUID, models.Dossier.uuidid == models.UUID.id)
            .filter(fb["bug"].astext == str(bug), fb["filed"].astext == "true")
            .all())
    for uuid, dossier in rows:
        out.append({"table": "dossier", "uuid": uuid, "row": dossier,
                    "filing": dict((dossier.payload or {}).get("filed_bug") or {})})
    filing = models.SpikeEscalation.payload["filing"]
    escs = (db.session.query(models.SpikeEscalation)
            .filter(filing["bug"].astext == str(bug), filing["filed"].astext == "true")
            .all())
    for esc in escs:
        out.append({"table": "spike", "uuid": esc.uuid, "row": esc,
                    "filing": dict((esc.payload or {}).get("filing") or {})})
    return out


def _write(record, filing):
    row = record["row"]
    if record["table"] == "dossier":
        payload = dict(row.payload or {})
        payload["filed_bug"] = filing
        row.payload = payload
        db.session.add(row)
    else:
        row.merge_payload({"filing": filing}, commit=False)


def main():
    from crashclouseau import inspector

    ap = argparse.ArgumentParser()
    ap.add_argument("--bug", type=int, action="append", required=True,
                    help="a bug we filed; repeatable")
    ap.add_argument("--title", action="append", default=[], metavar="BUG=TEXT",
                    help="the bucket title for that bug's records (a report with an idle "
                         "awaited thread has none of its own)")
    ap.add_argument("--bucket", action="append", default=[], metavar="BUG=KEY",
                    help="override the bucket key for that bug's records")
    ap.add_argument("--apply", action="store_true", help="write; the default reports only")
    ap.add_argument("--force", action="store_true", help="overwrite recorded fields too")
    args = ap.parse_args()
    titles, buckets = _pairs(args.title), _pairs(args.bucket)

    changes = 0
    for bug in args.bug:
        records = records_for_bug(bug)
        if not records:
            print("bug {}: no filing record".format(bug))
            continue
        for record in records:
            raw = {}
            if record["uuid"]:
                try:
                    raw = inspector.get_crash_data(record["uuid"]) or {}
                except Exception as exc:  # noqa: BLE001
                    logger.warning("backfill: could not read %s: %s", record["uuid"], exc)
            fields = fields_for(raw, title=titles.get(bug), bucket=buckets.get(bug))
            filing, changed = apply_fields(record["filing"], fields, force=args.force)
            print("bug {} [{} {}] bucket {!r} -> {!r}; title {!r} -> {!r}{}".format(
                bug, record["table"], record["uuid"], record["filing"].get("bucket"),
                filing.get("bucket"), record["filing"].get("bucket_title"),
                filing.get("bucket_title"), "" if changed else "  (unchanged)"))
            if changed and args.apply:
                _write(record, filing)
                changes += 1
    if args.apply:
        db.session.commit()
        print("written: {} record(s)".format(changes))
    else:
        print("report only; add --apply to write")


if __name__ == "__main__":
    main()
