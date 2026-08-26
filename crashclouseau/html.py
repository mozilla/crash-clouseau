# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from flask import request, render_template, abort, redirect
from datetime import datetime, timezone
import json
import re
from libmozdata.hgmozilla import Mercurial
from . import utils, models, population, report_bug, bugzilla_apply, config
from . import api, sensitive
from .logger import logger
from .pushlog import pushlog_for_buildid_url, pushlog_for_rev_url

_EMAIL_RE = re.compile(r"<([^<>@\s]+@[^<>@\s]+)>")


def crashstack():
    uuid = request.args.get("uuid", "")
    stack, uuid_info = models.CrashStack.get_by_uuid(uuid)
    if uuid_info:
        channel = uuid_info["channel"]
        repo_url = Mercurial.get_repo_url(channel)
        sgn_url = utils.make_url_for_signature(
            uuid_info["signature"],
            uuid_info["buildid"],
            utils.get_buildid(uuid_info["buildid"]),
            channel,
            uuid_info["product"],
        )
        evidence = bugzilla_apply.build_evidence(uuid, public=not api.viewer_authorized())
        # Demangle call-path symbols for display (mangled kept as the hover title).
        if evidence:
            cp = (evidence.get("dossier") or {}).get("call_path") or {}
            for e in cp.get("edges") or []:
                e["caller_pretty"] = utils.demangle(e.get("caller_symbol", ""))
                e["callee_pretty"] = utils.demangle(e.get("callee_symbol", ""))
        # Show the panel for strong-evidence (culprit) and lead verdicts, and whenever
        # area-experts were found (a knowledgeable person to surface, even on abstain);
        # for a bare ABSTAIN only when the UI is configured to surface them.
        vt = evidence["verdict"] if evidence else None
        ui = evidence["ui"] if evidence else {}
        has_experts = bool((evidence.get("dossier") or {}).get("area_experts")) if evidence else False
        show_evidence = bool(evidence and (
            vt == "culprit" or (vt == "lead" and ui.get("show_lead", True)) or (has_experts and ui.get("show_experts", True)) or (vt == "abstain" and ui.get("show_abstain"))
        ))
        # Informative "bug we'd file" preview (eval phase): only when the agent found a
        # regressor to file against (culprit/lead). Best-effort — a lookup failure must
        # never 500 the page.
        bug_preview = None
        if show_evidence and vt in ("culprit", "lead"):
            try:
                bug_preview = report_bug.build_bug_preview(
                    uuid_info, stack, evidence.get("dossier") or {}
                )
            except Exception:
                logger.error("crashstack bug preview failed for %s", uuid, exc_info=True)
        # How many machines this signature is really coming from. Shown for every crash,
        # verdict or not — the report count is on the page whether or not the agent ran, and
        # it is the number most likely to be misread. Best-effort, same as the preview.
        pop = None
        try:
            pop = population.for_crash(uuid_info)
        except Exception:
            logger.error("crashstack population failed for %s", uuid, exc_info=True)
        return render_template(
            "crashstack.html",
            uuid_info=uuid_info,
            stack=stack,
            colors=utils.get_colors(),
            enumerate=enumerate,
            repo_url=repo_url,
            channel=channel,
            sgn_url=sgn_url,
            evidence=evidence,
            show_evidence=show_evidence,
            bug_preview=bug_preview,
            population=pop,
        )
    abort(404)


