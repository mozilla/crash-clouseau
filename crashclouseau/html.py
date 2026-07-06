# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from flask import request, render_template, abort, redirect
import json
import re
from libmozdata.hgmozilla import Mercurial
from . import utils, models, report_bug, bugzilla_apply
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
        evidence = bugzilla_apply.build_evidence(uuid)
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

        return render_template(
            "reports.html",
            buildids=json.dumps(products),
            products=products,
            selected_product=prod,
            selected_channel=channel,
            selected_bid=buildid,
            signatures=signatures,
            verdicts=verdicts,
            colors=utils.get_colors(),
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
# 301-redirect to these). Unknown channels fall back to the main tree.
_SF_TREE = {
    "nightly": "firefox-main",
    "beta": "firefox-beta",
    "release": "firefox-release",
}
_SF_DEFAULT_TREE = "firefox-main"


def _searchfox_tree(channel):
    return _SF_TREE.get(channel, _SF_DEFAULT_TREE)


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
        ev = bugzilla_apply.build_evidence(uuid)
        if ev:
            dossier = ev.get("dossier") or {}
    diff_lines = _collect_diff_lines(dossier, filename) if filename else []

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
