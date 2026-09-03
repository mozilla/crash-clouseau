# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# The beta SELECTION window: which build-days the spike detector may look at, which of them
# it is allowed to use as a baseline, and how long the baseline is. Plan #18 §6.3 T2, items
# 7 and 8.
#
# Two independent defects lived here, and neither subsumes the other (plan #18 §7.2 —
# "believe the replay": with the no-user build removed the merge blackout SHIFTS to the
# shipped b1 and shortens, 10 of 127 run-days instead of 12).
#
#   ITEM 7 — `Build.get_last_versions` applied `.limit(3)` in SQL and then broke out of the
#   Python loop on the first row of a different major version, so the day after a
#   central->beta merge the three newest rows are `155.0b1 / 154.0b10 / 154.0b9`, the break
#   fired on row two, `len(res) >= 2` failed and it returned `[]`. `get_builds` then handed
#   back `bids=[], search_date=""`, `get_new_signatures` logged one warning and returned
#   `({}, [])` — no `Stats`, no `uuids`, and (because `record_many([])` returns early) no
#   `Selection` rows either, so the one table built to answer "why did you do nothing" was
#   silent too. Measured over the Buildhub build list: 12 of 127 beta run-days (9.4%) across
#   5 merges, 2/2/2/2/4 days each — exactly the days a freshly uplifted regression first
#   reaches beta users.
#
#   ITEM 8 — every cycle ships TWO builds tagged `N.0b1` and the merge-day one has never had
#   a user: lifetime totals 8 / 13 / 7 / 17 / 1 reports for v151-v155 against 435-9,124 for
#   all 54 other builds since 2026-04-01 (median 2,850). 5 of 5 below 20, 54 of 54 above
#   435 — a 25x gap with NO overlap, so any floor in [20, 400] is the same decision and 100
#   is not fitted. Sitting between two real builds in the 3-build window it is a ZERO
#   BASELINE, and `utils.is_spike`'s from-zero branch is gated by neither `floor` nor
#   `ratio`, so every signature clearing the 6-install threshold on the NEXT build spikes:
#   replayed over 30 beta run-days, 4 run-days carried 108 of 179 selections (60%) and 104
#   of 160 from-zero fires (65%), topped by boilerplate no analysis can act on (`OOM | small`
#   236, `OOM | unknown | js::AutoEnterOOMUnsafeRegion::crash_impl` 125).
#
# TWO tests are `@unittest.expectedFailure`. They assert behaviour the tree does not have and
# name the defect above themselves: item 8's floor is compared against the reports VISIBLE at
# run time while the "any value in [20, 400] is equivalent" argument that justifies 100 was
# measured on LIFETIME totals — and window index 1, the only index that ever selects anything
# (135 of 135 replayed selections), is one cadence gap old. See
# `test_a_real_build_seen_early_is_not_a_build_with_no_users`.
#
# WHY THIS FILE IS NOT POSTGRES-GATED even though it hits the `builds`/`nodes` tables: the
# item-7 bug WAS the interaction between a database-side LIMIT and a Python-side loop, so a
# fake row-set driven through the method would have passed both before and after the fix and
# proved nothing. Real SQL is the whole point. `nodes` and `builds` carry no Postgres-only
# type (Integer / String / DateTime / Enum), so sqlite runs the real query, the real
# `ORDER BY buildid DESC` and the real `LIMIT`. The one sqlite artifact is that
# `DateTime(timezone=True)` round-trips NAIVE, so `utils.get_buildid` re-reads a stored
# buildid as local time; the window's identity is therefore asserted on `revision` (unique
# per build) and `version`, never on a buildid literal. Prod is Postgres on UTC dynos.
#
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_beta_selection
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import contextlib  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import config, datacollector as dc, db, models, utils  # noqa: E402


# The real cycle-154/155 builds, `(buildid, version, revision)`, from plan #18 §2.1, §3 items
# 9/23 and §7.3. Real: every buildid but 154.0b9's, 155.0b3's and 155.0b4's; the merge-day
# revision `22761955d964`, 154.0b9's `cd001e124b15` and 155.0b2's `485039f30ace`. The rest are
# placeholders — the window's identity does not depend on them, only on their uniqueness and
# their order. Every buildid hour is >= 08:00 so the sqlite local-time re-read (see the header)
# cannot cross a day boundary and move `search_date` for any offset a developer machine has.
B9 = ("20260810081234", "154.0b9", "cd001e124b15")
B10 = ("20260812080401", "154.0b10", "aabbccddeeff")
# The merge-day build. Its revision is "Update configs after merge day operations" and it is a
# MEMBER of the merge push (pushid 27990, pushdate 2026-08-13 14:15:59 == the buildid).
MERGE_B1 = ("20260813141559", "155.0b1", "22761955d964")
# ...and the 155.0b1 users actually got, 4 days later. Same version string, different build.
SHIPPED_B1 = ("20260817142839", "155.0b1", "112233445566")
B2 = ("20260819090452", "155.0b2", "485039f30ace")
B3 = ("20260821093000", "155.0b3", "665544332211")
B4 = ("20260824091500", "155.0b4", "ffeeddccbbaa")

_ALL_REVS = [r for _, _, r in (B9, B10, MERGE_B1, SHIPPED_B1, B2, B3, B4)]


def _install_tables():
    models.Node.__table__.create(bind=db.engine, checkfirst=True)
    models.Build.__table__.create(bind=db.engine, checkfirst=True)