def reports():
    try:
        prod = request.args.get("product", "Firefox")
        channel = request.args.get("channel", "nightly")
        buildid = request.args.get("buildid", "")
        products = models.UUID.get_buildids()
        signatures = []
        if products:
            if prod not in products:
                prod = next(iter(products))
            if channel not in products[prod]:
                channel = next(iter(products[prod]))
            if not buildid:
                buildid = products[prod][channel][0][0]
            signatures = models.UUID.get_uuids_from_buildid(buildid, prod, channel)

        # Agent verdicts for this build, keyed by uuid, so the index can tag the
        # culprit/lead crashes (empty when the agent hasn't run -> index unchanged).
        verdicts = (
            models.Verdict.map_for_build(buildid, prod, channel) if buildid else {}
        )
        # Abstains are badged on the index only in evaluation mode (SHOW_ABSTAIN), so
        # they're findable while evaluating without cluttering prod (most crashes abstain).
        show_abstain = config.get_agent_ui().get("show_abstain", False)

        return render_template(
            "reports.html",
            buildids=json.dumps(products),
            products=products,
            selected_product=prod,
            selected_channel=channel,
            selected_bid=buildid,
            signatures=signatures,
            verdicts=verdicts,
            show_abstain=show_abstain,
            colors=utils.get_colors(),
        )
    except Exception:
        logger.error("Invalid URL: {}".format(request.url), exc_info=True)
        abort(404)


def _fmt_duration(seconds):
    """Human-readable duration for the tasks view ('45s', '18m', '1.4h')."""
    if seconds is None:
        return "—"
    if seconds < 90:
        return "{:.0f}s".format(seconds)
    if seconds < 5400:
        return "{:.0f}m".format(seconds / 60)
    return "{:.1f}h".format(seconds / 3600)


def _aware(dt):
    """Treat naive timestamps (sqlite) as UTC so arithmetic matches the aware
    timestamps Postgres returns."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _parse_ts(value):
    """Read the ISO timestamp `set_job_id` stamps into the JSONB payload. Returns None
    for anything unreadable — a display clock must never take the page down."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _task_view(rows, stale_after_s, now):
    """Turn raw Dossier.list_tasks rows into per-task display dicts + a fleet summary.

    Pure (takes `now`) so it's testable without patching the clock. A "running" task
    whose last update is older than stale_after_s is flagged stalled: its worker very
    likely died -- the same threshold the reaper uses to requeue orphans. Duration is
    the run time for finished tasks (done/error) and elapsed-so-far for live ones."""
    tasks = []
    counts = {"done": 0, "running": 0, "error": 0, "pending": 0}
    stalled = 0
    filed = 0
    cost_total = 0.0
    costed = 0
    cost_done_total = 0.0
    done_costed = 0
    durations_done = []
    for r in rows:
        created = _aware(r.created)
        updated = _aware(r.updated)
        status = r.status or "pending"
        counts[status] = counts.get(status, 0) + 1

        # Time THIS attempt, not the row. `created` is the crash's first ingest and
        # `reset_for_retrigger` leaves it untouched, so on a retriggered crash it can be
        # days old — the page showed runs that started twenty minutes earlier as
        # "29h running", which reads as a hung fleet during exactly the bulk recovery it
        # was being used to watch. `run_started` is stamped when the attempt claims the
        # row; absent on rows written before it existed, and there `created` is right.
        started = _aware(_parse_ts(getattr(r, "run_started", None))) or created
        if status in ("done", "error") and started and updated:
            duration = (updated - started).total_seconds()
        elif started:
            duration = (now - started).total_seconds()
        else:
            duration = None

        is_stalled = status == "running" and updated is not None and (now - updated).total_seconds() > stale_after_s
        if is_stalled:
            stalled += 1

        cost = float(r.cost_usd) if r.cost_usd is not None else None
        if cost is not None:
            cost_total += cost
            costed += 1
            if status == "done":
                cost_done_total += cost
                done_costed += 1
        if status == "done" and duration is not None:
            durations_done.append(duration)

        tasks.append(
            {
                "uuid": r.uuid,
                # WHICH CHANNEL, and it has to be copied here or the column is dead. The query
                # selects it (`Dossier.list_tasks`) and the template renders it
                # (`templates/tasks.html`), but this dict is between them, and it is a literal:
                # a key it does not name is `Undefined` in Jinja and renders as the `or '?'`
                # fallback, silently, for every row. `getattr` with a default, like the
                # `filed_*` keys below, because the view's tests feed hand-built rows.
                #
                # It is the day-one observable both DEPLOY.md and next_session.md point an
                # operator at for the beta rollout, and at ~4-6 beta dossiers a day against
                # nightly's 85-120 an unlabelled beta row is not findable by eye.
                "channel": getattr(r, "channel", None),
                "version": getattr(r, "version", None),
                "signature": r.signature or "",
                "status": status,
                "stalled": is_stalled,
                "duration_s": duration,
                "duration_str": _fmt_duration(duration),
                "cost_usd": cost,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cache_read_tokens": r.cache_read_tokens,
                "verdict": r.verdict,
                "confidence": r.confidence,
                "created": created,
                "error": getattr(r, "error", None),
                # `getattr` with a default: these came in with automatic filing, and
                # `_task_view` is also fed hand-built rows by the tests.
                "filed_bug": getattr(r, "filed_bug", None),
                "filed_mode": getattr(r, "filed_mode", None),
                "filed_needinfo": getattr(r, "filed_needinfo", None),
                "filed_needinfo_missed": getattr(r, "filed_needinfo_missed", None),
            }
        )
        if getattr(r, "filed_bug", None):
            filed += 1

    total = len(rows)
    done = counts.get("done", 0)
    avg_dur = sum(durations_done) / len(durations_done) if durations_done else None
    return tasks, {
        "total": total,
        "done": done,
        "running": counts.get("running", 0),
        "error": counts.get("error", 0),
        "pending": counts.get("pending", 0),
        "stalled": stalled,
        "filed": filed,
        "pct_done": round(100 * done / total) if total else 0,
        "cost_total": cost_total,
        # Averaged over runs that FINISHED, not over every row carrying a cost. An
        # abandoned or errored run keeps the cost it burned before dying, so dividing by
        # `costed` mixes part-runs into a figure the template labels "avg/done" — it read
        # $2.54 against a true $2.93, a 13% understatement that grows with the failure
        # rate, i.e. it looks best exactly when things are worst. Divided by the done runs
        # that actually RECORDED a cost, not by every done run: a finished row can carry a
        # NULL cost_usd, and counting it as $0 reintroduces the same downward bias from the
        # other side.
        "cost_avg": cost_done_total / done_costed if done_costed else 0.0,
        "duration_avg_s": avg_dur,
        "duration_avg_str": _fmt_duration(avg_dur),
    }


