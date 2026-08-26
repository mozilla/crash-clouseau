# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# THE ONE-SECOND ACCIDENT AT THE CYCLE MERGE -- and the single `builds` row that is holding
# it in place. Plan #18 §1.3 and work item 9; this file is assertions only, no production
# change is being pinned here other than "do not touch these four rows".
#
# THREE windows read the `builds` table and the merge-day build's row bounds TWO of them:
#   * `Build.get_last_versions(n=3)`  -> the spike SELECTION window (`datacollector.get_builds`)
#   * `Build.get_pushdate_before`     -> the ON-STACK candidate `mindate` (`update.put_report`)
#   * `Build.get_two_last`            -> the OFF-STACK pushlog window
#                                       (`orchestrator._offstack_candidates`)
#
# A central->beta merge arrives as ONE push carrying a whole development cycle at ONE
# pushdate. Measured on push 27990 (re-read from json-pushes on 2026-08-25 for this file):
# `2026-08-13 14:15:59`, pushuser ffxbld@lando.moz.tools, **5,130 changesets of which only 20
# are merge-flagged** -- so 5,110 of them pass `Changeset.find`'s `Node.merge.is_(False)` and
# 1,937 touch a file `utils.is_interesting_file` accepts (plan #18 counts 1,932
# candidate-bearing; same object, same order of magnitude). And the merge-day build's own
# revision `22761955d964` ("Update configs after merge day operations") is a MEMBER of that
# push, so its `nodes.pushdate` IS 14:15:59 -- which is also, literally, the buildid
# `20260813141559`. That push-timestamp == buildid identity held 4 of 4 measured cycles.
#
# Consequence: `get_pushdate_before(the shipped 155.0b1)` returns 14:15:59, `put_report` adds
# ONE SECOND, and all ~5,100 merged changesets land exactly one second below the window. A
# beta crash's candidate set is therefore the cycle's uplifts -- 47 changesets for
# `22761955d964 -> 645eb5721e80`, re-measured live, matching plan #18 item 9's "155.0b1 47"
# -- and not its ~5,100 merged ones. THE EXCLUSION IS AN ACCIDENT AND NOTHING IN THE TREE
# RECORDED IT. Delete that one `builds` row and both windows silently widen by two orders of
# magnitude (plan #18: 5,192 changesets for `20260812080401 -> 20260817142839`, the pair
# `get_two_last` then returns).
#
# Which is why the row can never be dropped at ingestion: `Build.put_data` inserts
# `if rev in revs_c` (`models.py`), i.e. only when a `nodes` row for that revision already
# exists, and `builds.nodeid` is ON DELETE CASCADE -- so dropping the merge push, or pruning
# its nodes, deletes the row that is doing the bounding. Hence `pushlog.collect` emits merge
# members with `files: []` rather than dropping the push. `test_removing_the_merge_day_build_
# widens_both_windows` is that argument, executed.
#
# Every buildid, revision and pushdate below was read from Buildhub
# (`target.channel=beta` + `source.product=firefox`) and hg.mozilla.org on 2026-08-25.
#
# Postgres-gated: `changesets` uses pg.ARRAY and `Build.put_data` uses
# `pg.insert(...).on_conflict_do_nothing()`, so on sqlite this whole file is a silent skip.
#   docker exec clouseau_test_pg psql -U clouseau -d clouseau_test -c 'CREATE DATABASE betawin'
#   DATABASE_URL=postgresql://clouseau:passwd@localhost:55432/betawin \
#     REDIS_URL=redis://localhost:6379/0 python -m unittest tests.test_beta_windows
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from dateutil.relativedelta import relativedelta  # noqa: E402

from crashclouseau import (  # noqa: E402
    config,
    datacollector as dc,
    db,
    models,
    update,
    utils,
)
from crashclouseau.agent import orchestrator  # noqa: E402


def _is_postgres():
    try:
        return db.engine.dialect.name == "postgresql"
    except Exception:
        return False


_UTC = timezone.utc

# (buildid, revision, version) -- Buildhub, beta/Firefox, 2026-08-05..08-22. Note the TWO
# builds tagged 155.0b1: the merge-day one (1 report, 1 install lifetime) and the one that
# actually shipped four days later.
_B9 = ("20260810090305", "a2581c04bbce", "154.0b9")
_B10 = ("20260812080401", "1d78fecadb05", "154.0b10")
_MERGE_DAY = ("20260813141559", "22761955d964", "155.0b1")
_SHIPPED = ("20260817142839", "645eb5721e80", "155.0b1")
_ALL_BUILDS = (_B9, _B10, _MERGE_DAY, _SHIPPED)