def _drop_fixture_rows():
    """Delete only what this module inserted. The suite shares one in-memory sqlite when run
    through `unittest discover`, so this must not truncate tables another module owns."""
    nodes = db.session.query(models.Node.id).filter(models.Node.node.in_(_ALL_REVS))
    ids = [nid for (nid,) in nodes]
    if ids:
        db.session.query(models.Build).filter(models.Build.nodeid.in_(ids)).delete(
            synchronize_session=False
        )
        db.session.query(models.Node).filter(models.Node.id.in_(ids)).delete(
            synchronize_session=False
        )
    db.session.commit()


def _load(rows, channel="beta", product="Firefox"):
    """Insert `(buildid, version, revision)` build rows, each with the `nodes` row
    `get_last_versions`' inner join needs."""
    for bid, version, rev in rows:
        pushdate = utils.get_build_date(bid)
        res = db.session.execute(
            models.Node.__table__.insert().values(
                channel=channel, node=rev, pushdate=pushdate,
                backedout=False, merge=False, bug=0, hgauthor=None,
            )
        )
        db.session.execute(
            models.Build.__table__.insert().values(
                buildid=pushdate, product=product, channel=channel,
                version=version, nodeid=res.inserted_primary_key[0],
            )
        )
    db.session.commit()


class TestGetLastVersionsAcrossTheMerge(unittest.TestCase):
    """Item 7. The three row-sets are the real ones from plan #18 §6.3; the first is the one
    that returned `[]` for 12 of 127 run-days (9.4%).

    The claim under test is not "the window is right", it is "the window EXISTS". A silent
    switch-off is the worse failure: `[]` here costs a whole run-day of ingestion and leaves
    one warning line against a ~2h log window."""

    # A run on 2026-08-14, i.e. the day after the merge — the first blacked-out day.
    DAY_AFTER_MERGE = datetime(2026, 8, 14, 10, 0)
    MID_CYCLE = datetime(2026, 8, 25, 10, 0)

    def setUp(self):
        _install_tables()
        _drop_fixture_rows()

    def tearDown(self):
        _drop_fixture_rows()

    def _window(self, date, n=3):
        return models.Build.get_last_versions(date, "beta", "Firefox", n=n)

    def test_the_day_after_a_merge_returns_a_window_instead_of_nothing(self):
        _load([B9, B10, MERGE_B1])
        window = self._window(self.DAY_AFTER_MERGE)
        self.assertTrue(window, "this is the row-set that returned [] and killed the run-day")
        self.assertEqual(
            [(r["version"], r["revision"]) for r in window],
            [("155.0b1", "22761955d964"), ("154.0b10", "aabbccddeeff"),
             ("154.0b9", "cd001e124b15")],
        )

    def test_the_post_merge_window_deliberately_spans_two_majors(self):
        """The exact regression. A window holding only 155 rows, or only 154 rows, means the
        break is back: `.limit(3)` runs in SQL, so any major filter applied afterwards can
        only ever SHRINK an already-truncated window, never refill it.

        Mixing majors is the question, not a defect — "is this build crashier than the ones
        before it", and the builds before the first beta of a cycle ARE the previous cycle's
        last betas."""
        _load([B9, B10, MERGE_B1])
        window = self._window(self.DAY_AFTER_MERGE)
        self.assertEqual(len(window), 3)
        self.assertEqual(
            {utils.get_major(r["version"]) for r in window}, {154, 155}
        )

    def test_the_respin_window_of_two_identical_versions_survives(self):
        """`155.0b1 / 155.0b1` — the merge-day build and the one users got. Same version
        string, different build. The old code handled this row-set (one major, so the break
        never fired); it must keep working, and it must come back newest-first so the shipped
        b1 is the day under test and the merge-day build is its baseline."""
        _load([MERGE_B1, SHIPPED_B1])
        window = self._window(datetime(2026, 8, 18, 10, 0))
        self.assertEqual([r["version"] for r in window], ["155.0b1", "155.0b1"])
        self.assertEqual(
            [r["revision"] for r in window], ["112233445566", "22761955d964"]
        )

    def test_a_mid_cycle_window_is_unchanged(self):
        """`155.0b4 / b3 / b2` — the 90% of the cycle where the break never fired, so the
        control arm: removing it must not have moved anything here."""
        _load([B9, B10, MERGE_B1, SHIPPED_B1, B2, B3, B4])
        window = self._window(self.MID_CYCLE)
        self.assertEqual(
            [r["version"] for r in window], ["155.0b4", "155.0b3", "155.0b2"]
        )

    def test_a_single_build_is_a_short_window_not_an_empty_one(self):
        """`len(res) >= 2` used to swallow a one-row window. It is reachable outside the merge
        too: `update_builds` fed Buildhub only `get_ndays()` = 3 days, and beta ships every
        ~2.00 days (median of 58 gaps), so a rolling 3-day window holds 0 builds on 23 of 196
        days and 1 build on 117 — 71% of the moments someone could flip `INGEST_CHANNELS`
        gave fewer than 2 rows (item 6).

        A short window is a worse window, not a broken one."""
        _load([MERGE_B1])
        window = self._window(self.DAY_AFTER_MERGE)
        self.assertEqual(len(window), 1)
        self.assertEqual(window[0]["revision"], "22761955d964")

    def test_a_build_newer_than_the_run_date_is_still_excluded(self):
        """The date bound is the one piece of filtering that must survive: a replay of an
        older run-day must not see builds from the future."""
        _load([B9, B10, MERGE_B1, SHIPPED_B1, B2])
        window = self._window(self.DAY_AFTER_MERGE)
        self.assertEqual(
            [r["version"] for r in window], ["155.0b1", "154.0b10", "154.0b9"]
        )
        self.assertEqual(window[0]["revision"], "22761955d964")   # the merge-day build

    def test_the_window_is_capped_at_n_and_ordered_newest_first(self):
        """`n` still binds, and the order is still descending — `get_builds` reads `bids[-1]`
        as the OLDEST build to derive `search_date`, and `evaluate_days` needs the series
        sorted by day, so an ascending window would silently shorten the Socorro date range to
        a few hours."""
        _load([B9, B10, MERGE_B1, SHIPPED_B1, B2, B3, B4])
        window = self._window(self.MID_CYCLE, n=3)
        bids = [r["buildid"] for r in window]
        self.assertEqual(len(bids), 3)
        self.assertEqual(bids, sorted(bids, reverse=True))
        for bid in bids:
            self.assertRegex(bid, r"^\d{14}$")

    def test_another_channel_never_leaks_into_the_beta_window(self):
        """The `builds` table is shared by every non-nightly channel, and with the major-version
        break gone the channel filter is the ONLY thing keeping release rows out of beta's
        window. A release build would be both newer and far louder than any beta build."""
        _load([B2, B3], channel="release")
        _load([MERGE_B1, SHIPPED_B1])
        window = self._window(self.MID_CYCLE)
        self.assertEqual([r["version"] for r in window], ["155.0b1", "155.0b1"])

    def test_the_selector_gets_buildids_and_a_search_date_after_the_merge(self):
        """The blast radius, one level up: `get_builds` returned `([], "")` on this row-set,
        which is what made `get_new_signatures` return `({}, [])`."""
        _load([B9, B10, MERGE_B1])
        bids, search_date = dc.get_builds("Firefox", "beta", self.DAY_AFTER_MERGE)
        self.assertEqual(len(bids), 3)
        # The oldest build in the window bounds the Socorro date range.
        self.assertEqual(search_date, ">=2026-08-10")

    def test_a_one_build_window_still_reaches_the_selection_log(self):
        """The docstring's promise, end to end: with one build the caller evaluates one
        build-day, `evaluate_days` declines it as an untestable prefix and RECORDS it. Before
        item 7 this path produced zero `Selection` rows, so the table that exists to answer
        "we had a spike and you did nothing" answered nothing."""
        _load([MERGE_B1])
        # The population is keyed off what `get_builds` actually returns rather than off the
        # buildid literal, because the sqlite round-trip shifts it by the local UTC offset
        # (see the module header). `builds=None` keeps the real `get_builds` in the chain.
        bids, _ = dc.get_builds("Firefox", "beta", self.DAY_AFTER_MERGE)
        self.assertEqual(len(bids), 1)
        data, selection = _collect(
            {bids[0]: {"OOM | small": (236, 180)}},
            channel="beta",
            date=self.DAY_AFTER_MERGE,
            builds=None,
        )
        self.assertEqual(data, {})
        self.assertEqual([r["outcome"] for r in selection], [utils.UNTESTABLE_PREFIX])
        self.assertEqual(selection[0]["signature"], "OOM | small")
        self.assertFalse(selection[0]["evaluable"])


