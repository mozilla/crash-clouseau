# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from flask import request, render_template, abort, redirect
import json
from libmozdata.hgmozilla import Mercurial
from . import utils, models, report_bug
from .logger import logger
from .pushlog import pushlog_for_buildid_url, pushlog_for_rev_url


def crashstack():
    uuid = request.args.get('uuid', '')
    stack, uuid_info = models.CrashStack.get_by_uuid(uuid)
    if uuid_info:
        channel = uuid_info['channel']
        repo_url = Mercurial.get_repo_url(channel)
        sgn_url = utils.make_url_for_signature(uuid_info['signature'],
                                               uuid_info['buildid'],
                                               utils.get_buildid(uuid_info['buildid']),
                                               channel,
                                               uuid_info['product'])
        return render_template('crashstack.html',
                               uuid_info=uuid_info,
                               stack=stack,
                               colors=utils.get_colors(),
                               enumerate=enumerate,
                               repo_url=repo_url,
                               channel=channel,
                               sgn_url=sgn_url)
    abort(404)


def reports():
    try:
        prod = request.args.get('product', 'Firefox')
        channel = request.args.get('channel', 'nightly')
        buildid = request.args.get('buildid', '')
        products = models.UUID.get_buildids()
        if not buildid:
            buildid = products[prod][channel][0][0]
        signatures = models.UUID.get_uuids_from_buildid(buildid,
                                                        prod,
                                                        channel)

        return render_template('reports.html',
                               buildids=json.dumps(products),
                               products=products,
                               selected_product=prod,
                               selected_channel=channel,
                               selected_bid=buildid,
                               signatures=signatures,
                               colors=utils.get_colors())
    except Exception:
        logger.error('Invalid URL: {}'.format(request.url), exc_info=True)
        abort(404)


def reports_no_score():
    try:
        prod = request.args.get('product', 'Firefox')
        channel = request.args.get('channel', 'nightly')
        buildid = request.args.get('buildid', '')
        products = models.UUID.get_buildids(no_score=True)
        if not buildid:
            buildid = products[prod][channel][0][0]
        signatures = models.UUID.get_uuids_from_buildid_no_score(buildid,
                                                                 prod,
                                                                 channel)

        return render_template('reports_no_score.html',
                               buildids=json.dumps(products),
                               products=products,
                               selected_product=prod,
                               selected_channel=channel,
                               selected_bid=buildid,
                               signatures=signatures)
    except Exception:
        logger.error('Invalid URL: {}'.format(request.url), exc_info=True)
        abort(404)


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
        url, ni, signature, bugdata = report_bug.get_info(uuid, changeset)
        bugdata = sorted(bugdata.items())
        return render_template('bug.html',
                               uuid=uuid,
                               url=url,
                               needinfo=ni,
                               bugdata=bugdata,
                               signature=signature)
    abort(404)


def pushlog():
    url = ''
    buildid = request.args.get('buildid', '')
    if buildid:
        channel = request.args.get('channel', 'nightly')
        product = request.args.get('product', 'Firefox')
        url = pushlog_for_buildid_url(buildid, channel, product)
    else:
        rev = request.args.get('rev', '')
        if rev:
            channel = request.args.get('channel', 'nightly')
            product = request.args.get('product', 'Firefox')
            url = pushlog_for_rev_url(rev, channel, product)
    if url:
        return redirect(url)

    abort(404)
