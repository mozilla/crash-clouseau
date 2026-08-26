# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Plan #18 §6.3 T1 — the four places a channel label leaves the process: a Socorro query, an
# MCP tool context, a searchfox tree, and the provenance line of a filed bug.
#
# WHY THIS FILE EXISTS: `aurora` IS beta. Socorro files Developer Edition under
# `release_channel=aurora`, and that is 36-41% of the channel — three independent
# measurements agree (36.6% = 16,275 of 44,488 reports over 30 d; 38.5% over 08-18..25; 41%
# over 364 d in `sigage.hardware_noise`'s own docstring). So a query keyed on OUR label
# `"beta"` silently answers about two thirds of the channel, and `utils.get_search_channel`
# exists to widen it — it was applied at 6 of 13 `release_channel` sites and **had no test
# anywhere** (plan #18 §3 item 4: `grep -rn get_search_channel tests/` → 0 hits before this
# file). Per-site cost, measured: the filed bug's crash count on build 20260819090452
# (155.0b2) reads 1,600 instead of 2,149 over the top 12 beta signatures by install
# cardinality = **25.5% low overall, 0.0% to 72.4% per signature**; the bad-machine gate's
# install history returns nothing at all for a DevEdition crash.
#
# The other three are the same mistake in three other shapes: `SearchfoxCtx` was the ONLY one
# of the five MCP contexts with no `channel` field, so every `calls_from`/`define`/`search`/
# `field_layout` and every permalink cited in a beta bug came from firefox-main tip (code that
# may never have been in the beta build, missing the uplifts that were); and `_PROVENANCE` was
# a constant reading "analyses nightly crashes", printed at the bottom of a beta filing, in the
# one paragraph whose whole job is to tell a reader what wrote the analysis. `grep
# "analyses nightly crashes\|_PROVENANCE" tests/` → 0 hits before this file.
#
# Three tests are `@unittest.expectedFailure`: they assert behaviour the tree does not have
# yet, each one commented with the defect it names. They are tripwires, not wishes — an
# unexpected success fails the suite, so fixing the defect forces the xfail off.
#
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_beta_channel_wiring
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import asyncio  # noqa: E402
import dataclasses  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import datacollector as dc  # noqa: E402
from crashclouseau import machine, population, report_bug, searchfox, utils  # noqa: E402
from crashclouseau.agent import orchestrator as orch  # noqa: E402
from crashclouseau.agent import second_opinion, triage  # noqa: E402
from crashclouseau.agent.tools import searchfox_cg as sfcg  # noqa: E402
from crashclouseau.agent.tools import socorro as socorro_tools  # noqa: E402
from crashclouseau.searchfox import SearchfoxNoResult  # noqa: E402

# What every query about a beta crash must ask Socorro. The ORDER is pinned because this goes
# into a URL parameter and the two twin queries in `report_bug` are compared byte-for-byte
# against each other.
BETA = ["beta", "aurora"]

# 155.0b2. Its buildid is used as a string (`models.UUID.get_info` returns
# `utils.get_buildid(...)`) and as a datetime, depending on the caller.
BUILDID = "20260819090452"
BUILD_DT = datetime(2026, 8, 19, 9, 4, 52, tzinfo=timezone.utc)
SIGNATURE = "OOM | unknown | js::AutoEnterOOMUnsafeRegion::crash_impl"


