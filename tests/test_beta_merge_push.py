# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Plan #18 item 30 / test plan T4: a MERGE PUSH must create its `nodes` rows and NO
# `changesets` rows -- and item 23: a candidate that arrived by merge must not be called a
# regressor.
#
# WHY THE FILES ARE DROPPED. On a release branch a whole development cycle arrives as ONE
# push at ONE pushdate: 5,130 / 6,185 / 6,413 / 6,917 changesets for the four
# mozilla-beta merges of 2026-04..08. `models.Changeset.add` creates a `changesets` row per
# interesting file and `update.analyze_patches` then owes one SERIAL, self-re-enqueuing
# `patch.parse` per row -- 1,932-2,678 of the members touch an interesting file, at
# 3.45-6.51 s a fetch = 3.3-4.5 HOURS of the shared queue per merge, plus ~32,155 SQL
# round-trips. And 100% of it is redundant: 1,932 of 1,932 merge-window candidates have a
# node hash that also exists in the mozilla-central pushlog (5,116 of 5,124 non-merge =
# 99.8%) and both repos serve byte-identical `raw-rev`.
#
# WHY THE NODES ARE KEPT -- the trap, and the only reason this file is Postgres-gated.
# `Build.put_data` inserts a `builds` row only `if rev in revs_c`, i.e. only when a `nodes`
# row for that revision already exists on the channel, and the merge-day build's OWN
# revision (22761955d964, "Update configs after merge day operations") is a MEMBER of the
# merge push (pushid 27990, pushdate 2026-08-13 14:15:59, which is literally the buildid
# 20260813141559; the push-timestamp == buildid identity holds 4 of 4 cycles). Dropping the
# push therefore deletes the row that bounds BOTH the on-stack candidate window
# (`Build.get_pushdate_before`) and the off-stack one (`Build.get_two_last`), widening a
# crash's candidate set from the cycle's 45-122 uplifts to the merge's 5,144-6,952
# changesets. See tests/test_beta_windows.py for the windows themselves.
#
# WHAT THE SELECTION RULE IS NOT: a size threshold. Plan #18 measured exactly 5 of 2,356
# BETA pushes over 126 days as containing a merge changeset (the 4 cycle merges + one
# 11-changeset push with 0 scorable files), against a median ordinary beta push of 1
# changeset and p99 = 6. `TestTheRuleOnTrunk` below is the other half of that denominator,
# and it does not agree.
#
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_beta_merge_push
#   # the row-level half needs a DISPOSABLE Postgres (pg.insert/on_conflict, pg.ARRAY):
#   DATABASE_URL=postgresql://clouseau:passwd@localhost:55432/beta_merge_push \
#     REDIS_URL=redis://localhost:6379/0 python -m unittest tests.test_beta_merge_push
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import db, models, pushlog, report_bug, utils  # noqa: E402
from crashclouseau.agent import orchestrator as orch  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    Candidate,
    Claim,
    Confidence,
    Decision,
    Dossier,
    SearchfoxCitation,
    Verdict,
)


def _is_postgres():
    try:
        return db.engine.dialect.name == "postgresql"
    except Exception:
        return False


# --- the measured beta merge, push 27990 --------------------------------------------------
# `lmdutils.get_date_from_timestamp(1786630559)` == 2026-08-13 14:15:59+00:00 == the
# buildid of the merge-day build, 20260813141559.
MERGE_TS = 1786630559
MERGE_BUILDID = "20260813141559"
# The merge-day build's own revision, and a MEMBER of the merge push -- this is the hash the
# `builds` row hangs off.
MERGE_DAY_REV = "22761955d964"
# A real non-merge member of push 27990 (beta pushdate 2026-08-13T14:15:59, central
# 2026-07-21T09:46:48 -- the 23.2-day forward shift of plan #18 item 12).
MERGE_MEMBER = "f44045181a24"
SCORABLE = "dom/ipc/ContentParent.cpp"     # the most-touched interesting file on beta


def _member(i):
    """A synthetic non-merge member of the cycle merge: an ordinary bug fix that landed on
    central weeks ago and is reaching beta now, touching one scorable file."""
    return {
        "node": "be7a{:08x}".format(i) + "0" * 28,
        "desc": "Bug {} - do a thing r=someone".format(2050000 + i),
        "author": "Dev {0} <dev{0}@example.com>".format(i % 7),
        "files": [SCORABLE, "browser/components/Foo.sys.mjs"],
        "parents": ["e" * 40],
    }