def tasks():
    try:
        # Flag orphans with the same threshold the reaper uses: a run past job_timeout
        # plus a buffer that avoids racing a run legitimately near the cap (see the
        # _STALE_BUFFER_S note in agent.orchestrator).
        stale_after = config.get_agent_job_timeout() + 300
        rows = models.Dossier.list_tasks()
        tasks_, summary = _task_view(rows, stale_after, datetime.now(timezone.utc))
        return render_template("tasks.html", tasks=tasks_, summary=summary)
    except Exception:
        logger.error("Invalid URL: {}".format(request.url), exc_info=True)
        abort(404)


def selection():
    """The selection log: why a signature was, or was not, analysed.

    Answers the question the pipeline could not answer about itself. With a signature it
    shows every recorded decision for it; without one, the recent feed —
    ``untestable_prefix`` rows are the ones a wider window is meant to eliminate."""
    try:
        sgn = request.args.get("signature", "").strip()
        outcome = request.args.get("outcome", "").strip() or None
        if outcome is not None and outcome not in models.SELECTION_OUTCOMES:
            outcome = None
        if sgn:
            rows = models.Selection.for_signature(sgn)
            summary = None
        else:
            rows = models.Selection.recent(outcome)
            summary = sorted(models.Selection.summary().items())
        return render_template(
            "selection.html",
            signature=sgn,
            outcome=outcome,
            outcomes=sorted(models.SELECTION_OUTCOMES),
            rows=rows,
            summary=summary,
        )
    except Exception:
        logger.error("Invalid URL: {}".format(request.url), exc_info=True)
        abort(404)


