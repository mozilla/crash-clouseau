# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Plan #18 §6.3 T8 — the two places where adding a SECOND agent channel can quietly take
# coverage away from the first one, plus the kill switch that lets an operator undo it without a
# deploy. Work items 10 and 28.
#
# 1. THE PROTO-SIGNATURE CLUSTER IS PER CHANNEL (item 10). `uuids` has no channel column and
#    `protohash = utils.hash(proto_signature)` (models.py) is the same string on every channel, so
#    before `_cluster_dossiers` took a channel whichever channel was analysed FIRST closed the
#    cluster for the other one, permanently. Measured blast radius: of the 224 beta
#    (signature, proto) clusters behind 40 emulated selections, 37 (16.5%) also occur verbatim on
#    nightly within 60 days — an upper bound, since the gate additionally needs the nightly
#    cluster to hold a `status=done` dossier (§7.5). The MIRROR direction is the one that matters
#    most here: 187 of the 224 clusters (6.2/day) are beta-only, so a beta dossier closing a
#    NIGHTLY cluster would mean enabling beta silently reduced desktop coverage.
#    Both predicates are covered in both directions, because there are two of them —
#    `UUID.proto_already_analyzed` (the per-crash gate) and `UUID.untriaged` (the sweeper's
#    correlated version of the same question). They share `_cluster_dossiers` precisely so they
#    cannot disagree, and a test that only exercised one would not notice if they did.
#
# 2. THE SWEEP CAP IS PER CHANNEL (item 28). `agent.sweep.max_per_channel` (2) inside the
#    per-tick `max_per_run` (3). The sweep is the one periodic job that spends money unattended
#    (~$1-3 a crash) and prod 2026-08-12 measured 86 untriaged crashes arriving at ~3.4/day in
#    BURSTS OF 8 — so a tick full of one channel's backlog is the normal shape, not a
#    hypothetical, and beta's own selections are 48% concentrated in the 4 days after a merge.
#    `tests/test_sweep_untriaged.py:302-313` describes this scenario in its own comment but keeps
#    passing with beta enabled (its assertion is about the channel FILTER, which is exactly what
#    a beta rollout removes), so it is not a tripwire for it. These tests are, and they run the
#    REAL query end to end — the cap and the `SweepMark` cursor interact, and mocking the
#    candidate list would hide the interaction.
#
# 3. `AGENT_CHANNELS` (item 28). `get_agent_channels` was the ONE of the ~14 canary levers with
#    no environment override, so turning a channel's triage on or off needed a DEPLOY — and a
#    deploy kills every in-flight ~20-minute run at ~$3 each. `AUTOFILE_BUGS=0` is global, so
#    without this the only way to stop beta was to stop nightly too.
#
# Postgres-gated: the cluster predicate is a correlated JSONB subquery and the cap test drives the
# real `untriaged` query, neither of which sqlite can answer. Make your OWN database so parallel
# runs cannot collide:
#   docker exec clouseau_test_pg psql -U clouseau -d clouseau_test -c 'CREATE DATABASE betasweep'
#   DATABASE_URL=postgresql://clouseau:passwd@localhost:55432/betasweep \
#     REDIS_URL=redis://localhost:6379/0 uv run python -m unittest tests.test_beta_sweep_and_dedup
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import config, db, models, utils  # noqa: E402
from crashclouseau.agent import orchestrator as orch  # noqa: E402


def _is_postgres():
    try:
        return db.engine.dialect.name == "postgresql"
    except Exception:
        return False


# The verdict payload a SUCCESSFUL run leaves behind — the only kind that is allowed to close a
# cluster (a broken run, or one suppressed for a reason specific to its own report, is not).
_DONE = {"dossier": {"verdict": {"decision": "culprit"}}}
_BROKEN = {"dossier": {"verdict": {
    "decision": "abstain",
    "abstain_reason": "dossier validation failed (verdict unusable): 1 malformed field: x",
}}}