# The merge push. Its pushdate IS the merge-day buildid, which is the whole accident.
_MERGE_PUSHDATE = datetime(2026, 8, 13, 14, 15, 59, tzinfo=_UTC)

_BUILDHUB = {
    "Firefox": {
        "beta": {
            utils.get_build_date(bid): {"revision": rev, "version": ver}
            for bid, rev, ver in _ALL_BUILDS
        }
    }
}

# Two file names taken from real crash-stack frames, each touched BOTH inside the merge push
# and by a post-merge uplift -- so "which candidates come back" is decided by the date
# boundary alone and by nothing else.
_BROWSER_PARENT = "dom/ipc/BrowserParent.cpp"
_IM_CONTEXT = "widget/gtk/IMContextWrapper.cpp"
_CRASH_FILES = [_BROWSER_PARENT, _IM_CONTEXT]


def _chgset(node, pushdate, files=(), bug=-1, merge=False, desc=""):
    """One `pushlog.collect` output row, the shape `models.Changeset.add` consumes."""
    return {
        "node": node,
        "date": pushdate,
        "backedout": False,
        "files": list(files),
        "merge": merge,
        "bug": bug,
        "author": [],
        "desc": desc,
    }


# --- the four build revisions. Each one's pushdate equals its own buildid (verified for all
# four via json-rev); none of them touches a file we score, which is real -- three are l10n /
# config bumps and the fourth is a devtools fix in .js files.
# A MEMBER of push 27990, sharing its pushdate with the 5,109 others. This one row is the
# whole subject of this file.
_MERGE_DAY_BUILD_NODE = _chgset(
    "22761955d964", _MERGE_PUSHDATE,
    desc="No Bug - Update configs after merge day operations a=release",
)
_BUILD_NODES = [
    _chgset("a2581c04bbce", utils.get_build_date(_B9[0]),
            desc="No Bug - Bumping Mobile l10n changesets r=release a=l10n-bump"),
    _chgset("1d78fecadb05", utils.get_build_date(_B10[0]),
            desc="No Bug - Bumping Mobile l10n changesets r=release a=l10n-bump"),
    _MERGE_DAY_BUILD_NODE,
    _chgset("645eb5721e80", utils.get_build_date(_SHIPPED[0]), bug=2062153,
            desc="Bug 2062153 - [devtools] Fix RDM Device Settings modal. a=RyanVM"),
]

# --- members of the merge push, WITH their real file lists. Deliberately not what
# `pushlog.collect` emits today (item 30 gives merge members `files: []`, so they get no
# `changesets` rows at all): giving them their files back is what `Changeset.add` stored
# before that item and what it would store again if it were ever reverted or refactored
# away. The point of this fixture is that the DATE boundary excludes them on its own, which
# is the mechanism nobody wrote down. Two independent mechanisms, pinned one at a time.
_MERGE_MEMBERS = [
    _chgset("0e9094edbbdf", _MERGE_PUSHDATE, files=[_BROWSER_PARENT], bug=2062204,
            desc="Bug 2062204 - Guard browser-parent count against overflow"),
    _chgset("91f8065a47c7", _MERGE_PUSHDATE,
            files=[_BROWSER_PARENT, "widget/nsBaseDragService.cpp"], bug=2054665,
            desc="Bug 2054665 - r=edgar"),
    _chgset("a6f80307f127", _MERGE_PUSHDATE, files=[_IM_CONTEXT], bug=2051354,
            desc="Bug 2051354 - part 1: Update `IsIBusInSyncMode()` r=m_kato"),
    # The merge changeset itself (parents > 1), one of the 20 in the push.
    _chgset("874fc106363e", _MERGE_PUSHDATE, merge=True, desc="Promote main to beta"),
]