def reports_no_score():
    try:
        prod = request.args.get("product", "Firefox")
        channel = request.args.get("channel", "nightly")
        buildid = request.args.get("buildid", "")
        products = models.UUID.get_buildids(no_score=True)
        signatures = []
        if products:
            if prod not in products:
                prod = next(iter(products))
            if channel not in products[prod]:
                channel = next(iter(products[prod]))
            if not buildid:
                buildid = products[prod][channel][0][0]
            signatures = models.UUID.get_uuids_from_buildid_no_score(
                buildid, prod, channel
            )

        return render_template(
            "reports_no_score.html",
            buildids=json.dumps(products),
            products=products,
            selected_product=prod,
            selected_channel=channel,
            selected_bid=buildid,
            signatures=signatures,
        )
    except Exception:
        logger.error("Invalid URL: {}".format(request.url), exc_info=True)
        abort(404)


# searchfox tree per Firefox channel (post hg->git migration; mozilla-* names
# 301-redirect to these). RENDERED from `searchfox.repo_for_channel`, not restated: this map
# was a second hand-written copy of the agent side's channel->repo decision, and the agent
# side did not have one at all (`SearchfoxCtx` had no channel, so every beta read went to
# firefox-main). One table, two consumers -- see `searchfox._CHANNEL_REPO`.
_SF_DEFAULT_TREE = "firefox-main"


def _searchfox_tree(channel):
    try:
        from crashclouseau.searchfox import repo_for_channel

        return repo_for_channel(channel).tree
    except Exception:  # pragma: no cover - defensive; a UI link must never 500
        return _SF_DEFAULT_TREE


def _collect_diff_lines(dossier, filename):
    """Gather the persisted ``diff_line`` citations for one file from anywhere in the
    dossier (hunks, call-path edges, data-flow, verdict claims) — the changed lines
    are already evidence, so the diff pane needs no fetch (cloud-safe). Deduped by
    (line, side, content), sorted by line."""
    out = {}

    def walk(node):
        if isinstance(node, dict):
            if node.get("kind") == "diff_line" and node.get("filename") == filename:
                key = (node.get("line"), node.get("side"), node.get("content"))
                out.setdefault(key, {
                    "line": node.get("line") or 0,
                    "side": node.get("side") or "context",
                    "content": node.get("content") or "",
                })
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(dossier or {})
    return sorted(
        out.values(),
        key=lambda d: (d["line"], 0 if d["side"] == "deleted" else 1),
    )


_DIFF_FILE_RE = re.compile(r"^diff --git a/(.*?) b/(.*)$")
_DIFF_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _file_diff_lines(raw_diff, filename, cited):
    """Render the FULL unified diff of ``filename`` from a git-format raw changeset diff
    as ``[{kind, ln, content, hl}]`` (kind: hunk|added|deleted|context). ``cited`` is a
    set of ``(side, line)`` the dossier flagged — those lines get ``hl=True`` so the
    crash-relevant hunk is highlighted within the whole file's diff. Empty if the file
    isn't in the diff (or the fetch failed)."""
    out = []
    if not raw_diff:
        return out
    in_file = False
    old_ln = new_ln = 0
    for ln in raw_diff.splitlines():
        fm = _DIFF_FILE_RE.match(ln)
        if fm:
            in_file = filename in (fm.group(1), fm.group(2))
            continue
        if not in_file:
            continue
        if ln.startswith("@@"):
            hm = _DIFF_HUNK_RE.match(ln)
            if hm:
                old_ln, new_ln = int(hm.group(1)), int(hm.group(2))
            out.append({"kind": "hunk", "ln": None, "old": None, "new": None,
                        "content": ln, "hl": False})
        elif ln.startswith("+++") or ln.startswith("---"):
            continue  # file-header meta lines
        elif ln.startswith("+"):
            # ``old`` is None: an added line does NOT exist in the old file. Keeping the
            # two numbers in separate columns (``old``/``new``) is what avoids the
            # single-column collision where a deleted line's OLD number and an added
            # line's NEW number both landed in one gutter and read as duplicated/backwards.
            out.append({"kind": "added", "ln": new_ln, "old": None, "new": new_ln,
                        "content": ln[1:], "hl": ("added", new_ln) in cited})
            new_ln += 1
        elif ln.startswith("-"):
            out.append({"kind": "deleted", "ln": old_ln, "old": old_ln, "new": None,
                        "content": ln[1:], "hl": ("deleted", old_ln) in cited})
            old_ln += 1
        elif ln[:1] in ("d", "i", "n", "r", "c", "B", "G", "S") and (
            ln.startswith(("diff ", "index ", "new file", "deleted file",
                           "rename ", "copy ", "Binary ", "GIT binary", "similarity "))
        ):
            continue  # inter-file / mode meta
        else:
            content = ln[1:] if ln.startswith(" ") else ln  # context (or blank)
            out.append({"kind": "context", "ln": new_ln, "old": old_ln, "new": new_ln,
                        "content": content, "hl": ("context", new_ln) in cited})
            old_ln += 1
            new_ln += 1
    return out