class _TwoChannelFixture(unittest.TestCase):
    """A nightly build and a beta build, and crashes on each. Own buildid + version marker per
    subclass so a failure cleans up by identity: deleting the build cascades to its uuids and
    their dossiers, which is the whole teardown. Without that, one failure leaves a build behind
    and every later setUp collides on uix_builds — one failure becomes ten."""

    SIG = "Beta::SweepAndDedup"
    BUILDID = datetime(2026, 8, 20, 9, 17, tzinfo=timezone.utc)
    NIGHTLY_VERSION = "9.9.9an1"  # <= 10 chars (builds.version)
    BETA_VERSION = "9.9.9bt1"
    UUID_PREFIX = "t8-"

    def setUp(self):
        models.create()
        self._clean()
        self.sig = models.Signature.get_id(self.SIG)
        # Same buildid on both channels: uix_builds is (buildid, product, channel), and using one
        # date proves the split is the CHANNEL and nothing else.
        self.nightly = models.Build(
            self.BUILDID, "Firefox", "nightly", self.NIGHTLY_VERSION, None
        )
        self.beta = models.Build(self.BUILDID, "Firefox", "beta", self.BETA_VERSION, None)
        db.session.add_all([self.nightly, self.beta])
        db.session.commit()

    def _clean(self):
        db.session.rollback()
        # By the unique key, not the version marker: whatever a previous run left behind collides
        # on (buildid, product, channel), so that is what has to go.
        db.session.query(models.Build).filter(
            models.Build.buildid == self.BUILDID,
            models.Build.product == "Firefox",
            models.Build.channel.in_(["nightly", "beta"]),
        ).delete(synchronize_session=False)
        # ...and by uuid name too (cascades to the dossiers): `uuids.uuid` is unique GLOBALLY, so
        # a row this class left on some other build row would collide on the name alone.
        db.session.query(models.UUID).filter(
            models.UUID.uuid.like(self.UUID_PREFIX + "%")
        ).delete(synchronize_session=False)
        db.session.query(models.SweepMark).delete()
        db.session.commit()

    def tearDown(self):
        self._clean()

    def _uuid(self, name, build, proto="proto|frame0|frame1", age_h=48):
        """One ingested, scored crash. `protohash` is computed the way production computes it
        (`utils.hash` of the proto signature) rather than hand-written, because the whole premise
        of item 10 is that that hash is channel-blind — writing the hash by hand would prove
        nothing about the real key."""
        u = models.UUID(name, self.sig, utils.hash(proto), build.id)
        u.useless = False
        u.analyzed = True
        db.session.add(u)
        db.session.commit()
        # `created` has a server default, so backdating past the sweep's grace period is a second
        # write.
        db.session.query(models.UUID).filter(models.UUID.uuid == name).update(
            {"created": datetime.now(timezone.utc) - timedelta(hours=age_h)},
            synchronize_session=False,
        )
        db.session.commit()
        return db.session.query(models.UUID.id).filter(models.UUID.uuid == name).scalar()


