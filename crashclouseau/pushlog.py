# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from dateutil.relativedelta import relativedelta
from libmozdata.hgmozilla import Mercurial, Revision
from libmozdata import utils as lmdutils
import re
from . import net
from . import buildhub, hgauthors, models, utils
from .logger import logger


BACKOUT_PAT = re.compile(
    r"^(?:(?:back(?:ed|ing|s)?(?:[ _]*out[_]?))|(?:revert(?:ing|s)?)) (?:(?:cset|changeset|revision|rev|of)s?)?",
    re.I | re.DOTALL,
)
BUG_PAT = re.compile(r"^bug[ \t]*([0-9]+)", re.I)


def is_backed_out(desc):
    """Check the patch description to know if we've a backout or not"""
    return BACKOUT_PAT.match(desc) is not None


def get_bug(desc):
    """Get a bug number from the patch description"""
    m = BUG_PAT.search(desc)
    if m:
        return int(m.group(1))
    return -1


# The channel whose pushes must ALWAYS be patch-extracted, whatever they contain. See
# ``suppresses_merge_extraction``.
_ORIGIN_CHANNEL = "nightly"


def suppresses_merge_extraction(channel):
    """May a merge push's members be left un-extracted on *channel*?

    ONLY ON A RELEASE BRANCH, and this scoping is the whole rule — without it the merge rule is
    a catastrophic regression on the one channel production actually ingests.
    ``is_merge_push`` fires on any push containing a ``len(parents) > 1`` changeset, and on
    **mozilla-central that is a normal day**: sheriffs land autoland into central in merge pushes
    several times a day. Measured live off hg.mozilla.org 2026-08-25:

    * 2026-07-26..08-23 (28 d): **26 of 191 pushes (13.6%) contain a merge changeset, and they
      carry 1,193 of 2,186 candidate-bearing changesets — 54.6%.**
    * 2026-08-18..25 (7 d): 11 of 53 pushes, 411 of 558 candidate-bearing = 73.7%. Descriptions:
      "Merge mozilla-central to autoland" x8, "Merge autoland to mozilla-central" x2, "Merge
      firefox-autoland to firefox-main" x1.
    * 2026-08-24 (1 d): 3 of 14 pushes, 139 of 150 changesets = 92.7%.

    So applied to nightly the rule deletes over half the on-stack candidate supply: no
    ``changesets`` rows, no ``patch.parse``, nothing for ``Changeset.find``/``get_scores`` to
    return, and every affected crash falls through to the off-stack path — which is live and
    action-emitting in prod. Caught by ``tests/test_beta_merge_push.TestTheRuleOnTrunk``.

    THE ASYMMETRY IS REAL, not a hedge. On trunk a merge push's members are ORDINARY LANDINGS
    that arrived via autoland — they are the candidates. On a release branch a merge push is the
    cycle merge: its members are a whole development cycle of work that already has ``changesets``
    rows under nightly (1,932 of 1,932 merge-window candidates share a node hash with the m-c
    pushlog, and both repos serve byte-identical ``raw-rev``). Same SQL shape, opposite meaning.

    An UNKNOWN channel extracts everything, which is the pre-existing behaviour and the safe
    direction: a redundant patch parse costs one hg fetch, a missing one costs the candidate."""
    return bool(channel) and channel.lower() != _ORIGIN_CHANNEL


def is_merge_push(push):
    """Does this push contain a merge changeset? (``len(parents) > 1``.)

    Necessary but NOT sufficient for skipping patch extraction — see
    ``suppresses_merge_extraction`` for the channel half, which is load-bearing.

    On a release branch the whole of a development cycle arrives as ONE push carrying the merge
    plus every changeset it brings — measured on mozilla-beta: 5,130 / 6,185 / 6,413 / 6,917
    changesets at a single pushdate for the four merges of 2026-04..08, of which only ~20 are
    themselves merge-flagged. Exactly **5 of 2,356 beta pushes over 126 days** contain a merge
    changeset: the four cycle merges and one 11-changeset push carrying no scorable file at all.
    The median ordinary beta push is 1 changeset and p99 is 6, so on THAT channel nothing else is
    touched — which is why no size threshold is needed there, and why a size threshold would have
    been the wrong fix on trunk too (it would have to be re-fitted per branch)."""
    return any(len(c.get("parents") or ()) > 1 for c in push.get("changesets") or ())