def _capturing_supersearch(seen, payload=None):
    """Stand-in for ``libmozdata.socorro.SuperSearch`` that records the outgoing params.

    Both call shapes the tree uses: ``params=`` (one query) and ``queries=[Query(...)]``
    (``population.for_crash`` runs its two lookups in parallel). ``URL`` is a class attribute
    on the real thing and `population` reads it to build its ``Query`` objects. ``payload``,
    when given, is handed to the caller's handler on ``wait()`` so the code under test runs
    its whole path instead of its error branch.
    """

    class FakeSearch:
        URL = "https://crash-stats.mozilla.org/api/SuperSearch/"

        def __init__(self, params=None, handler=None, handlerdata=None, queries=None, **kwargs):
            self._handlers = []
            for query in queries or []:
                seen.append(dict(query.params or {}))
                self._handlers.append((query.handler, query.handlerdata))
            if params is not None:
                seen.append(dict(params))
                self._handlers.append((handler, handlerdata))

        def wait(self):
            if payload is None:
                return None
            for handler, handlerdata in self._handlers:
                if handler is not None:
                    handler(payload, handlerdata)

    return FakeSearch


class TestGetSearchChannel(unittest.TestCase):
    """`utils.get_search_channel` — the whole beta fix rests on it and nothing tested it.

    Denominator for the widening: beta 28,213 reports + aurora 16,275 = 44,488 over 30 days,
    so `aurora` is 36.6% of what a query about "beta" should see (plan #18 §2.2).
    """

    def test_beta_means_beta_and_developer_edition(self):
        self.assertEqual(utils.get_search_channel("beta"), BETA)
        # A list of the two Socorro labels, not a comma-joined string and not a regex:
        # SuperSearch ORs a repeated parameter, and anything else is a term that matches no
        # report at all.
        got = utils.get_search_channel("beta")
        self.assertIsInstance(got, list)
        self.assertEqual(len(got), 2)

    def test_every_other_channel_is_passed_through_unchanged(self):
        """Widening is beta-only. Nightly has no second label, and release's `esr` siblings
        are separate populations we must not fold in.

        `aurora` in that list is the asymmetry that matters: the map is keyed on OUR stored
        label (`nightly`/`beta`/`release`), so handing it Socorro's raw `aurora` widens
        nothing and searches DevEdition alone. Every caller therefore has to pass the DB
        channel — see `TestTheSeedAsksAboutTheChannelTheCrashIsOn`, where one does not."""
        for channel in ("nightly", "release", "esr", "aurora", "esr140"):
            self.assertEqual(utils.get_search_channel(channel), channel)

    def test_the_channel_it_was_given_is_never_dropped(self):
        """The invariant that survives a re-tune: whatever comes back, the caller's own label
        is still in it. A widening that REPLACED `beta` (say, with `aurora` alone) would look
        plausible and answer about a third of the channel."""
        for channel in ("nightly", "beta", "release"):
            got = utils.get_search_channel(channel)
            self.assertIn(channel, got if isinstance(got, list) else [got])

    def test_a_missing_channel_stays_missing(self):
        """`machine`, `population` and the crash_stats tool guard with `if channel:` and drop
        the parameter when it is falsy — an unknown channel must search ALL channels rather
        than silently becoming a beta query."""
        for channel in (None, ""):
            self.assertFalse(utils.get_search_channel(channel))