def _collect(population, channel="beta", date=datetime(2026, 8, 18), builds=None,
             product="Firefox", extra=()):
    """Drive the real `get_new_signatures` over a synthetic Socorro population.

    `population` is `{buildid: {signature: (count, installs)}}`, which is exactly the shape
    the per-build `_aggs.signature` facet delivers: `get_new_signatures` issues one
    SuperSearch per buildid with `build_id` pinned, so each response carries a single
    `build_id` sub-facet per signature. `builds=None` means let the real `get_builds` read
    the `builds` table; otherwise it is the buildid list to hand back."""

    class FakeSuperSearch:
        def __init__(self, params=None, handler=None, handlerdata=None):
            bid = params["build_id"]
            facets = [
                {
                    "term": sgn,
                    "facets": {
                        "cardinality_install_time": {"value": installs},
                        "build_id": [{"term": bid, "count": count}],
                    },
                }
                for sgn, (count, installs) in population.get(bid, {}).items()
            ]
            handler({"errors": None, "facets": {"signature": facets}}, handlerdata)

        def wait(self):
            pass

    patches = [
        mock.patch.object(dc.socorro, "SuperSearch", FakeSuperSearch),
        # The proto-signature and uuid fetches are a different plan item; the selection
        # decision is made before them.
        mock.patch.object(dc, "get_proto_small"),
        mock.patch.object(dc, "get_proto_big"),
        # The rate path reads the daily rollup and the selection log (`test_frequency_regression`
        # covers it); here only the spike test is under test.
        mock.patch.object(dc, "_rising_picks", return_value={}),
    ]
    if builds is not None:
        patches.append(
            mock.patch.object(dc, "get_builds", return_value=(list(builds), ">=2026-08-10"))
        )
    patches.extend(extra)
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        return dc.get_new_signatures(product, channel, date)