def collect(data, file_filter, channel=None, drop_merge_files=False):
    """Collect the data we need in the pushlog got from hg.mozilla.org.

    A MERGE PUSH YIELDS ITS MEMBERS WITH NO FILES, and both halves of that matter.

    *No files*, because a merge push's members are work already done on the channel they came
    from. ``models.Changeset.add`` creates a ``changesets`` row per interesting file, and
    ``update.analyze_patches`` then owes one serial ``patch.parse`` per row: measured per beta
    merge, 1,932-2,678 of the members touch an interesting file at 3.45-6.51 s a fetch =
    **3.3-4.5 hours of the shared queue**, delivering ~24-33 days of nightly's normal
    changeset rate in one burst, plus ~32,155 SQL round-trips. And 100% of it is redundant:
    1,932 of 1,932 merge-window candidates have a node hash that also exists in the
    mozilla-central pushlog (5,116 of 5,124 non-merge = 99.8%) and both repos serve
    byte-identical ``raw-rev``.

    *Its members*, and NOT "drop the push", because ``models.Build.put_data`` inserts a
    ``builds`` row only ``if rev in revs_c`` — only when a ``nodes`` row for that revision
    already exists on the channel — and the merge-day build's own revision is a MEMBER of the
    merge push (measured: true in 3 of 4 cycles, and its push timestamp IS the buildid in 4 of
    4). Dropping the push therefore deletes the ``builds`` row that bounds BOTH the on-stack
    candidate window (``Build.get_pushdate_before``) and the off-stack one
    (``Build.get_two_last``), silently widening a crash's candidate set from the cycle's ~45-122
    uplifts to the merge's ~5,100 changesets. Keeping the nodes keeps both windows exactly
    where they are; ``Changeset.find`` could not have returned these anyway, since it joins
    ``changesets``."""
    res = []
    # A CYCLE MERGE, which is a release-branch thing ONLY. On trunk a merge push is how autoland
    # lands and its members are ordinary candidates -- 54.6% of them over 28 days -- so neither
    # half of what follows may fire there. See `suppresses_merge_extraction`.
    on_release_branch = suppresses_merge_extraction(channel)
    for push in data["pushes"].values():
        pushdate = lmdutils.get_date_from_timestamp(push["date"])
        cycle_merge = on_release_branch and is_merge_push(push)
        # TWO SEPARATE CONSEQUENCES, and only the first is the caller's choice. `via_merge` is a
        # FACT about how the changeset reached the branch and is always recorded (item 23 reads it
        # to refuse the `regression` keyword). Dropping the FILES is an optimisation that belongs
        # only to the path that writes `changesets` rows and then owes a `patch.parse` per row --
        # `put_filelog` -> `pushlog()`. The off-stack enumeration (`pushlog_for_revs`) needs the
        # file lists to rank with (`_looks_pref_flip` reads them), creates no rows, and pays for
        # no parses, so it keeps them.
        if cycle_merge and drop_merge_files:
            logger.info(
                "merge push at {}: keeping {} node(s), extracting 0 patches".format(
                    pushdate, len(push.get("changesets") or ())
                )
            )
        for chgset in push["changesets"]:
            files = ([] if (cycle_merge and drop_merge_files)
                     else [f for f in chgset["files"] if file_filter(f)])
            desc = chgset["desc"]
            author = chgset["author"]
            res.append(
                {
                    "date": pushdate,
                    "node": utils.short_rev(chgset["node"]),
                    "backedout": is_backed_out(desc),
                    "files": files,
                    "merge": len(chgset["parents"]) > 1,
                    "bug": get_bug(desc),
                    "author": hgauthors.analyze_author(author),
                    # The changeset description (commit message). Kept because it is the
                    # primary triage signal for the P1 off-stack path, where a candidate
                    # has NO line-proximity score — the agent picks what to diff by desc
                    # first. Harmless for existing callers (an extra dict key).
                    "desc": desc,
                    # Did this changeset reach the channel as part of a MERGE push (a whole
                    # development cycle arriving on a release branch at one pushdate) rather
                    # than as its own landing? The on-stack path never sees these -- a merge
                    # push gets no `changesets` rows above, so nothing can score onto a frame
                    # -- but the off-stack path reads this pushlog LIVE, so a merge member
                    # can be an off-stack candidate. What it must not become is a "regression"
                    # claim: "in this build's pushlog window" then degenerates from "landed in
                    # the last 1-3 days" to "landed on trunk some time in the last month" --
                    # 5,192 changesets against the 45-122 of an ordinary beta window. See
                    # `report_bug.is_suspected_regression`.
                    "via_merge": cycle_merge,
                }
            )
    return res