class TestEverySocorroQueryUsesGetSearchChannel(unittest.TestCase):
    """Plan #18 §3 item 4's six sites, each asserted at the outgoing HTTP parameter.

    Patching the Socorro/HTTP layer rather than the helper is the point: these were six
    one-line edits and the failure they fix is invisible unless you look at what left the
    process.
    """

    def test_the_bad_machine_gates_install_history_covers_developer_edition(self):
        """`machine.install_history` (`machine.py`). Before item 4 a DevEdition crash asked
        `release_channel=beta`, matched none of its own reports (they are all `aurora`),
        returned the all-None `empty` dict and the bad-machine gate could not fire at all on
        36-41% of the channel."""
        for channel, expected in (("beta", BETA), ("nightly", "nightly")):
            seen = []
            with mock.patch.object(machine.socorro, "SuperSearch",
                                   _capturing_supersearch(seen)):
                machine.install_history("1755500000", product="Firefox", channel=channel)
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0]["release_channel"], expected, channel)

    def test_the_population_panels_denominator_covers_developer_edition(self):
        """`population.for_crash` (`population.py`) — the crashstack.html panel whose whole
        subject is how CONCENTRATED a signature's installations are. A denominator missing a
        third of the channel reads as concentration that is not there.

        Two queries go out; only the signature facet query is channel-scoped (the second asks
        about this one crash's own uuid), so the channel term must appear exactly once."""
        for channel, expected in (("beta", BETA), ("nightly", "nightly")):
            seen = []
            uuid_info = {"uuid": "u-1", "signature": SIGNATURE, "channel": channel,
                         "buildid": BUILD_DT, "product": "Firefox"}
            with mock.patch.object(population.socorro, "SuperSearch",
                                   _capturing_supersearch(seen)):
                population.for_crash(uuid_info)
            scoped = [p for p in seen if "release_channel" in p]
            self.assertEqual(len(scoped), 1, seen)
            self.assertEqual(scoped[0]["release_channel"], expected, channel)

    def test_the_second_opinions_crash_stats_tool_covers_developer_edition(self):
        """`agent/tools/socorro.crash_stats` — the ONLY crash-stats instrument the blind
        second opinion has. Under-counting hits both the printed total and the
        `release_channel` facet the model reads to judge which channels are affected."""
        history = {"first_seen": None, "first_seen_channel": None, "total": 0,
                   "total_other_channels": 0}
        for channel, expected in (("beta", BETA), ("nightly", "nightly")):
            seen = []
            ctx = socorro_tools.SocorroCtx(product="Firefox", channel=channel)
            with mock.patch.object(socorro_tools.socorro, "SuperSearch",
                                   _capturing_supersearch(seen)), \
                 mock.patch.object(socorro_tools.sigage, "signature_history",
                                   return_value=history):
                out = asyncio.run(socorro_tools.crash_stats(ctx, SIGNATURE))
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0]["release_channel"], expected, channel)
            # The tool still answers when the facet lookup is empty; it must not raise into
            # the agent loop.
            self.assertIn(SIGNATURE, out)

    def test_the_buildid_to_changeset_vote_covers_developer_edition(self):
        """`datacollector.get_changeset` — resolves a buildid to its revision by VOTING on the
        `topmost_filenames` facet, so a third of the reports missing is a third of the ballots
        missing. A beta build and its DevEdition twin share the buildid (58 of 59 since
        2026-04-01, identical revisions)."""
        for channel, expected in (("beta", BETA), ("nightly", "nightly")):
            seen = []
            with mock.patch.object(dc.socorro, "SuperSearch", _capturing_supersearch(seen)):
                dc.get_changeset(BUILD_DT, channel, "Firefox")
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0]["release_channel"], expected, channel)

    def test_the_two_bug_comment_queries_ask_the_same_channel(self):
        """The twins: `report_bug.get_info_helper`'s hand-drafted `enter_bug.cgi` link and
        `report_bug.fetch_signature_stats`'s autofiled comment compute the SAME number for the
        same bug ("There are N crashes from M installations"). If only one is widened the two
        comments quote different counts for one crash — 25.5% apart overall and up to 72.4% on
        a single signature (build 20260819090452)."""
        for channel, expected in (("beta", BETA), ("nightly", "nightly")):
            helper, stats = self._twin_params(channel)
            self.assertEqual(helper["release_channel"], expected, channel)
            self.assertEqual(stats["release_channel"], expected, channel)
            self.assertEqual(helper["release_channel"], stats["release_channel"])

    # ------------------------------------------------------------------ #
    # DEFECT (pre-existing, NOT introduced by plan #18): the twins are widened together but
    # they were never the same query. `fetch_signature_stats` anchors `date >= <build day>`;
    # `get_info_helper` sends no `date` at all, and SuperSearch with no date silently answers
    # only the last ~8 days of REPORT dates (measured 2026-08-11, pinned in
    # tests/test_report_bug_date_anchor.py: a bare nightly query answered 2026-08-04..08-11
    # and nothing older; a 2026-07-26 uuid returned 0 hits bare, 1 anchored). So on any build
    # older than that window the hand-drafted comment says "There are 0 crashes (from 0
    # installations)" while the filed comment says 2,149 — the exact failure the anchor was
    # added to fix, still live in the twin. Reachable on nightly today (21-day window) and on
    # beta as soon as an analysis backlog outlives the window.
    # ------------------------------------------------------------------ #
    def test_the_two_bug_comment_queries_are_the_same_query(self):
        helper, stats = self._twin_params("beta")
        self.assertEqual(helper, stats)

    @staticmethod
    def _twin_params(channel):
        """``(get_info_helper's params, fetch_signature_stats' params)`` for one crash."""
        info = {"buildid": BUILDID, "product": "Firefox", "channel": channel,
                "version": "155.0b2", "signature": SIGNATURE}
        # The Socorro report page the hand draft scrapes its pre-filled comment out of.
        page = ('<a href="https://bugzilla.mozilla.org/enter_bug.cgi?keywords=crash&amp;'
                'comment=Crash+report&amp;product=Firefox&amp;component=General">bug</a>')
        facets = {"facets": {"build_id": [{
            "term": int(BUILDID), "count": 2149,
            "facets": {"install_time": [{"term": 1}, {"term": 2}]}}]}}
        seen = []

        class Response:
            text = page

            def json(self):
                return facets

        def fake_get(url, **kwargs):
            seen.append((url, kwargs.get("params")))
            return Response()

        class Waiter:
            def wait(self):
                return None

        with mock.patch.object(report_bug.models.UUID, "get_info", return_value=dict(info)), \
             mock.patch.object(report_bug.models.Node, "get_bugid", return_value=0), \
             mock.patch.object(report_bug.buginfo, "get_bugs", return_value=(Waiter(), {})), \
             mock.patch.object(report_bug.net, "get", fake_get):
            asyncio.run(report_bug.get_info_helper("u-1", "0123456789ab"))
        helper = [p for url, p in seen if "SuperSearch" in url and p]
        assert len(helper) == 1, seen

        stats_seen = []
        report_bug._STATS_CACHE.clear()
        with mock.patch.object(report_bug.socorro, "SuperSearch",
                               _capturing_supersearch(stats_seen, payload=facets)):
            first, counts = report_bug.fetch_signature_stats("u-1", dict(info))
        report_bug._STATS_CACHE.clear()
        assert len(stats_seen) == 1, stats_seen
        # Both twins really did read the same 2,149 crashes out of the same response shape;
        # what is compared below is the question they asked to get there.
        assert counts == {"count": 2149, "installs": 2}, counts
        return helper[0], stats_seen[0]


