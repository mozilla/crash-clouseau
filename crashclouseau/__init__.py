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


@app.template_filter("linkify")
def linkify(text, repo_url=""):
    """Escape free text, then hyperlink ``bug NNN`` -> Bugzilla and bare 12-40 hex
    changeset hashes -> the channel's hg repo (``repo_url``). The text is HTML-escaped
    FIRST, so the only markup is the anchors we inject from a matched bug id (digits) or
    changeset (hex) — no agent-authored text can inject HTML. Used on the evidence
    panel's free-text fields (mechanism/consistency/data-flow/needinfo/rationale)."""
    if not text:
        return ""
    s = str(escape(text))
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
    return Markup(s)


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