# --- the uplifts: the cycle's real candidate window. `fc5f8f714b14` landed TWENTY-SEVEN
# SECONDS after the merge push, which is the tightest available demonstration that the
# boundary is not a day or an hour -- it is one second.
_UPLIFTS = [
    _chgset("fc5f8f714b14", datetime(2026, 8, 13, 14, 16, 26, tzinfo=_UTC),
            desc="No bug - Tagging 607a3fc85294 with FIREFOX_155_0b1_BUILD1 a=release"),
    _chgset("bb4e5396abea", datetime(2026, 8, 17, 14, 26, 1, tzinfo=_UTC),
            files=[_BROWSER_PARENT, "dom/ipc/BrowserParent.h"], bug=2060153,
            desc="Bug 2060153 - Add the guard to `BrowserParent::RecvSynthesizeNativeKey`"),
    _chgset("1129fc997b6d", datetime(2026, 8, 17, 14, 27, 39, tzinfo=_UTC),
            files=[_IM_CONTEXT, "widget/gtk/nsGtkKeyUtils.cpp"], bug=2060599,
            desc="Bug 2060599 - Make `IMContextWrapper::OnKeyEvent` forget the key"),
]

_NODES = _BUILD_NODES + _MERGE_MEMBERS + _UPLIFTS