class TestTheSeedAsksAboutTheChannelTheCrashIsOn(unittest.TestCase):
    """The caller of site 1. `install_history` takes a channel; what the seed HANDS it is the
    other half of item 4."""

    RAW = {"install_time": "1755500000", "product": "Firefox",
           "date_processed": "2026-08-19T10:00:00+00:00"}

    def _install_history_params(self, release_channel):
        seen = []
        raw = dict(self.RAW, release_channel=release_channel)
        with mock.patch.object(machine.socorro, "SuperSearch", _capturing_supersearch(seen)):
            orch._install_history(raw)
        self.assertEqual(len(seen), 1, seen)
        return seen[0]

    def test_a_beta_crashs_install_history_covers_developer_edition(self):
        self.assertEqual(self._install_history_params("beta")["release_channel"], BETA)

    def test_a_nightly_crashs_install_history_stays_on_nightly(self):
        self.assertEqual(self._install_history_params("nightly")["release_channel"], "nightly")

    # ------------------------------------------------------------------ #
    # DEFECT: item 4's second half is not implemented. `orchestrator._install_history`
    # (orchestrator.py:779) passes `channel=raw.get("release_channel") or "nightly"` — the
    # PROCESSED CRASH's label — so a DevEdition crash arrives here as `aurora`,
    # `get_search_channel("aurora")` widens nothing, and the query is `release_channel=aurora`.
    # Plan #18 §3 item 4: "Also pass the crash's DB channel (`"beta"`), not the processed
    # crash's `release_channel` (`"aurora"`) — an installation is one channel, so widening
    # costs nothing." The seed's own `channel` variable is in scope at the call site (line 719,
    # where the sibling `_hardware_noise(info, channel)` already receives it), so the fix is
    # one argument. Consequence today: the widening this item shipped never reaches the 36-41%
    # of beta that IS DevEdition, and the gate's input is asymmetric across the two halves of
    # one channel. The comment now sitting in machine.py claims the opposite ("a DevEdition
    # crash ... saw NOTHING AT ALL, so install_history returned its all-None empty dict"),
    # which its only caller cannot produce, because it never passed `beta` in the first place.
    # ------------------------------------------------------------------ #
    def test_a_developer_edition_crashs_install_history_covers_the_channel_it_is_on(self):
        self.assertEqual(self._install_history_params("aurora")["release_channel"], BETA)


