# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Read-only audit of hot-engine suppressions, hold-out verdicts and later Bugzilla outcomes."""
from collections import Counter
from datetime import datetime, timezone

from libmozdata.bugzilla import Bugzilla
from libmozdata.socorro import Bugs as SocorroBugs

from crashclouseau import models

REPORTED = ("strong-evidence", "actionable", "lead")
_CHUNK = 50


def holdout(rows):
    """Count hold-out decisions and list runs with a decision in ``REPORTED``."""
    runs = [r for r in rows if r.get("holdout")]
    decisions = Counter((r.get("verdict") or {}).get("decision") or "unknown" for r in runs)
    reported = [r for r in runs if (r.get("verdict") or {}).get("decision") in REPORTED]
    return {"runs": len(runs), "by_decision": dict(decisions), "reported": reported}


def first_skipped(rows):
    """``{signature: earliest suppression time}``."""
    first = {}
    for r in rows:
        sig, at = r.get("signature"), r.get("created")
        if sig and at is not None and (sig not in first or at < first[sig]):
            first[sig] = at
    return first


def outcomes(rows, lookup=None, since=None):
    """List bugs fixed or filed after their signature's first suppression and *since*.

    Pass the full suppression history; *since* filters bug event dates."""
    first = first_skipped(rows)
    bugs = (lookup or bugs_for)(sorted(first))
    out = []
    for sig, skipped in sorted(first.items()):
        floor = max(_aware(skipped), _aware(since)) if since else _aware(skipped)
        for bug in bugs.get(sig) or []:
            resolved = _parse(bug.get("cf_last_resolved"))
            created = _parse(bug.get("creation_time"))
            if bug.get("resolution") == "FIXED" and resolved and resolved >= floor:
                kind = "fixed"
            elif created and created >= floor:
                kind = "filed"
            else:
                continue
            out.append({"signature": sig, "since": _aware(skipped), "kind": kind, "bug": bug})
    return out


def bugs_for(signatures):
    """``{signature: [bug, ...]}`` from Socorro's signature-to-bug map and Bugzilla."""
    s2b = {}
    for i in range(0, len(signatures), _CHUNK):
        s2b.update(SocorroBugs.get_bugs(signatures[i:i + _CHUNK]) or {})
    ids = sorted({b for v in s2b.values() for b in (v or [])})
    got = {}
    if ids:
        def handler(bug, data):
            data[bug["id"]] = bug

        Bugzilla(bugids=[str(i) for i in ids],
                 include_fields=["id", "summary", "status", "resolution", "cf_last_resolved",
                                 "creation_time", "creator"],
                 bughandler=handler, bugdata=got).get_data().wait()
    return {sig: [got[b] for b in ids_ if b in got] for sig, ids_ in s2b.items()}


def report(since=None, lookup=None, with_bugzilla=True):
    """Summarize suppressions since *since* and bug outcomes on all recorded signatures."""
    rows = models.HotEngineAudit.rows()
    recent = [r for r in rows if since is None or _aware(r["created"]) >= _aware(since)]
    return {"skipped": len(recent), "signatures": len(first_skipped(recent)),
            "holdout": holdout(recent),
            "outcomes": outcomes(rows, lookup, since=since) if with_bugzilla else None}


def format_report(rep):
    h = rep["holdout"]
    lines = ["skipped crashes: {} on {} signatures".format(rep["skipped"], rep["signatures"]),
             "hold-out runs: {} ({})".format(h["runs"], ", ".join(
                 "{} {}".format(n, d) for d, n in sorted(h["by_decision"].items())) or "none")]
    for r in h["reported"]:
        v = r.get("verdict") or {}
        lines.append("  {} {}/{} would publish: {} -- {} -- {}".format(
            r["uuid"], v.get("decision"), v.get("confidence"), v.get("would_publish") or "no",
            r["signature"], v.get("title") or ""))
        lines.append("    https://crash-stats.mozilla.org/report/index/{}".format(r["uuid"]))
    if rep["outcomes"] is None:
        return "\n".join(lines)
    lines.append("bugs fixed or filed after the first skip: {}".format(len(rep["outcomes"])))
    for o in rep["outcomes"]:
        bug = o["bug"]
        lines.append("  {} bug {} ({} {}, by {}) on {} -- skipped since {:%Y-%m-%d} -- {}".format(
            o["kind"], bug.get("id"), bug.get("status"), bug.get("resolution") or "",
            bug.get("creator"), o["signature"], o["since"], bug.get("summary") or ""))
    return "\n".join(lines)


def _parse(value):
    if not value:
        return None
    try:
        return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