def pushlog(
    startdate, enddate, channel="nightly", file_filter=utils.is_interesting_file
):
    """Get the pushlog from hg.mozilla.org"""
    # Get the pushes where startdate <= pushdate <= enddate
    # pushlog uses strict inequality, it's why we add +/- 1 second
    fmt = "%Y-%m-%d %H:%M:%S"
    startdate -= relativedelta(seconds=1)
    startdate = startdate.strftime(fmt)
    enddate += relativedelta(seconds=1)
    enddate = enddate.strftime(fmt)
    url = "{}/json-pushes".format(Mercurial.get_repo_url(channel))
    r = net.get(
        url,
        params={"startdate": startdate, "enddate": enddate, "version": 2, "full": 1},
    )
    # `drop_merge_files=True`: this is the path whose output becomes `changesets` rows, and
    # therefore one serial `patch.parse` per row -- 1,932-2,678 of them per beta merge.
    return collect(r.json(), file_filter, channel=channel,
                   drop_merge_files=suppresses_merge_extraction(channel))


def pushlog_for_revs(
    startrev, endrev, channel="nightly", file_filter=utils.is_interesting_file
):
    """Get the pushlog from startrev to endrev"""
    # startrev is not include in the pushlog
    url = "{}/json-pushes".format(Mercurial.get_repo_url(channel))
    r = net.get(
        url,
        params={"fromchange": startrev, "tochange": endrev, "version": 2, "full": 1},
    )
    return collect(r.json(), file_filter, channel=channel)


def pushlog_for_revs_url(startrev, endrev, channel):
    """Get the pushlog url from startrev to endrev"""
    return "{}/pushloghtml?fromchange={}&tochange={}".format(
        Mercurial.get_repo_url(channel), startrev, endrev
    )


def pushlog_for_buildid(
    buildid, channel, product, file_filter=utils.is_interesting_file
):
    """Get the pushlog for a buildid/channel/product"""
    data = buildhub.get_two_last(buildid, channel, product)
    if data:
        startrev = data[0]["revision"]
        endrev = data[1]["revision"]
        return pushlog_for_revs(
            startrev, endrev, channel=channel, file_filter=file_filter
        )
    return None


def pushlog_for_buildid_url(buildid, channel, product):
    """Get the pushlog url for a buildid/channel/product"""
    data = models.Build.get_two_last(utils.get_build_date(buildid), channel, product)
    if len(data) != 2:
        data = buildhub.get_two_last(buildid, channel, product)
    if data:
        startrev = data[0]["revision"]
        endrev = data[1]["revision"]
        return pushlog_for_revs_url(startrev, endrev, channel)
    return None


def pushlog_for_pushdate_url(pushdate, channel, product):
    """Get the pushlog url for the build containing pushdate"""
    data = buildhub.get_enclosing_builds(pushdate, channel, product)
    if data:
        startrev = data[0]["revision"]
        if data[1] is None:
            endrev = "tip"
        else:
            endrev = data[1]["revision"]
        return pushlog_for_revs_url(startrev, endrev, channel)
    return None


def pushlog_for_rev_url(revision, channel, product):
    """Get the pushlog url for the build containing revision"""
    data = Revision.get_revision(channel=channel, node=revision)
    pushdate = lmdutils.get_date_from_timestamp(data["pushdate"][0])
    return pushlog_for_pushdate_url(pushdate, channel, product)