class TestTheSignatureLinkOnTheCrashPage(unittest.TestCase):
    """`utils.make_url_for_signature` — the "this signature on crash-stats" link rendered on
    crashstack.html and reports.html."""

    def _link(self, channel):
        return utils.make_url_for_signature(
            SIGNATURE, "2026-08-19", BUILDID, channel, "Firefox")

    def test_a_nightly_link_filters_on_nightly(self):
        self.assertIn("nightly", self._link("nightly"))

    # ------------------------------------------------------------------ #
    # DEFECT: the 13th `release_channel` site, keyed on our label and not routed through
    # `get_search_channel` (utils.py:388-396). It is the one channel-keyed Socorro reference on
    # crashstack.html that item 4's list of six missed, and it now CONTRADICTS the page it sits
    # on: `population.for_crash` counts beta+aurora in the panel while the link beside it takes
    # the reader to a beta-only search — 25.5% fewer reports on the measured build, up to 72.4%
    # fewer for one signature. Same one-line fix as the other six.
    # ------------------------------------------------------------------ #
    def test_a_beta_link_shows_the_developer_edition_reports_too(self):
        link = self._link("beta")
        self.assertIn("beta", link)
        self.assertIn("aurora", link)


class TestEveryMcpContextGetsTheSeedChannel(unittest.TestCase):
    """All five MCP contexts must carry the crash's channel, in BOTH pipelines.

    `SearchfoxCtx` was the ONLY one without a `channel` field, and the other four DEFAULT to
    `"nightly"` — so a call site that forgets `channel=channel` does not fail, it silently
    claims the crash is a nightly one. That is why this enumerates what `build_options`
    actually constructed instead of asserting four literals: a SIXTH context added later
    without a channel is caught here rather than in a beta filing.
    """

    SEED = {"uuid": "u-1", "signature": SIGNATURE, "channel": "beta", "buildid": BUILDID,
            "product": "Firefox", "stack": "", "is_offstack": False}

    # A context with no channel is a DECISION, not an omission, and it has to be recorded
    # here with its reason. Bugzilla is the only one: a bug id is not channel-scoped.
    CHANNEL_FREE = {"BugzillaCtx"}

    def _contexts(self, module, crash):
        """``{class name: ctx}`` for every context ``module.build_options`` constructed."""
        got = {}
        real = module.build_sdk_server

        def spy(name, ctx, tools, **kwargs):
            got[type(ctx).__name__] = ctx
            return real(name, ctx, tools, **kwargs)

        with mock.patch.object(module, "build_sdk_server", spy):
            module.build_options(crash, searchfox_client=object())
        return got

    def _assert_all_channels(self, module, channel):
        got = self._contexts(module, dict(self.SEED, channel=channel))
        self.assertTrue(got)
        for name, ctx in got.items():
            fields = {f.name for f in dataclasses.fields(ctx)}
            if "channel" not in fields:
                self.assertIn(
                    name, self.CHANNEL_FREE,
                    "{} reaches the model with no channel at all: either it must carry the "
                    "crash's channel or it must be declared channel-free here with a "
                    "reason".format(name),
                )
                continue
            self.assertEqual(ctx.channel, channel,
                             "{} claims channel {!r} for a {} crash".format(
                                 name, ctx.channel, channel))
        return got

    def test_the_principal_hands_every_context_the_beta_channel(self):
        got = self._assert_all_channels(triage, "beta")
        self.assertLessEqual({"SearchfoxCtx", "PatchCtx", "HistoryCtx", "SourceCtx"},
                             set(got), got)
        # What the channel is FOR on the searchfox context: the tree the whole run reads.
        self.assertEqual(got["SearchfoxCtx"].repo, "mozilla-beta")

    def test_the_blind_second_opinion_hands_every_context_the_beta_channel(self):
        got = self._assert_all_channels(second_opinion, "beta")
        self.assertLessEqual(
            {"SearchfoxCtx", "PatchCtx", "HistoryCtx", "SourceCtx", "SocorroCtx"},
            set(got), got)
        self.assertEqual(got["SearchfoxCtx"].repo, "mozilla-beta")

    def test_the_five_contexts_are_all_reachable_across_the_two_pipelines(self):
        """Named explicitly so a context DROPPED from a pipeline is caught too — the
        enumeration above would happily pass on an empty-of-that-context run."""
        both = set(self._contexts(triage, self.SEED))
        both |= set(self._contexts(second_opinion, self.SEED))
        self.assertLessEqual(
            {"SearchfoxCtx", "PatchCtx", "HistoryCtx", "SourceCtx", "SocorroCtx"}, both)

    def test_a_nightly_crash_still_says_nightly_everywhere(self):
        """The negative arm: the channel is threaded, not hardcoded to beta."""
        for module in (triage, second_opinion):
            self._assert_all_channels(module, "nightly")