def _beta_merge_push(n_members=499):
    """The cycle merge as `json-pushes?full=1` serves it: ONE push, one `parents`-of-2
    changeset, and everything the cycle brings as ordinary single-parent members. 501
    changesets here against the measured 5,130-6,952 -- the count is the only thing scaled
    down, because 501 already takes ~2 s of `HGAuthor.get_id` round-trips."""
    changesets = [
        {
            "node": "be7a0000merg" + "0" * 28,
            "desc": "Merge mozilla-central to mozilla-beta a=merge",
            "author": "ffxbld <ffxbld@lando.moz.tools>",
            "files": ["browser/config/version.txt"],
            "parents": ["b" * 40, "c" * 40],
        },
        {
            # The revision the merge-day `builds` row points at. Not scorable, but its
            # `nodes` row is load-bearing -- see the module comment.
            "node": MERGE_DAY_REV + "0" * 28,
            "desc": "Update configs after merge day operations a=release CLOSED TREE "
                    "DONTBUILD",
            "author": "ffxbld <ffxbld@lando.moz.tools>",
            "files": ["browser/config/version.txt", "browser/config/version_display.txt"],
            "parents": ["d" * 40],
        },
        {
            "node": MERGE_MEMBER + "0" * 28,
            "desc": "Bug 2049938 - define and send new fxa personalization ping. r=mconley",
            "author": "Dev <dev@example.com>",
            "files": [SCORABLE],
            "parents": ["f" * 40],
        },
    ]
    changesets += [_member(i) for i in range(n_members - len(changesets))]
    return {"pushes": {"27990": {"date": MERGE_TS, "changesets": changesets,
                                 "user": "ffxbld@lando.moz.tools"}}}


def _ordinary_beta_push(n=6):
    """An in-cycle beta push: p99 of the 2,356 measured pushes is 6 changesets, and 93.2% of
    the candidate-bearing in-cycle ones are approved bug-fix uplifts (`a=<release manager>`)
    or backouts -- a stronger regression claim than anything nightly produces."""
    return {"pushes": {"28104": {
        "date": 1787000000,
        "user": "rvandermeulen@mozilla.com",
        "changesets": [
            {
                "node": "0ff1ce0000{:02x}".format(i) + "0" * 28,
                "desc": "Bug {} - fix the thing r=someone a=RyanVM".format(2060000 + i),
                "author": "Dev <dev@example.com>",
                "files": ["dom/base/Element{}.cpp".format(i),
                          "browser/locales/l10n-changesets.json"],
                "parents": ["a" * 40],
            }
            for i in range(n)
        ],
    }}}


# --- the measured mozilla-central push 45154 ----------------------------------------------
# Read live off hg.mozilla.org on 2026-08-25 (`json-pushes?full=1`, allowlisted UA): 18
# changesets pushed by ctuns@mozilla.com at 1787564047, of which SEVENTEEN are ordinary
# single-parent landings and the eighteenth is `Merge autoland to mozilla-central` with two
# parents. Four of the seventeen are reproduced verbatim below.
TRUNK_MERGE_PUSH = {"pushes": {"45154": {
    "date": 1787564047,
    "user": "ctuns@mozilla.com",
    "changesets": [
        {"node": "4ff0ae7f291c" + "0" * 28,
         "desc": "Bug 2060295 r=gfx-reviewers,lsalzman",
         "author": "Lee Salzman <lsalzman@mozilla.com>",
         "files": ["gfx/layers/composite/TextureHost.cpp",
                   "gfx/layers/opengl/TextureHostOGL.cpp"],
         "parents": ["1" * 40]},
        {"node": "94678b10b000" + "0" * 28,
         "desc": "Bug 2061393 - Validate the content compositor bridge namespace and "
                 "reject live p... r=gfx-reviewers",
         "author": "Dev <dev@mozilla.com>",
         "files": ["gfx/ipc/GPUParent.cpp", "gfx/ipc/GPUParent.h",
                   "gfx/ipc/GPUProcessManager.cpp"],
         "parents": ["2" * 40]},
        {"node": "b02f2c45258a" + "0" * 28,
         "desc": "Bug 2065364 - Fix assertion failure when suspending with "
                 "JS_IS_CONSTRUCTING r=jandem",
         "author": "Dev <dev@mozilla.com>",
         "files": ["js/src/jit-test/tests/generators/resume-constructing.js",
                   "js/src/jit/WarpCacheIRTranspiler.cpp"],
         "parents": ["3" * 40]},
        {"node": "a8c7946401dd" + "0" * 28,
         "desc": "No Bug - Bumping Firefox l10n changesets r=release a=l10n-bump "
                 "DONTBUILD CLOSED TREE",
         "author": "ffxbld <ffxbld@lando.moz.tools>",
         "files": ["browser/locales/l10n-changesets.json"],
         "parents": ["4" * 40]},
        {"node": "69981ff91c5f" + "0" * 28,
         "desc": "Merge autoland to mozilla-central",
         "author": "Cristian Tuns <ctuns@mozilla.com>",
         "files": ["netwerk/sctp/src/win32-free.patch",
                   "netwerk/sctp/src/win32-rands.patch"],
         "parents": ["5" * 40, "6" * 40]},
    ],
}}}


