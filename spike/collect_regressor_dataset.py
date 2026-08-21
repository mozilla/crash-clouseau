# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Collect a rich *regressor-strategy* dataset from BMO (research, off the DB).

Motivation
----------
Clouseau's original signal -- crossing a crash stack against the lines a patch
touched -- is decaying: modern crash stacks are dominated by Rust panic hooks,
``MOZ_CRASH`` assertion machinery, and generic dispatch frames, so the regressor
increasingly does NOT touch a file on the stack (it is "off-stack"). To design
better signals we first need GROUND TRUTH with everything a human uses to guess a
regressor. This module harvests, for a date range, every BMO bug that has:

  * a crash signature (``cf_crash_signature``),
  * a crash stack pasted in an early comment (the bot template, or any frame block),
  * one or more known regressors (``regressed_by``), and
  * a fix (``resolution = FIXED``; a backout counts as a fix -- we record but do
    NOT deep-analyze it, since a backout just reverts the regressor).

For each such bug it assembles a self-contained record:

  crash bug meta + parsed crash stack (uuid / MOZ_CRASH reason / frames / insights)
  regressor(s): landing changeset(s) + per-changeset diff (files / enclosing
       functions / touched identifiers / change-tags / churn / cosmetic flags),
       the FIRST nightly build containing the regressor, and that build's PUSHLOG
       WINDOW (the candidate set a human reasons over -- see bug 2056116 c#1),
       plus an on-stack/off-stack label vs the parsed stack.
  fix: landing changeset(s) + diff, and whether it is just a backout.
  the full comment thread (verbatim) + cheap deterministic "strategy" pre-tags
       (mozregression / bisection / pushlog / first-bad-build / feature-flip /
       reviewer-signal / "looks like bug N" ...), so the later LLM strategy-mining
       pass has both the raw text and a structured starting point.

Migration note (2026): ``hg.mozilla.org/mozilla-central`` is frozen (git migration);
individual revs still resolve there via git-cinnabar, but bulk pushlog-by-window is
served by **hg-edge.mozilla.org/mozilla-central** (verified). buildhub is alive and
maps buildid->revision. We import the ``crashclouseau`` package (which spins up a
Flask/SQLAlchemy app at import) with ``DATABASE_URL=sqlite://`` so its maintained
helpers (patch_extract / buildhub / utils) are reusable without a real DB.

This is a research collector: heavily parallel (thread pool -- pure network I/O),
best-effort per field (a failure is recorded as a note, never aborts the run), and
writes plain JSON. It does not touch Clouseau's DB or the production pipeline.

Usage
-----
    DATABASE_URL=sqlite:// uv run python -m spike.collect_regressor_dataset \\
        --start 2026-01-01 --end 2026-07-21 --workers 32 \\
        --out spike/regressor_dataset
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# Import the package with an in-memory DB so its Flask/SQLAlchemy init at import
# time does not require a real database (we never touch the DB here).
os.environ.setdefault("DATABASE_URL", "sqlite://")

# Imports below intentionally follow the DATABASE_URL setup above (the crashclouseau
# package builds a Flask/SQLAlchemy app at import time). E402 is expected here.
import requests  # noqa: E402
from libmozdata.bugzilla import Bugzilla  # noqa: E402

from crashclouseau import buildhub, utils  # noqa: E402
from crashclouseau.agent import patch_extract  # noqa: E402

log = logging.getLogger("collect_regressor_dataset")

COLLECTOR_VERSION = 1

# hg-edge is the live mirror after the git migration: hg.mozilla.org/mozilla-central
# json-pushes is frozen and hg.mozilla.org redirects (302) to hg-edge anyway. We hit
# hg-edge directly (json-rev / raw-rev / json-pushes) so we control headers + retry.
HG_EDGE = "https://hg-edge.mozilla.org/mozilla-central"
# The "crash-clouseau" User-Agent is ALLOWLISTED on hgmo: it is exempt from the WAF
# rate-limiter that otherwise answers bursts with HTTP 406. This MUST be sent verbatim
# (libmozdata's HG client does NOT put the config UA on the wire, which is why the
# early runs got throttled). Verified: a 12-way concurrent raw-rev burst -> 0x406.
_UA = "crash-clouseau"
_TIMEOUT = 60
# Even allowlisted we stay polite: cap concurrent hg requests (a global semaphore,
# independent of the worker count) and still retry 406/429/5xx with backoff + jitter as
# a safety net. Re-created in run() from --hg-concurrency.
_HG_CONCURRENCY_DEFAULT = 12
_HG_SEM = threading.Semaphore(_HG_CONCURRENCY_DEFAULT)
_HG_RETRY_STATUS = {406, 429, 500, 502, 503, 504}


def _hg_get(path, params=None, as_json=True, retries=6):
    """GET an hg-edge endpoint through the concurrency gate, retrying transient
    throttles (406/429/5xx) with backoff. Returns parsed json / text, or None on a
    hard 404 or exhausted retries (logged)."""
    url = HG_EDGE + path
    backoff = 1.0
    for attempt in range(retries):
        resp = None
        with _HG_SEM:
            try:
                resp = requests.get(url, params=params, timeout=_TIMEOUT,
                                    headers={"User-Agent": _UA})
            except requests.RequestException as exc:
                log.debug("hg get %s failed (attempt %d): %s", path, attempt, exc)
        if resp is not None:
            if resp.status_code == 200:
                try:
                    return resp.json() if as_json else resp.text
                except ValueError:
                    return None
            if resp.status_code == 404:
                return None
            if resp.status_code not in _HG_RETRY_STATUS:
                log.debug("hg get %s -> %d (giving up)", path, resp.status_code)
                return None
        time.sleep(backoff + random.random() * 0.5)
        backoff = min(backoff * 2, 20)
    log.warning("hg get %s exhausted retries (params=%s)", path, params)
    return None


# Native-crash bugs are filed mostly under Core; Firefox is largely frontend, and
# Toolkit/DevTools/etc. carry the rest. Empty tuple => no product filter.
DEFAULT_PRODUCTS: tuple = ()

_BZ_FIELDS = [
    "id", "summary", "product", "component", "severity", "priority", "status",
    "resolution", "keywords", "creation_time", "cf_last_resolved", "assigned_to",
    "regressed_by", "regressions", "cf_crash_signature", "depends_on", "see_also",
]


# --------------------------------------------------------------------------- #
# BMO query
# --------------------------------------------------------------------------- #
def query_bugs(start_date, end_date, products=(), severities=(), require_fixed=True):
    """BMO bugs with a crash signature + a regressor, created in [start, end]."""
    params = {
        "include_fields": _BZ_FIELDS,
        "f1": "cf_crash_signature", "o1": "isnotempty",
        "f2": "regressed_by", "o2": "isnotempty",
        "f3": "creation_ts", "o3": "greaterthaneq", "v3": start_date,
        "f4": "creation_ts", "o4": "lessthaneq", "v4": end_date,
    }
    if require_fixed:
        params.update({"f5": "resolution", "o5": "equals", "v5": "FIXED"})
    if products:
        params["product"] = list(products)
    if severities:
        params.update({"f6": "bug_severity", "o6": "anyexact", "v6": ",".join(severities)})

    bugs = []
    Bugzilla(
        params,
        bughandler=lambda b, d: d.append(b),
        bugdata=bugs,
    ).get_data().wait()
    log.info("BMO returned %d crash+regressor bugs (%s..%s)", len(bugs), start_date, end_date)
    return bugs


def fetch_thread(bug_ids):
    """One BMO call per batch: comments + attachments -> {id: {comments, attachments}}.

    Attachments carry GitHub-PR fix/regressor references (``text/x-github-pull-request``
    with a ``[org/repo] ... (#PR)`` description) that never appear as URLs in the text."""
    out = {int(b): {"comments": [], "attachments": []} for b in bug_ids}

    def ch(data, bugid):
        out[int(bugid)]["comments"] = data.get("comments", [])

    def ah(data, bugid):
        out[int(bugid)]["attachments"] = data or []

    Bugzilla(
        [str(b) for b in bug_ids],
        commenthandler=ch,
        attachmenthandler=ah,
        attachment_include_fields=["id", "content_type", "description", "is_obsolete", "creation_time"],
    ).get_data().wait()
    return out


def fetch_comments(bug_ids):
    """Comments only (compat shim) -> {id: [comment, ...]}."""
    return {bid: t["comments"] for bid, t in fetch_thread(bug_ids).items()}


# --------------------------------------------------------------------------- #
# Crash-stack parsing (from the pasted comment, not Socorro)
# --------------------------------------------------------------------------- #
_UUID_RE = re.compile(
    r"crash-stats\.mozilla\.org/report/index/([0-9a-f]{8}-[0-9a-f-]{20,})"
)
_MOZCRASH_RE = re.compile(r"MOZ_CRASH Reason:\s*`{0,3}\s*(.+?)\s*`{0,3}\s*$", re.M)
# Markers that mean "a crash stack lives in this comment", across the formats bugs use.
_STACK_MARK_RE = re.compile(
    r"Top\s+\d+\s+frames|frames of (?:the )?crashing thread|AddressSanitizer"
    r"|SUMMARY:\s|MOZ_CRASH|MOZ_RELEASE_ASSERT|Assertion fail|SIGSEGV|SIGABRT"
    r"|Hit MOZ_|Segmentation fault",
    re.I,
)
_INSIGHT_RE = re.compile(r"^\s*[-*]\s*\*\*(.+?):\*\*\s*(.+?)\s*$", re.M)
# Socorro bot template frame: "3  xul.dll  std::panicking::panic  library/std/...:833"
_FRAME_BOT_RE = re.compile(r"^\s*(\d+)\s{2,}(\S+)\s{2,}(.+)$")
# ASan/LSan/gdb frame: "    #3 0x7bff.. in Func /builds/.../PLDHashTable.cpp:440:53"
_FRAME_HASH_RE = re.compile(r"^\s*#(\d+)\s+(.*)$")
# Trailing "path:line[:col]" source location.
_SRCLOC_RE = re.compile(r"^(\S+?):(\d+)(?::\d+)?$")


def _parse_frame_line(line):
    """Parse one stack-frame line across formats -> {stackpos, module, function, file, line}.

    Handles the Socorro bot template ("N  module  func  file:line"), ASan/LSan
    ("#N 0xaddr in func /path:line:col"), and gdb-ish ("#N func (...) at file:line")."""
    s = line.rstrip()
    stripped = s.lstrip()
    if not stripped.startswith("#"):
        m = _FRAME_BOT_RE.match(s)
        if m:
            pos, module, rest = int(m.group(1)), m.group(2), m.group(3).strip()
            parts = re.split(r"\s{2,}", rest)
            func, srcfile, srcline = rest, "", -1
            loc = _SRCLOC_RE.match(parts[-1]) if parts else None
            if loc and (len(parts) >= 2 or " " not in parts[-1]):
                srcfile, srcline = loc.group(1), int(loc.group(2))
                func = "  ".join(parts[:-1]).strip() or func
            return {"stackpos": pos, "module": module, "function": func,
                    "file": srcfile, "line": srcline}
        return None
    m = _FRAME_HASH_RE.match(s)
    if not m:
        return None
    pos, rest = int(m.group(1)), m.group(2).strip()
    rest = re.sub(r"^0x[0-9a-fA-F]+\s+", "", rest)   # drop the address
    rest = re.sub(r"^in\s+", "", rest)               # drop the ASan "in "
    func, srcfile, srcline = rest, "", -1
    toks = rest.rsplit(None, 1)                       # trailing "path:line[:col]"
    if len(toks) == 2:
        loc = _SRCLOC_RE.match(toks[1])
        if loc:
            srcfile, srcline = loc.group(1), int(loc.group(2))
            func = toks[0].strip().rstrip(" at").strip()  # tidy gdb "... at"
    return {"stackpos": pos, "module": "", "function": func,
            "file": srcfile, "line": srcline}


def parse_crash_stack(comments):
    """Extract the crash stack + metadata from the earliest comment that carries one.

    Returns a dict with ``has_stack`` plus (when found) uuid / moz_crash_reason /
    frames / insights / the source comment index and its raw text."""
    result = {
        "has_stack": False, "uuid": "", "crash_report_url": "",
        "moz_crash_reason": "", "frames": [], "stack_files": [], "insights": {},
        "source_comment": None, "raw": "",
    }
    # Pick the stack-bearing comment with the MOST parsed frames (earliest on a tie), so a
    # comment that merely mentions "MOZ_CRASH" doesn't shadow the one holding the frames.
    best = None  # (n_frames, comment_count, comment, frames)
    any_url = None
    for c in comments:
        text = c.get("text", "") or ""
        frames = [f for f in (_parse_frame_line(ln) for ln in text.splitlines()) if f]
        um = _UUID_RE.search(text)
        if um and any_url is None:
            any_url = um
        if not (bool(um) or bool(_STACK_MARK_RE.search(text)) or len(frames) >= 3):
            continue
        cnt = c.get("count", 0) or 0
        if best is None or len(frames) > best[0] or (len(frames) == best[0] and cnt < best[1]):
            best = (len(frames), cnt, c, frames)
    if best is None:
        return result
    _n, _cnt, c, frames = best
    text = c.get("text", "") or ""
    result["has_stack"] = True
    result["source_comment"] = c.get("count")
    result["raw"] = text
    um = _UUID_RE.search(text) or any_url
    if um:
        result["uuid"] = um.group(1)
        result["crash_report_url"] = um.group(0)
    mc = _MOZCRASH_RE.search(text)
    if mc:
        result["moz_crash_reason"] = mc.group(1)
    result["frames"] = frames
    result["stack_files"] = sorted({
        os.path.basename(f["file"]) for f in frames if f["file"]
    })
    result["insights"] = {k.strip(): v.strip() for k, v in _INSIGHT_RE.findall(text)}
    return result


# --------------------------------------------------------------------------- #
# Changeset helpers (diff + metadata), migration-aware
# --------------------------------------------------------------------------- #
_BACKOUT_RE = re.compile(
    r"^(?:(?:back(?:ed|ing|s)?(?:[ _]*out[_]?))|(?:revert(?:ing|s)?))\b", re.I
)
_BUG_IN_DESC_RE = re.compile(r"\bbug[ \t]*([0-9]+)", re.I)
_GH_LINK_RE = re.compile(
    r"github\.com/([\w.-]+/[\w.-]+)/(commit|pull)/([0-9a-f]{7,40}|\d+)"
)
# GitHub-PR attachment description: "[mozilla/application-services] Bug N - title (#7491)"
_ATTACH_PR_RE = re.compile(r"\[([\w.-]+/[\w.-]+)\][^\n]*?\(#(\d+)\)")
_HG_LINK_RE = re.compile(
    r"hg\.mozilla\.org/(?P<repo>integration/autoland|mozilla-central|releases/[^/\s]+)/rev/([0-9a-f]{12,40})"
)


def _revision_meta(node, channel="nightly"):
    """desc / author / pushdate(UTC) / backedout / git hash for an hg central node,
    via hg-edge json-rev (throttled + retried)."""
    rev = _hg_get("/json-rev", {"node": node}, as_json=True)
    if not rev:
        return None
    pd = rev.get("pushdate") or [None]
    pushdate = datetime.fromtimestamp(pd[0], tz=timezone.utc) if pd and pd[0] else None
    desc = rev.get("desc", "") or ""
    return {
        "node": utils.short_rev(rev.get("node", node)),
        "desc": desc.splitlines()[0][:200] if desc else "",
        "author": rev.get("user", ""),
        "pushdate": pushdate.isoformat() if pushdate else None,
        "_pushdate_dt": pushdate,
        "backedout": bool(rev.get("backedoutby")) or bool(_BACKOUT_RE.match(desc.strip())),
        "git_commit": rev.get("git_commit", ""),
        "n_files": len(rev.get("files", []) or []),
    }


_DIFF_BYTE_CAP = 1_000_000


def _diff_summary(node, channel="nightly"):
    """patch_extract summary for a changeset: files/functions/identifiers/tags/churn.

    Fetches the git-format diff from hg-edge raw-rev (throttled + retried, no negative
    caching) and reuses patch_extract's pure parse + derived-signal functions."""
    raw = _hg_get("/raw-rev", {"node": node}, as_json=False)
    if raw is None:
        return None
    if len(raw) > _DIFF_BYTE_CAP:
        raw = raw[:_DIFF_BYTE_CAP]
    files = patch_extract.parse_hunks(raw)
    ext = patch_extract.PatchExtraction(node=node, channel=channel, raw_diff=raw, files=files)
    if ext.is_empty():
        return {"empty": True, "files": []}
    return {
        "empty": False,
        "files": [
            {
                "filename": fd.filename, "status": fd.status,
                "functions": patch_extract.enclosing_functions([fd]).get(fd.filename, []),
            }
            for fd in ext.files
        ],
        "enclosing_functions": sorted({
            fn for names in ext.enclosing_functions().values() for fn in names
        }),
        "touched_identifiers": sorted(ext.touched_identifiers())[:200],
        "change_tags": sorted(ext.change_tags()),
        "churn": ext.churn(),
        "is_cosmetic": ext.is_cosmetic(),
        "is_inert": ext.is_inert(),
    }


def _changeset_record(node, channel="nightly"):
    """Combined metadata + diff summary for one landing changeset."""
    meta = _revision_meta(node, channel) or {"node": node}
    diff = _diff_summary(node, channel) or {}
    rec = {**{k: v for k, v in meta.items() if not k.startswith("_")}, "diff": diff}
    rec["_pushdate_dt"] = meta.get("_pushdate_dt")
    return rec


def _landing_nodes(comments, channels=("nightly",)):
    """Central/autoland landing revs parsed from a bug's comments (via libmozdata)."""
    nodes = []
    seen = set()
    for land in Bugzilla.get_landing_comments(comments, list(channels)):
        short = land["revision"][:12]
        if short not in seen:
            seen.add(short)
            nodes.append(short)
    return nodes


def _external_landings(comments, attachments=()):
    """Non-central landings (vendored repos / GitHub PRs) -- referenced, not fetched.

    e.g. bug 2056116's fix landed in mozilla/application-services#7491; the central
    tree only sees a vendoring bump, so the real diff lives in another repo. Sourced
    from both github URLs in comment text and github-PR attachments (the common case)."""
    out = []
    seen = set()

    def add(repo, kind, ident):
        if repo.lower() == "mozilla-firefox/firefox":
            return  # that's the central tree itself, handled via hg
        key = (repo, kind, ident)
        if key not in seen:
            seen.add(key)
            out.append({"repo": repo, "kind": kind, "id": ident})

    for c in comments:
        for repo, kind, ident in _GH_LINK_RE.findall(c.get("text", "") or ""):
            add(repo, kind, ident)
    for a in attachments or ():
        if a.get("is_obsolete"):
            continue
        if "github" in (a.get("content_type", "") or ""):
            m = _ATTACH_PR_RE.search(a.get("description", "") or "")
            if m:
                add(m.group(1), "pull", m.group(2))
    return out


# --------------------------------------------------------------------------- #
# First build containing the regressor + its pushlog window
# --------------------------------------------------------------------------- #
def _window_changesets(from_rev, to_rev):
    """Changesets in the pushlog window (from_rev, to_rev] via hg-edge json-pushes.

    from_rev/to_rev are the previous and the containing nightly's revisions. Tries
    them as-is (buildhub often stores hg revs) and, on 'unknown revision', converts
    via lando git2hg and retries. Returns (changesets, note)."""
    def _query(a, b):
        return _hg_get(
            "/json-pushes",
            {"fromchange": a, "tochange": b, "version": 2, "full": 1},
            as_json=True,
        )

    data = _query(from_rev, to_rev)
    if not data or data.get("error"):
        # Fall back to git->hg for revs hg-edge doesn't recognise as git hashes.
        from crashclouseau import inspector
        a = inspector.git2hg(from_rev) or from_rev
        b = inspector.git2hg(to_rev) or to_rev
        if (a, b) != (from_rev, to_rev):
            data = _query(a, b)
    if not data or data.get("error"):
        return [], "window query failed: %s" % (data.get("error") if data else "no response")

    csets = []
    for _pid, push in sorted(data.get("pushes", {}).items(), key=lambda kv: int(kv[0])):
        for cs in push["changesets"]:
            desc = (cs.get("desc") or "").splitlines()[0] if cs.get("desc") else ""
            bm = _BUG_IN_DESC_RE.search(desc)
            csets.append({
                "node": utils.short_rev(cs["node"]),
                "bug": int(bm.group(1)) if bm else None,
                "backedout": bool(_BACKOUT_RE.match(desc.strip())),
                "merge": len(cs.get("parents", [])) > 1,
                "desc": desc[:160],
                "files": cs.get("files", []),
            })
    return csets, ""


def first_build_and_window(pushdate, channel="nightly", product="Firefox"):
    """First nightly build containing a regressor pushed at ``pushdate`` + its window.

    Uses buildhub.get_enclosing_builds to bracket the pushdate: the build strictly
    before (does not contain the regressor) and the first build at/after (the first
    that does). The window is the pushlog between those two builds' revisions -- the
    exact candidate set a human triages (see bug 2056116 c#1)."""
    if pushdate is None:
        return {"error": "no pushdate for regressor"}
    try:
        builds = buildhub.get_enclosing_builds(pushdate, channel, product)
    except Exception as exc:
        return {"error": "buildhub enclosing_builds failed: %s" % exc}
    if not builds or len(builds) != 2 or not builds[0] or not builds[1]:
        return {"error": "could not bracket pushdate with builds"}
    # get_enclosing_builds returns [before(lt), after(gte)] but order can vary; sort.
    ordered = sorted(builds, key=lambda b: b["buildid"])
    before, after = ordered[0], ordered[1]
    csets, note = _window_changesets(before["revision"], after["revision"])
    out = {
        "buildid": after["buildid"],
        "build_date": utils.get_build_date(after["buildid"]).isoformat(),
        "version": after.get("version", ""),
        "revision": after["revision"],
        "prev_buildid": before["buildid"],
        "prev_revision": before["revision"],
        "n_changesets": len(csets),
        "n_bugs": len({c["bug"] for c in csets if c["bug"]}),
        "changesets": csets,
    }
    if note:
        out["note"] = note
    return out


# --------------------------------------------------------------------------- #
# Regressor + fix assembly
# --------------------------------------------------------------------------- #
def collect_regressor(reg_bug_id, stack_files, channel="nightly", product="Firefox"):
    """Everything about one regressor bug: metadata, landing diffs, first build +
    window, and an on-stack/off-stack label vs the crash's parsed stack."""
    rec = {"bug": reg_bug_id, "summary": "", "product": "", "component": ""}
    # bug meta
    meta = []
    Bugzilla(
        [str(reg_bug_id)],
        include_fields=["id", "summary", "product", "component", "severity", "keywords"],
        bughandler=lambda b, d: d.append(b), bugdata=meta,
    ).get_data().wait()
    if meta:
        b = meta[0]
        rec.update({
            "summary": b.get("summary", ""), "product": b.get("product", ""),
            "component": b.get("component", ""), "severity": b.get("severity", ""),
            "keywords": b.get("keywords", []),
        })
    # landing comments -> nodes
    thread = fetch_thread([reg_bug_id]).get(reg_bug_id, {"comments": [], "attachments": []})
    cmts = thread["comments"]
    nodes = _landing_nodes(cmts, (channel, "beta", "release"))
    rec["landing_revs"] = nodes
    rec["external_landings"] = _external_landings(cmts, thread["attachments"])
    changesets = []
    reg_files = set()
    earliest_pushdate = None
    for node in nodes:
        cs = _changeset_record(node, channel)
        pd = cs.pop("_pushdate_dt", None)
        # non-backout landings define the introduction; a backout among a regressor's
        # own comments is a re-land dance -- keep it, but earliest real land drives build.
        if pd and not cs.get("backedout") and (earliest_pushdate is None or pd < earliest_pushdate):
            earliest_pushdate = pd
        for f in cs.get("diff", {}).get("files", []) or []:
            reg_files.add(os.path.basename(f["filename"]))
        changesets.append(cs)
    rec["changesets"] = changesets
    # on/off-stack label
    if reg_files and stack_files:
        overlap = reg_files & set(stack_files)
        rec["on_stack"] = bool(overlap)
        rec["on_stack_files"] = sorted(overlap)
    else:
        rec["on_stack"] = None
        rec["on_stack_files"] = []
    # first build + pushlog window
    if earliest_pushdate is None and changesets:
        # fall back to first changeset's pushdate even if flagged backout
        for cs in changesets:
            if cs.get("pushdate"):
                earliest_pushdate = datetime.fromisoformat(cs["pushdate"])
                break
    rec["first_build"] = first_build_and_window(earliest_pushdate, channel, product)
    return rec


def collect_fix(crash_comments, crash_attachments=(), channel="nightly"):
    """The crash bug's own fix landing(s). A backout is recorded but flagged so the
    analysis skips deep-diffing it (it just reverts the regressor)."""
    nodes = _landing_nodes(crash_comments, (channel, "beta", "release"))
    external = _external_landings(crash_comments, crash_attachments)
    changesets = []
    any_backout = False
    for node in nodes:
        cs = _changeset_record(node, channel)
        cs.pop("_pushdate_dt", None)
        any_backout = any_backout or cs.get("backedout", False)
        changesets.append(cs)
    return {
        "landing_revs": nodes,
        "external_landings": external,
        "changesets": changesets,
        "is_backout": any_backout,
        "in_tree": bool(nodes),
        "note": ("fix landed in a vendored/external repo (see external_landings); "
                 "no in-tree diff" if (not nodes and external) else ""),
    }


# --------------------------------------------------------------------------- #
# Human-strategy pre-tags (deterministic; substrate for the LLM mining pass)
# --------------------------------------------------------------------------- #
_STRATEGY_PATTERNS = {
    "mozregression": re.compile(r"mozregression", re.I),
    "bisection": re.compile(r"\bbisect", re.I),
    "regression_range": re.compile(r"regression (?:range|window)|pushlog.{0,20}regress", re.I),
    "pushlog": re.compile(r"pushlog|pushloghtml", re.I),
    "first_bad_build": re.compile(r"first (?:bad )?build|first nightly|first (?:build )?with this crash", re.I),
    "backout": re.compile(r"back(?:ed)?[ -]?out|backout", re.I),
    "feature_flip": re.compile(r"enabl\w+.{0,40}by default|flip.{0,20}pref|pref.{0,20}(?:on|enabled)", re.I),
    "reviewer_signal": re.compile(r"reviewer|sync-reviewers|\br=|the (?:one|only) patch", re.I),
    "speculative": re.compile(r"looks like|seems to|might be|probably|good guess|i think", re.I),
    "caused_by": re.compile(r"caused by|introduced (?:by|in)|responsible for", re.I),
    "code_archaeology": re.compile(r"\bblame\b|searchfox|last (?:changed|modified)|who touched", re.I),
    "similar_signature": re.compile(r"same (?:signature|crash)|similar (?:crash|signature)|dup", re.I),
}
_BUG_MENTION_RE = re.compile(r"\bbug[ \t]*([0-9]{6,})", re.I)
_BOT_CREATORS = ("bot@", "-bot@", "@bmo.tld", "automation", "release-mgmt-account")


def strategy_pretags(comments, crash_bug_id, regressor_bugs):
    """Cheap regex pre-tags over the whole thread + provenance of the regressor guess."""
    joined = "\n".join(c.get("text", "") or "" for c in comments)
    tags = {k: bool(p.search(joined)) for k, p in _STRATEGY_PATTERNS.items()}

    # Which comment first names each regressor bug, and by whom (human vs bot)?
    reg_set = {int(b) for b in regressor_bugs}
    first_mentions = []
    other_bugs = set()
    for c in comments:
        text = c.get("text", "") or ""
        creator = c.get("creator", "") or ""
        is_bot = any(tok in creator for tok in _BOT_CREATORS)
        for m in _BUG_MENTION_RE.finditer(text):
            n = int(m.group(1))
            if n == crash_bug_id:
                continue
            if n in reg_set:
                first_mentions.append({
                    "regressor_bug": n, "comment": c.get("count"),
                    "creator": creator, "is_bot": is_bot,
                })
            else:
                other_bugs.add(n)
    # keep only the earliest mention per regressor
    seen = set()
    first_reg_mentions = []
    for fm in first_mentions:
        if fm["regressor_bug"] not in seen:
            seen.add(fm["regressor_bug"])
            first_reg_mentions.append(fm)

    return {
        "tags": tags,
        "regressor_first_mentions": first_reg_mentions,
        "regressor_identified_by_human": any(not fm["is_bot"] for fm in first_reg_mentions),
        "other_bugs_cited": sorted(other_bugs)[:40],
        "n_comments": len(comments),
    }


# --------------------------------------------------------------------------- #
# Per-bug record
# --------------------------------------------------------------------------- #
def collect_bug(bug, channel="nightly", product="Firefox"):
    """Assemble the full record for one crash bug (safe: errors -> notes)."""
    bug_id = bug["id"]
    notes = []
    comments = []
    attachments = []
    try:
        thread = fetch_thread([bug_id]).get(bug_id, {"comments": [], "attachments": []})
        comments = thread["comments"]
        attachments = thread["attachments"]
    except Exception as exc:
        notes.append("comment fetch failed: %s" % exc)

    stack = parse_crash_stack(comments)
    regressor_bugs = [int(b) for b in (bug.get("regressed_by") or []) if b]

    regressors = []
    for rb in regressor_bugs:
        try:
            regressors.append(collect_regressor(rb, stack.get("stack_files", []), channel, product))
        except Exception as exc:  # pragma: no cover - defensive
            regressors.append({"bug": rb, "error": str(exc)})
            notes.append("regressor %s failed: %s" % (rb, exc))

    try:
        fix = collect_fix(comments, attachments, channel)
    except Exception as exc:
        fix = {"error": str(exc)}
        notes.append("fix collection failed: %s" % exc)

    strat = strategy_pretags(comments, bug_id, regressor_bugs)

    record = {
        "crash_bug": {
            "id": bug_id,
            "summary": bug.get("summary", ""),
            "product": bug.get("product", ""),
            "component": bug.get("component", ""),
            "severity": bug.get("severity", ""),
            "priority": bug.get("priority", ""),
            "status": bug.get("status", ""),
            "resolution": bug.get("resolution", ""),
            "keywords": bug.get("keywords", []),
            "creation_time": bug.get("creation_time", ""),
            "cf_last_resolved": bug.get("cf_last_resolved", ""),
            "cf_crash_signature": bug.get("cf_crash_signature", ""),
            "signatures": sorted(utils.get_signatures([bug.get("cf_crash_signature", "")])) if bug.get("cf_crash_signature") else [],
            "regressed_by": regressor_bugs,
            "see_also": bug.get("see_also", []),
        },
        "crash_stack": stack,
        "regressors": regressors,
        "fix": fix,
        "strategy": strat,
        "comments": [
            {"count": c.get("count"), "creator": c.get("creator"),
             "time": c.get("creation_time"), "text": c.get("text", "")}
            for c in comments
        ],
        "meta": {
            "collector_version": COLLECTOR_VERSION,
            "any_regressor_off_stack": any(r.get("on_stack") is False for r in regressors),
            "any_regressor_on_stack": any(r.get("on_stack") is True for r in regressors),
            "notes": notes,
        },
    }
    return record


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def _index_row(rec):
    cb = rec["crash_bug"]
    return {
        "id": cb["id"], "summary": cb["summary"][:80], "product": cb["product"],
        "component": cb["component"], "severity": cb["severity"],
        "signature": (cb["signatures"] or [""])[0],
        "has_stack": rec["crash_stack"]["has_stack"],
        "moz_crash_reason": rec["crash_stack"].get("moz_crash_reason", ""),
        "n_regressors": len(rec["regressors"]),
        "any_off_stack": rec["meta"]["any_regressor_off_stack"],
        "any_on_stack": rec["meta"]["any_regressor_on_stack"],
        "fix_is_backout": rec["fix"].get("is_backout"),
        "fix_in_tree": rec["fix"].get("in_tree"),
        "regressor_identified_by_human": rec["strategy"]["regressor_identified_by_human"],
        "strategy_tags": [k for k, v in rec["strategy"]["tags"].items() if v],
    }


def run(args):
    global _HG_SEM
    _HG_SEM = threading.Semaphore(args.hg_concurrency)
    bugs = query_bugs(
        args.start, args.end,
        products=tuple(args.products), severities=tuple(args.severities),
        require_fixed=not args.no_require_fix,
    )
    if args.limit:
        bugs = bugs[: args.limit]
    os.makedirs(args.out, exist_ok=True)

    lock = threading.Lock()
    done = [0]
    dropped_no_stack = []
    records = []

    def _work(bug):
        rec = collect_bug(bug, channel=args.channel, product=args.product)
        with lock:
            done[0] += 1
            if done[0] % 10 == 0 or done[0] == len(bugs):
                log.info("  %d/%d bugs collected", done[0], len(bugs))
        return rec

    log.info("collecting %d bugs with %d workers", len(bugs), args.workers)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_work, b): b["id"] for b in bugs}
        for fut in as_completed(futures):
            bid = futures[fut]
            try:
                rec = fut.result()
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("bug %s failed entirely: %s", bid, exc)
                continue
            if args.require_stack and not rec["crash_stack"]["has_stack"]:
                dropped_no_stack.append(bid)
                continue
            records.append(rec)
            with open(os.path.join(args.out, "%d.json" % rec["crash_bug"]["id"]), "w") as fh:
                json.dump(rec, fh, indent=2)

    records.sort(key=lambda r: r["crash_bug"]["id"])
    index = [_index_row(r) for r in records]
    with open(os.path.join(args.out, "index.json"), "w") as fh:
        json.dump(index, fh, indent=2)

    off = sum(1 for r in records if r["meta"]["any_regressor_off_stack"])
    on = sum(1 for r in records if r["meta"]["any_regressor_on_stack"])
    manifest = {
        "collector_version": COLLECTOR_VERSION,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "date_range": {"start": args.start, "end": args.end},
        "products": list(args.products) or "all",
        "severities": list(args.severities) or "all",
        "require_stack": args.require_stack,
        "require_fix": not args.no_require_fix,
        "n_bugs_from_bmo": len(bugs),
        "n_records": len(records),
        "n_dropped_no_stack": len(dropped_no_stack),
        "dropped_no_stack_ids": dropped_no_stack,
        "n_any_off_stack": off,
        "n_any_on_stack": on,
    }
    with open(os.path.join(args.out, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    log.info(
        "wrote %d records to %s (dropped %d w/o stack); off-stack>=1: %d, on-stack>=1: %d",
        len(records), args.out, len(dropped_no_stack), off, on,
    )
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", default="2026-01-01", help="creation date >= (YYYY-MM-DD)")
    ap.add_argument("--end", default="2026-07-21", help="creation date <= (YYYY-MM-DD)")
    ap.add_argument("--products", nargs="*", default=list(DEFAULT_PRODUCTS),
                    help="BMO products to restrict to (default: all)")
    ap.add_argument("--severities", nargs="*", default=[],
                    help="restrict to severities (e.g. S1 S2); default: all")
    ap.add_argument("--channel", default="nightly", help="hg/build channel for revs")
    ap.add_argument("--product", default="Firefox", help="build product (buildhub)")
    ap.add_argument("--workers", type=int, default=32, help="thread-pool size")
    ap.add_argument("--hg-concurrency", type=int, default=_HG_CONCURRENCY_DEFAULT,
                    help="max concurrent hg-edge requests (WAF-throttle guard)")
    ap.add_argument("--limit", type=int, default=None, help="cap number of bugs")
    ap.add_argument("--require-stack", action="store_true", default=True,
                    help="keep only bugs whose comments carry a crash stack (default on)")
    ap.add_argument("--no-require-stack", dest="require_stack", action="store_false")
    ap.add_argument("--no-require-fix", action="store_true",
                    help="do not require resolution=FIXED in the BMO query")
    ap.add_argument("--out", default="spike/regressor_dataset", help="output directory")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    run(args)


if __name__ == "__main__":
    main()
