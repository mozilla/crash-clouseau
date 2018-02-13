# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from flask import Flask, send_from_directory
from flask_cors import CORS, cross_origin
from flask_sqlalchemy import SQLAlchemy
import logging
import os
from . import config


app = Flask(__name__, template_folder='../templates')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL',
                                                       config.get_database())
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
cors = CORS(app)
app.config['CORS_HEADERS'] = 'Content-Type'
log = logging.getLogger(__name__)


@app.route('/crashstack.html')
def crashstack_html():
    from crashclouseau import html
    return html.crashstack()


@app.route('/diff.html')
def diff_html():
    from crashclouseau import html
    return html.diff()


@app.route('/')
@app.route('/reports.html')
def reports_html():
    from crashclouseau import html
    return html.reports()


@app.route('/bug.html')
def bug_html():
    from crashclouseau import html
    return html.bug()


@app.route('/favicon.ico')
def favicon():
    return send_from_directory('../static', 'clouseau.ico')


@app.route('/<image>.png')
def image(image):
    return send_from_directory('../static', image + '.png')


@app.route('/ZillaSlabHighlight-Bold.woff2')
def zilla():
    return send_from_directory('../static', 'ZillaSlabHighlight-Bold.woff2')


@app.route('/clouseau.js')
def stop_js():
    return send_from_directory('../static', 'clouseau.js')


@app.route('/clouseau.css')
def stop_css():
    return send_from_directory('../static', 'clouseau.css')


@app.route('/api/javast', methods=['POST'])
@cross_origin()
def api_javast():
    from crashclouseau import api
    return api.javast()


@app.route('/api/bugs', methods=['GET'])
@cross_origin()
def api_bugs():
    from crashclouseau import api
    return api.bugs()