class TestTheSelectionRule(unittest.TestCase):
    """`is_merge_push` is a predicate on ONE push, and the whole of item 30's selection rule.

    Deliberately not a size threshold: "a threshold would have to be re-fitted per branch"
    (`pushlog.is_merge_push`'s own docstring). The 5th of the 5 qualifying beta pushes over
    126 days is push 26875 with 11 changesets and 0 candidate-bearing ones, so a size cut
    would have to sit somewhere between 11 and 5,130 and would then be a fit."""

    def test_a_push_is_a_merge_push_when_any_member_has_two_parents(self):
        self.assertTrue(pushlog.is_merge_push(_beta_merge_push(4)["pushes"]["27990"]))
        self.assertFalse(pushlog.is_merge_push(_ordinary_beta_push()["pushes"]["28104"]))

    def test_the_rule_is_a_merge_changeset_and_not_a_push_size(self):
        # Big and single-parent throughout: NOT a merge push, however large. This is the
        # shape a bulk backout or an l10n import takes.
        big = {"date": MERGE_TS, "changesets": [_member(i) for i in range(400)]}
        self.assertFalse(pushlog.is_merge_push(big))
        # Small and carrying a merge: IS a merge push. This is the measured 5th beta push
        # (26875, 11 changesets, 0 candidate-bearing).
        small = _beta_merge_push(11)["pushes"]["27990"]
        self.assertEqual(len(small["changesets"]), 11)
        self.assertTrue(pushlog.is_merge_push(small))

    def test_a_push_with_no_usable_parents_field_is_not_a_merge_push(self):
        # `collect` runs on whatever json-pushes hands back on every 20-minute tick, and a
        # predicate that raises there stops ingestion for the channel. Absent/None/empty all
        # have to answer "no", not explode.
        for push in ({}, {"changesets": None}, {"changesets": []},
                     {"changesets": [{"parents": None}]},
                     {"changesets": [{"parents": []}]}):
            with self.subTest(push=push):
                self.assertFalse(pushlog.is_merge_push(push))


def _ingest(data, file_filter=None):
    """`collect` AS `update.put_filelog` CALLS IT for a release branch.

    Both keyword arguments are load-bearing and they are separate decisions (see
    `pushlog.collect`): `channel` decides whether a merge push is a CYCLE merge at all -- on
    trunk it never is, because that is how autoland lands, and applying the rule there would
    delete 54.6% of nightly's candidate-bearing changesets (`TestTheRuleOnTrunk`) -- and
    `drop_merge_files` is the patch-extraction optimisation, which belongs only to the path that
    creates `changesets` rows and therefore owes one `patch.parse` per row. The off-stack
    enumeration keeps its files; `_offstack` below is that shape."""
    return pushlog.collect(data, file_filter or utils.is_interesting_file,
                           channel="beta", drop_merge_files=True)


def _offstack(data, file_filter=None):
    """`collect` as `pushlog.pushlog_for_revs` calls it: the off-stack window enumeration, which
    ranks with the file lists (`orchestrator._looks_pref_flip` reads them), writes no rows and
    pays for no parses -- so it KEEPS the files while still recording `via_merge`."""
    return pushlog.collect(data, file_filter or (lambda f: True),
                           channel="beta", drop_merge_files=False)