class _RecordingClient:
    """A searchfox client that records the repo token each tool handed it.

    Every method raises `SearchfoxNoResult` after recording: the repo is what is under test,
    and the tools' formatting is covered by tests/test_agent_tools.py.
    """

    def __init__(self):
        self.repos = []

    def _seen(self, repo):
        self.repos.append(repo)
        raise SearchfoxNoResult("empty")

    def calls_from(self, symbol, repo=None, depth=1, rev_label=None):
        self._seen(repo)

    def calls_to(self, symbol, repo=None, depth=1, rev_label=None):
        self._seen(repo)

    def calls_between(self, src, dst, repo=None, depth=2, rev_label=None):
        self._seen(repo)

    def define(self, symbol, repo=None, rev_label=None):
        self._seen(repo)

    def lookup(self, name_or_symbol, repo=None, limit=50, rev_label=None):
        self._seen(repo)

    def search(self, query, regex=False, repo=None, limit=50, rev_label=None):
        self._seen(repo)

    def field_layout(self, class_name, repo=None, rev_label=None):
        self._seen(repo)


# Enough of an argument for any searchfox tool, by parameter name. Keyed off the tool's own
# args model so a NEW searchfox tool is exercised by the loop below without editing it.
_TOOL_ARGS = {"symbol": "mozilla::Foo::Bar", "name": "Foo", "query": "Foo",
              "class_name": "mozilla::detail::nsTStringRepr", "source": "Foo",
              "target": "Bar"}