def _day(bid):
    when = utils.get_build_date(bid)
    return datetime(when.year, when.month, when.day)


# The whole 3-build window this class works in: 154.0b10, then the merge-day 155.0b1, then
# the 155.0b1 users got. The merge-day build sits at index 1 — no longer the newest, which is
# the only position in which it does harm.
_WINDOW = [B10[0], MERGE_B1[0], SHIPPED_B1[0]]
# The measured merge-day payload for v155: ONE report in the build's whole lifetime, and it is
# not on the signature we care about. So every other signature reads 0 there, which is what
# makes it a zero baseline rather than merely a quiet one.
_ONE_REPORT = {"AsyncShutdownTimeout | profile-before-change": (1, 1)}


class TestNoUserBuildIsNotABaseline(unittest.TestCase):
    """Item 8. A build nobody ran must not be allowed to act as a baseline — and its removal
    must leave a trace.

    Denominators for every number quoted below: 5 merge-day builds (v151-v155) at 8/13/7/17/1
    lifetime reports against 54 other builds at 435-9,124 since 2026-04-01, no overlap; and a
    30-run-day replay in which 4 run-days carried 108 of 179 selections and 104 of 160
    from-zero fires."""

    def test_the_floor_is_off_on_nightly_whatever_the_config_says(self):
        """The nightly exemption lives in CODE, not in the config value — so a stripped or
        mistyped config cannot switch it on. `_SPIKE_DEFAULTS["min_build_installs"]` is 15, so
        the config read alone would return 15 for an unknown product on nightly.

        It must stay off: nightly's builds do not come from the `builds` table, there is no
        merge-day build, and a quiet nightly build-day is ordinary (median 315 lifetime
        reports per build against beta's 2,674). Dropping one would REMOVE a real baseline and
        make the from-zero branch fire MORE."""
        self.assertEqual(dc.get_no_user_build_floor("Firefox", "nightly"), 0)
        self.assertEqual(config.get_spike("min_build_installs", "Nonesuch", "nightly"), 15)
        self.assertEqual(dc.get_no_user_build_floor("Nonesuch", "nightly"), 0)

    def test_the_floor_is_fifteen_installations_on_beta(self):
        self.assertEqual(dc.get_no_user_build_floor("Firefox", "beta"), 15)

    def test_any_floor_in_the_measured_gap_is_the_same_decision(self):
        """Not a fitted threshold, but a NARROWER gap than the retired report rule claimed, so
        the test states the real bound. Measured installations: the 5 merge-day builds sit at
        4/6/7/4/1 forever (a build nobody runs never acquires an installation), the 54 real
        builds at 268-5,084, and the quietest of those shows ~29 while it is at window index 1
        (268 lifetime at the ~11% arrival measured for a 1.25-day-old build). So every floor in
        [8, 24] partitions them identically. Beyond 24 it starts eating real builds, which is
        exactly what the report-based version did."""
        data = _population(
            {B10[0]: 1974, MERGE_B1[0]: 7, SHIPPED_B1[0]: 29}
        )
        decisions = {
            floor: dc.find_no_user_days(data, floor) for floor in (8, 15, 20, 24)
        }
        self.assertEqual(
            list(decisions.values()), [{_day(MERGE_B1[0])}] * 4, decisions
        )

    def test_a_no_user_build_day_between_two_real_ones_is_dropped(self):
        data = _population({B10[0]: 1974, MERGE_B1[0]: 1, SHIPPED_B1[0]: 2000})
        self.assertEqual(
            dc.find_no_user_days(data, 15), {_day(MERGE_B1[0])}
        )

    def test_the_newest_build_day_in_the_window_is_never_dropped(self):
        """A build holds 0.2-2.7% of its eventual crashes on its own ship day (154.0b10 1.1%,
        155.0b1 0.2%, 155.0b2 2.5%) and 77-96% by day 4 — so the newest build-day is quiet for
        a reason that has nothing to do with users, and it is the day we are here to select.

        This is also the shape of the merge day itself: on 2026-08-13 the window is
        `154.0b9 / 154.0b10 / 155.0b1(merge-day)` and the merge-day build IS the newest, so
        nothing is dropped and the run proceeds. It only starts doing harm on 08-17, when the
        shipped b1 pushes it to index 1."""
        merge_day_is_newest = _population(
            {B9[0]: 1974, B10[0]: 2100, MERGE_B1[0]: 1}
        )
        self.assertEqual(dc.find_no_user_days(merge_day_is_newest, 15), set())

        merge_day_is_a_baseline = _population(
            {B10[0]: 2100, MERGE_B1[0]: 1, SHIPPED_B1[0]: 3000}
        )
        self.assertEqual(
            dc.find_no_user_days(merge_day_is_a_baseline, 15), {_day(MERGE_B1[0])}
        )

    def test_every_quiet_day_but_the_newest_is_dropped(self):
        """The `builds` table can hold nothing but merge-day-grade builds — a fresh canary DB
        one build at a time (item 6). The newest survives, so the window degrades to a short
        one rather than to nothing."""
        data = _population({B10[0]: 3, MERGE_B1[0]: 1, SHIPPED_B1[0]: 2})
        self.assertEqual(
            dc.find_no_user_days(data, 15), {_day(B10[0]), _day(MERGE_B1[0])}
        )

    def test_a_zero_floor_drops_nothing(self):
        """The kill switch, and the nightly path: `get_no_user_build_floor` returns 0 there,
        so this whole feature is arithmetically absent from nightly."""
        data = _population({B10[0]: 1, MERGE_B1[0]: 1, SHIPPED_B1[0]: 1})
        for floor in (0, None):
            self.assertEqual(dc.find_no_user_days(data, floor), set(), floor)

    def test_no_signatures_at_all_drops_nothing(self):
        self.assertEqual(dc.find_no_user_days({}, 15), set())

    # ------------------------------------------------------------------ #
    # DEFECT — the floor is compared against a quantity that is NOT the one the
    # "any value in [20, 400]" equivalence was measured on.
    #
    # The equivalence rests on LIFETIME totals: 5 merge-day builds at 8/13/7/17/1 reports
    # against 54 real builds at 435-9,124, a 25x gap with no overlap. The code compares the
    # reports VISIBLE AT RUN TIME, and that number is steeply age-dependent — the docstring
    # says so itself when it exempts the newest day ("0.2-2.7% of its eventual crashes on its
    # own ship day"). But the exemption is exactly one build-day wide and the curve is still
    # steep after it: the one measured early point is 154.0b10 at **11.0% of its eventual
    # crashes at 1.25 days** (plan #18 §8 kill 16), and window index 1 is one cadence gap old
    # — **min 1.26 d, p25 2.00 d over 58 measured gaps** (§2.1). So the lowest-volume REAL
    # build (435 lifetime reports, 268 installs) shows ~48 reports while it sits at index 1
    # and is dropped as having "no users".
    #
    # Why index 1 is the whole game, and why this is a blackout rather than a haircut: plan
    # #18 §4 replayed the selector over 14 run-days and two cycles and **all 135 of 135
    # selections landed at index 1** — index 2 (the newest, and the day the floor exempts)
    # selected 0 times because it holds 0.2-2.7% of its crashes and can never clear
    # `3 x max(before)`, and index 0 cannot be tested at all. Dropping index 1 therefore
    # removes 100% of the measured selecting surface for that run-day: the run selects
    # nothing, which is the same silent switch-off item 7 just fixed, reached from a
    # different direction. And it is INVISIBLE — the log line reads "Dropping 1 build-day(s)
    # with no users", indistinguishable from the merge-day build doing what it is supposed to.
    #
    # Not asserting a fix, because the right one is a design decision (age-normalise the
    # count, exempt on the build's own age rather than on its rank, or use the install
    # cardinality the record already carries). Asserting only that a build-day carrying 25
    # distinct installations is not a build-day with no users.
    # ------------------------------------------------------------------ #
    def test_a_real_build_seen_early_is_not_a_build_with_no_users(self):
        data = _population_multi(
            {
                # ~4 days old, 92% of its reports arrived.
                B9[0]: [("noise::a", 398, 300)],
                # ~1.3 days old: 11.0% of an eventual 435 reports, from 25 installations.
                B10[0]: [("noise::a", 36, 25), ("mozilla::dom::NewRegression", 12, 8)],
                # just shipped.
                MERGE_B1[0]: [("noise::a", 2, 2)],
            }
        )
        self.assertEqual(dc.find_no_user_days(data, 15), set())

    def test_dropping_a_real_index_one_day_costs_the_whole_run_days_selection(self):
        """The consequence of the test above, driven through the selector: a brand-new
        signature at 12 crashes / 8 installs on that build clears the 6-install threshold and
        `is_spike`'s from-zero branch, i.e. it is exactly what the selector exists to find —
        and it is thrown away with the build-day."""
        population = {
            B9[0]: {"noise::a": (398, 300)},
            B10[0]: {"noise::a": (36, 25), "mozilla::dom::NewRegression": (12, 8)},
            MERGE_B1[0]: {"noise::a": (2, 2)},
        }
        data, _ = _collect(
            population,
            builds=[B9[0], B10[0], MERGE_B1[0]],
            date=datetime(2026, 8, 13, 20),
        )
        self.assertIn("mozilla::dom::NewRegression", data)

    def test_the_merge_day_build_is_still_selectable_while_it_is_the_newest_day(self):
        """A TRIPWIRE on the accepted residual risk, not a wish. The newest-day exemption is
        justified by "a build is quiet because its crashes have not arrived yet", which is
        false for the merge-day build — nobody will ever run it. So on the merge day itself it
        is both the newest day AND selectable, and the ONLY thing between it and a filing is
        the install threshold: plan #18 §3 item 23 calls that "near-unreachable ... 0-7
        installs against an install threshold of 6", and v153's merge-day build measured 7
        reports / 7 installs, so 1 of the 5 measured merge builds clears it.

        It matters because that build's candidate window is the merge push: `mindate` = its
        pushdate + 1s bounds a window of **5,144 changesets** (1,932 candidate-bearing)
        against 46-122 for every other beta build. If the install threshold ever drops below
        7, item 23's `candidate_arrived_by_merge` guard stops being belt-and-braces and
        becomes the only thing standing there."""
        self.assertGreaterEqual(
            config.get_threshold("installs", "Firefox", "beta"), 6,
            "the merge-day build is selectable while it is newest; the install threshold is "
            "the only gate, and item 23's guard the only backstop",
        )
        window = [B9[0], B10[0], MERGE_B1[0]]
        base = {
            B9[0]: {"noise::a": (2800, 1900)},
            B10[0]: {"noise::a": (3100, 2000)},
        }
        below, _ = _collect(
            dict(base, **{MERGE_B1[0]: {"js::Foo": (7, 5)}}),
            builds=window, date=datetime(2026, 8, 14),
        )
        self.assertEqual(below, {})
        # 7 reports from 7 installations: v153's merge-day build, exactly.
        at_seven, _ = _collect(
            dict(base, **{MERGE_B1[0]: {"js::Foo": (7, 7)}}),
            builds=window, date=datetime(2026, 8, 14),
        )
        self.assertIn("js::Foo", at_seven)

    def test_the_day_total_is_summed_over_signatures_not_over_installs(self):
        """Report COUNT, not summed install cardinality: cardinalities do not add, so a
        machine crashing on five signatures would be counted five times and a build with 4
        real installs could clear a floor of 100. Ten signatures at 9 reports each is 90 — a
        no-user build by count, and this must drop it."""
        data = _population_multi(
            {
                B10[0]: [("sig%d" % i, 9, 4) for i in range(10)],
                MERGE_B1[0]: [("sig%d" % i, 9, 4) for i in range(10)],
                SHIPPED_B1[0]: [("sig0", 6000, 4000)],
            }
        )
        self.assertEqual(
            dc.find_no_user_days(data, 100), {_day(B10[0]), _day(MERGE_B1[0])}
        )