class TestCollectOnAMergePush(unittest.TestCase):
    """What `collect` hands `Changeset.add`: every member, with no files, tagged `via_merge`.

    No database needed -- this is the half of item 30 that decides whether ~2,000 serial
    `patch.parse` jobs get queued."""

    def test_a_merge_push_yields_every_member_with_no_files(self):
        data = _beta_merge_push(501)
        got = _ingest(data)
        self.assertEqual(len(got), 501)                       # nothing dropped
        self.assertEqual([c for c in got if c["files"]], [])  # nothing extractable
        self.assertTrue(all(c["via_merge"] for c in got))
        # One pushdate for the whole cycle -- the fact that makes "in this build's pushlog
        # window" stop being recency evidence (see TestAMergeMemberIsNotARegressor).
        self.assertEqual({c["date"].isoformat() for c in got},
                         {"2026-08-13T14:15:59+00:00"})
        # `merge` stays what it always was: the flag of the merge CHANGESET, not of the
        # push. Only 20 of push 27990's 5,130 changesets are merge-flagged, so `Node.merge`
        # excludes 20 and `via_merge` is what covers the other 5,110.
        self.assertEqual(sum(1 for c in got if c["merge"]), 1)

    def test_the_merge_day_build_revision_is_still_emitted(self):
        # First half of the trap. `Build.put_data` needs a `nodes` row for this exact
        # revision or the merge-day `builds` row is never written.
        got = _ingest(_beta_merge_push(501))
        self.assertIn(MERGE_DAY_REV, {c["node"] for c in got})

    def test_an_ordinary_push_keeps_its_files_and_is_not_via_merge(self):
        got = _ingest(_ordinary_beta_push())
        self.assertEqual(len(got), 6)
        self.assertFalse(any(c["via_merge"] for c in got))
        # The interesting-extensions filter still does its normal job: the .cpp survives,
        # the l10n .json does not.
        self.assertEqual([c["files"] for c in got],
                         [["dom/base/Element{}.cpp".format(i)] for i in range(6)])

    def test_a_pref_flip_inside_a_merge_push_is_still_recognised(self):
        """THE DEFECT THIS TEST WAS WRITTEN AGAINST IS FIXED, and this is now the regression
        test for the fix. It was reported as: emptying `files` also blinds the off-stack
        pref-flip detector.

        `collect`'s `file_filter` serves two unrelated consumers. On the INGESTION path it
        decides which `changesets` rows exist, which is what item 30 means to suppress -- one
        `patch.parse` is owed per row, 1,932-2,678 of them per beta merge. Off-stack it is the
        candidate's touched-file list, and `orchestrator._looks_pref_flip` keys on it
        (`_PREF_FILE_HINTS`: staticpreflist, modules/libpref/init/, browser/app/profile/,
        /nimbus/ ...) -- the canonical off-stack regressor is a pref FLIP with no stack-file
        touch (bug 2056116). A flip whose description does not say "by default" would then be
        invisible: it would lose its ranking key AND the `[feature-flip: ...]` line the prompt
        prints for it (`triage._candidate_lines`).

        The fix separated the two: `collect(..., drop_merge_files=)` is the ingestion
        optimisation and is passed only by `pushlog.pushlog`, while `via_merge` -- the FACT that
        item 23 reads to refuse the `regression` keyword -- is recorded either way. So the
        off-stack window keeps its file lists (`_offstack` above) and the ingestion path still
        owes no parses (`_ingest`)."""
        flip = {
            "node": "0dd1cede4d01" + "0" * 28,
            "desc": "Bug 2056116 - Update StaticPrefList r=someone",
            "author": "Dev <dev@example.com>",
            "files": ["modules/libpref/init/StaticPrefList.yaml"],
            "parents": ["e" * 40],
        }
        data = _beta_merge_push(4)
        data["pushes"]["27990"]["changesets"].append(flip)
        got = _offstack(data)
        c = next(x for x in got if x["node"].startswith("0dd1ce"))
        self.assertTrue(orch._looks_pref_flip(c["desc"], c["files"]))


