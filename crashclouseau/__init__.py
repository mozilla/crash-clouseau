# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from flask import Flask, send_from_directory
from flask_cors import CORS, cross_origin
from flask_sqlalchemy import SQLAlchemy
from libmozdata.socorro import Socorro
from markupsafe import Markup, escape
import logging
import os
import re
from . import config
# Import for its module-level side effect: stamps our allowlisted `crash-clouseau`
# User-Agent onto libmozdata's hg Connection (which otherwise sends User-Agent: None and
# gets 406-rate-limited). Imported here so the fix is applied on ANY `crashclouseau.*`
# import, before the agent's hg-backed tools (blame/history/patch-diff) make a request.
from . import net  # noqa: F401


app = Flask(__name__, template_folder="../templates")

Socorro.TOKEN = os.getenv("SOCORRO_TOKEN", config.get_socorro())

uri = os.getenv("DATABASE_URL", config.get_database())
# Workaround for Heroku
if uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = uri
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)
cors = CORS(app)
app.config["CORS_HEADERS"] = "Content-Type"
log = logging.getLogger(__name__)
app.app_context().push()


_BUG_RE = re.compile(r"\bbug\s+(\d+)", re.I)
_HASH_RE = re.compile(r"\b[0-9a-f]{12,40}\b")
# Pretty-print ASCII flow arrows in agent prose (e.g. call chains "A -> B -> C"),
# but ONLY when whitespace-delimited — never touch C++ member access like
# `data->SetInvoker`, which has no surrounding spaces. Applied to the raw text
# before escaping, so the substituted Unicode char passes through escape() untouched.
_ARROWS = {"->": "→", "<-": "←", "<->": "↔"}
_ARROW_RE = re.compile(r"(?<=\s)(<->|<-|->)(?=\s)")
# Inline `code` spans (markdown backticks) the agent emits in its prose — e.g. a
# quoted expression or symbol in a data-flow/mechanism summary. Kept to a single line
# so a stray backtick can't swallow a whole paragraph.
_CODE_RE = re.compile(r"`([^`\n]+)`")


def _linkify_prose(text, repo_url):
    """Prose pipeline: prettify whitespace-delimited arrows, HTML-escape, then hyperlink
    ``bug NNN`` and bare 12-40 hex changeset hashes. Code spans are handled separately in
    ``linkify`` (this never sees them)."""
    s = _ARROW_RE.sub(lambda m: _ARROWS[m.group(1)], text)
    s = str(escape(s))
    s = _BUG_RE.sub(
        r'<a href="https://bugzilla.mozilla.org/\1" target="_blank" '
        r'rel="noopener">bug \1</a>',
        s,
    )
    if repo_url:
        s = _HASH_RE.sub(
            lambda m: '<a href="{}/rev?node={}" target="_blank" rel="noopener">{}</a>'
            .format(repo_url, m.group(0), m.group(0)),
            s,
        )
    return s


@app.template_filter("linkify")
def linkify(text, repo_url=""):
    """Render an agent free-text field to safe HTML. Inline ```code``` spans become
    ``<code>`` (their content HTML-escaped and EXEMPT from the arrow/bug/hash rewrites,
    so C++ like ``a->b`` stays literal); the prose around them is escaped and gets
    ``bug NNN`` / changeset-hash links plus pretty arrows. Everything is HTML-escaped, so
    no agent-authored text can inject markup. Used on the evidence panel's free-text
    fields (mechanism/consistency/data-flow/needinfo/rationale/expert reason)."""
    if not text:
        return ""
    s = str(text)
    parts, last = [], 0
    for m in _CODE_RE.finditer(s):
        parts.append(_linkify_prose(s[last:m.start()], repo_url))
        parts.append("<code>" + str(escape(m.group(1))) + "</code>")
        last = m.end()
    parts.append(_linkify_prose(s[last:], repo_url))
    return Markup("".join(parts))


@app.template_filter("human_gap")
def human_gap(seconds):
    """A gap between two install_time values, in the largest unit that keeps it readable
    ("20s", "2.4min", "8.0h", "3.1d"). The interesting values span five orders of magnitude —
    the measured median is 8 hours and the flagged cases are tens of seconds — so a single unit
    would make one end or the other unreadable."""
    if seconds is None:
        return "—"
    s = float(seconds)
    if s < 90:
        return "{:.0f}s".format(s)
    if s < 5400:
        return "{:.1f}min".format(s / 60)
    if s < 86400:
        return "{:.1f}h".format(s / 3600)
    return "{:.1f}d".format(s / 86400)


@app.teardown_request
def _remove_db_session(exc=None):
    # The module-level app.app_context().push() above gives the worker/clock a
    # persistent context, but it also means Flask reuses that single app context
    # for every web request, so Flask-SQLAlchemy's per-context session teardown
    # never fires. Without this, one failed query (e.g. right after the DB is
    # rebuilt) leaves the session in an aborted transaction and every later
    # request fails with PendingRollbackError (surfacing as a 404) until the
    # dyno is restarted. Resetting the session per request lets the web app
    # recover on its own.
    db.session.remove()


@app.route("/crashstack.html")
def crashstack_html():
    from crashclouseau import html

    return html.crashstack()


@app.route("/diff.html")
def diff_html():
    from crashclouseau import html

    return html.diff()


@app.route("/codeview.html")
def codeview_html():
    from crashclouseau import html

    return html.codeview()


@app.route("/")
@app.route("/reports.html")
def reports_html():
    from crashclouseau import html

    return html.reports()


@app.route("/reports_no_score.html")
def reports_no_scorehtml():
    from crashclouseau import html

    return html.reports_no_score()


@app.route("/tasks.html")
def tasks_html():
    from crashclouseau import html

    return html.tasks()


@app.route("/selection.html")
def selection_html():
    from crashclouseau import html

    return html.selection()


@app.route("/bug.html")
def bug_html():
    from crashclouseau import html

    return html.bug()


@app.route("/pushlog.html")
def pushlog_html():
    from crashclouseau import html

    return html.pushlog()


@app.route("/favicon.ico")
def favicon():
    return send_from_directory("../static", "clouseau.ico")


@app.route("/<image>.png")
def image(image):
    return send_from_directory("../static", image + ".png")


@app.route("/ZillaSlabHighlight-Bold.woff2")
def zilla():
    return send_from_directory("../static", "ZillaSlabHighlight-Bold.woff2")


@app.route("/clouseau.js")
def stop_js():
    return send_from_directory("../static", "clouseau.js")


@app.route("/clouseau.css")
def stop_css():
    return send_from_directory("../static", "clouseau.css")


@app.route("/api/javast", methods=["POST"])
@cross_origin()
def api_javast():
    from crashclouseau import api

    return api.javast()


@app.route("/api/bugs", methods=["GET"])
@cross_origin()
def api_bugs():
    from crashclouseau import api

    return api.bugs()


@app.route("/api/reports", methods=["GET"])
@cross_origin()
def api_reports():
    from crashclouseau import api

    return api.reports()


@app.route("/api/selection", methods=["GET"])
@cross_origin()
def api_selection():
    from crashclouseau import api

    return api.selection()


@app.route("/api/evidence", methods=["GET"])
@cross_origin()
def api_evidence():
    from crashclouseau import api

    return api.evidence()


@app.route("/api/evidence/apply", methods=["POST"])
@cross_origin()
def api_evidence_apply():
    from crashclouseau import api

    return api.apply_actions()


@app.route("/api/tasks/retrigger", methods=["POST"])
@cross_origin()
def api_tasks_retrigger():
    from crashclouseau import api

    return api.retrigger()