class TestSearchfoxReadsTheCrashsOwnTree(unittest.TestCase):
    """`SearchfoxCtx.repo` — item 13.

    A beta crash's code is on `mozilla-beta`, which searchfox indexes at its own branch tip
    (measured 2026-08-11: `cd001e124b15` / 154.0b9 while `firefox-main` was at 155.0a1). With
    no channel field every `SearchfoxClient` call fell through `_coerce_repo(None)` to
    `agent.searchfox.default_repo` = `mozilla-central`, and `SearchfoxCitation.repo` then
    honestly recorded trunk for code that may never have been in the build. Two deterministic
    gates read the same tree (`_resolve_struct_layout`, which is FAIL-CLOSED, and
    `compiled_out`), so the cost is not only a wrong citation.
    """

    def test_a_beta_crash_reads_mozilla_beta_and_a_nightly_one_reads_central(self):
        self.assertEqual(sfcg.SearchfoxCtx(client=None, channel="beta").repo, "mozilla-beta")
        self.assertEqual(sfcg.SearchfoxCtx(client=None, channel="nightly").repo,
                         "mozilla-central")
        self.assertEqual(sfcg.SearchfoxCtx(client=None, channel="release").repo,
                         "mozilla-release")

    def test_developer_edition_reads_the_beta_tree(self):
        """`aurora` is not an unknown channel: DevEdition is BUILT FROM mozilla-beta (58 of 59
        buildids shared with beta since 2026-04-01, identical revisions), and it is 36-41% of
        the channel. Falling back to central for it would send a third of beta's crashes to
        the wrong tree."""
        self.assertEqual(sfcg.SearchfoxCtx(client=None, channel="aurora").repo,
                         "mozilla-beta")

    def test_an_unknown_channel_falls_back_to_central_rather_than_raising(self):
        """A wrong-but-indexed tree degrades one answer; an exception loses the whole run."""
        for channel in ("esr140", "unknown", "", None):
            self.assertEqual(sfcg.SearchfoxCtx(client=None, channel=channel).repo,
                             "mozilla-central", channel)
        # The channel label is ours and its case is not guaranteed anywhere.
        self.assertEqual(sfcg.SearchfoxCtx(client=None, channel="Beta").repo, "mozilla-beta")

    def test_every_repo_the_map_can_return_is_a_repo_searchfox_accepts(self):
        """The token goes on a `searchfox-cli --repo` command line; an unknown one raises
        `SearchfoxInvocationError` from `_coerce_repo` and costs the run."""
        for channel in ("nightly", "beta", "aurora", "release", "esr140", None):
            repo = sfcg.SearchfoxCtx(client=None, channel=channel).repo
            self.assertEqual(searchfox.Repo(repo).value, repo, channel)

    def test_an_explicit_repo_still_wins(self):
        """A FEATURE, not a leak: a beta regressor usually LANDED on trunk, so "look at where
        it came from" is a legitimate query and the tools' descriptions invite it."""
        ctx = sfcg.SearchfoxCtx(client=None, channel="beta")
        self.assertEqual(ctx.repo_or("mozilla-central"), "mozilla-central")
        self.assertEqual(ctx.repo_or(None), "mozilla-beta")
        self.assertEqual(ctx.repo_or(""), "mozilla-beta")

    def test_every_searchfox_tool_reads_the_runs_own_repo(self):
        """Enumerated over `sfcg.TOOLS`, so a NEW tool that forgets `ctx.repo_or(repo)` — and
        therefore silently reads trunk for a beta crash — fails here."""
        for channel, expected in (("beta", "mozilla-beta"), ("nightly", "mozilla-central")):
            for tool in sfcg.TOOLS:
                client = _RecordingClient()
                ctx = sfcg.SearchfoxCtx(client=client, channel=channel)
                self._call(tool, ctx)
                self.assertEqual(client.repos, [expected],
                                 "{} on {}".format(tool.name, channel))

    def test_every_searchfox_tool_still_honours_an_explicit_repo(self):
        for tool in sfcg.TOOLS:
            client = _RecordingClient()
            ctx = sfcg.SearchfoxCtx(client=client, channel="beta")
            self._call(tool, ctx, repo="mozilla-central")
            self.assertEqual(client.repos, ["mozilla-central"], tool.name)

    def _call(self, tool, ctx, **extra):
        kwargs = {name: value for name, value in _TOOL_ARGS.items()
                  if name in tool.args_model.model_fields}
        self.assertTrue(kwargs, tool.name)
        kwargs.update(extra)
        try:
            asyncio.run(tool.handler(ctx, **kwargs))
        except Exception:
            # `SearchfoxNoResult` is an abstain for some tools and a `ToolError` for others;
            # either way the client already recorded the repo, which is what is under test.
            pass