class TestTheRuleOnTrunk(unittest.TestCase):
    """THE DENOMINATOR ITEM 30 WAS NEVER MEASURED AGAINST, and it does not agree.

    `collect` is channel-blind and `update.put_filelog(channel)` runs it for every ingested
    channel on every 20-minute tick. Item 30's "exactly 5 of 2,356 pushes ... so nothing
    else is touched" is a mozilla-BETA count. On mozilla-central, sheriffs land autoland in
    MERGE pushes several times a day.

    Measured live off hg.mozilla.org on 2026-08-25 (`json-pushes?full=1`, one request per
    window, allowlisted UA):

      * 2026-07-26..08-23, 28 days: 26 of 191 pushes (13.6%) contain a merge changeset, and
        those 26 carry 3,308 of 6,019 changesets (55.0%) and **1,193 of 2,186
        candidate-bearing ones (54.6%)**. (6,019 in 28 days cross-checks plan #18's own
        "mozilla-central pushed 6,209 changesets in 30 days".)
      * 2026-08-18..08-25, 7 days: 11 of 53 pushes, 411 of 558 candidate-bearing (73.7%).
        Merge descriptions: "Merge mozilla-central to autoland" x8, "Merge autoland to
        mozilla-central" x2, "Merge firefox-autoland to firefox-main" x1.
      * 2026-08-24, one day: 3 of 14 pushes, 139 of 150 changesets (92.7%).

    So on the one channel production actually ingests (`INGEST_CHANNELS=nightly`) this rule
    deletes over half of the on-stack candidate supply: no `changesets` rows, no
    `patch.parse`, nothing for `Changeset.find`/`get_scores` to return, and every affected
    crash falls through to the off-stack path -- which is LIVE and action-emitting in prod
    (`OFFSTACK_ENABLED=1`, `OFFSTACK_OBSERVE_ONLY=0`). Nothing else in the suite reads a
    trunk push through `collect`, which is why this landed green."""

    def test_a_nightly_autoland_merge_push_keeps_its_files(self):
        push = TRUNK_MERGE_PUSH["pushes"]["45154"]
        self.assertTrue(any(len(c["parents"]) > 1 for c in push["changesets"]))
        got = pushlog.collect(TRUNK_MERGE_PUSH, utils.is_interesting_file)
        scorable = {c["node"]: c["files"] for c in got if c["files"]}
        self.assertEqual(
            scorable,
            {
                "4ff0ae7f291c": ["gfx/layers/composite/TextureHost.cpp",
                                 "gfx/layers/opengl/TextureHostOGL.cpp"],
                "94678b10b000": ["gfx/ipc/GPUParent.cpp", "gfx/ipc/GPUParent.h",
                                 "gfx/ipc/GPUProcessManager.cpp"],
                "b02f2c45258a": ["js/src/jit/WarpCacheIRTranspiler.cpp"],
            },
            "an autoland<->central merge push is a NORMAL day on trunk (26 of 191 pushes "
            "over 28 days, carrying 54.6% of all candidate-bearing changesets): its members "
            "are ordinary landings and must still get `changesets` rows, or the on-stack "
            "path goes dark on the one channel production ingests",
        )

    def test_the_two_populations_are_told_apart_by_something(self):
        """Whatever discriminator lands, it has to separate these two pushes -- both contain
        a `parents > 1` changeset.

        The observable differences, from the live reads: the cycle merge is ONE push of
        5,130-6,952 changesets by `ffxbld@lando.moz.tools`, arriving on a release branch;
        the trunk merge is 18-52 changesets by a sheriff account. Asserted as a property of
        the fixtures rather than a proposed rule, so this test does not pre-commit the fix
        to a threshold nobody has measured."""
        trunk = TRUNK_MERGE_PUSH["pushes"]["45154"]
        cycle = _beta_merge_push(501)["pushes"]["27990"]
        self.assertTrue(pushlog.is_merge_push(trunk))
        self.assertTrue(pushlog.is_merge_push(cycle))
        self.assertNotEqual(trunk["user"], cycle["user"])
        self.assertLess(len(trunk["changesets"]), len(cycle["changesets"]))