class TestTheDropLeavesATrace(unittest.TestCase):
    """Item 8's other half. The whole point of the `Selection` table is that a declined
    build-day leaves a trace; a build-day removed before it is even evaluated is the easiest
    kind of decision to lose."""

    def _records(self):
        data = _population_multi(
            {
                B10[0]: [("OOM | small", 236, 180)],
                MERGE_B1[0]: [("AsyncShutdownTimeout | profile-before-change", 1, 1)],
                SHIPPED_B1[0]: [("OOM | small", 40, 30)],
            }
        )
        dead = dc.find_no_user_days(data, 100)
        return data, dead

    def test_a_dropped_day_is_recorded_with_the_dropped_no_users_outcome(self):
        data, dead = self._records()
        records = dc.dropped_day_records(
            data["AsyncShutdownTimeout | profile-before-change"], dead
        )
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["outcome"], utils.DROPPED_NO_USERS)
        self.assertEqual(record["day"], _day(MERGE_B1[0]))
        self.assertEqual(record["count"], 1)
        self.assertFalse(record["spiked"])
        self.assertIsNone(record["picked"])

    def test_a_dropped_day_claims_no_position_it_never_had(self):
        """The day was never placed in a series, so the record says so (-1 / [] / False)
        instead of inventing an index and a baseline. A reader who trusts `position` must not
        be told this pair sat at index 1 with a baseline it was never compared against."""
        data, dead = self._records()
        record = dc.dropped_day_records(
            data["AsyncShutdownTimeout | profile-before-change"], dead
        )[0]
        self.assertEqual(record["index"], -1)
        self.assertEqual(record["baseline"], [])
        self.assertFalse(record["evaluable"])

    def test_a_signature_that_did_not_crash_on_the_dropped_day_gets_no_row(self):
        """A no-user build carries 1-17 reports, so the trace is a handful of rows, not one
        per signature in the window."""
        data, dead = self._records()
        self.assertEqual(dc.dropped_day_records(data["OOM | small"], dead), [])

    def test_no_dropped_days_means_no_records(self):
        data, _ = self._records()
        self.assertEqual(dc.dropped_day_records(data["OOM | small"], set()), [])

    def test_the_dropped_record_survives_the_selection_row_conversion(self):
        """`models.Selection._row` reads the record positionally by name, so a key this
        function forgets is an exception at write time inside `record_many`'s blanket
        `except` — i.e. a silently empty log. Convert one for real."""
        data, dead = self._records()
        record = dc.dropped_day_records(
            data["AsyncShutdownTimeout | profile-before-change"], dead
        )[0]
        record = dict(record, signature="AsyncShutdownTimeout | profile-before-change")
        run_date = datetime(2026, 8, 18, 12, 0)
        row = models.Selection._row(record, "Firefox", "beta", run_date)
        self.assertEqual(row["outcome"], utils.DROPPED_NO_USERS)
        self.assertEqual(row["channel"], "beta")
        self.assertEqual(row["build_day"], datetime(2026, 8, 13).date())
        self.assertEqual(row["number"], 1)
        self.assertEqual(row["position"], -1)
        self.assertFalse(row["evaluable"])
        self.assertIsNone(row["picked"])
        self.assertFalse(row["ever_selected"])
        self.assertEqual(row["bids"], {MERGE_B1[0]: {"count": 1, "installs": 1}})

    def test_the_outcome_is_in_the_vocabulary_and_fits_the_column(self):
        """A record whose outcome is not in `SELECTION_OUTCOMES`, or is wider than the column,
        would be written by `record_many`'s bulk upsert and rejected by the database — inside
        the blanket `except`, which logs and returns 0. The log would just stop."""
        self.assertIn(utils.DROPPED_NO_USERS, models.SELECTION_OUTCOMES)
        self.assertLessEqual(
            len(utils.DROPPED_NO_USERS),
            models.Selection.__table__.c.outcome.type.length,
        )