@unittest.skipUnless(_is_postgres(), "the cluster predicate needs a disposable Postgres backend")
class TestTheProtoClusterIsPerChannel(_TwoChannelFixture):
    """Item 10. One (signature, proto) cluster, one crash of it on each channel."""

    N = "t8-dedup-n01-aaaa-bbbb-ccccddddeeee"
    B = "t8-dedup-b01-aaaa-bbbb-ccccddddeeee"

    def setUp(self):
        super().setUp()
        self.nid = self._uuid(self.N, self.nightly)
        self.bid = self._uuid(self.B, self.beta)

    def _sweeps(self, channel):
        """What the sweeper would offer on `channel` — its correlated form of the same question
        the gate answers per crash."""
        return [
            r[1] for r in models.UUID.untriaged(0, 21600, 1209600, 10, channels=[channel])
        ]

    def test_a_nightly_dossier_does_not_close_a_beta_cluster(self):
        # 16.5% of beta clusters (37 of 224 behind 40 emulated selections) also occur verbatim on
        # nightly, so this is what decides whether roughly one beta crash in six is ever looked
        # at. A beta filing is a different bug, against a different repo, from a different build,
        # with a different candidate window — the nightly dossier does not answer it.
        models.Dossier.upsert(self.N, payload=_DONE, status="done")
        self.assertFalse(models.UUID.proto_already_analyzed(self.B))
        self.assertEqual(self._sweeps("beta"), [self.B])
        # ...and the nightly crash IS closed, by both predicates: the fix must be a scoping
        # change, not a disabled gate.
        self.assertTrue(models.UUID.proto_already_analyzed(self.N))
        self.assertEqual(self._sweeps("nightly"), [])

    def test_a_beta_dossier_does_not_close_a_nightly_cluster(self):
        # THE MIRROR, and the one that protects desktop coverage: 187 of those 224 clusters
        # (6.2/day) are beta-only, and if a beta dossier could answer for nightly then switching
        # beta on would silently reduce the coverage nightly already had. Same shape as plan #16
        # §6.2's Fenix note.
        models.Dossier.upsert(self.B, payload=_DONE, status="done")
        self.assertFalse(models.UUID.proto_already_analyzed(self.N))
        self.assertEqual(self._sweeps("nightly"), [self.N])
        self.assertTrue(models.UUID.proto_already_analyzed(self.B))
        self.assertEqual(self._sweeps("beta"), [])

    def test_an_unfiltered_sweep_offers_the_sibling_on_the_other_channel(self):
        # The sweeper's channel argument is a spend filter, not the cluster scope. With no filter
        # at all (`AGENT_CHANNELS=""`) the beta crash must still be offered, or the two predicates
        # have drifted apart: the gate says untriaged, the sweeper says triaged.
        models.Dossier.upsert(self.N, payload=_DONE, status="done")
        self.assertEqual(
            [r[1] for r in models.UUID.untriaged(0, 21600, 1209600, 10)], [self.B]
        )

    def test_a_same_channel_sibling_still_closes_the_cluster(self):
        # The anti-regression control, and the reason the fix had to be a channel ARGUMENT rather
        # than a dropped filter: the same crash recurring on a newer build of the same channel is
        # what the gate exists to stop paying ~$3 for. It is what turns beta's projected 7.6
        # UUIDs/day into 4.2-5.8 dossiers/day (§4).
        sib = "t8-dedup-b02-aaaa-bbbb-ccccddddeeee"
        self._uuid(sib, self.beta)
        models.Dossier.upsert(sib, payload=_DONE, status="done")
        self.assertTrue(models.UUID.proto_already_analyzed(self.B))
        self.assertEqual(self._sweeps("beta"), [])
        # ...on a build of its own channel, whatever the buildid: the dedup is deliberately
        # across builds.
        self.assertFalse(models.UUID.proto_already_analyzed(self.N))

    def test_a_different_cluster_on_the_other_channel_is_not_confused_for_a_sibling(self):
        # Guards the correlation itself, and it is the ONLY test here that does.
        # `_cluster_dossiers` is spliced into `untriaged` as a correlated EXISTS, so if the
        # sibling `uuids` join loses its alias then `UUID.signatureid == UUID.signatureid`
        # resolves both sides to the outer row, is trivially true, and the predicate matches any
        # done dossier at all — the sweep would then find nothing, ever, and every crash would
        # look triaged. Mutation-checked: un-aliasing that join fails this test and NOTHING else
        # in the class (the other alias, on the sibling's `builds` row, is what the two mirror
        # tests above catch).
        other = "t8-dedup-n02-aaaa-bbbb-ccccddddeeee"
        self._uuid(other, self.nightly, proto="proto|somewhere|else")
        models.Dossier.upsert(other, payload=_DONE, status="done")
        self.assertFalse(models.UUID.proto_already_analyzed(self.B))
        self.assertFalse(models.UUID.proto_already_analyzed(self.N))
        self.assertEqual(
            sorted(r[1] for r in models.UUID.untriaged(0, 21600, 1209600, 10)),
            sorted([self.B, self.N]),
        )

    def test_broken_nightly_runs_do_not_spend_the_beta_clusters_failure_budget(self):
        # `agent.proto_max_unusable` (2) closes a cluster that keeps BREAKING, loudly, so that a
        # stack which reliably fails validation is not re-paid for forever. It counts the same
        # channel-scoped set, and it had better: two nightly failures declaring the beta cluster
        # "triaged" would be the cross-channel loss again, wearing the disguise of a cost bound.
        cap = config.get_agent_proto_max_unusable()
        self.assertEqual(cap, 2, "the fixture below writes exactly `cap` broken runs")
        for i in range(cap):
            name = "t8-dedup-n1{}-aaaa-bbbb-ccccddddeeee".format(i)
            self._uuid(name, self.nightly, proto="proto|frame0|frame1")
            models.Dossier.upsert(name, payload=_BROKEN, status="done")
        with self.assertLogs(level="WARNING"):
            self.assertTrue(models.UUID.proto_already_analyzed(self.N))
        self.assertFalse(models.UUID.proto_already_analyzed(self.B))
        self.assertEqual(self._sweeps("beta"), [self.B])