@unittest.skipUnless(_is_postgres(), "the row half needs a DISPOSABLE Postgres backend")
class TestMergePushRows(unittest.TestCase):
    """`collect` -> `Changeset.add` -> `Build.put_data`, on a throwaway Postgres DB.

    Postgres-gated because everything on this path is Postgres-only: `HGAuthor.get_id` and
    `Build.put_data` are `pg.insert(...).on_conflict...`, and `changesets` carries
    `pg.ARRAY` columns."""

    CHANNEL = "beta"
    PRODUCT = "Firefox"

    def setUp(self):
        models.create()
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        # Scoped to this file's fixture hashes -- never a bare `delete()` on `nodes`, so the
        # module can share a database with the rest of the suite. `builds.nodeid` and
        # `changesets.nodeid` are both ON DELETE CASCADE, so the nodes delete takes the rows
        # that hang off them with it.
        q = db.session.query(models.Node).filter(
            db.or_(
                models.Node.node.like("be7a%"),
                models.Node.node.like("0ff1ce%"),
                models.Node.node.like("0dd1ce%"),
                models.Node.node.in_([MERGE_DAY_REV, MERGE_MEMBER]),
            )
        )
        q.delete(synchronize_session=False)
        db.session.query(models.Build).filter(
            models.Build.product == self.PRODUCT,
            models.Build.channel == self.CHANNEL,
            models.Build.buildid == utils.get_build_date(MERGE_BUILDID),
        ).delete(synchronize_session=False)
        db.session.commit()

    def _nodes(self, hashes):
        return (
            db.session.query(models.Node)
            .filter(models.Node.channel == self.CHANNEL, models.Node.node.in_(hashes))
            .count()
        )

    def _changesets(self, hashes):
        return (
            db.session.query(models.Changeset)
            .join(models.Node)
            .filter(models.Node.channel == self.CHANNEL, models.Node.node.in_(hashes))
            .count()
        )

    def _put_merge_day_build(self):
        bid = utils.get_build_date(MERGE_BUILDID)
        models.Build.put_data({self.PRODUCT: {self.CHANNEL: {
            bid: {"revision": MERGE_DAY_REV, "version": "155.0b1"}}}})
        return models.Build.get_id(bid, self.CHANNEL, self.PRODUCT)

    def test_a_merge_push_creates_nodes_but_no_changesets(self):
        got = _ingest(_beta_merge_push(501))
        hashes = [c["node"] for c in got]
        self.assertEqual(len(hashes), 501)

        models.Changeset.add(got, got[0]["date"], self.CHANNEL)

        # Every member is in `nodes` -- that is what keeps the three windows where they are.
        self.assertEqual(self._nodes(hashes), 501)
        # ...and not one `changesets` row, so `analyze_patches` owes zero `patch.parse`
        # jobs. This is the 3.3-4.5 hours of shared queue per merge that item 30 buys back.
        self.assertEqual(self._changesets(hashes), 0)
        self.assertEqual(
            models.Changeset.to_analyze(chgsets=hashes, channel=self.CHANNEL), [])

        # THE TRAP: the merge-day `builds` row still gets written, because its revision has
        # a `nodes` row. `put_data`'s guard is `if rev in revs_c` and nothing else.
        self.assertIsNotNone(self._put_merge_day_build())

    def test_no_merge_member_can_be_an_on_stack_candidate(self):
        """The reason dropping the FILES is enough, with no second gate anywhere.

        Only the merge changeset itself is `Node.merge`-flagged (1 of 501 here, 20 of push
        27990's 5,130), so `Changeset.find`'s `Node.merge.is_(False)` filter does NOT hold
        the other 500 out. What holds them out is that `find` joins `changesets`, and there
        are no rows to join."""
        got = _ingest(_beta_merge_push(501))
        models.Changeset.add(got, got[0]["date"], self.CHANNEL)
        pushdate = got[0]["date"]
        found = models.Changeset.find(
            [SCORABLE], pushdate, pushdate, self.CHANNEL)
        self.assertEqual(found, {})
        # And the same node hash on the other channel is untouched by this: `find` and
        # `get_scores` are both channel-filtered, which is what stops a beta merge row from
        # answering for a nightly crash (1,932 of 1,932 merge candidates share their hash
        # with mozilla-central).
        self.assertEqual(models.Changeset.find([SCORABLE], pushdate, pushdate, "nightly"),
                         {})

    def test_an_ordinary_push_is_untouched(self):
        got = _ingest(_ordinary_beta_push())
        hashes = [c["node"] for c in got]
        self.assertEqual(len(hashes), 6)
        self.assertFalse(any(c["via_merge"] for c in got))

        models.Changeset.add(got, got[0]["date"], self.CHANNEL)

        self.assertEqual(self._nodes(hashes), 6)
        # One `changesets` row per interesting file: the l10n json is filtered out, the
        # .cpp is not. Median 1 interesting file per in-cycle beta changeset.
        self.assertEqual(self._changesets(hashes), 6)
        # ...and all six are owed to the patch-parsing chain, unlike the merge's 501.
        self.assertEqual(
            len(models.Changeset.to_analyze(chgsets=hashes, channel=self.CHANNEL)), 6)

    def test_dropping_the_merge_push_would_delete_the_merge_day_builds_row(self):
        """WHY item 30 is "emit the members with no files" and not "drop the push".

        Same `Build.put_data` call as the first test, with the push never ingested. The row
        that bounds the on-stack `mindate` (`get_pushdate_before`) and the off-stack window
        (`get_two_last`) simply does not appear -- silently, because `put_data` has no
        else-branch and logs nothing."""
        self.assertEqual(self._nodes([MERGE_DAY_REV]), 0)
        self.assertIsNone(self._put_merge_day_build())
        # ...and it is not a permanent loss of the build: ingest the push and the row
        # appears, which is exactly what the shipped behaviour does.
        got = _ingest(_beta_merge_push(4))
        models.Changeset.add(got, got[0]["date"], self.CHANNEL)
        self.assertIsNotNone(self._put_merge_day_build())


