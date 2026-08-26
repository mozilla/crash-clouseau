# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""What became of the bugs we filed, pulled back from Bugzilla.

The pipeline has always recorded what it DID (``Dossier.payload["filed_bug"]``) and never what
came of it, so "how often are we right?" has only ever been answerable by hand — and the answer
that exists (~28% of leads name the true regressor, CI 0.13-0.50) came from a one-off spike, not
from the product's own output.

Two things make this cheap rather than a labelling project:

* the bug ids are already ours, in ``filed_bug``;
* the correction is STRUCTURED. A reviewer who rejects our attribution sets BMO's own
  ``regressed_by``. Bug 2062119 carries ``regressed_by: [1412726]`` against the ``1768581`` we
  named, so "were we right?" is a comparison, not an inference from prose.

Since the filer began setting ``regressed_by`` itself, that comparison needs one more input: what
WE wrote there. A field that agrees with us because we wrote it is not a reviewer agreeing with
us, so it scores ``unconfirmed`` rather than ``correct`` — see ``models.Feedback.classify``.

And a second input, because the field can also be copied out of our comment by somebody who never
read the analysis: 10 of the 15 non-filer ``regressed_by`` settings across the 52 filings were
made by one release-management account answering a BugBot nag, and on two of those bugs (2061969,
2061691) no human other than the filer has ever written a word. ``_independent_reviewers`` asks
the ``ReviewNote`` corpus this module already collects whether anybody but us and the machinery
commented at all. That is the difference between a reviewer endorsing us and the loop reading its
own prose back.

Read-only and unauthenticated: everything needed is public on a bug we filed publicly.

