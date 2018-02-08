# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from flask import request, render_template, abort
from libmozdata.hgmozilla import Mercurial
from . import utils, models, report_bug


def crashstack():
    uuid = request.args.get('uuid', '')
    stack, uuid_info = models.CrashStack.get_by_uuid(uuid)
    if uuid_info:
        repo_url = Mercurial.get_repo_url(uuid_info['channel'])
        return render_template('crashstack.html',
                               uuid_info=uuid_info,
                               stack=stack,
                               colors=utils.get_colors(),
                               enumerate=enumerate,
                               sort_chgsets=utils.sort_chgsets,
                               repo_url=repo_url)
    abort(404)


def report():
    score = request.args.get('score', '')
    uuids = []
    if score:
        uuids = models.Score.get_by_score(score)

    return render_template('report.html',
                           uuids=uuids)


def reports():
    prod = request.args.get('product', 'Firefox')
    buildid = request.args.get('buildid', '')
    score = request.args.get('score', '---')
    score = utils.get_correct_score(score)
    channel = 'nightly'
    products = models.UUID.get_buildids_from_channel(channel)
    if not buildid:
        buildid = products[prod][0]
    signatures = models.UUID.get_uuids_from_buildid(buildid,
                                                    prod,
                                                    channel,
                                                    score)

    scores = ['---'] + list(map(str, range(11)))
    return render_template('reports.html',
                           products=products,
                           selected_product=prod,
                           selected_bid=buildid,
                           scores=scores,
                           selected_score=score,
                           signatures=signatures,
                           colors=utils.get_colors(),)


def diff():
    filename = request.args.get('filename', '')
    line = request.args.get('line', '')
    style = request.args.get('style', 'file')
    node = request.args.get('node', '')
    changeset = request.args.get('changeset', '')
    channel = request.args.get('channel', '')
    repo_url = Mercurial.get_repo_url(channel)
    annotate_url = '{}/{}/{}/{}#l{}'.format(repo_url,
                                            style,
                                            node,
                                            filename,
                                            line)
    diff_url = '{}/diff/{}/{}'.format(repo_url,
                                      changeset,
                                      filename)

    return render_template('diff.html',
                           changeset=changeset,
                           filename=filename,
                           annotate_url=annotate_url,
                           diff_url=diff_url)


def bug():
    uuid = request.args.get('uuid', '')
    changeset = request.args.get('changeset', '')

    if uuid and changeset:
        url, ni = report_bug.get_info(uuid, changeset)
        return render_template('bug.html',
                               uuid=uuid,
                               url=url,
                               needinfo=ni)
    abort(404)