@unittest.skipUnless(_is_postgres(), "the three windows are DB queries; need a disposable Postgres")
class TestTheThreeWindowsAtTheMerge(unittest.TestCase):
    """Requires DATABASE_URL to point at a THROWAWAY Postgres DB -- writes+deletes rows."""

    def setUp(self):
        models.db.create_all()
        # Written through the production writers on purpose: `Changeset.add` is what
        # ingestion calls with `pushlog.collect`'s output, and `Build.put_data` is what
        # carries the `if rev in revs_c` guard this file is about.
        models.Changeset.add(_NODES, utils.get_build_date(_SHIPPED[0]), "beta")
        models.Build.put_data(_BUILDHUB)

    def tearDown(self):
        # `builds.nodeid` and `changesets.nodeid` are both ON DELETE CASCADE, so the nodes
        # are the only thing to delete -- which is also the hazard this file documents.
        db.session.query(models.Node).filter(
            models.Node.channel == "beta",
            models.Node.node.in_([c["node"] for c in _NODES]),
        ).delete(synchronize_session=False)
        db.session.commit()

    # ------------------------------------------------------------------ helpers

    def _mindate_from_put_report(self, buildid):
        """The `mindate` `update.put_report` really computes for a crash on `buildid`, plus
        whatever `Changeset.find` answers for it -- obtained by letting `put_report` run and
        standing in for `inspector.get_crash`, which is the only thing between the mindate
        and the network. `inspector` calls `filelog(files, mindate, buildid, channel)`; a
        `None` return means "no json_dump", which makes `put_report` return immediately."""
        seen = {}

        def fake_get_crash(uuid, bid, channel, mindate, chgset, filelog, interesting):
            seen["mindate"] = mindate
            seen["maxdate"] = bid
            seen["channel"] = channel
            seen["found"] = filelog(_CRASH_FILES, mindate, bid, channel)
            return None

        with mock.patch.object(update.inspector, "get_crash", side_effect=fake_get_crash):
            update.put_report("d0e5b3e0-0000-0000-0000-000000000001",
                              utils.get_build_date(buildid), "beta", "Firefox",
                              _MERGE_DAY[1])
        return seen

    # ------------------------------------------------------------------ item 7

    def test_selection_window_is_non_empty_after_the_merge(self):
        """The day after the merge the three newest rows are 155.0b1 / 154.0b10 / 154.0b9,
        and the window has to survive that. `get_last_versions` applied `.limit(n)` in SQL
        BEFORE a Python-side `major != get_major(...)`: the break fired on row two,
        `len(res) >= 2` failed, and `get_builds` returned `bids=[], search_date=""` -- so
        `get_new_signatures` wrote no Stats, no uuids, and (because `record_many([])` returns
        early) not even a `Selection` row saying why. Measured 12 of 127 beta run-days (9.4%)
        over 5 merges, 2/2/2/2/4 days each. Mixing majors is the QUESTION, not a defect: the
        builds before a cycle's first beta are the previous cycle's last betas."""
        window = models.Build.get_last_versions(
            datetime(2026, 8, 14, 12, 0, 0, tzinfo=_UTC), "beta", "Firefox", n=3
        )
        self.assertEqual([w["version"] for w in window],
                         ["155.0b1", "154.0b10", "154.0b9"])
        self.assertEqual([w["buildid"] for w in window],
                         [_MERGE_DAY[0], _B10[0], _B9[0]])
        # Two majors in one window is the shape the removed break forbade.
        self.assertEqual({w["version"].split(".")[0] for w in window}, {"154", "155"})

        # And the consumer: three buildids to facet on, and a Socorro date filter that
        # reaches back to the OLDEST of them (otherwise its crashes are invisible whatever
        # the buildid facet says).
        bids, search_date = dc.get_builds("Firefox", "beta",
                                          datetime(2026, 8, 14, 12, 0, 0, tzinfo=_UTC))
        self.assertEqual(bids, [_MERGE_DAY[0], _B10[0], _B9[0]])
        self.assertTrue(search_date.startswith(">=2026-08-10"), search_date)

    # ------------------------------------------------------------------ item 9

    def test_on_stack_mindate_excludes_the_merge_push(self):
        """One second is the whole margin. `mindate` = merge pushdate + 1s = 14:16:00, so
        every changeset of push 27990 is below the window and the uplifts are above it --
        including `fc5f8f714b14`, 27 seconds above.

        Asserted through `update.put_report` and the real `Changeset.find` rather than by
        recomputing the arithmetic here, because the arithmetic is what is under test."""
        seen = self._mindate_from_put_report(_SHIPPED[0])

        self.assertEqual(seen["mindate"], datetime(2026, 8, 13, 14, 16, 0, tzinfo=_UTC))
        self.assertEqual(seen["mindate"], _MERGE_PUSHDATE + relativedelta(seconds=1))
        # ... and the lower bound came from the merge-day BUILD, whose buildid is the merge
        # pushdate spelled as a string. This is the identity that makes the accident.
        self.assertEqual(
            models.Build.get_pushdate_before(
                utils.get_build_date(_SHIPPED[0]), "beta", "Firefox"
            ),
            _MERGE_PUSHDATE,
        )
        self.assertEqual(utils.get_buildid(_MERGE_PUSHDATE), _MERGE_DAY[0])

        # Every merge-push node is BELOW the boundary; every uplift is above it.
        pushdates = dict(
            db.session.query(models.Node.node, models.Node.pushdate)
            .filter(models.Node.channel == "beta")
            .all()
        )
        for c in _MERGE_MEMBERS + [_MERGE_DAY_BUILD_NODE]:
            self.assertLess(pushdates[c["node"]], seen["mindate"], c["node"])
        for c in _UPLIFTS:
            self.assertGreaterEqual(pushdates[c["node"]], seen["mindate"], c["node"])

        # The behaviour that follows: the candidate set is the uplifts. Both crash files
        # were touched inside the merge push AND by an uplift, so this is the boundary
        # talking and not the fixture.
        self.assertEqual(
            seen["found"],
            {_BROWSER_PARENT: ["bb4e5396abea"], _IM_CONTEXT: ["1129fc997b6d"]},
        )
        self.assertEqual(seen["maxdate"], utils.get_build_date(_SHIPPED[0]))
        # `Changeset.find` filters `Node.channel`, so the window is only right if the crash's
        # own channel is what reaches it -- nightly's `mindate` is arithmetic and would not
        # have produced any of the above.
        self.assertEqual(seen["channel"], "beta")

    def test_a_crash_on_the_merge_day_build_itself_sees_the_whole_merge(self):
        """The accident protects the SHIPPED builds, and only those. For a crash on the
        merge-day build the predecessor row is 154.0b10, so `mindate` is 08-12 08:04:02 and
        the merge push -- which arrives at 14:15:59 on 08-13, i.e. at exactly this build's
        `maxdate`, and the upper bound is inclusive -- is inside the window. Plan #18 §1.3
        counts 5,144 changesets there against 47 for the shipped b1.

        Near-unreachable rather than unreachable: that build shipped to 1-7 installations
        against an install threshold of 6, so the selector almost never picks it -- but any
        report ingested from it is scored, and this is the one path on which a candidate can
        be "in this build's pushlog window" while being a month old. It is what item 23's
        `candidate_arrived_by_merge` guard exists for; pinned here so the guard has a
        measured reach and this window is never mistaken for the uplift window."""
        seen = self._mindate_from_put_report(_MERGE_DAY[0])
        self.assertEqual(seen["mindate"], datetime(2026, 8, 12, 8, 4, 2, tzinfo=_UTC))
        self.assertEqual(seen["maxdate"], _MERGE_PUSHDATE)
        self.assertEqual(
            {k: sorted(v) for k, v in seen["found"].items()},
            {
                _BROWSER_PARENT: sorted(["0e9094edbbdf", "91f8065a47c7"]),
                _IM_CONTEXT: ["a6f80307f127"],
            },
        )
        # The uplifts are all ABOVE this build, so the two windows are disjoint: no crash
        # ever gets both sets.
        self.assertNotIn("bb4e5396abea", seen["found"][_BROWSER_PARENT])

    def test_off_stack_window_is_the_uplifts(self):
        """`get_two_last` has no major-version break of its own (it is a plain `limit(2)`),
        so for the shipped b1 it returns [merge-day build, shipped b1] and the off-stack
        pushlog window is the 47 uplifts -- not the 5,192 changesets of
        `20260812080401 -> 20260817142839`, which is what two recon reports computed by
        skipping the merge-day build."""
        two = models.Build.get_two_last(
            utils.get_build_date(_SHIPPED[0]), "beta", "Firefox"
        )
        self.assertEqual([t["revision"] for t in two], [_MERGE_DAY[1], _SHIPPED[1]])
        self.assertEqual([t["buildid"] for t in two], [_MERGE_DAY[0], _SHIPPED[0]])

        # The consumer: the live `json-pushes` window the off-stack seeder asks for. Mocked
        # because a test may not touch the network -- and because 5,192 changesets is
        # 7.5-16.2 MB of it.
        captured = {}

        def fake_pushlog_for_revs(startrev, endrev, channel=None, file_filter=None):
            captured.update(start=startrev, end=endrev, channel=channel)
            return [
                _chgset("0e9094edbbdf", _MERGE_PUSHDATE, files=[_BROWSER_PARENT],
                        bug=2062204, desc="Bug 2062204 - Guard the count"),
                _chgset("bb4e5396abea", datetime(2026, 8, 17, 14, 26, 1, tzinfo=_UTC),
                        files=[_BROWSER_PARENT], bug=2060153,
                        desc="Bug 2060153 - Add the guard"),
            ]

        uuid_info = {
            # `UUID.get_bid_chan_by_uuid`'s shape: buildid is a tz-aware datetime and `node`
            # is the BUILD's revision (Build join Node).
            "uuid": "d0e5b3e0-0000-0000-0000-000000000001",
            "buildid": utils.get_build_date(_SHIPPED[0]),
            "channel": "beta",
            "product": "Firefox",
            "node": _SHIPPED[1],
            "signature": "mozilla::dom::BrowserParent::RecvSynthesizeNativeKeyEvent",
        }
        with mock.patch("crashclouseau.pushlog.pushlog_for_revs",
                        side_effect=fake_pushlog_for_revs):
            cands = orchestrator._offstack_candidates(
                uuid_info, config.get_agent_offstack()
            )

        self.assertEqual(captured["start"], _MERGE_DAY[1])
        self.assertEqual(captured["end"], _SHIPPED[1])
        self.assertEqual(captured["channel"], "beta")
        self.assertEqual({c["node"] for c in cands},
                         {"0e9094edbbdf", "bb4e5396abea"})

    def test_removing_the_merge_day_build_widens_both_windows(self):
        """WHY THE ROW MUST STAY, executed. Delete that one `builds` row and the on-stack
        `mindate` falls back to 154.0b10's pushdate -- 08-12 08:04:02, which is BELOW the
        merge -- while the off-stack window becomes exactly the `20260812080401 ->
        20260817142839` pair measured at 5,192 changesets. Both windows go from the cycle's
        47 uplifts to the merge's ~5,100 changesets in one row deletion.

        And it is a deletion nobody would type: `Build.put_data` inserts only
        `if rev in revs_c`, i.e. only when a `nodes` row for the revision exists, and the
        merge-day build's revision is a member of the merge push. So dropping that push at
        ingestion -- or letting `Node.clean` prune it -- takes the row out via
        ON DELETE CASCADE and cannot put it back. Hence `pushlog.collect` emits merge
        members with `files: []` instead of dropping the push."""
        db.session.query(models.Build).filter(
            models.Build.buildid == utils.get_build_date(_MERGE_DAY[0]),
            models.Build.channel == "beta",
        ).delete(synchronize_session=False)
        db.session.commit()

        seen = self._mindate_from_put_report(_SHIPPED[0])
        self.assertEqual(seen["mindate"],
                         datetime(2026, 8, 12, 8, 4, 2, tzinfo=_UTC))
        self.assertLess(seen["mindate"], _MERGE_PUSHDATE)
        # The merge push is now IN the on-stack candidate window: three candidates on the
        # crashing frame's file instead of one, two of them a whole cycle old.
        self.assertEqual(
            {k: sorted(v) for k, v in seen["found"].items()},
            {
                _BROWSER_PARENT: sorted(["bb4e5396abea", "0e9094edbbdf", "91f8065a47c7"]),
                _IM_CONTEXT: sorted(["1129fc997b6d", "a6f80307f127"]),
            },
        )

        # ... and the off-stack window is now the pair whose live pushlog is 5,192 changesets.
        two = models.Build.get_two_last(
            utils.get_build_date(_SHIPPED[0]), "beta", "Firefox"
        )
        self.assertEqual([t["buildid"] for t in two], [_B10[0], _SHIPPED[0]])
        self.assertEqual([t["revision"] for t in two], [_B10[1], _SHIPPED[1]])

    def test_dropping_the_merge_push_deletes_the_build_row_for_good(self):
        """The other half of the same argument: the row cannot be rebuilt from Buildhub.
        Deleting the merge-day NODE (what dropping the merge push at ingestion, or pruning
        30-day-old nodes, does) cascades the `builds` row away, and re-running
        `Build.put_data` over the very same Buildhub payload does NOT recreate it, because
        of `if rev in revs_c`. `put_data` is otherwise idempotent -- the other three rows
        survive the replay -- so the failure is silent and specific."""
        self.assertIsNotNone(
            models.Build.get_id(utils.get_build_date(_MERGE_DAY[0]), "beta", "Firefox")
        )
        db.session.query(models.Node).filter(
            models.Node.channel == "beta", models.Node.node == _MERGE_DAY[1]
        ).delete(synchronize_session=False)
        db.session.commit()

        self.assertIsNone(
            models.Build.get_id(utils.get_build_date(_MERGE_DAY[0]), "beta", "Firefox"),
            "builds.nodeid is ON DELETE CASCADE: losing the node loses the build row",
        )
        models.Build.put_data(_BUILDHUB)
        self.assertIsNone(
            models.Build.get_id(utils.get_build_date(_MERGE_DAY[0]), "beta", "Firefox"),
            "Build.put_data only inserts `if rev in revs_c` -- Buildhub cannot heal this",
        )
        for bid, _, _ in (_B9, _B10, _SHIPPED):
            self.assertIsNotNone(
                models.Build.get_id(utils.get_build_date(bid), "beta", "Firefox"), bid
            )

    # ------------------------------------------------------------------ item 3

    def test_no_earlier_build_does_not_raise(self):
        """`get_pushdate_before` used to be `return qs.pushdate` on a `.first()` that is
        `None` whenever no earlier build row exists -- an AttributeError which, on the
        ingestion path, becomes `UUID.set_error` on a report with nothing wrong with it, and
        an errored report is never retried. Zero test coverage before this
        (`grep -rn get_pushdate_before tests/` -> 0 hits).

        Reachable off the selector: `Node.clean` prunes at `max_ndays` and `builds.nodeid`
        is ON DELETE CASCADE, and on beta a whole merge push's ~5,100 nodes age out in one
        instant. So the oldest build row in the table is a real state, not a fresh-DB
        curiosity."""
        self.assertIsNone(
            models.Build.get_pushdate_before(
                utils.get_build_date(_B9[0]), "beta", "Firefox"
            )
        )
        # Nor may another channel's builds be borrowed as a lower bound: with no nightly
        # rows at all, the newest beta build still has no predecessor. (Only one product is
        # configured, so the product half of the filter cannot be probed the same way --
        # `PRODUCT_TYPE` is a Postgres enum of `config.get_products()`.)
        self.assertIsNone(
            models.Build.get_pushdate_before(
                utils.get_build_date(_SHIPPED[0]), "nightly", "Firefox"
            )
        )

        # And the caller keeps the report: `put_report` falls back to the nightly rule
        # (buildid - `ndays`, 3 today) and says so, rather than raising.
        with self.assertLogs(level="WARNING") as logs:
            seen = self._mindate_from_put_report(_B9[0])
        self.assertEqual(
            seen["mindate"],
            utils.get_build_date(_B9[0]) - relativedelta(days=config.get_ndays()),
        )
        self.assertTrue(
            any("no build before" in m for m in logs.output),
            "a silent fallback is how the builds table falls behind unnoticed: {}".format(
                logs.output
            ),
        )