The reason this module exists at all is ``models.Archetype``. A learned rule is a guess until
its firings can be scored against outcomes, and ``Feedback.archetypes`` is what makes that join
possible.
"""
from datetime import datetime, timezone

from crashclouseau import models, net
from crashclouseau.logger import logger

_BZ_REST = "https://bugzilla.mozilla.org/rest/bug"
_HTTP_TIMEOUT = 60
# BMO takes many `id=` params happily, but a URL has limits and a partial failure should cost
# one batch rather than the sweep.
_BATCH = 50
# ``comment_count`` is the whole cost control for the comment sweep: there is NO bulk comment
# endpoint (``/rest/bug/comment?ids=...`` answers error 100, "Sorry, I can't find COMMENT",
# and libmozdata does one GET per bug), so an ungated pass is 52-and-growing serial GETs every
# tick. Gated on a changed count it is ~2.2 bugs a tick (measured: distinct panel bugs
# receiving a non-ours comment per 6h bucket over the last 7 days, max 9).
# ``creator`` is free in the same call and is who "us" is for the needinfo read: on a
# ``new_bug`` row the creator IS the filing account, so nothing has to be configured.
_FIELDS = "id,status,resolution,dupe_of,regressed_by,comment_count,creator"

# How we recognise OUR OWN comment. Not the author email: the filer posts as cdenizet, who is
# also a real reviewer on these bugs -- 8 cdenizet comments sit past comment 0 across 7 of the
# 52 filings and 6 of them are the filer again. The body marker is exact on the panel: 58
# marker-bearing comments, every one written by the filer, none by anybody else.
# (``bugzilla_apply._post_comment`` already RETURNS the new comment id and the caller discards
# it; persisting it would make this a lookup instead of a match, but only for filings made
# after that change -- the marker is what covers the 52 already on file.)
_COMMENT_MARK = "Crash report: https://crash-stats.mozilla.org/report/index/"

# Filing modes whose bug is OURS. An allowlist, not a denylist: ``mode ==
# "comment_on_existing"`` also records ``filed: True`` (``bugzilla_apply.py:693``) on a bug
# somebody else created, where every human comment is ordinary discussion of their own bug.
# The one such bug in the public record, 2057980, carries 29 non-ours comments -- 24% of the
# whole corpus -- of Thunderbird contributors talking to each other. An unrecognised mode is
# skipped and counted rather than trusted.
_NOTE_MODES = ("new_bug",)

# Per-bug comment-count watermark, in the existing named-cursor table. ``SweepMark.set``
# never moves backwards, which is exactly right for a count that only grows, and the table is
# already in ``models._ADDED_TABLES`` -- so this needs no new plumbing.
_NOTE_CURSOR = "revnote:{}"


def _filed_bugs():
    """``[{bug_id, uuid, named_bug, named_node, archetypes, claimed, filed_at}]`` for every bug
    the pipeline has filed, newest last. Sourced from the dossiers, which are the only record.

    ``claimed`` is the ``regressed_by`` the FILER set on that bug, and it is what stops this
    module from marking its own homework: see ``models.Feedback.classify``. Absent on anything
    filed before the filer started setting the field, which reads as "we claimed nothing" and
    leaves those rows scored exactly as they were."""
    out = []
    for row in models.Dossier.filed_bug_rows():
        info = row.get("filed_bug") or {}
        bug_id = info.get("bug")
        if not bug_id:
            continue
        dossier = row.get("dossier") or {}
        candidate = dossier.get("candidate") or {}
        corrob = dossier.get("corroborations") or {}
        out.append({
            "bug_id": int(bug_id),
            "uuid": info.get("uuid") or row.get("uuid"),
            "named_bug": candidate.get("bug"),
            "named_node": candidate.get("node"),
            "archetypes": corrob.get("archetypes") or [],
            "claimed": info.get("regressed_by") or [],
            "filed_at": info.get("at"),
            # "new_bug" | "comment_on_existing" | absent on a pre-``mode`` filing. Only the
            # first is a bug whose comments are reactions to us; see ``_NOTE_MODES``.
            "mode": info.get("mode"),
            # WHICH CHANNEL FILED IT, absent on anything filed before the filer recorded it
            # (every one of those is nightly, the only channel that has ever filed). Carried
            # because `_NOTE_MODES = ("new_bug",)` is exactly a never-comment channel's mode, so
            # without this the review corpus pools two populations and any rate read off it
            # describes neither -- the denominator IS the rule.
            "channel": info.get("channel"),
            "buildid": info.get("buildid"),
        })
    return out


def _fetch(bug_ids):
    """``{bug_id -> bug dict}`` from BMO for these ids. Missing ids simply do not appear — a
    restricted bug is ABSENT from the query form rather than an error."""
    out = {}
    for start in range(0, len(bug_ids), _BATCH):
        chunk = bug_ids[start:start + _BATCH]
        try:
            r = net.get(_BZ_REST,
                        params={"include_fields": _FIELDS, "id": [str(b) for b in chunk]},
                        timeout=_HTTP_TIMEOUT)
            r.raise_for_status()
            for bug in (r.json() or {}).get("bugs") or []:
                out[int(bug["id"])] = bug
        except Exception as exc:                            # pragma: no cover - network
            logger.warning("feedback: lookup failed for %s: %s", chunk, exc)
    return out


def _as_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _fetch_comments(bug_id):
    """``[comment dict]`` for one bug, or ``None`` if the read failed.

    One GET per bug, and there is no way around it: ``/rest/bug/comment?ids=...`` answers
    error 100 and libmozdata's own client loops one bug at a time. That is why the caller
    must gate on a CHANGED ``comment_count`` — see ``_NOTE_MODES``' neighbour ``_FIELDS``."""
    try:
        r = net.get("{}/{}/comment".format(_BZ_REST, bug_id), timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        bugs = (r.json() or {}).get("bugs") or {}
        return (bugs.get(str(bug_id)) or {}).get("comments") or []
    except Exception as exc:                                # pragma: no cover - network
        logger.warning("feedback: comments failed for %s: %s", bug_id, exc)
        return None


def _needinfo_setters(bug_id, account):
    """Who has ever put a ``needinfo?`` on *account* on this bug. ``set()`` on failure.

    Read from ``/history``'s ``flagtypes.name`` ADDITIONS rather than the live ``flags``
    field, because the addition is durable: a needinfo that has since been answered and
    cleared is invisible in ``flags`` and permanent in the history. On the panel the live
    field would have found 1 of the 18."""
    if not account:
        return set()
    try:
        r = net.get("{}/{}/history".format(_BZ_REST, bug_id), timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        bugs = (r.json() or {}).get("bugs") or []
        events = (bugs[0].get("history") if bugs else []) or []
    except Exception as exc:                                # pragma: no cover - network
        logger.warning("feedback: history failed for %s: %s", bug_id, exc)
        return set()
    want = "needinfo?({})".format(account)
    return {(e.get("who") or "").strip().lower()
            for e in events
            for ch in (e.get("changes") or [])
            if ch.get("field_name") == "flagtypes.name" and want in (ch.get("added") or "")}


def _independent_reviewers(filers):
    """``{bug_id: bool}`` for the bugs asked about — has a HUMAN who is not the filer written on
    it? ``filers`` is ``{bug_id: the filing account}``. A bug MISSING from the answer means
    "not checked", and ``models.Feedback.classify`` scores that exactly as it did before.

    Reads the ``ReviewNote`` rows ``_ingest_notes`` below already stores, so it costs no request
    — and, more importantly, it reuses the classifier that was measured on the panel.
    ``ReviewNote.classify_author`` knows the five automation accounts, the boilerplate posted
    under human accounts, AND the two AGENT accounts. ``experts._is_bot``, the obvious thing to
    reach for here, scores ``firefoxmanagerdev@gmail.com`` as a human; that account is an LLM and
    all THREE of its appearances in the panel (bugs 2060922, 2061973 and 2062335) are REFUTATIONS
    of our attribution. Reading a refutation as an independent endorsement is the exact failure
    this check exists to stop, so the weaker test is not an acceptable substitute for the
    stronger one.

    A bug is answered only once its comments have actually been SWEPT (``_NOTE_CURSOR``). Two
    consequences, both deliberate: on a database with no note corpus yet nothing is relabelled,
    and because ``_ingest_notes`` runs AFTER the verdicts in ``refresh`` below, a bug's first scan
    and its first independence answer are one tick (6h) apart. The lag can only delay a
    correction, never invent one.

    The filer is excluded by EMAIL on top of the body-marker filter ``_ingest_notes`` applies:
    one operator note on bug 2063003 is a genuine human comment written by the filing account,
    and counting our own note as an independent reviewer is the shape being removed."""
    if not filers:
        return {}
    try:
        names = {_NOTE_CURSOR.format(b): b for b in filers}
        marks = (models.db.session.query(models.SweepMark.name)
                 .filter(models.SweepMark.name.in_(sorted(names)),
                         models.SweepMark.position > 0).all())
        scanned = {names[row[0]] for row in marks}
        rows = ((models.db.session.query(models.ReviewNote.bug_id, models.ReviewNote.author)
                 .filter(models.ReviewNote.bug_id.in_(sorted(scanned)),
                         models.ReviewNote.author_kind == "human").all())
                if scanned else [])
    except Exception as exc:                                # pragma: no cover - defensive
        models.db.session.rollback()
        logger.warning("feedback: independence lookup failed: %s", exc)
        return {}
    out = {bug_id: False for bug_id in scanned}
    for bug_id, author in rows:
        if (author or "").strip().lower() != (filers.get(bug_id) or "").strip().lower():
            out[bug_id] = True
    return out


def _ingest_notes(filed, fetched):
    """Store every comment on OUR bugs that we did not write. Returns a counts dict.

    Deliberately separate from the outcome refresh above and deliberately failure-isolated:
    ``Feedback.record`` commits per row before this runs, so a bad comment, a truncation or a
    BMO hiccup can cost this pass and never the verdicts."""
    out = {"eligible": 0, "skipped_mode": 0, "unchanged": 0, "scanned": 0,
           "ours": 0, "automation": 0, "seen": 0, "new": 0, "failed": 0}
    for entry in filed:
        bug = fetched.get(entry["bug_id"])
        if bug is None:
            continue
        if entry.get("mode") not in _NOTE_MODES:
            out["skipped_mode"] += 1
            continue
        out["eligible"] += 1
        bug_id = entry["bug_id"]
        count = int(bug.get("comment_count") or 0)
        try:
            cursor = models.SweepMark.get(_NOTE_CURSOR.format(bug_id))
        except Exception:                                   # pragma: no cover - defensive
            cursor = 0
        if count and count <= cursor:
            out["unchanged"] += 1
            continue
        out["scanned"] += 1
        try:
            comments = _fetch_comments(bug_id)
            if comments is None:
                out["failed"] += 1
                continue
            theirs = [c for c in comments if _COMMENT_MARK not in (c.get("text") or "")]
            out["ours"] += len(comments) - len(theirs)
            setters = (_needinfo_setters(bug_id, (bug.get("creator") or "").strip().lower())
                       if theirs else set())
            for c in theirs:
                author = (c.get("creator") or c.get("author") or "").strip().lower()
                kind = models.ReviewNote.classify_author(author, c.get("text") or "")
                if kind == "automation":
                    out["automation"] += 1
                _, created = models.ReviewNote.record(
                    bug_id, c.get("id"),
                    comment_no=int(c.get("count") or 0),
                    author=author,
                    author_kind=kind,
                    needinfo=author in setters,
                    created_at=_as_datetime(c.get("creation_time")),
                    body=c.get("text") or "",
                )
                out["seen"] += 1
                out["new"] += int(created)
            # Last, and only on success: the mark never moves backwards, so a pass that dies
            # half way simply re-reads the bug next tick (``ReviewNote.record`` is a no-op on
            # an id already stored, so re-reading costs one GET and writes nothing).
            models.SweepMark.set(_NOTE_CURSOR.format(bug_id), count)
        except Exception as exc:                            # pragma: no cover - defensive
            out["failed"] += 1
            models.db.session.rollback()
            logger.warning("feedback: notes failed for %s: %s", bug_id, exc)
    return out


def refresh():
    """Pull the current state of every filed bug into ``models.Feedback``. Returns a summary.

    Idempotent, and safe to run on a schedule: a row is keyed by bug id and simply re-stamped.
    A bug BMO did not return is left exactly as it was rather than being blanked — an
    unreadable bug (restricted, or a lookup that failed) must not erase a verdict we already
    recorded about it."""
    filed = _filed_bugs()
    fetched = _fetch([f["bug_id"] for f in filed])
    # `correct` is the ONLY verdict the independence check can move, so ask the scorer itself
    # which rows are even eligible rather than re-implementing its `correct` branch here, which
    # would drift. 14 of the 52 filings on the 2026-08-21 panel are eligible and 2 move.
    eligible = {}
    for entry in filed:
        bug = fetched.get(entry["bug_id"])
        if bug is None:
            continue
        if models.Feedback.classify(bug.get("resolution") or None, entry["named_bug"],
                                    bug.get("regressed_by") or [],
                                    entry["claimed"]) == "correct":
            eligible[entry["bug_id"]] = (bug.get("creator") or "").strip().lower()
    independent = _independent_reviewers(eligible)
    updated = 0
    for entry in filed:
        bug = fetched.get(entry["bug_id"])
        if bug is None:
            continue
        models.Feedback.record(
            entry["bug_id"],
            claimed_regressed_by=entry["claimed"],
            independent_comment=independent.get(entry["bug_id"]),
            uuid=entry["uuid"],
            named_bug=entry["named_bug"],
            named_node=entry["named_node"],
            archetypes=entry["archetypes"],
            filed_at=_as_datetime(entry["filed_at"]),
            status=bug.get("status"),
            resolution=bug.get("resolution") or None,
            dupe_of=bug.get("dupe_of"),
            regressed_by=bug.get("regressed_by") or [],
        )
        updated += 1
    # AFTER the verdicts, and it can never take them with it: `Feedback.record` has already
    # committed each row, and this whole pass is wrapped because it runs on a schedule now —
    # an exception here would otherwise cost the tick, and the next one is six hours away.
    try:
        notes = _ingest_notes(filed, fetched)
    except Exception as exc:                                # pragma: no cover - defensive
        models.db.session.rollback()
        logger.warning("feedback: note ingestion failed wholesale: %s", exc)
        notes = {"failed": len(filed)}
    # A bug WE FILED that we can no longer read is the single highest-value label this pass can
    # produce, and until 2026-08-24 it was pure silence: three `if bug is None: continue` sites
    # dropped it without a count. It means a human took a bug we filed PUBLIC and restricted it,
    # which is the ground truth for "we published something we should not have" -- exactly what
    # bug 2065051 turned out to be, and the only way we learned was a reviewer mentioning it.
    #
    # WARNING, not info: this is the label that says the security gate missed one. It can also be
    # an ordinary BMO read failure, which is why it names the ids instead of asserting a cause.
    unreadable = sorted({f["bug_id"] for f in filed} - set(fetched))
    if unreadable:
        logger.warning(
            "feedback: %d filed bug(s) are no longer readable by this account: %s -- a bug we "
            "filed PUBLIC that has since been restricted is the ground truth for a missed "
            "security filing; check each before assuming a BMO blip",
            len(unreadable), ", ".join(str(b) for b in unreadable))
    summary = {"filed": len(filed), "fetched": len(fetched), "updated": updated,
               "unreadable": unreadable, "notes": notes,
               **models.Feedback.scoreboard()}
    logger.info("feedback: refreshed %s of %s filed bugs -> %s",
                updated, len(filed), summary["by_attribution"])
    return summary