def codeview():
    """Two-pane code view for a touched file (#12): searchfox source on the left,
    the changed lines (rendered from the persisted dossier evidence) on the right.
    No hg/GitHub web viewer, no fetch — cloud-safe."""
    uuid = request.args.get("uuid", "")
    filename = request.args.get("filename", "")
    node = request.args.get("node", "")          # regressor changeset (for display)
    line = request.args.get("line", "")
    channel = request.args.get("channel", "")
    rev = request.args.get("rev", "")            # build source revision (from the DB)
    tree = request.args.get("repo", "") or _searchfox_tree(channel)

    dossier = {}
    if uuid:
        ev = bugzilla_apply.build_evidence(uuid, public=not api.viewer_authorized())
        if ev:
            # A withheld dossier is `{}` here, so the changeset's own diff still renders (it is
            # public hg content) with none of OUR line highlighting, which is the part that says
            # where the analysis was looking.
            dossier = ev.get("dossier") or {}
    diff_lines = _collect_diff_lines(dossier, filename) if filename else []

    # Full diff of the changeset (``node``) for this file, with the dossier's cited
    # (crash-relevant) lines highlighted. Fetched on demand (cached per process) via the
    # patch-extraction raw-rev source; degrades to the cited lines alone on any failure.
    file_diff = []
    if node and filename:
        try:
            from crashclouseau.agent import patch_extract
            cited = {(d["side"], int(d["line"])) for d in diff_lines if d.get("line")}
            file_diff = _file_diff_lines(
                patch_extract.fetch_raw_diff(node, channel), filename, cited
            )
        except Exception as exc:  # pragma: no cover - defensive; degrade to cited lines
            logger.warning("codeview: full diff for %s @ %s failed: %s", filename, node, exc)

    # searchfox indexes ~tip, NOT arbitrary build revisions, so pinning to the crash's
    # build rev gives "Bad revision" (HTTP 500). Use the tip /source/ view, which always
    # resolves; the crash-accurate changed lines live in the right pane (from the
    # dossier). `rev` is kept for display only. (The agent's own searchfox citations
    # work because searchfox-cli returns permalinks at searchfox's INDEXED rev.)
    if filename:
        sf_base = "https://searchfox.org/{}/source/{}".format(tree, filename)
    else:
        sf_base = "https://searchfox.org/{}/".format(tree)
    # searchfox anchors each line as id="line-<n>" (NOT id="<n>"), so the fragment
    # must be "#line-<n>" for the browser to natively scroll to it.
    sf_src = sf_base + ("#line-{}".format(line) if line else "")

    return render_template(
        "codeview.html",
        filename=filename,
        node=node,
        line=line,
        channel=channel,
        rev=rev,
        tree=tree,
        sf_base=sf_base,
        sf_src=sf_src,
        diff_lines=diff_lines,
        file_diff=file_diff,
    )