class TestTheProvenanceLineNamesTheChannel(unittest.TestCase):
    """`report_bug._provenance` — item 5.

    This is the one paragraph of a filed bug whose job is to say what wrote the analysis, so
    the reader can discount it and knows an INVALID is welcome. It was the constant
    `_PROVENANCE`, reading "analyses nightly crashes", printed verbatim at the bottom of every
    filing whatever the channel. Nothing asserted it: `grep "analyses nightly crashes\\|
    _PROVENANCE" tests/` → 0 hits.
    """

    def test_a_beta_filing_says_beta(self):
        note = report_bug._provenance("beta")
        self.assertIn("beta", note)
        # The claim that used to be false on a beta bug.
        self.assertNotIn("nightly", note)
        # 36-41% of the channel is Developer Edition and the crash counts above this line now
        # include it, so this sentence has to admit it.
        self.assertIn("Developer Edition", note)

    def test_a_nightly_filing_still_says_nightly(self):
        note = report_bug._provenance("nightly")
        self.assertIn("nightly", note)
        self.assertNotIn("beta", note)

    def test_an_unknown_channel_says_something_honest_and_general(self):
        """A channel with no phrase must not guess, and must not fall back to a claim about a
        DIFFERENT channel. `""`/`None` are the reachable ones: `build_bug_comment` reads the
        channel out of `uuid_info`, which is a dict."""
        for channel in (None, "", "esr140", "aurora"):
            note = report_bug._provenance(channel)
            self.assertNotIn("nightly", note, channel)
            self.assertNotIn("beta ", note, channel)
            self.assertIn("Firefox crashes", note, channel)
        self.assertEqual(report_bug._provenance(), report_bug._provenance(None))

    def test_the_channel_is_the_only_thing_that_changes(self):
        """Everything else in the paragraph is load-bearing and must survive the
        substitution: the INVALID invitation, and :mccr8's attribution sentence (bug 2065051 —
        a machine filing "looks like we found it internally and thus won't pay a bounty")."""
        for channel in ("nightly", "beta", "release", None):
            note = report_bug._provenance(channel)
            self.assertIn("github.com/mozilla/crash-clouseau", note)
            self.assertIn("INVALID", note)
            self.assertIn("not an independent Mozilla discovery", note)
            self.assertIn("resolve THIS bug as the duplicate", note)
        # Case is not guaranteed anywhere upstream.
        self.assertEqual(report_bug._provenance("Beta"), report_bug._provenance("beta"))

    def test_the_filed_comment_carries_the_crashs_own_provenance(self):
        """The wiring, not the helper: `build_bug_comment` takes the channel from `uuid_info`
        and this line is the LAST section of the comment that reaches Bugzilla."""
        for channel in ("beta", "nightly"):
            info = {"uuid": "u-1", "signature": SIGNATURE, "channel": channel,
                    "buildid": BUILDID, "version": "155.0b2"}
            comment = report_bug.build_bug_comment(info, [], None)
            self.assertTrue(comment.endswith(report_bug._provenance(channel)), channel)
            self.assertNotIn("analyses nightly crashes" if channel == "beta"
                             else "analyses beta", comment)


if __name__ == "__main__":
    unittest.main()
