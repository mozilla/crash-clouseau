# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""What Clouseau wrote on Bugzilla and what became of it, for dashboard.html.

``refresh`` builds one ``models.Impact`` row per bug from our own records and from Bugzilla;
``summarize`` turns the rows into the page."""
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

from crashclouseau import config, models, net
from crashclouseau.logger import logger

_BZ_REST = "https://bugzilla.mozilla.org/rest/bug"
_HTTP_TIMEOUT = 60
_BATCH = 50
_BOT = "clouseau-bot@mozilla.tld"
# Filings before 2026-08-27 were posted from this account: only its comments carrying the
# crash-report line are ours.
_LEGACY_FILERS = frozenset({"cdenizet@mozilla.com"})
_MARK = "Crash report: https://crash-stats.mozilla.org/report/index/"
# How close to one of our comments a legacy-account needinfo must be to be ours.
_LEGACY_WINDOW = timedelta(minutes=10)
# How close to the flag removal a comment must be to count as the answer.
_ANSWER_WINDOW = timedelta(minutes=5)
_FIELDS = ",".join((
    "id", "summary", "status", "resolution", "dupe_of", "duplicates", "creator",
    "creation_time", "cf_last_resolved", "last_change_time", "product", "component",
    "assigned_to", "severity", "priority", "regressed_by", "groups"))
_TARGET_FIELDS = "id,status,resolution,creation_time"
_OPEN = frozenset({"UNCONFIRMED", "NEW", "ASSIGNED", "REOPENED"})
_NOBODY = frozenset({"", "nobody@mozilla.org"})
_NEW_MODES = frozenset({"new_bug", "spike_new_bug"})
_NI = re.compile(r"needinfo\?\(([^)\s]+)\)")
_PATCH = re.compile(r"^Created attachment \d+\s*\n\s*Bug \d+\b")
_CHANNELS = (
    re.compile(r"which analyses (nightly|beta|release|esr)", re.I),
    re.compile(r"\bin (nightly|beta|release|esr)\s*\d*\b[^\n]*\bbuildid\b"),
    re.compile(r"^Build: \d+ \((\w+)\)", re.M),
)
_PRODUCT = re.compile(r"\b(Fenix|Focus) has \d+ crash")

OUTCOMES = (
    ("fixed", "Fixed"),
    ("duplicate", "Duplicate"),
    ("open_assigned", "Open, assigned"),
    ("open", "Open, unassigned"),
    ("closed", "Closed without a fix"),
    ("closed_before", "Already closed when we commented"),
    ("unreadable", "Not readable"),
)
NI_OUTCOMES = (
    ("answered", "Answered by the requestee"),
    ("answered_by_other", "Answered by someone else"),
    ("redirected", "Redirected to someone else"),
    ("cancelled", "Cancelled by someone else"),
    ("auto_cleared", "Withdrawn by Clouseau or cleared by a bot"),
    ("pending", "Still pending"),
)
MODES = {
    "new_bug": "new bug",
    "spike_new_bug": "spike: new bug",
    "comment_on_existing": "comment",
    "wake_stale": "comment on a stale bug",
    "same_defect": "same defect: signature added",
    "spike_comment": "spike: comment",
}


def _ts(value):
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat() if dt else None


def _lower(value):
    return (value or "").strip().lower()


def _is_us(account):
    return "clouseau" in _lower(account)


def _is_our_comment(comment):
    author = _lower(comment.get("creator") or comment.get("author"))
    return _is_us(author) or (author in _LEGACY_FILERS and _MARK in (comment.get("text") or ""))


def _kind_of(author, text=""):
    return models.ReviewNote.classify_author(author, text)


def _same_person(requestee, who):
    """Bugzilla history names the remover by their current login and the flag by the login it
    was set on, so a renamed account only matches on the local part."""
    return requestee == who or requestee.split("@")[0] == who.split("@")[0]


def _is_human(account, text=""):
    """A person other than Clouseau and the account it used to file from."""
    account = _lower(account)
    return (not _is_us(account) and account not in _LEGACY_FILERS
            and _kind_of(account, text or "") == "human")


# --- our own records -------------------------------------------------------------------------

def _write(info, source, **extra):
    mode = info.get("mode")
    if mode == "comment_on_existing" and info.get("wake_stale"):
        mode = "wake_stale"
    out = {
        "at": info.get("at"),
        "mode": mode,
        "uuid": info.get("uuid"),
        "signature": info.get("signature"),
        "channel": info.get("channel"),
        "product": info.get("product"),
        "needinfo": info.get("needinfo"),
        "needinfo_missed": info.get("needinfo_dropped") or info.get("needinfo_failed"),
        "asked_as": (info.get("wake_stale") or {}).get("asked_as"),
        "restricted": bool(info.get("restricted") or info.get("security_groups")
                           or info.get("findings_withheld")),
        "source": source,
    }
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def collect_writes():
    """``{bug_id: [write]}`` from the dossiers, the spike escalations and the feedback table."""
    out = defaultdict(list)
    for row in models.Dossier.filed_bug_rows():
        info = row.get("filed_bug") or {}
        if info.get("bug"):
            out[int(info["bug"])].append(_write(dict(info, uuid=info.get("uuid") or row["uuid"]),
                                                "dossier"))
    for row in models.SpikeEscalation.filed_rows():
        info = row["filing"]
        out[int(info["bug"])].append(_write(
            info, "spike", signature=info.get("signature") or row["signature"],
            channel=info.get("channel") or row["channel"],
            product=row["product"], uuid=info.get("uuid") or row["uuid"]))
    for bug_id, uuid, filed_at in models.db.session.query(
            models.Feedback.bug_id, models.Feedback.uuid, models.Feedback.filed_at).all():
        out[int(bug_id)].append(_write({"uuid": uuid, "at": _iso(_ts(filed_at))}, "feedback"))
    return dict(out)


def merge_writes(old, new):
    """One entry per write (keyed by crash uuid), the gaps of one filled from the other."""
    merged = {}
    for w in list(old or []) + list(new or []):
        key = w.get("uuid") or w.get("at")
        cur = merged.get(key)
        if cur is None:
            merged[key] = dict(w)
            continue
        for k, v in w.items():
            if cur.get(k) in (None, "", False) and v not in (None, "", False):
                cur[k] = v
    return sorted(merged.values(), key=lambda w: w.get("at") or "")


# --- Bugzilla ----------------------------------------------------------------------------------

def _get(path, token, params=None):
    r = net.get(_BZ_REST + path, params=params, timeout=_HTTP_TIMEOUT,
                headers={"X-Bugzilla-API-Key": token} if token else {})
    r.raise_for_status()
    return r.json() or {}


def _search(params, token):
    try:
        data = _get("", token, dict(params, include_fields="id", limit=0))
    except Exception as exc:
        logger.warning("impact: search %s failed: %s", params, exc)
        return set()
    return {int(b["id"]) for b in data.get("bugs") or []}


def _fetch_bugs(ids, token, fields=_FIELDS):
    """``({bug_id: bug}, failed_ids)``; a bug missing from both is not readable."""
    out, failed = {}, set()
    ids = sorted(ids)
    for start in range(0, len(ids), _BATCH):
        chunk = ids[start:start + _BATCH]
        try:
            data = _get("", token, {"id": ",".join(str(b) for b in chunk),
                                    "include_fields": fields})
        except Exception as exc:
            logger.warning("impact: lookup failed for %s: %s", chunk, exc)
            failed.update(chunk)
            continue
        for bug in data.get("bugs") or []:
            out[int(bug["id"])] = bug
    return out, failed


def _history(bug_id, token):
    bugs = _get("/{}/history".format(bug_id), token).get("bugs") or []
    return (bugs[0].get("history") if bugs else None) or []


def _comments(bug_id, token):
    bugs = _get("/{}/comment".format(bug_id), token).get("bugs") or {}
    return (bugs.get(str(bug_id)) or {}).get("comments") or []


# --- derivation --------------------------------------------------------------------------------

def _flags(change, field):
    return {_lower(x) for x in _NI.findall(change.get(field) or "")}


def _needinfos(history, comments, our_times):
    """Every needinfo we set on the bug, with how it ended."""
    def by_us(who, when):
        if _is_us(who):
            return True
        return who in _LEGACY_FILERS and any(abs(when - t) <= _LEGACY_WINDOW for t in our_times)

    def commented(who, when):
        return any(_lower(c.get("creator")) == who and _ts(c.get("creation_time"))
                   and abs(_ts(c.get("creation_time")) - when) <= _ANSWER_WINDOW
                   for c in comments)

    asks, pending = [], {}
    for event in sorted(history, key=lambda e: e.get("when") or ""):
        when, who = _ts(event.get("when")), _lower(event.get("who"))
        if when is None:
            continue
        added, removed = set(), set()
        for ch in event.get("changes") or []:
            if ch.get("field_name") == "flagtypes.name":
                added |= _flags(ch, "added")
                removed |= _flags(ch, "removed")
        for person in removed:
            ask = pending.pop(person, None)
            if ask is None:
                continue
            if added - {person}:
                outcome = "redirected"
            elif _same_person(person, who):
                outcome = "answered"
            elif (by_us(who, when) or who in _LEGACY_FILERS
                  or _kind_of(who) == "automation"):
                outcome = "auto_cleared"
            elif commented(who, when):
                outcome = "answered_by_other"
            else:
                outcome = "cancelled"
            ask.update(outcome=outcome, closed_at=_iso(when), by=who)
        for person in added:
            if person not in pending and by_us(who, when):
                ask = {"to": person, "at": _iso(when), "outcome": "pending"}
                asks.append(ask)
                pending[person] = ask
    return asks


def _parsed_origin(comments):
    """Channel and product read from our own comment text, for writes no record describes."""
    channel = product = None
    for c in comments:
        text = c.get("text") or ""
        for pattern in _CHANNELS:
            m = pattern.search(text)
            if m and not channel:
                channel = m.group(1).lower()
        m = _PRODUCT.search(text)
        product = product or (m.group(1) if m else None)
    # Every filing without a Fenix or Focus marker predates Fenix filing (2026-09-15) or has a
    # dossier record naming its product.
    return channel, product or ("Firefox" if comments else None)


def derive(bug, comments, history, writes):
    """The stored facts for one readable bug."""
    comments = sorted(comments or [], key=lambda c: c.get("count") or 0)
    history = history or []
    writes = writes or []
    ours = [c for c in comments if _is_our_comment(c)]
    our_times = [t for t in (_ts(c.get("creation_time")) for c in ours) if t]
    modes = {w.get("mode") for w in writes if w.get("mode")}
    creator = _lower(bug.get("creator"))
    created = _ts(bug.get("creation_time"))
    opened_by_us = bool(comments) and _is_our_comment(comments[0])
    if modes & _NEW_MODES or _is_us(creator):
        kind = "filed"
    elif modes:
        kind = "commented"
    else:
        kind = "filed" if creator in _LEGACY_FILERS and opened_by_us else "commented"
    if kind == "filed":
        first_at = created
    else:
        later = [t for t in our_times if not created or t > created]
        later += [t for t in (_ts(w.get("at")) for w in writes) if t]
        first_at = min(later) if later else None

    status_before = None
    changed_after = False
    fixed_at = None
    for event in sorted(history, key=lambda e: e.get("when") or ""):
        when = _ts(event.get("when"))
        for ch in event.get("changes") or []:
            field = ch.get("field_name")
            if field == "status" and when and first_at and when <= first_at:
                status_before = ch.get("added")
            if field in ("status", "resolution") and when and first_at and when > first_at:
                changed_after = True
            if field == "resolution" and ch.get("added") == "FIXED":
                fixed_at = when
    after = first_at
    reactions = [c for c in comments
                 if not _is_our_comment(c) and after and _ts(c.get("creation_time"))
                 and _ts(c.get("creation_time")) > after]
    humans = [c for c in reactions if _is_human(c.get("creator"), c.get("text"))]
    touches = [_ts(c.get("creation_time")) for c in humans]
    touches += [_ts(e.get("when")) for e in history
                if after and _ts(e.get("when")) and _ts(e.get("when")) > after
                and _is_human(e.get("who"))]
    channel, product = _parsed_origin(ours)
    assignee = _lower(bug.get("assigned_to"))
    resolution = bug.get("resolution") or None
    return {
        "readable": True,
        "kind": kind,
        "first_at": _iso(first_at),
        "summary": bug.get("summary"),
        "product": bug.get("product"),
        "component": bug.get("component"),
        "status": bug.get("status"),
        "resolution": resolution,
        "dupe_of": bug.get("dupe_of"),
        "duplicates": len(bug.get("duplicates") or []),
        "created": _iso(created),
        "resolved_at": bug.get("cf_last_resolved") if resolution else None,
        "fixed_at": _iso(fixed_at) if resolution == "FIXED" else None,
        "assignee": None if assignee in _NOBODY else assignee,
        "severity": bug.get("severity"),
        "priority": bug.get("priority"),
        "regressed_by": bug.get("regressed_by") or [],
        "restricted": bool(bug.get("groups")),
        "open_at_first": status_before is None or status_before in _OPEN,
        "changed_after": changed_after,
        "our_comments": len(ours),
        "needinfos": _needinfos(history, comments, our_times),
        "human_comments": len(humans),
        "humans": sorted({_lower(c.get("creator")) for c in humans}),
        "first_human_at": _iso(min((t for t in touches if t), default=None)),
        "patch": any(_PATCH.match(c.get("text") or "") for c in reactions),
        "landed": any(_lower(c.get("creator")) == "pulsebot@bmo.tld"
                      and "Pushed by" in (c.get("text") or "") for c in reactions),
        "parsed_channel": channel,
        "parsed_product": product,
    }


def _unreadable(old_facts, writes):
    facts = dict(old_facts or {})
    if not facts:
        modes = {w.get("mode") for w in writes}
        times = [t for t in (_ts(w.get("at")) for w in writes) if t]
        facts = {"kind": "filed" if modes & _NEW_MODES else "commented",
                 "first_at": _iso(min(times)) if times else None}
    facts["readable"] = False
    return facts


def build(ids, writes, stored, token):
    """The ``models.Impact`` rows for *ids*; history and comments are only re-read for a bug
    whose ``last_change_time`` moved."""
    bugs, failed = _fetch_bugs(ids, token)
    targets, _ = _fetch_bugs({int(b["dupe_of"]) for b in bugs.values() if b.get("dupe_of")},
                             token, _TARGET_FIELDS)
    rows, stats = [], Counter()
    for bug_id in sorted(ids):
        old = stored.get(bug_id) or {}
        merged = merge_writes(old.get("writes"), writes.get(bug_id))
        bug = bugs.get(bug_id)
        if bug_id in failed:
            stats["failed"] += 1
            if not old:
                continue
            facts, last_change = old.get("facts") or {}, old.get("last_change")
        elif bug is None:
            stats["unreadable"] += 1
            facts, last_change = _unreadable(old.get("facts"), merged), old.get("last_change")
        elif (old.get("last_change") == bug.get("last_change_time")
              and (old.get("facts") or {}).get("readable")
              and old.get("writes") == merged):
            stats["unchanged"] += 1
            facts, last_change = dict(old["facts"]), old["last_change"]
        else:
            try:
                history, comments = _history(bug_id, token), _comments(bug_id, token)
            except Exception as exc:
                logger.warning("impact: history/comments failed for %s: %s", bug_id, exc)
                stats["failed"] += 1
                if not old:
                    continue
                facts, last_change = old.get("facts") or {}, old.get("last_change")
            else:
                stats["read"] += 1
                facts, last_change = (derive(bug, comments, history, merged),
                                      bug.get("last_change_time"))
        target = targets.get(int(facts["dupe_of"])) if facts.get("dupe_of") else None
        if target:
            facts["dupe_target"] = {"id": target["id"], "status": target.get("status"),
                                    "resolution": target.get("resolution") or None}
        rows.append({"bug_id": bug_id, "kind": facts.get("kind") or "commented",
                     "first_at": facts.get("first_at"), "writes": merged, "facts": facts,
                     "last_change": last_change})
    return rows, dict(stats)


def refresh(token=None):
    """Rebuild the ``impact`` table from our records and Bugzilla. Returns counts."""
    token = config.get_bugzilla_token() if token is None else token
    stored = {r["bug_id"]: r for r in models.Impact.rows()}
    writes = collect_writes()
    found = (_search({"creator": _BOT}, token)
             | _search({"f1": "commenter", "o1": "equals", "v1": _BOT}, token))
    ids = set(stored) | set(writes) | found
    rows, stats = build(ids, writes, stored, token)
    models.Impact.store(rows)
    stats.update(bugs=len(ids), stored=len(rows), from_search=len(found - set(writes)))
    logger.info("impact: refreshed %s", stats)
    return stats


# --- the page ----------------------------------------------------------------------------------

def outcome(facts):
    if not facts.get("readable"):
        return "unreadable"
    if (facts.get("kind") == "commented" and not facts.get("open_at_first", True)
            and not facts.get("changed_after")):
        return "closed_before"
    resolution = facts.get("resolution")
    if resolution == "FIXED":
        return "fixed"
    if resolution == "DUPLICATE":
        return "duplicate"
    if facts.get("status") in _OPEN:
        return "open_assigned" if facts.get("assignee") else "open"
    return "closed"


def _origin(row):
    facts = row.get("facts") or {}
    writes = row.get("writes") or []
    channel = next((w["channel"] for w in writes if w.get("channel")), None)
    product = next((w["product"] for w in writes if w.get("product")), None)
    return (channel or facts.get("parsed_channel") or "unknown",
            product or facts.get("parsed_product") or "unknown")


def _mode(row):
    modes = [w.get("mode") for w in row.get("writes") or [] if w.get("mode")]
    if row.get("kind") == "filed":
        return next((m for m in modes if m in _NEW_MODES), "new_bug")
    return next((m for m in modes if m not in _NEW_MODES), "comment_on_existing")


# Minimum age since our first write for the silence rate.
SILENT_AFTER = timedelta(days=7)
# Minimum age for listing open, silent bugs.
LIST_SILENT_AFTER = timedelta(days=14)
# Before role tracking, stale-bug comments targeted the triage owner.
_OLD_WAKE_ROLE = "triage owner (before v279)"

# Remainder categories drawn in the neutral gray rather than a series hue.
_GRAY = frozenset({"unreadable", "pending"})


def _segments(counter, vocabulary):
    total = sum(counter.values())
    out = []
    for slot, (key, label) in enumerate(vocabulary, start=1):
        count = counter.get(key, 0)
        if count:
            out.append({"key": key, "label": label, "count": count,
                        "slot": 0 if key in _GRAY else slot, "pct": 100.0 * count / total})
    return out


def _hours(start, end):
    if not start or not end:
        return None
    return (end - start).total_seconds() / 3600.0


def _median(values):
    values = [v for v in values if v is not None]
    return median(values) if values else None


def _pct(part, whole):
    return round(100.0 * part / whole) if whole else 0


def _axis(peak):
    """A round axis maximum at or above *peak*, and its ticks as (value, % height)."""
    step = 1
    for candidate in (1, 2, 5, 10, 20, 25, 50, 100):
        step = candidate
        if peak / candidate <= 4:
            break
    top = step * -(-peak // step)
    return top, [(v, 100.0 * v / top) for v in range(0, top + 1, step)]


def _week(dt):
    day = dt.date()
    return day - timedelta(days=day.weekday())


def _bug_row(row, attribution, public, now):
    facts = row.get("facts") or {}
    channel, product = _origin(row)
    restricted = bool(facts.get("restricted")) or any(
        w.get("restricted") for w in row.get("writes") or [])
    hidden = public and restricted
    first = _ts(row.get("first_at") or facts.get("first_at"))
    ni = facts.get("needinfos") or []
    target = facts.get("dupe_target") or {}
    aged = bool(facts.get("readable") and first and now - first >= SILENT_AFTER)
    return {
        "id": row["bug_id"],
        "kind": row.get("kind"),
        "mode": _mode(row),
        "mode_label": MODES.get(_mode(row), _mode(row)),
        "first_at": first,
        "channel": channel,
        "product": product,
        "restricted": restricted,
        "summary": None if hidden else facts.get("summary"),
        "component": None if hidden else (
            "{} :: {}".format(facts["product"], facts["component"])
            if facts.get("product") and facts.get("component") else None),
        "outcome": outcome(facts),
        "status": facts.get("status"),
        "resolution": facts.get("resolution"),
        "dupe_of": facts.get("dupe_of"),
        "dupe_fixed": target.get("resolution") == "FIXED",
        "assignee": None if hidden else facts.get("assignee"),
        "needinfos": [dict(n, to=None if hidden else n.get("to")) for n in ni],
        "needinfo_missed": any(w.get("needinfo_missed") for w in row.get("writes") or []),
        "human_comments": facts.get("human_comments") or 0,
        "first_human_h": _hours(first, _ts(facts.get("first_human_at"))),
        "fix_days": (_hours(first, _ts(facts.get("fixed_at"))) or 0) / 24.0
        if facts.get("fixed_at") and outcome(facts) == "fixed" else None,
        "patch": bool(facts.get("patch")),
        "landed": bool(facts.get("landed")),
        "duplicates": facts.get("duplicates") or 0,
        "triaged": (facts.get("severity") or "").startswith("S")
        or (facts.get("priority") or "").startswith("P"),
        "attribution": attribution.get(row["bug_id"]),
        "asked_as": next((w["asked_as"] for w in row.get("writes") or [] if w.get("asked_as")),
                         None),
        "aged": aged,
        "silent": aged and not facts.get("first_human_at"),
        "silent_days": (now - first).days if first else None,
    }


def _tally(bugs):
    """The per-group counts shown in the breakdown tables."""
    out = Counter()
    for b in bugs:
        out[b["kind"]] += 1
        out["{}:{}".format(b["kind"], b["outcome"])] += 1
        asks = b["needinfos"]
        out["aged"] += b["aged"]
        out["silent"] += b["silent"]
        out["ni"] += len(asks)
        out["ni_answered"] += sum(n["outcome"] in ("answered", "answered_by_other", "redirected")
                                  for n in asks)
    return out


def summarize(rows, attribution=None, now=None, channel=None, product=None, days=None,
              public=True):
    """The view model for dashboard.html, over the rows the filters keep."""
    now = now or datetime.now(timezone.utc)
    attribution = attribution or {}
    every = [_bug_row(r, attribution, public, now) for r in rows]
    channels = sorted({b["channel"] for b in every})
    products = sorted({b["product"] for b in every})
    since = now - timedelta(days=days) if days else None
    bugs = [b for b in every
            if (not channel or b["channel"] == channel)
            and (not product or b["product"] == product)
            and (not since or (b["first_at"] and b["first_at"] >= since))]
    bugs.sort(key=lambda b: b["first_at"] or datetime.min.replace(tzinfo=timezone.utc),
              reverse=True)
    filed = [b for b in bugs if b["kind"] == "filed"]
    commented = [b for b in bugs if b["kind"] == "commented"]

    def ni_counts(group):
        return Counter(n["outcome"] for b in group for n in b["needinfos"])

    ni_filed, ni_commented = ni_counts(filed), ni_counts(commented)
    asks = [n for b in bugs for n in b["needinfos"]]
    answered = sum(1 for n in asks
                   if n["outcome"] in ("answered", "answered_by_other", "redirected"))
    answer_h = [_hours(_ts(n["at"]), _ts(n.get("closed_at"))) for n in asks
                if n["outcome"] in ("answered", "answered_by_other", "redirected")]
    pending_d = [_hours(_ts(n["at"]), now) / 24.0 for n in asks if n["outcome"] == "pending"]
    readable = [b for b in bugs if b["outcome"] != "unreadable"]
    engaged = [b for b in readable if b["first_human_h"] is not None]

    weekly = []
    dated = [b for b in bugs if b["first_at"]]
    if dated:
        start = _week(min(b["first_at"] for b in dated))
        weeks = defaultdict(lambda: {"filed": Counter(), "commented": Counter()})
        for b in dated:
            weeks[_week(b["first_at"])][b["kind"]][b["outcome"]] += 1
        peak = 1
        day = start
        while day <= _week(now):
            w = weeks[day]
            peak = max(peak, sum(w["filed"].values()), sum(w["commented"].values()))
            weekly.append({"week": day, "filed": w["filed"], "commented": w["commented"]})
            day += timedelta(days=7)
        top, ticks = _axis(peak)
        for w in weekly:
            for kind in ("filed", "commented"):
                total = sum(w[kind].values())
                w[kind] = {"total": total, "height": 100.0 * total / top,
                           "segments": _segments(w[kind], OUTCOMES)}
    else:
        ticks = []

    def table(key, among=None):
        groups = defaultdict(list)
        for b in bugs if among is None else among:
            groups[key(b)].append(b)
        out = []
        for name, group in groups.items():
            row = _tally(group)
            row["name"] = name
            out.append((len(group), row))
        return [row for _, row in sorted(out, key=lambda r: (-r[0], r[1]["name"]))]

    components = defaultdict(list)
    for b in filed:
        components[b["component"] or ("restricted" if b["restricted"] else "unknown")].append(b)
    top_components = sorted(
        ({"name": name, "total": len(group),
          "outcomes": Counter(b["outcome"] for b in group)}
         for name, group in components.items()),
        key=lambda r: (-r["total"], r["name"]))[:15]

    aged = [b for b in bugs if b["aged"]]
    silent = [b for b in aged if b["silent"]]
    silent_open = sorted((b for b in silent if b["outcome"] in ("open", "open_assigned")
                          and now - b["first_at"] >= LIST_SILENT_AFTER),
                         key=lambda b: b["first_at"])
    wakes = [b for b in bugs if b["mode"] == "wake_stale"]

    attributions = Counter(b["attribution"] for b in filed if b["attribution"])
    filed_out = Counter(b["outcome"] for b in filed)
    commented_out = Counter(b["outcome"] for b in commented)
    return {
        "filters": {"channel": channel, "product": product, "days": days},
        "channels": channels,
        "products": products,
        "checked_at": max((_ts(r.get("checked_at")) for r in rows if r.get("checked_at")),
                          default=None),
        "tiles": {
            "filed": len(filed),
            "commented": len(commented),
            "fixed": filed_out["fixed"],
            "fixed_pct": _pct(filed_out["fixed"], len(filed)),
            "duplicate": filed_out["duplicate"],
            "dupe_fixed": sum(1 for b in filed if b["outcome"] == "duplicate" and b["dupe_fixed"]),
            "open": filed_out["open"] + filed_out["open_assigned"],
            "closed": filed_out["closed"],
            "canonical": sum(1 for b in filed if b["duplicates"]),
            "absorbed": sum(b["duplicates"] for b in filed),
            "fixed_after_comment": commented_out["fixed"],
            "ni_asked": len(asks),
            "ni_answered": answered,
            "ni_pct": _pct(answered, len(asks)),
            "ni_cancelled": sum(n["outcome"] == "cancelled" for n in asks),
            "ni_pending": sum(n["outcome"] == "pending" for n in asks),
            "ni_missed": sum(1 for b in bugs if b["needinfo_missed"]),
            "silent": len(silent),
            "silent_aged": len(aged),
            "silent_pct": _pct(len(silent), len(aged)),
            "silent_open": len(silent_open),
            "engaged": len(engaged),
            "engaged_pct": _pct(len(engaged), len(readable)),
            "landed": sum(1 for b in bugs if b["landed"]),
            "patch": sum(1 for b in bugs if b["patch"] or b["landed"]),
            "triaged": sum(1 for b in filed if b["triaged"]),
            "triaged_pct": _pct(sum(1 for b in filed if b["triaged"]), len(filed)),
            "median_first_human_h": _median(b["first_human_h"] for b in engaged),
            "median_fix_days": _median(b["fix_days"] for b in filed),
            "median_answer_h": _median(answer_h),
            "median_pending_d": _median(pending_d),
        },
        "filed_outcomes": _segments(filed_out, OUTCOMES),
        "commented_outcomes": _segments(commented_out, OUTCOMES),
        "ni_filed": _segments(ni_filed, NI_OUTCOMES),
        "ni_commented": _segments(ni_commented, NI_OUTCOMES),
        "weekly": weekly,
        "weekly_ticks": ticks,
        "by_origin": table(lambda b: "{} / {}".format(b["product"], b["channel"])),
        "by_mode": table(lambda b: b["mode_label"]),
        "by_requestee": table(lambda b: b["asked_as"] or _OLD_WAKE_ROLE, wakes),
        "silent_open": silent_open,
        "components": top_components,
        "closed_resolutions": Counter(b["resolution"] for b in filed if b["outcome"] == "closed"),
        "attribution": attributions,
        "attribution_total": sum(attributions.values()),
        "bugs": bugs,
        "outcomes": OUTCOMES,
        "ni_outcomes": NI_OUTCOMES,
    }