@unittest.skipUnless(_is_postgres(), "the sweep cap test drives the real untriaged query")
class TestTheSweepCapIsPerChannel(_TwoChannelFixture):
    """Item 28, end to end: the real `untriaged` query, the real `SweepMark`, the cap in
    `sweep_untriaged_crashes`. Only the RQ queue and `enqueue_agent` are mocked, because the
    cursor and the cap only interact through the rows the query actually returns.

    Fixture shape: three beta candidates OLDER (lower uuid id) than two nightly ones, which is
    the starvation scenario item 28 names — beta's selections are 48% concentrated in the 4 days
    after a merge, and `untriaged` orders by uuid id with one global `LIMIT max_per_run`."""

    # Its own buildid, so a failure in the other class cannot leave rows that break this one.
    BUILDID = datetime(2026, 8, 21, 9, 17, tzinfo=timezone.utc)
    NIGHTLY_VERSION = "9.9.9an2"
    BETA_VERSION = "9.9.9bt2"

    def setUp(self):
        super().setUp()
        self.betas, self.nightlies = [], []
        for i in range(3):
            name = "t8-cap-b0{}-aaaa-bbbb-ccccddddeeee".format(i)
            self._uuid(name, self.beta, proto="proto|beta|{}".format(i))
            self.betas.append(name)
        for i in range(2):
            name = "t8-cap-n0{}-aaaa-bbbb-ccccddddeeee".format(i)
            self._uuid(name, self.nightly, proto="proto|nightly|{}".format(i))
            self.nightlies.append(name)

    def _tick(self, channels=("nightly", "beta"), **cfg_over):
        """One clock tick of the sweep. Returns (n, [uuids enqueued], [log lines])."""
        cfg = dict(config.get_agent_sweep(), **cfg_over)
        enqueued = []
        with mock.patch.object(orch.config, "get_agent_sweep", return_value=cfg), \
                mock.patch.object(orch.config, "get_agent_channels",
                                  return_value=list(channels)), \
                mock.patch.object(orch.worker, "get_queue"), \
                mock.patch.object(orch, "_live_job_uuids", return_value=set()), \
                mock.patch.object(orch, "enqueue_agent",
                                  side_effect=lambda u, c=None, **k: enqueued.append(u)), \
                self.assertLogs(level="INFO") as logs:
            n = orch.sweep_untriaged_crashes()
        return n, enqueued, logs.output

    def test_one_channel_cannot_take_more_of_a_tick_than_the_per_channel_cap(self):
        # Three beta candidates and two nightly ones, `max_per_channel` 2 against `max_per_run`
        # 3: beta takes two and the freed slot goes to the oldest NIGHTLY candidate. That last
        # part is the whole point of a cap -- see the sibling test that measured the first
        # version doing "-1 beta run, +0 nightly runs, ever".
        n, enqueued, _ = self._tick()
        self.assertEqual(n, 3)
        self.assertEqual(enqueued[:2], self.betas[:2])
        self.assertIn(self.nightlies[0], enqueued)

    def test_the_cap_is_inert_when_only_one_channel_is_waiting(self):
        """A cap that fires with one channel in play protects nobody and costs a third of the
        drain rate. Prod's measured shape is a burst of 8 on ONE channel, and the "86-crash
        backlog drains in about a week" figure is 3/tick x 4 ticks = 12/day; at 2/tick it is
        8/day. So the cap binds on the fetched candidates' actual channel spread, not on the
        config value alone."""
        n, enqueued, _ = self._tick(channels=["beta"])
        self.assertEqual(n, 3)
        self.assertEqual(enqueued, self.betas[:3])

    def test_a_candidate_deferred_by_the_cap_is_logged_by_uuid_and_channel(self):
        # The mark now STOPS at the first deferred candidate, so a defer is genuinely a defer and
        # the sibling test pins that it comes back next tick. The log line stays required anyway:
        # it is the only way to see that a tick was capped at all, and "the sweep quietly did
        # fewer runs than it could have" is otherwise indistinguishable from an empty backlog.
        _, _, logs = self._tick()
        deferred = [line for line in logs if "deferred" in line]
        self.assertEqual(len(deferred), 1, logs)
        self.assertIn(self.betas[2], deferred[0])
        self.assertIn("beta", deferred[0])

    def test_the_kill_switch_stops_beta_spend_without_stopping_nightly(self):
        # `AGENT_CHANNELS=nightly` (item 28): the channel filter is applied in the QUERY, so the
        # tick is spent on crashes the agent will accept instead of on three beta rows that
        # `enqueue_agent` would drop while the log claimed three were swept.
        n, enqueued, _ = self._tick(channels=["nightly"])
        self.assertEqual((n, enqueued), (2, self.nightlies))

    def test_the_slot_the_cap_takes_from_beta_is_spent_on_a_waiting_nightly_candidate(self):
        """FIXED (item 28). It could not, before: the cap could not give the slot to anyone.

        `untriaged` applies ONE global `LIMIT max_per_run` in SQL, ordered by uuid id, so the
        candidate list is fixed at three beta rows BEFORE the per-channel cap in
        `sweep_untriaged_crashes` runs. Capping beta at 2 therefore cannot admit the nightly
        candidates waiting behind them — it just does one run fewer. Measured: tick enqueues
        `[cap-b00, cap-b01]`, zero nightly, and the mark advances to cap-b02's id, so tick 2
        serves the nightly pair — which is exactly what it would have done with the cap inert.
        Net effect of the cap on this fixture: -1 beta run, +0 nightly runs, ever.
        Fix shape: fetch per channel (or over-fetch `max_per_run * len(channels)`) so there is a
        row for the freed slot to go to."""
        n, enqueued, _ = self._tick()
        self.assertEqual(n, 3)
        self.assertTrue(
            set(enqueued) & set(self.nightlies),
            "the cap denied beta a slot and no nightly candidate got it: {}".format(enqueued),
        )

    def test_a_candidate_deferred_by_the_cap_is_offered_again_on_the_next_tick(self):
        """FIXED (item 28). Before the fix, "deferred" was permanent.

        `SweepMark` advances to `max(candidate ids)` — every row CONSIDERED, including the ones
        the cap declined — and `untriaged` filters `UUID.id > after_id`. So the deferred crash is
        never offered again by any later tick: it is dropped, not deferred, and the sweep is the
        only mechanism that would ever have looked at it (the reaper works from dossier rows and
        there are none). A candidate the cap declined was not examined and should stay behind the
        cursor."""
        self._tick()
        _, second, _ = self._tick()
        self.assertIn(self.betas[2], second)

    def test_a_single_channel_burst_still_fills_the_whole_tick(self):
        """FIXED (item 28). Before the fix the cap fired with only ONE channel in play, which is
        the only shape prod has ever seen.

        `config._SWEEP_DEFAULTS` says "with one channel it is inert (3 of 3)", but
        `config/global.json` ships `max_per_channel: 2`, and `per_channel` is read from the
        config, not from the number of live channels — so a nightly-only backlog is capped at 2
        of 3 too. Prod 2026-08-12 measured the untriaged backlog arriving in BURSTS OF 8 on one
        channel, and the 3/tick figure is what "the 86-crash backlog drains in about a week"
        (12/day) was priced on; at 2/tick it is 8/day and one crash in three is dropped for good
        (see the sibling test). That is desktop coverage lost to a beta work item, before beta is
        even switched on."""
        # One more nightly crash, so that a tick's three candidates are all on ONE channel:
        # ids 6, 7, 8 against `max_per_run` 3.
        self._uuid("t8-cap-n02-aaaa-bbbb-ccccddddeeee", self.nightly, proto="proto|nightly|2")
        # `untriaged` pages in id order, so clear the older beta rows out of the way first; the
        # cursor then starts the second tick on the nightly block.
        self._tick()
        n, enqueued, _ = self._tick(channels=["nightly"])
        self.assertEqual(
            n, 3,
            "a tick with a single channel in it still lost a slot to the per-channel cap, and "
            "the deferred crash is now behind the cursor for good: {}".format(enqueued),
        )