def _sf():
    return SearchfoxCitation(
        permalink="https://searchfox.org/mozilla-beta/rev/x#1", symbol_id="_Z1",
        repo="mozilla-beta")


def _lead(node):
    return Dossier(
        candidate=Candidate(node=node, bug=2049938),
        verdict=Verdict(decision=Decision.lead, confidence=Confidence.probable,
                        needinfo_draft="could you take a look?",
                        mechanism=Claim(text="UAF of mFoo", citations=[_sf()])))


_STACK = {"frames": [{"stackpos": 0, "function": "Foo::bar", "filename": "dom/Foo.cpp",
                      "line": 51, "module": "xul.dll"}]}


def _preview(node, corroborations, channel="beta"):
    """`report_bug.build_bug_preview` with every network read mocked -- the same mock shape
    as `tests/test_product_wiring.py`'s preview tests."""
    ui = {"uuid": "u-1", "signature": "Foo::bar", "channel": channel,
          "buildid": MERGE_BUILDID}
    dossier = {
        "candidate": {"node": node, "bug": 2049938, "author": "Dev <dev@x.com>"},
        "corroborations": corroborations,
        "verdict": {"confidence": "high", "mechanism": {"statement": "UAF of mFoo"}},
    }
    with mock.patch.object(report_bug, "resolve_product_component",
                           return_value=("Core", "DOM: Content Processes")), \
            mock.patch.object(report_bug, "fetch_crash_reason",
                              return_value={"reason": "SIGSEGV"}), \
            mock.patch.object(report_bug, "fetch_signature_stats",
                              return_value=(True, {"count": 3, "installs": 2})), \
            mock.patch("crashclouseau.models.UUID.get_info",
                       return_value={"version": "155.0b1"}), \
            mock.patch("crashclouseau.models.Node.authors_for",
                       return_value={node: {"nick": "hgnick", "real": "Dev",
                                            "email": "dev@x.com"}}), \
            mock.patch.object(report_bug, "_bugzilla_user",
                              return_value={"exists": True, "nick": "bznick"}):
        return report_bug.build_bug_preview(ui, _STACK, dossier)