def diff():
    filename = request.args.get("filename", "")
    line = request.args.get("line", "")
    style = request.args.get("style", "file")
    node = request.args.get("node", "")
    changeset = request.args.get("changeset", "")
    channel = request.args.get("channel", "")
    repo_url = Mercurial.get_repo_url(channel)
    annotate_url = "{}/{}/{}/{}#l{}".format(repo_url, style, node, filename, line)
    diff_url = "{}/diff/{}/{}".format(repo_url, changeset, filename)

    return render_template(
        "diff.html",
        changeset=changeset,
        filename=filename,
        annotate_url=annotate_url,
        diff_url=diff_url,
    )


def _draft_evidence(uuid, changeset):
    """Evidence summary + suspected-regressor author for the preserved enter_bug
    draft (#12). Returns ``(summary_text|None, ni_email|None, author_display|None)``.
    The author is surfaced only when the dossier's culprit node matches the drafted
    changeset. Degrades to ``(None, None, None)`` for any non-culprit/absent verdict."""
    try:
        ev = models.Verdict.get_evidence(uuid)
    except Exception:
        ev = None
    if not ev or ev.get("verdict") != "culprit":
        return None, None, None
    # bug.html is the fourth publishing surface and the sharpest of them: it renders the text we
    # would FILE, which for a memory-safety crash includes `build_exposer_note`'s "so this is a
    # use-after-free or an uninitialised read". It does not come through `build_evidence`, so it
    # needs its own check -- see `sensitive.py`.
    if not api.viewer_authorized() and sensitive.is_withheld(
            (ev.get("dossier") or {}).get("corroborations")):
        return None, None, None

    dossier = ev.get("dossier") or {}
    cand = dossier.get("candidate") or {}
    vdict = dossier.get("verdict") or {}
    mech = ((vdict.get("mechanism") or {}).get("statement") or "").strip()
    conf = vdict.get("confidence") or ""

    lines = []
    if mech:
        head = "Clouseau evidence" + (" (confidence {})".format(conf) if conf else "")
        lines.append("{}: {}".format(head, mech))

    node = cand.get("node") or ""
    author = (cand.get("author") or "").strip()
    if node:
        detail = "Suspected regressor: {}".format(node)
        if cand.get("bug"):
            detail += " (bug {})".format(cand["bug"])
        if author:
            detail += " by {}".format(author)
        lines.append(detail + ".")

    summary = "\n".join(lines) if lines else None

    matches = bool(node and changeset and node[:12] == changeset[:12])
    author_display = author if (matches and author) else None
    ni_email = None
    if author_display:
        m = _EMAIL_RE.search(author_display)
        if m:
            ni_email = m.group(1)
        elif "@" in author_display and " " not in author_display:
            ni_email = author_display
    return summary, ni_email, author_display


def bug():
    uuid = request.args.get("uuid", "")
    changeset = request.args.get("changeset", "")

    if uuid and changeset:
        summary, culprit_ni, culprit_author = _draft_evidence(uuid, changeset)
        url, ni, signature, bugdata = report_bug.get_info(
            uuid, changeset, evidence_summary=summary
        )
        bugdata = sorted(bugdata.items())
        return render_template(
            "bug.html",
            uuid=uuid,
            url=url,
            needinfo=ni,
            bugdata=bugdata,
            signature=signature,
            evidence_summary=summary,
            culprit_author=culprit_author,
            culprit_ni=culprit_ni,
        )
    abort(404)


def pushlog():
    url = ""
    buildid = request.args.get("buildid", "")
    if buildid:
        channel = request.args.get("channel", "nightly")
        product = request.args.get("product", "Firefox")
        url = pushlog_for_buildid_url(buildid, channel, product)
    else:
        rev = request.args.get("rev", "")
        if rev:
            channel = request.args.get("channel", "nightly")
            product = request.args.get("product", "Firefox")
            url = pushlog_for_rev_url(rev, channel, product)
    if url:
        return redirect(url)

    abort(404)