class TestTheZeroBaselineIsGoneEndToEnd(unittest.TestCase):
    """The behaviour the floor exists for, driven through the real `get_new_signatures`.

    `OOM | small` is the measured top of the burst (236 reports on the run-day that carried
    60% of all selections). It is boilerplate: whatever the pipeline spends on it is wasted."""

    DATE = datetime(2026, 8, 18)

    def _population(self, merge_day, sig_on_ship=(40, 30)):
        return {
            B10[0]: {"OOM | small": (236, 180)},
            MERGE_B1[0]: dict(merge_day),
            SHIPPED_B1[0]: {"OOM | small": sig_on_ship},
        }

    def test_the_no_user_build_stops_being_a_zero_baseline(self):
        """With the merge-day build out of the series, `OOM | small` on the shipped b1 is
        judged against 154.0b10's 236 — 40 is not 3x236, so it does not spike."""
        data, selection = _collect(
            self._population(_ONE_REPORT), builds=_WINDOW, date=self.DATE
        )
        self.assertEqual(data, {})
        outcomes = {(r["signature"], r["day"].date()): r["outcome"] for r in selection}
        self.assertEqual(
            outcomes[("OOM | small", datetime(2026, 8, 17).date())], utils.NOT_SPIKING
        )
        self.assertNotIn(utils.SELECTED, set(outcomes.values()))

    def test_without_the_floor_the_same_window_selects_the_boilerplate(self):
        """The counterfactual, and the kill switch. `min_build_reports: 0` restores the
        pre-item-8 behaviour exactly: the merge-day build reads 0 for `OOM | small`, so
        `is_spike`'s from-zero branch fires (gated by neither `floor` nor `ratio`) and the
        only remaining bar is the 6-install threshold, which 30 installs clears.

        If this test ever stops selecting, the two arms are no longer comparable and the test
        above proves nothing."""
        data, selection = _collect(
            self._population(_ONE_REPORT),
            builds=_WINDOW,
            date=self.DATE,
            extra=[mock.patch.object(dc, "get_no_user_build_floor", return_value=0)],
        )
        self.assertIn("OOM | small", data)
        picked = [r for r in selection if r["outcome"] == utils.SELECTED]
        self.assertEqual([r["day"].date() for r in picked], [datetime(2026, 8, 17).date()])
        # ...and this is the from-zero branch, not the ratio branch.
        self.assertEqual(picked[0]["baseline"], [0])

    def test_a_real_predecessor_with_no_crashes_is_still_a_zero_baseline(self):
        """The floor must not turn into "ignore any quiet build-day". A build with 2,850
        reports (the measured median for a real beta build) that carried NONE of this
        signature is a genuine zero: the signature really did appear from nothing, and that is
        the detection the from-zero branch exists for."""
        population = self._population(
            {"some::other::signature": (2850, 1974)}
        )
        data, selection = _collect(population, builds=_WINDOW, date=self.DATE)
        self.assertIn("OOM | small", data)
        picked = [r for r in selection
                  if r["signature"] == "OOM | small" and r["outcome"] == utils.SELECTED]
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["baseline"], [0])
        self.assertEqual(picked[0]["day"].date(), datetime(2026, 8, 17).date())

    def test_the_drop_is_recorded_for_the_signature_that_crashed_on_it(self):
        """End to end: the run declined a build-day, and the `Selection` log says so."""
        data, selection = _collect(
            self._population(_ONE_REPORT), builds=_WINDOW, date=self.DATE
        )
        dropped = [r for r in selection if r["outcome"] == utils.DROPPED_NO_USERS]
        self.assertEqual(len(dropped), 1)
        self.assertEqual(
            dropped[0]["signature"], "AsyncShutdownTimeout | profile-before-change"
        )
        self.assertEqual(dropped[0]["day"].date(), datetime(2026, 8, 13).date())

    def test_a_signature_seen_only_on_the_dropped_day_disappears_quietly(self):
        """Its whole series is the dropped day, so there is nothing left to evaluate — but the
        `dropped_no_users` row is still written, which is the only reason the signature is
        traceable at all."""
        population = {
            B10[0]: {},
            MERGE_B1[0]: {"only::here": (1, 1)},
            SHIPPED_B1[0]: {},
        }
        data, selection = _collect(population, builds=_WINDOW, date=self.DATE)
        self.assertEqual(data, {})
        self.assertEqual(
            [(r["signature"], r["outcome"]) for r in selection],
            [("only::here", utils.DROPPED_NO_USERS)],
        )

    def test_nightly_never_drops_a_build_day(self):
        """Same window, same population, nightly config: floor 0, so nothing is dropped and
        the merge-day day stays in the series. Nightly's `shift` is 3, so a 3-day window has
        no evaluable day at all — which is exactly why `get_builds` gives nightly 21 days."""
        data, selection = _collect(
            self._population(_ONE_REPORT), channel="nightly", builds=_WINDOW, date=self.DATE
        )
        self.assertEqual(data, {})
        self.assertNotIn(
            utils.DROPPED_NO_USERS, {r["outcome"] for r in selection}
        )