class TestTheShippedSweepCap(unittest.TestCase):
    """Deliberately NOT Postgres-gated: if these two numbers were ever equal the per-channel cap
    would be inert and every cap test in this file would pass vacuously — which is the one way
    this file could go quietly useless, so the guard has to run on every backend."""

    def test_the_shipped_per_channel_cap_is_smaller_than_the_per_tick_total(self):
        cfg = config.get_agent_sweep()
        self.assertEqual((cfg["max_per_run"], cfg["max_per_channel"]), (3, 2))


class TestAgentChannelsEnvOverride(unittest.TestCase):
    """Item 28's kill switch. Backend-agnostic: no row is read.

    `AGENT_CHANNELS` is the flag that decides whether the pipeline SPENDS MONEY on a channel, and
    it was the one canary lever of ~14 with no environment override — so switching beta triage on
    or off required a DEPLOY, which kills every in-flight ~20-minute run at ~$3 each. It also has
    to be per channel: `AUTOFILE_BUGS=0` is global, so without this the only way to stop beta was
    to stop nightly too."""

    def _channels(self, value=None):
        with mock.patch.dict(os.environ):
            if value is None:
                os.environ.pop("AGENT_CHANNELS", None)
            else:
                os.environ["AGENT_CHANNELS"] = value
            return config.get_agent_channels()

    def _enqueues(self, channel, env):
        """Does `enqueue_agent` put a job on the queue for a crash on `channel`? The behaviour
        the override exists for; the config value on its own proves nothing."""
        q = mock.MagicMock()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(orch.config, "get_agent_enabled", return_value=True), \
                mock.patch.object(orch, "_proto_already_triaged", return_value=False), \
                mock.patch.object(orch.worker, "get_queue", return_value=q):
            orch.enqueue_agent("u-1", channel)
        return q.enqueue_call.called

    def test_agent_channels_can_be_overridden_from_the_environment(self):
        # Unset -> the config, whatever the config happens to say (the shipped VALUE is
        # tests/test_shipped_channels.py's business, per plan #18 §6.3 T10).
        self.assertEqual(self._channels(), config.get_agent().get("channels", ["nightly"]))
        self.assertEqual(self._channels("nightly beta"), ["nightly", "beta"])
        self.assertEqual(self._channels("nightly"), ["nightly"])
        # Space-separated, same shape as INGEST_CHANNELS — including a value a human typed.
        self.assertEqual(self._channels("  nightly   beta  "), ["nightly", "beta"])

    def test_an_empty_override_means_no_filter_not_no_triage(self):
        # Matching the config's own empty-list semantics. The other reading — "" meaning "unset",
        # or "" meaning "no channel at all" — would make this a foot-gun in opposite directions:
        # silently ignoring the operator, or silently stopping ALL triage while
        # `get_agent_enabled()` still says the agent is on.
        self.assertEqual(self._channels(""), [])
        for channel in ("nightly", "beta", "release"):
            self.assertTrue(
                self._enqueues(channel, {"AGENT_CHANNELS": ""}),
                "an empty AGENT_CHANNELS must not filter {} out".format(channel),
            )

    def test_the_override_can_stop_beta_without_stopping_nightly(self):
        self.assertFalse(self._enqueues("beta", {"AGENT_CHANNELS": "nightly"}))
        self.assertTrue(self._enqueues("nightly", {"AGENT_CHANNELS": "nightly"}))

    def test_the_override_can_arm_beta_without_a_deploy(self):
        self.assertTrue(self._enqueues("beta", {"AGENT_CHANNELS": "nightly beta"}))

    def test_the_sweep_asks_the_query_for_the_overridden_channels(self):
        # The sweep is the one caller that reads the channel set itself rather than being handed
        # one, and it passes it INTO the query so the per-tick cap is spent on crashes the agent
        # will accept. An override the sweep did not honour would leave the ~6-hourly job
        # spending on a channel the operator had just switched off.
        with mock.patch.dict(os.environ, {"AGENT_CHANNELS": "beta"}), \
                mock.patch.object(orch.models.SweepMark, "get", return_value=0), \
                mock.patch.object(orch.models.UUID, "untriaged", return_value=[]) as q:
            orch.sweep_untriaged_crashes()
        self.assertEqual(q.call_args.kwargs["channels"], ["beta"])


if __name__ == "__main__":
    unittest.main()