class TestAMergeMemberIsNotARegressor(unittest.TestCase):
    """Item 23: `candidate_in_pushlog_window` is TRUE for a merge member and means nothing.

    THE NUMBER BEHIND IT. The window for the beta build after the 2026-08-13 merge
    (20260812080401 -> 20260817142839) is 5,192 changesets, against 45 / 76 / 61 for the
    three builds after it -- and mozilla-central pushes ~6,200 in a whole MONTH. So on a
    merge-spanning window "in this build's pushlog window" degenerates from "landed in the
    last 1-3 days" to "landed on trunk some time in the last month", and it can no longer
    carry the `regression` keyword release management triages on, the `regressed_by` field,
    or the words "Suspected regressor" -- the three claims bug 2062119 was filed for
    breaching with a changeset from 2022.

    A merge member reaches a verdict only through the OFF-STACK path: the merge push gets no
    `changesets` rows (above), so nothing can score onto a frame. `_offstack_candidates`
    drops the merge changeset itself (`Node.merge`) but keeps its 5,110 non-flagged members,
    which is precisely why `via_merge` has to be carried."""

    def test_a_merge_member_cannot_assert_a_regression(self):
        d = _lead(MERGE_MEMBER)
        seed = {"candidates": [
            {"node": MERGE_MEMBER, "via_merge": True, "pushdate": [MERGE_TS, 0]},
            {"node": "be7a00000001", "via_merge": True, "pushdate": [MERGE_TS, 0]},
        ], "candidate_pushdates": {MERGE_MEMBER: [MERGE_TS, 0]}}
        orch._record_window_membership(d, seed)

        # Both facts recorded: it WAS in the window, and the window was a merge.
        self.assertIs(d.corroborations["candidate_in_pushlog_window"], True)
        self.assertIs(d.corroborations["candidate_arrived_by_merge"], True)
        # False, not None: the run established where the candidate came from, so this is a
        # negative answer rather than a silence (which every caller also reads as "no").
        self.assertIs(report_bug.is_suspected_regression(d.corroborations), False)

        prev = _preview(MERGE_MEMBER, d.corroborations)
        self.assertEqual(prev["keywords"], ["crash"])
        self.assertEqual(prev["regressed_by"], [])
        self.assertNotIn("Suspected regressor", prev["comment"])
        # The candidate is still NAMED -- the guard removes the structured claim, not the
        # lead. A merge member can perfectly well be the cause.
        self.assertIn(MERGE_MEMBER, prev["comment"])
        # ...and nothing else about the filing moved.
        self.assertEqual(prev["blocked"], ["clouseau"])
        self.assertEqual(prev["needinfo_email"], "dev@x.com")

    def test_an_ordinary_uplift_window_candidate_still_asserts_the_regression(self):
        """The control, and the reason this is a merge guard and not a beta guard.

        The in-cycle beta window is 45-122 changesets (median 61, 21 candidate-bearing)
        against nightly's ~672, and 93.2% of its candidate-bearing changesets are approved
        bug-fix uplifts (`a=<release manager>`) or backouts. That is a STRONGER regression
        claim than anything nightly produces, so it keeps all three structured claims."""
        uplift = "0ff1ce000001"
        d = _lead(uplift)
        orch._record_window_membership(d, {"candidates": [
            {"node": uplift, "via_merge": False, "pushdate": [1787000000, 0]},
            {"node": "0ff1ce000002", "via_merge": False},
        ]})
        self.assertIs(d.corroborations["candidate_in_pushlog_window"], True)
        self.assertNotIn("candidate_arrived_by_merge", d.corroborations)
        self.assertIs(report_bug.is_suspected_regression(d.corroborations), True)

        prev = _preview(uplift, d.corroborations)
        self.assertEqual(prev["keywords"], ["crash", "regression"])
        self.assertEqual(prev["regressed_by"], [2049938])
        self.assertIn("Suspected regressor", prev["comment"])

    def test_an_out_of_window_candidate_is_not_labelled_a_merge_member(self):
        # A candidate the agent found by blame is outside the seeded set entirely. It must
        # read as plain out-of-window: the merge flag is an explanation of a TRUE
        # `candidate_in_pushlog_window`, and writing it here would make the corroboration
        # uncountable (it is the flag we want to measure the beta merge population with).
        d = _lead("dead00000000")
        orch._record_window_membership(d, {"candidates": [
            {"node": MERGE_MEMBER, "via_merge": True, "pushdate": [MERGE_TS, 0]}]})
        self.assertIs(d.corroborations["candidate_in_pushlog_window"], False)
        self.assertNotIn("candidate_arrived_by_merge", d.corroborations)
        self.assertIs(report_bug.is_suspected_regression(d.corroborations), False)

    def test_a_merge_member_is_still_ranked_and_still_gets_a_needinfo(self):
        # The guard is deliberately narrow. It moves no rung (the verdict is unchanged) and
        # it does not gate the needinfo or the `blocked` list, both of which are
        # unconditional -- so a real merge-window regressor still reaches a human.
        d = _lead(MERGE_MEMBER)
        orch._record_window_membership(d, {"candidates": [
            {"node": MERGE_MEMBER, "via_merge": True, "pushdate": [MERGE_TS, 0]}]})
        self.assertEqual(d.verdict.decision, Decision.lead)
        self.assertEqual(d.verdict.confidence, Confidence.probable)
        prev = _preview(MERGE_MEMBER, d.corroborations)
        self.assertEqual(prev["needinfo"], ":bznick, can you have a look please?")

    def test_the_starting_point_prose_does_not_deny_the_window(self):
        """FIXED. The filed bug used to state something false about a merge member.

        `is_suspected_regression` returning False drops the candidate onto
        `build_analysis_comment`'s existing "Starting point" branch, whose prose is written
        for the OTHER reason it can be False -- an out-of-window candidate. It says "This
        changeset did not land in this build's pushlog window", and for a merge member that
        is exactly backwards: `candidate_in_pushlog_window` is TRUE, the changeset is in the
        window, and the window is why it was seeded at all. A reviewer can falsify the
        sentence from the pushlog link in the same comment, on a bug whose whole purpose is
        to invite that correction. (The same paragraph also calls it "the closest thing
        found on the crash path", which no off-stack candidate ever was.) Item 23 said to
        drop these onto the existing prose; the prose got a merge variant instead, which says
        the window IS the reason it was seeded and that one push carried a whole cycle."""
        d = _lead(MERGE_MEMBER)
        orch._record_window_membership(d, {"candidates": [
            {"node": MERGE_MEMBER, "via_merge": True, "pushdate": [MERGE_TS, 0]}]})
        comment = _preview(MERGE_MEMBER, d.corroborations)["comment"]
        self.assertNotIn("did not land in this build's pushlog window", comment)
        # ...and it says the true thing instead, rather than merely omitting the false one.
        self.assertIn("reached this branch with the cycle merge", comment)
        self.assertIn("not evidence", comment)


if __name__ == "__main__":
    unittest.main()