def _population(installs, signature="OOM | small"):
    """`{buildid: distinct installations}` -> the `{signature: {day: {...}}}` structure
    `get_new_signatures` assembles, with one signature carrying the whole day.

    INSTALLATIONS, because that is what the floor reads. It used to be reports, and that was the
    defect: a report count is age-dependent, so the floor fired on a real build seen early
    (`get_no_user_build_floor`). The crash count here is set well clear of every threshold so
    that these tests are about the install statistic alone."""
    return _population_multi(
        {bid: [(signature, max(n * 3, 50), n)] for bid, n in installs.items()})


def _population_multi(per_build):
    """`{buildid: [(signature, count, installs), ...]}` -> `{signature: {day: {...}}}`.

    Every signature carries every day in the window (as `copy.deepcopy(base)` does in
    `get_new_signatures`), because a day missing from one signature's series is precisely the
    zero this feature is about."""
    days = {}
    for bid in per_build:
        when = utils.get_build_date(bid)
        days[bid] = datetime(when.year, when.month, when.day)
    signatures = {sgn for rows in per_build.values() for sgn, _, _ in rows}
    data = {}
    for sgn in signatures:
        numbers = {}
        for bid, day in days.items():
            numbers.setdefault(
                day, {"count": 0, "bids": {}, "installs": {}}
            )["bids"][utils.get_build_date(bid)] = 0
        for bid, rows in per_build.items():
            for name, count, installs in rows:
                if name != sgn:
                    continue
                day = days[bid]
                when = utils.get_build_date(bid)
                numbers[day]["count"] += count
                numbers[day]["bids"][when] = count
                numbers[day]["installs"][when] = installs
        data[sgn] = numbers
    return data


class TestShiftPerChannel(unittest.TestCase):
    """`datacollector`'s baseline length (`shift`) is `get_ndays()` = 3 on nightly and 1
    everywhere else, and NOTHING in the suite exercised it (`grep -rn shift tests/` found one
    unrelated comment). It is the value that decides which build-days are testable at all:
    `utils.evaluate_days` never tests `i < ndays`.

    On beta that interacts directly with `get_last_versions(n=3)`: a 3-build window has
    exactly two testable days, so losing one build-day to item 8's floor costs a third of the
    testable surface — and a 3-build window on NIGHTLY's shift would have none."""

    def _shift_for(self, channel):
        seen = {}

        def fake(numbers, ndays, *args, **kwargs):
            seen["ndays"] = ndays
            return {}, False, []

        with mock.patch.object(dc.utils, "evaluate_days", side_effect=fake):
            _collect(
                {B10[0]: {"OOM | small": (236, 180)}},
                channel=channel,
                builds=_WINDOW,
            )
        return seen["ndays"]

    def test_beta_uses_a_one_build_day_baseline(self):
        self.assertEqual(self._shift_for("beta"), 1)

    def test_nightly_uses_the_configured_ndays(self):
        """Not the literal 3 — the knob. `backward_lookup_ndays` is also the buildhub
        backfill and the regressor pushlog window, so it moves for reasons unrelated to this
        one and the wiring has to follow it."""
        self.assertEqual(self._shift_for("nightly"), config.get_ndays())
        self.assertEqual(config.get_ndays(), 3)

    def test_release_is_on_the_off_nightly_side(self):
        self.assertEqual(self._shift_for("release"), 1)

    def test_two_of_three_build_days_are_testable_on_beta(self):
        """Indices 1 and 2. Index 0 has no baseline at all, so it is the untestable prefix —
        recorded, never selected."""
        numbers = _series([(B10[0], 236), (MERGE_B1[0], 0), (SHIPPED_B1[0], 40)])
        _, _, records = utils.evaluate_days(numbers, 1, 6, 10, 3)
        self.assertEqual(
            [(r["index"], r["evaluable"]) for r in records],
            [(0, False), (1, True), (2, True)],
        )

    def test_no_build_day_is_testable_in_a_three_day_nightly_window(self):
        """Same three build-days at nightly's shift: `i >= 3` is never true, so a 3-build
        window is entirely prefix. This is why nightly's build window is
        `nightly_window_ndays` = 21 and not `get_ndays()` = 3, and why a beta-sized window
        cannot simply be reused there."""
        numbers = _series([(B10[0], 236), (MERGE_B1[0], 0), (SHIPPED_B1[0], 40)])
        picked, _, records = utils.evaluate_days(numbers, config.get_ndays(), 1, 3, 3)
        self.assertEqual(picked, {})
        self.assertEqual([r["evaluable"] for r in records], [False, False, False])

    def test_a_one_day_baseline_compares_against_the_previous_build_day_only(self):
        """The consequence of shift=1 that a reader has to know: the baseline is a single
        build-day, so `max(before)` IS the previous build-day's count. A quiet day two builds
        back cannot rescue the comparison, and a quiet day one build back is the entire
        baseline — which is the mechanism item 8 defuses."""
        numbers = _series([(B10[0], 236), (MERGE_B1[0], 0), (SHIPPED_B1[0], 40)])
        _, _, records = utils.evaluate_days(numbers, 1, 6, 10, 3)
        self.assertEqual(records[1]["baseline"], [236])
        self.assertEqual(records[2]["baseline"], [0])
        self.assertTrue(records[2]["spiked"])   # from zero, ungated by floor/ratio


def _series(pairs):
    """`[(buildid, count), ...]` -> one signature's `{day: {...}}` series."""
    numbers = {}
    for bid, count in pairs:
        when = utils.get_build_date(bid)
        numbers[datetime(when.year, when.month, when.day)] = {
            "count": count,
            "bids": {when: count},
            "installs": {when: max(count, 1)},
        }
    return numbers


if __name__ == "__main__":
    unittest.main()
